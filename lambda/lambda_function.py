"""
lambda_function.py — FaceAuth Post-Authentication Trigger

This Lambda is invoked asynchronously by the local Flask app after every
face authentication attempt.  It handles cloud-side actions:

  1. Publish auth event to AWS IoT Core (for door-controller hardware)
  2. Send SNS notification on denied access (optional)
  3. Write a secondary audit log entry to DynamoDB

Environment Variables (set in Lambda Console or via aws_setup.py):
  DYNAMO_LOGS_TABLE  — DynamoDB logs table name   (default: faceauth-logs)
  IOT_ENDPOINT       — IoT Core endpoint hostname  (leave empty to skip)
  IOT_TOPIC_PREFIX   — MQTT topic prefix           (default: faceauth/auth)
  SNS_ALERT_ARN      — SNS topic ARN for alerts    (leave empty to skip)
"""

import json
import uuid
import logging
import os
from datetime import datetime, timezone

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ── Config from Lambda environment variables ──────────────────────────────────
DYNAMO_LOGS_TABLE  = os.environ.get('DYNAMO_LOGS_TABLE',  'faceauth-logs')
IOT_ENDPOINT       = os.environ.get('IOT_ENDPOINT',       '')
IOT_TOPIC_PREFIX   = os.environ.get('IOT_TOPIC_PREFIX',   'faceauth/auth')
SNS_ALERT_ARN      = os.environ.get('SNS_ALERT_ARN',      '')

# ── Lazy-initialised clients (reused across warm invocations) ─────────────────
_dynamodb   = None
_iot_client = None


def _get_logs_table():
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource('dynamodb')
    return _dynamodb.Table(DYNAMO_LOGS_TABLE)


def _get_iot():
    global _iot_client
    if _iot_client is None and IOT_ENDPOINT:
        _iot_client = boto3.client(
            'iot-data',
            endpoint_url=f'https://{IOT_ENDPOINT}',
        )
    return _iot_client


def handler(event, context):
    """
    Lambda handler.

    Expected event payload (sent by aws_lambda_client.trigger_post_auth):
    {
        "user_id":      str,
        "username":     str,
        "full_name":    str,
        "match":        bool,
        "confidence":   float,
        "access_point": str,
        "event_type":   "Access Granted" | "Access Denied",
        "source":       "FaceAuth-Local"
    }
    """
    logger.info("Post-auth event received: %s", json.dumps(event))

    user_id      = event.get('user_id',      'unknown')
    username     = event.get('username',     'unknown')
    full_name    = event.get('full_name',    '')
    match        = event.get('match',        False)
    confidence   = float(event.get('confidence',   0.0))
    access_point = event.get('access_point', 'Unknown')
    event_type   = event.get('event_type',  'Access Denied')
    timestamp    = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

    # ── 1. IoT Core publish ───────────────────────────────────────────────────
    _publish_iot(event_type, username, access_point, match, confidence, timestamp)

    # ── 2. SNS alert for denied access ────────────────────────────────────────
    if not match and SNS_ALERT_ARN:
        _send_sns_alert(username, access_point, confidence, timestamp)

    # ── 3. Secondary DynamoDB audit log ──────────────────────────────────────
    _log_to_dynamo(event_type, username, access_point, match, confidence, timestamp)

    logger.info("Post-auth trigger complete — user='%s', match=%s", username, match)
    return {'statusCode': 200, 'body': 'OK'}


def _publish_iot(event_type, username, access_point, match, confidence, timestamp):
    """Publish auth event to IoT Core MQTT — used by physical door controllers."""
    iot = _get_iot()
    if iot is None:
        logger.info("IoT endpoint not configured — skipping MQTT publish.")
        return

    topic   = f"{IOT_TOPIC_PREFIX}/{'granted' if match else 'denied'}"
    payload = {
        'event_type':   event_type,
        'username':     username,
        'access_point': access_point,
        'match':        match,
        'confidence':   confidence,
        'timestamp':    timestamp,
    }
    try:
        iot.publish(topic=topic, qos=1, payload=json.dumps(payload))
        logger.info("Published to IoT topic: %s", topic)
    except Exception as exc:
        logger.error("IoT publish failed: %s", exc)


def _send_sns_alert(username, access_point, confidence, timestamp):
    """Send SNS notification for denied access attempts."""
    try:
        sns     = boto3.client('sns')
        message = (
            f"\u26a0\ufe0f  FaceAuth — Access Denied\n\n"
            f"User         : {username}\n"
            f"Access Point : {access_point}\n"
            f"Confidence   : {confidence:.1f}%\n"
            f"Time         : {timestamp}\n"
        )
        sns.publish(
            TopicArn = SNS_ALERT_ARN,
            Subject  = f"FaceAuth: Access Denied at {access_point}",
            Message  = message,
        )
        logger.info("SNS alert sent for denied access by '%s'", username)
    except Exception as exc:
        logger.error("SNS publish failed: %s", exc)


def _log_to_dynamo(event_type, username, access_point, match, confidence, timestamp):
    """Write a secondary Lambda-side audit entry to DynamoDB logs table."""
    try:
        table = _get_logs_table()
        table.put_item(Item={
            'log_id':       str(uuid.uuid4()),
            'timestamp':    timestamp,
            'event_type':   f"[Lambda] {event_type}",
            'username':     username,
            'access_point': access_point,
            'status':       'Success' if match else 'Failed',
            'details':      (
                f"Lambda post-auth trigger — "
                f"confidence={confidence:.1f}%, source=IoT+SNS"
            ),
        })
        logger.info("Secondary audit log written to DynamoDB.")
    except Exception as exc:
        logger.error("DynamoDB log write failed: %s", exc)
