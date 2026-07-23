"""
aws_lambda_client.py — Lambda invocation client for FaceAuth.

Asynchronously fires the post-authentication Lambda trigger after every
face recognition attempt (success or failure).

The Lambda function handles:
  • AWS IoT Core MQTT publish (door-controller integration)
  • SNS alert notifications for denied access
  • Secondary audit logging inside AWS

Invocation is fire-and-forget (InvocationType='Event') so it does NOT
block or slow down the Flask HTTP response.  If Lambda is not yet
deployed the failure is logged as a warning and the auth flow continues
normally.
"""

import json
import logging

from aws_config import get_lambda, LAMBDA_FUNCTION_NAME

logger = logging.getLogger(__name__)


def trigger_post_auth(
    *,
    user_id:      str,
    username:     str,
    full_name:    str,
    match:        bool,
    confidence:   float,
    access_point: str,
    event_type:   str,
) -> bool:
    """
    Asynchronously invoke the post-authentication Lambda trigger.

    This call returns immediately — Lambda processes the event in the
    background.  The Flask response is never delayed by this call.

    Args:
        user_id:      UUID of the user being authenticated
        username:     Login username
        full_name:    Display name
        match:        True  → Access Granted, False → Access Denied
        confidence:   Face match confidence 0–100 %
        access_point: Name of the physical entry point
        event_type:   'Access Granted' or 'Access Denied'

    Returns:
        True if Lambda was successfully invoked, False otherwise.
        Either way the auth flow is unaffected.
    """
    payload = {
        'user_id':      user_id,
        'username':     username,
        'full_name':    full_name,
        'match':        match,
        'confidence':   confidence,
        'access_point': access_point,
        'event_type':   event_type,
        'source':       'FaceAuth-Local',
    }

    try:
        client = get_lambda()
        client.invoke(
            FunctionName   = LAMBDA_FUNCTION_NAME,
            InvocationType = 'Event',                        # async — no wait
            Payload        = json.dumps(payload).encode(),
        )
        logger.info(
            "Lambda trigger fired — user='%s', event='%s', match=%s",
            username, event_type, match,
        )
        return True

    except Exception as exc:
        # Lambda trigger is optional — a missing or misconfigured function
        # should never break the authentication experience.
        logger.warning(
            "Lambda trigger skipped (non-fatal): %s. "
            "Deploy the Lambda function to enable post-auth actions.",
            exc,
        )
        return False
