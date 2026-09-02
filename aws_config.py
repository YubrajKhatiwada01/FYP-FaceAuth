"""
aws_config.py — Central boto3 client factory for FaceAuth AWS integration.

All AWS service clients are created here from environment variables so
credentials are never hard-coded anywhere in the codebase.
"""

import os
import logging
import boto3
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# ── AWS Credentials & Region ──────────────────────────────────────────────────
AWS_REGION            = os.environ.get('AWS_REGION', 'us-east-1')
AWS_ACCESS_KEY_ID     = os.environ.get('AWS_ACCESS_KEY_ID', '')
AWS_SECRET_ACCESS_KEY = os.environ.get('AWS_SECRET_ACCESS_KEY', '')

# ── Resource Names (can be overridden via .env) ───────────────────────────────
S3_BUCKET_NAME       = os.environ.get('S3_BUCKET_NAME',      'faceauth-fyp')
DYNAMO_USERS_TABLE   = os.environ.get('DYNAMO_USERS_TABLE',   'faceauth-users')
DYNAMO_LOGS_TABLE    = os.environ.get('DYNAMO_LOGS_TABLE',    'faceauth-logs')
DYNAMO_POINTS_TABLE  = os.environ.get('DYNAMO_POINTS_TABLE',  'faceauth-access-points')
LAMBDA_FUNCTION_NAME  = os.environ.get('LAMBDA_FUNCTION_NAME',  'faceauth-post-auth-trigger')
IOT_ENDPOINT          = os.environ.get('IOT_ENDPOINT',          '')   # e.g. xxxx-ats.iot.ap-south-1.amazonaws.com


import threading

_thread_local = threading.local()


def _session() -> boto3.Session:
    """Create or retrieve the thread-local boto3 Session."""
    if not hasattr(_thread_local, 'session'):
        if not AWS_ACCESS_KEY_ID or not AWS_SECRET_ACCESS_KEY:
            logger.warning(
                "AWS_ACCESS_KEY_ID or AWS_SECRET_ACCESS_KEY not set in .env. "
                "AWS services will be unavailable."
            )
        _thread_local.session = boto3.Session(
            region_name           = AWS_REGION,
            aws_access_key_id     = AWS_ACCESS_KEY_ID or None,
            aws_secret_access_key = AWS_SECRET_ACCESS_KEY or None,
        )
    return _thread_local.session


def get_dynamodb():
    """Return a thread-local DynamoDB resource object."""
    if not hasattr(_thread_local, 'dynamodb'):
        _thread_local.dynamodb = _session().resource('dynamodb')
    return _thread_local.dynamodb


def get_s3():
    """Return a thread-local S3 client."""
    if not hasattr(_thread_local, 's3'):
        _thread_local.s3 = _session().client('s3')
    return _thread_local.s3


def get_lambda():
    """Return a thread-local Lambda client."""
    if not hasattr(_thread_local, 'lambda_client'):
        _thread_local.lambda_client = _session().client('lambda')
    return _thread_local.lambda_client


def get_iot_data(region_name: str = 'ap-south-1'):
    """
    Return a thread-local AWS IoT Data Plane client.

    The IoT Data endpoint is read from the IOT_ENDPOINT env var.  If it is
    not set boto3 will fall back to the regional default endpoint, which is
    fine for most use-cases.

    Parameters
    ----------
    region_name : str
        AWS region where your IoT Core broker lives.  Defaults to ap-south-1.
    """
    cache_attr = f'iot_data_{region_name}'
    if not hasattr(_thread_local, cache_attr):
        endpoint_url = IOT_ENDPOINT
        client = _session().client(
            'iot-data',
            region_name  = region_name,
            endpoint_url = f'https://{endpoint_url}' if endpoint_url else None,
        )
        setattr(_thread_local, cache_attr, client)
    return getattr(_thread_local, cache_attr)
