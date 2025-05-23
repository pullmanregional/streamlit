"""
Utilities for fetching and loading data from remote storage.
"""

import sys, os, logging, json
import sqlite3
import boto3
from datetime import datetime
from dataclasses import dataclass
from botocore.exceptions import NoCredentialsError, PartialCredentialsError
from sqlalchemy import create_engine

# Import common modules from repo root
sys.path.append(os.path.join(os.path.dirname(__file__), ".."))
from prw_common import encrypt


# Temporary storage when loading DB from memory
TMP_DB_FILES = []


@dataclass(eq=True, frozen=True)
class S3Config:
    """Configuration for an S3 connection"""

    acct_id: str
    acct_key: str
    url: str
    region: str = "auto"


# -------------------------------------------------------
# S3 Utilities
# -------------------------------------------------------
def fetch_from_s3(
    s3_config: S3Config, bucket: str, obj: str, data_key: str = None
) -> bytes:
    """
    Fetches a file from a remote S3-compatible storage, decrypts it,
    and returns the bytes.
    """
    try:
        # Initialize the S3 client
        logging.info("Fetch remote S3 object")
        s3_client = boto3.client(
            "s3",
            endpoint_url=s3_config.url,
            region_name=s3_config.region,
            aws_access_key_id=s3_config.acct_id,
            aws_secret_access_key=s3_config.acct_key,
        )

        # Fetch the encrypted file from the remote storage
        response = s3_client.get_object(Bucket=bucket, Key=obj)
        remote_bytes = response["Body"].read()

        # Decrypt the database file using provided Fernet key
        logging.info("Decrypting")
        decrypted_bytes = (
            encrypt.decrypt(remote_bytes, data_key)
            if data_key is not None
            else remote_bytes
        )

        return decrypted_bytes

    except (NoCredentialsError, PartialCredentialsError) as e:
        logging.error("Credentials error: %s", e)
        raise
    except Exception as e:
        logging.error("Failed to fetch and load remote object: %s", e)
        raise


def sqlite_engine_from_s3(
    s3_config: S3Config, bucket: str, obj: str, data_key: str = None
):
    """
    Fetches the SQLite database file from a remote S3-compatible storage, decrypts it,
    and loads it into a temporary SQLite database.
    Returns a SQLAlchemy engine to the SQLite database in memory.
    """
    data = fetch_from_s3(s3_config, bucket, obj, data_key)
    # Write the decrypted database to a temporary SQLite database
    logging.info("Reading DB to memory")
    # Create a temporary file in the current directory
    TMP_DB_FILES.append(f"db_{datetime.now().strftime('%Y%m%d%H%M%S%f')}.sqlite3")
    open(TMP_DB_FILES[-1], "wb").write(data)
    conn = sqlite3.connect(TMP_DB_FILES[-1])
    return create_engine(f"sqlite://", creator=lambda: conn)


def json_from_s3(
    s3_config: S3Config, bucket: str, obj: str, data_key: str = None
) -> dict:
    """
    Fetches a json file from a remote S3-compatible storage, decrypts it,
    and loads it into a dictionary.
    """
    data = fetch_from_s3(s3_config, bucket, obj, data_key)
    return json.loads(data)


def cleanup():
    """
    Delete any temporary file
    """
    for file in TMP_DB_FILES:
        os.remove(file)
    TMP_DB_FILES.clear()


# -------------------------------------------------------
# File utilities
# -------------------------------------------------------
def sqlite_engine_from_file(file):
    """
    Reads the specified SQLite database file and returns a SQLAlchemy engine.
    """
    conn = sqlite3.connect(file)
    return create_engine(f"sqlite://", creator=lambda: conn)


def json_from_file(file):
    """
    Reads the specified JSON file and returns a dictionary.
    Returns an empty dictionary if the file does not exist or invalid.
    """
    try:
        with open(file, "r") as f:
            return json.loads(f.read())
    except Exception as e:
        logging.error("Failed to read JSON file: %s", e)
        return {}


# -------------------------------------------------------
# General utilities
# -------------------------------------------------------
def dedup_ignore_case(items: list) -> list:
    seen = set()
    deduped = []
    for item in items:
        if item.lower() not in seen:
            deduped.append(item)
            seen.add(item.lower())
    return deduped
