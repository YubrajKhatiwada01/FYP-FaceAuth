"""
faceauth_email_unlock_lambda.py
=============================================================================
AWS Lambda Function for: FaceAuth-email-unlock
Triggered by: AWS API Gateway (GET request from email button click)

What this function does:
  1. Extracts 'name' from URL query parameters (e.g. ?name=John+Doe).
  2. Connects to AWS IoT Core (iot-data).
  3. Publishes JSON: {"status": "GRANTED", "name": "<User Name>"}
     to topic: 'server_room/door_command' (QoS 0 or 1).
  4. Returns a styled HTML confirmation screen to the user's mobile browser.
=============================================================================
"""

import json
import os
import boto3
import urllib.parse
import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ── Configuration ──────────────────────────────────────────────────────────
# Set this in your Lambda Environment Variables OR leave the default below:
IOT_ENDPOINT = os.environ.get("IOT_ENDPOINT", "a1c2zh6eth22hy-ats.iot.us-east-1.amazonaws.com")
AWS_REGION   = os.environ.get("AWS_REGION", "us-east-1")
TOPIC_NAME   = "server_room/door_command"

# Initialize IoT Data Client
iot_client = boto3.client(
    "iot-data",
    region_name=AWS_REGION,
    endpoint_url=f"https://{IOT_ENDPOINT}",
)


def lambda_handler(event, context):
    logger.info("Received event from API Gateway: %s", json.dumps(event))

    # 1. Extract query parameters (name and user_id)
    query_params = event.get("queryStringParameters") or {}
    user_name = query_params.get("name", "Authorized User")
    # Clean up name
    user_name = urllib.parse.unquote_plus(user_name)

    # 2. Construct the exact JSON payload expected by the ESP32 Arduino sketch
    payload = {
        "status": "GRANTED",
        "name": user_name
    }
    payload_json = json.dumps(payload)

    # 3. Publish to AWS IoT Core topic: server_room/door_command
    try:
        logger.info("Publishing to IoT topic '%s': %s", TOPIC_NAME, payload_json)
        iot_client.publish(
            topic=TOPIC_NAME,
            qos=1,
            payload=payload_json.encode("utf-8")
        )
        logger.info("Successfully published door unlock command to AWS IoT Core.")
        unlock_success = True
        error_message = None
    except Exception as exc:
        logger.error("Failed to publish to AWS IoT Core: %s", exc)
        unlock_success = False
        error_message = str(exc)

    # 4. Return modern HTML page for the user's phone / browser
    if unlock_success:
        html_body = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Door Access Granted</title>
  <style>
    body {{
      margin: 0; padding: 0; background-color: #0b0f19; color: #f8fafc;
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
      display: flex; align-items: center; justify-content: center; min-height: 100vh;
    }}
    .card {{
      background: #131b2e; border: 1px solid #2a3859; border-radius: 20px;
      padding: 44px 28px; max-width: 440px; width: 90%; text-align: center;
      box-shadow: 0 20px 40px rgba(0,0,0,0.6);
    }}
    .icon {{ font-size: 56px; margin-bottom: 14px; animation: bounce 0.6s ease; }}
    h1 {{ margin: 0 0 10px; font-size: 24px; font-weight: 700; color: #10b981; }}
    p {{ color: #94a3b8; font-size: 15px; line-height: 1.6; margin: 0 0 24px; }}
    .badge {{
      display: inline-block; background: rgba(16, 185, 129, 0.14);
      color: #10b981; border: 1px solid rgba(16, 185, 129, 0.35);
      padding: 10px 22px; border-radius: 999px; font-weight: 600; font-size: 14px;
    }}
    @keyframes bounce {{
      0%, 100% {{ transform: translateY(0); }}
      50% {{ transform: translateY(-8px); }}
    }}
  </style>
</head>
<body>
  <div class="card">
    <div class="icon">🔓</div>
    <h1>Access Approved!</h1>
    <p>Door unlock command sent for <strong>{user_name}</strong>.</p>
    <div class="badge">✓ ESP32 Door Unlocked (5 Seconds)</div>
  </div>
</body>
</html>"""
        status_code = 200
    else:
        html_body = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Error Unlocking Door</title>
  <style>
    body {{
      margin: 0; padding: 0; background-color: #0b0f19; color: #f8fafc;
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
      display: flex; align-items: center; justify-content: center; min-height: 100vh;
    }}
    .card {{
      background: #131b2e; border: 1px solid #ef4444; border-radius: 20px;
      padding: 44px 28px; max-width: 440px; width: 90%; text-align: center;
    }}
    .icon {{ font-size: 56px; margin-bottom: 14px; }}
    h1 {{ margin: 0 0 10px; font-size: 24px; color: #ef4444; }}
    p {{ color: #94a3b8; font-size: 14px; line-height: 1.6; }}
  </style>
</head>
<body>
  <div class="card">
    <div class="icon">⚠️</div>
    <h1>Unlock Failed</h1>
    <p>Failed to send MQTT signal to AWS IoT Core.<br><code>{error_message}</code></p>
  </div>
</body>
</html>"""
        status_code = 500

    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "text/html",
            "Access-Control-Allow-Origin": "*"
        },
        "body": html_body
    }
