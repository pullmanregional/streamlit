"""
Source data as in-memory copy of all DB tables as dataframes
"""

import logging, json
import pandas as pd
import streamlit as st
from dataclasses import dataclass
from datetime import timedelta, datetime
from common import source_data_util

# Cloudflare R2 connection
R2_ACCT_ID = st.secrets.get("PRH_SAMPLE_R2_ACCT_ID")
R2_ACCT_KEY = st.secrets.get("PRH_SAMPLE_R2_ACCT_KEY")
R2_URL = st.secrets.get("PRH_SAMPLE_R2_URL")
R2_BUCKET = st.secrets.get("PRH_SAMPLE_R2_BUCKET")

# Local data file
DATA_FILE = st.secrets.get("DATA_FILE")

# Encryption keys for datasets
DATA_KEY = st.secrets.get("DATA_KEY")


@dataclass(eq=True)
class SourceData:
    """In-memory copy of DB tables"""

    df: pd.DataFrame = None
    kvdata: dict = None

    modified: datetime = None


def read() -> SourceData:
    if DATA_FILE:
        return from_file(DATA_FILE)
    else:
        return from_s3()


@st.cache_data(ttl=timedelta(minutes=2))
def from_file(db_file: str) -> SourceData:
    engine = source_data_util.sqlite_engine_from_file(db_file)
    source_data = from_db(engine)
    engine.dispose()
    return source_data


@st.cache_data(ttl=timedelta(hours=6), show_spinner="Loading...")
def from_s3() -> SourceData:
    logging.info("Fetching source data")
    r2_config = source_data_util.S3Config(R2_ACCT_ID, R2_ACCT_KEY, R2_URL)
    engine = source_data_util.sqlite_engine_from_s3(
        r2_config, R2_BUCKET, "prh-sample.sqlite3.enc", DATA_KEY
    )
    source_data = from_db(engine)
    engine.dispose()

    source_data_util.cleanup()
    return source_data


def from_db(db_engine) -> SourceData:
    """
    Read all data from specified DB connection into memory and return as dataframes
    """
    logging.info("Reading DB tables")

    # Get the latest time data was updated from meta table
    result = pd.read_sql_query("SELECT MAX(modified) FROM meta", db_engine)
    modified = result.iloc[0, 0] if result.size > 0 else None

    df = pd.read_sql_table("table_name", db_engine)

    # Get key/value data from the first row
    kv_data_df = pd.read_sql_query("SELECT data FROM _kv LIMIT 1", db_engine)
    kv_data = json.loads(kv_data_df.iloc[0]["data"])

    return SourceData(modified=modified, df=df, kvdata=kv_data)
