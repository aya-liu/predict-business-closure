import os
import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import shape
from dateutil import parser
from dateutil.relativedelta import relativedelta
from datetime import timedelta
import pickle
import json
import shapely
from sodapy import Socrata
import matplotlib.pyplot as plt
import census_features as cen
import additional_features as add
from link_datasets import *

pd.options.display.max_columns = 999

MAX_REQS = 1000000000
TOKENS_FILEPATH = 'tokens.json'
ZILLOW_FILEPATH = 'data/ZLW_Zip_MedianValuePerSqft_AllHomes.csv'
UMP_FILEPATH = 'data/Chicago_unemp_2001-2018.xlsx'
GDP_FILEPATH = 'data/Chicago_gdp2001-2017.xlsx'
CTA_MONTHS = 6
REAL_ESTATE_MONTHS = 6

def get_lcs_data(tokens_filepath=TOKENS_FILEPATH, cta_months=CTA_MONTHS, 
                 real_estate_months=REAL_ESTATE_MONTHS):
    '''
    obtain and clean business licenses data 
    
    input:
        tokens_filepath: (str) filepath to a tokens json
    returns: (geodataframe) clean business licenses dataframe
    '''
    tokens = load_tokens(tokens_filepath)

    print('Downloading licenses from Chicago Open Data Portal...')
    lcs = obtain_lcs(tokens)

    print('Cleaning data...')
    lcs = convert_lcs_dtypes(lcs)
    lcs = clean_lcs(lcs)

    print('Changing unit of analysis...')
    lcs = create_time_buckets(lcs, {'years': 2}, 'license_start_date',
                                       '2002-01-01', "2017-12-31") #parameterize?; need to deal with missing start date before here (apprx. 9863)
    lcs = collapse_licenses(lcs)

    print('Generating outcome variable...')
    lcs = add_outcome_variable(lcs)

    #### add geographies ####
    print('Creating geospatial properties...')
    lcs = lcs.reset_index()
    lcs = gdf_from_latlong(lcs, lat='latitude', long_='longitude')  

    # # add zillow neighborhoods
    # print('Linking Zillow Neighborhoods...')
    # nbh = gpd.read_file(ZILLOW_GEO)
    # nbh = nbh[nbh.City == 'Chicago'].drop(columns=['State', 'County', 'City']) 
    # lcs = add_geography_id(lcs, nbh) #issue here
    
    # add census tracts
    print('Linking census tracts...')
    lcs = add_census_tracts(lcs)

    # getting additional datasets
    print('Loading additional datasets...')
    census_2000 = cen.get_2000_census_data(tokens)
    census_2010 = cen.get_2010_census_data(tokens) 
    zbp = cen.get_zbp_data(tokens)
    cta_ward = add.get_rides(tokens)
    real_estate = add.get_realestate(ZILLOW_FILEPATH)
    ump, gdp = add.get_ecofeatures(UMP_FILEPATH, GDP_FILEPATH)

    print('Linking additional datasets...')
    lcs = link_zbp_licenses(zbp, lcs)
    lcs = link_census_licenses(census_2000, census_2010, lcs)
    lcs = link_cta_licenses(cta_ward, lcs, cta_months)
    lcs = link_real_estate_licenses(real_estate, lcs, real_estate_months)
    lcs = link_gdp_licenses(gdp, lcs)
    lcs = link_ump_licenses(ump, lcs)

    print('Dropping extraneous columns...')
    drop_cols = ['legal_name', 'doing_business_as_name',
                 'license_descriptions', 'business_activities',
                 'business_activity_ids', 'address', 'city', 'state',
                 'ward_precinct', 'geometry']
    lcs = lcs.drop(drop_cols, axis=1)

    return lcs

def obtain_lcs(tokens):
    '''
    obtain business licenses data from chicago open data portal.
    '''
    client = Socrata('data.cityofchicago.org', tokens['chicago_open_data_portal'])
    results = client.get('xqx5-8hwx', city='CHICAGO', limit=MAX_REQS)
    lcs = pd.DataFrame.from_records(results)
    return lcs

