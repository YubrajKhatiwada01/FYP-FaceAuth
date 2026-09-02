"""
lambda_function.py - FaceAuth Post-Authentication Trigger

Invoked asynchronously (fire-and-forget) by the local Flask app after every
face authentication attempt.  Handles cloud-side actions:

  1. Publish ACCESS_GRANTED to AWS IoT Core -> ESP32 door controller
        Topic : server_room/door_command   (must match ESP32 subscription)
        Payload: {"command": "ACCESS_GRANTED", "user_id": ..., "timestamp": ...}
  2. Publish a rich auth-event to a monitoring topic (both granted & denied)
        Topic : faceauth/auth/granted  or  faceauth/auth/denied
  3. Send SNS notification on denied access (optional)
  4. Write a secondary audit log entry to DynamoDB

Environment Variables (set in Lambda Console):
  DYNAMO_LOGS_TABLE  -- DynamoDB logs table name   (default: faceauth-logs)
  IOT_ENDPOINT       -- IoT Core data endpoint hostname (required for MQTT)
                        e.g. xxxx-ats.iot.ap-south-1.amazonaws.com
  SNS_ALERT_ARN      -- SNS topic ARN for denied-access alerts (leave blank to skip)
"""

import json
import uuid
import logging
import os
import time
from datetime import datetime, timezone

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# -- Config from Lambda environment variables ---------------------------------
DYNAMO_LOGS_TABLE  = os.environ.get('DYNAMO_LOGS_TABLE',  'faceauth-logs')
IOT_ENDPOINT       = os.environ.get('IOT_ENDPOINT',       '')
SNS_ALERT_ARN      = os.environ.get('SNS_ALERT_ARN',      '')

# -- IoT Topics ---------------------------------------------------------------
DOOR_TOPIC         = "server_room/door_command"   # ESP32 subscribes here
EVENT_TOPIC_PREFIX = "faceauth/auth"              # monitoring / dashboard topic

# -- Lazy-initialised clients (reused across warm Lambda invocations) ---------
_dynamodb   = None
_iot_client = None


def _get_logs_table():
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource('dynamodb')
    return _dynamodb.Table(DYNAMO_LOGS_TABLE)


def _get_iot():
    """Return an IoT Data client, or None if IOT_ENDPOINT is not configured."""
    global _iot_client
    if _iot_client is None and IOT_ENDPOINT:
        _iot_client = boto3.client(
            'iot-data',
            endpoint_url=f'https://{IOT_ENDPOINT}',
        )
    return _iot_client


# -----------------------------------------------------------------------------
# Handler
# -----------------------------------------------------------------------------

def handler(event, context):
    """
    Lambda entry point.

    Expected event payload (sent by aws_lambda_client.trigger_post_auth()):
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
    match        = bool(event.get('match',   False))
    confidence   = float(event.get('confidence', 0.0))
    access_point = event.get('access_point', 'Unknown')
    event_type   = event.get('event_type',  'Access Denied')
    timestamp    = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

    # 1. IoT Core: door unlock command + monitoring event
    _publish_iot(
        user_id=user_id, username=username, access_point=access_point,
        match=match, confidence=confidence, timestamp=timestamp,
    )

    # 2. SNS alert for denied access
    if not match and SNS_ALERT_ARN:
        _send_sns_alert(username, access_point, confidence, timestamp)

    # 3. Secondary DynamoDB audit log
    _log_to_dynamo(event_type, username, access_point, match, confidence, timestamp)

    logger.info("Post-auth trigger complete -- user='%s', match=%s", username, match)
    return {'statusCode': 200, 'body': 'OK'}


# -----------------------------------------------------------------------------
# Private helpers
# -----------------------------------------------------------------------------

def _publish_iot(user_id, username, access_point, match, confidence, timestamp):
    """
    Publish one MQTT monitoring event to IoT Core:

    NOTE: The door command (server_room/door_command) is intentionally NOT sent
    from Lambda. The local Flask app (aws_iot_door.py) already publishes
    ACCESS_GRANTED directly and enforces a cooldown + consecutive-frame threshold.
    Sending it from Lambda as well caused the ESP32 servo to receive two rapid
    messages and cycle open -> close -> open in quick succession.

    B) faceauth/auth/granted|denied -- rich monitoring event (both outcomes).
    """
    iot = _get_iot()
    if iot is None:
        logger.info("IOT_ENDPOINT not configured -- skipping MQTT publish.")
        return

    # B: Rich monitoring event (always, both granted and denied)
    event_topic = f"{EVENT_TOPIC_PREFIX}/{'granted' if match else 'denied'}"
    event_payload = json.dumps({
        'username':     username,
        'user_id':      str(user_id),
        'access_point': access_point,
        'match':        match,
        'confidence':   confidence,
        'timestamp':    timestamp,
    })
    try:
        iot.publish(topic=event_topic, qos=0, payload=event_payload)
        logger.info("Monitoring event published -> %s", event_topic)
    except Exception as exc:
        logger.error("Monitoring event publish failed: %s", exc)


def _send_sns_alert(username, access_point, confidence, timestamp):
    """Send an SNS notification for denied access attempts."""
    try:
        sns     = boto3.client('sns')
        message = (
            "FaceAuth - Access Denied\n\n"
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
    """Write a secondary Lambda-side audit entry to the DynamoDB logs table."""
    try:
        table = _get_logs_table()
        table.put_item(Item={
            'log_id':       str(uuid.uuid4()),
            'timestamp':    timestamp,
            'event_type':   event_type,
            'username':     username,
            'access_point': access_point,
            'status':       'Success' if match else 'Failed',
            'details':      (
                f"Lambda post-auth trigger - "
                f"confidence={confidence:.1f}%, source=FaceAuth-Local"
            ),
        })
        logger.info("Secondary audit log written to DynamoDB.")
    except Exception as exc:
        logger.error("DynamoDB log write failed: %s", exc)