def create_time_buckets(lcs, bucket_size, date_col, start_date=None, end_date=None):
    '''
    Labels each license with a time period. Time periods are defined by the
    bucket size and start date arguments and cut based on the date_col
    argument.

    Inputs:
    lcs (pandas dataframe): a license dataset
    bucket_size (dictionary): defines the size of each bucket, valid key-value
        pairs are parameters for a dateutil.relativedelta.relativedelta object
    date_col (col name): the column containg the date to split time periods on
    start_date (str): first day to include in a bucket, string of the form
        YYYY-MM-DD or YYYYMMDD
    end_date (str): last day to include in a bucket, string of the form
        YYYY-MM-DD or YYYYMMDD

    Returns: tuple of pandas dataframe, list of bucket starting dates

    Stray setting with copy warning here
    '''
    if not start_date:
        start_date = min(lcs[date_col])
    if end_date is not None:
        end_date = pd.to_datetime(end_date)
        lcs = lcs[lcs[date_col] <= end_date]
    if not pd.core.dtypes.common.is_datetime_or_timedelta_dtype(lcs[date_col]):
        lcs.loc[:, date_col] = pd.to_datetime(lcs[date_col])


    start_date = pd.to_datetime(start_date)
    bucket_size = relativedelta(**bucket_size)
    lcs.loc[:, 'time_period'] = float('nan')

    i = 0
    stop_date = max(lcs[date_col])
    while  start_date + i * bucket_size <= stop_date:
        start_mask = start_date + i * bucket_size <= lcs[date_col]
        end_mask = lcs[date_col] < start_date + (i + 1) * bucket_size
        lcs.loc[start_mask & end_mask, 'time_period'] = i
        lcs.loc[start_mask & end_mask, 'bucket_end'] = start_date + (i + 1) * bucket_size
        i += 1

    return lcs[lcs.time_period.notna()]

def collapse_licenses(lcs):
    '''
    Collapses all the licenses associated with a given accountid-siteid based on
    their time period so that each row represents one accountid-siteid-time
    period.

    Inputs:
    lcs (pandas dataframe): a license dataset

    Returns: pandas dataframe
    '''
    lcs['rev_or_rea'] = (lcs.license_status == 'REV') | (lcs.license_status == 'REA')
    lcs['canceled'] = lcs.license_status == 'AAC'
    lcs['conditional_tf'] = lcs.conditional_approval == 'Y'
    lcs_collapse = lcs.groupby(['account_number', 'site_number', 'time_period'])\
                      .agg({"license_id": 'count',
                            "legal_name": "first",
                            "doing_business_as_name": 'first',
                            "license_start_date": ["min", "max"],
                            "expiration_date": "max",
                            "application_type": set,
                            "license_code": set,
                            "license_description": set,
                            "business_activity": set,
                            "business_activity_id": set,
                            "rev_or_rea": 'mean',
                            "canceled": 'mean',
                            "conditional_tf": 'mean',
                            "address": "first",
                            "city": "first",
                            "state": "first",
                            "zip_code": "first",
                            "latitude": "first",
                            "longitude": "first",
                            "location": "first",
                            "police_district": "first",
                            "precinct": "first",
                            "ward": "first",
                            "ward_precinct": "first",
                            "ssa": "first",
                            "bucket_end": "first"})
    multi_index = lcs_collapse.columns.to_list()
    ind = pd.Index(["_".join(entry) for entry in multi_index])
    lcs_collapse.columns = ind
    lcs_collapse = lcs_collapse.rename({"license_id_count": 'n_licenses',
                                        "legal_name_first": 'legal_name',
                                        "doing_business_as_name_first": 'doing_business_as_name',
                                        'license_start_date_min': 'min_start_date',
                                        'license_start_date_max': 'max_start_date',
                                        'expiration_date_max': 'max_expiration_date',
                                        'application_type_set': 'application_types',
                                        "license_code_set": "license_codes",
                                        "license_description_set": "license_descriptions",
                                        "business_activity_set": "business_activities",
                                        "business_activity_id_set": "business_activity_ids",
                                        "rev_or_rea_mean": 'pct_revoked',
                                        "canceled_mean": 'pct_canceled',
                                        "conditional_tf_mean": 'pct_cndtl_approval',
                                        "address_first": "address",
                                        "city_first": "city",
                                        "state_first": "state",
                                        "zip_code_first": "zip_code",
                                        "latitude_first": "latitude",
                                        "longitude_first": "longitude",
                                        "location_first": "location",
                                        "police_district_first": "police_district",
                                        "precinct_first": "precinct",
                                        "ward_first": "ward",
                                        "ward_precinct_first": "ward_precinct",
                                        "ssa_first": "ssa",
                                        "bucket_end_first": "bucket_end"},
                                        axis=1)

    return lcs_collapse

def find_in_nextpd(row, lcs):
    '''
    Checks if an account_id, site_name combination appears in the index of a
    license dataset during the next time period.

    Inputs:
    row (pandas series): one account_id-site_name-time_period record
    lcs (pandas dataset): a full license dataset

    Returns: boolean
    '''
    account_number, site_number, time_period = row.name

    return (account_number, site_number, time_period + 1) in lcs.index

def add_outcome_variable(lcs):
    '''
    Generates a column called 'no_renew_nextpd' that is true if a particular
    account_id-site_name does not appear in the next period and false otherwise.

    Inputs:
    lcs (pandas dataframe): a license dataset

    Returns pandas dataframe
    '''
    lcs['no_renew_nextpd'] = None

    max_timepd = max(lcs.index.get_level_values('time_period'))
    mask = lcs.index.get_level_values('time_period') != max_timepd
    lcs.loc[mask, 'no_renew_nextpd'] = lcs.loc[mask, :].apply(find_in_nextpd,
                                                             axis=1,
                                                             args=[lcs])

    return lcs

def convert_lcs_dtypes(lcs):
    '''
    Convert data types of business licenses.

    Input: 
        lcs: (dataframe) raw business licenses data
    Returns: updated dataframe
    '''
    lcs_dates = ['application_created_date', 
                 'application_requirements_complete', 
                 'payment_date',
                 'license_start_date',
                 'expiration_date',
                 'license_approved_for_issuance',
                 'date_issued',
                 'license_status_change_date']
    lcs[lcs_dates] = lcs[lcs_dates].astype('datetime64')   
    lcs['latitude'] = lcs['latitude'].astype('float64')
    lcs['longitude'] = lcs['longitude'].astype('float64')
    return lcs

def clean_lcs(lcs): #May want to talk about what we're doing in this function
    '''
    Clean business licenses data.

    Input: 
        lcs: (dataframe) raw business licenses data
    Returns: updated dataframe    
    '''
    # fill license start dates
    # for issuance type: fill start date with issue date
    nastart_issue = lcs['license_start_date'].isna() & (lcs['application_type'] == 'ISSUE')
    lcs.loc[nastart_issue, 'license_start_date'] = lcs.loc[nastart_issue, 'date_issued']
    # for other types: drop (negligible)
    lcs = lcs.dropna(subset=['license_start_date'], axis=0)

    # drop rows with negative license length
    lcs = lcs[(lcs['expiration_date'] - lcs['license_start_date']) > timedelta(days=0)]

    # drop rows with no location
    lcs = lcs.dropna(subset=['location'], axis=0)
    return lcs


def add_census_tracts(lcs):
    '''
    add census tract number to business licenses dataset through spatial join
    with census tract boundaries (2000 and 2010) from chicago open data oprtal

    Input: 
        lcs: (geodataframe) business licenses data
    Returns: geodataframe with census tract number
    
    '''
    # census tracts
    # post 2010
    tokens = load_tokens('tokens.json')
    client = Socrata('data.cityofchicago.org', tokens['chicago_open_data_portal'])
    
    tracts10 = pd.DataFrame(client.get('74p9-q2aq', select='the_geom,tractce10',
                            limit=MAX_REQS))
    tracts10['the_geom'] = tracts10.the_geom\
                                       .apply(shapely.geometry.shape)
    tracts10 = gpd.GeoDataFrame(tracts10, geometry='the_geom')
    lcs_10 = add_geography_id(lcs[lcs.min_start_date >= parser.parse('2010-01-01')],
                              tracts10)
    lcs_10.rename(columns={'tractce10':'census_tract'}, inplace=True) 

    # pre 2010
    tracts00 = pd.DataFrame(client.get('4hp8-2i8z', select='the_geom,census_tra',
                            limit=MAX_REQS))
    tracts00['the_geom'] = tracts00.the_geom\
                                       .apply(shapely.geometry.shape)
    tracts00 = gpd.GeoDataFrame(tracts00, geometry='the_geom')
    lcs_00 = add_geography_id(lcs[lcs.min_start_date < parser.parse('2010-01-01')], 
                              tracts00)
    lcs_00.rename(columns={'census_tra':'census_tract'}, inplace=True) 

    # combine
    lcs = pd.concat([lcs_00, lcs_10], axis=0)
    return lcs



def gdf_from_latlong(df, lat, long_):
    '''
    convert a pandas dataframe to a geodataframe on lat long

    Inputs:
        df: (dataframe) original df
        lat: (str) column name for latitude
        long: (str) column name for longitude
    
    Returns: a geodataframe
    '''
    gdf = gpd.GeoDataFrame(df, geometry=gpd.points_from_xy(df[long_], df[lat]))

    return gdf


def add_geography_id(gdf, b):
    '''
    spatial join gdf with points (lat, long) with another geodataframe with
    polygons (boundaries).

    Input: 
        gdf: geodataframe with points
        b: geodataframe with polygons (boundaries)

    Returns: (geodataframe) gdf with geography id from b
    
    '''
    b.crs = {'init': 'epsg:4326'}
    gdf.crs = {'init': 'epsg:4326'}

    result = gpd.sjoin(gdf, b, how="left")
    # drop extra column
    result.drop('index_right', axis=1, inplace=True)
    return result


def load_tokens(tokens_file):
    '''
    Loads dictionary of API tokens.

    tokens_file (str): path to a JSON file containing API tokens
    '''
    with open(tokens_file,'r') as file:
        return json.load(file)
