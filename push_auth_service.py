"""
push_auth_service.py
=============================================================================
Push Authentication (Magic Link / HTML Email Button) Service for FaceAuth.

Handles:
  1. Sending a responsive, styled HTML email containing a clickable
     "Approve & Unlock Door" button.
  2. The button directly links to the AWS API Gateway endpoint that triggers
     the ESP32 door controller.
  3. Formats and delivers the email via Gmail SMTP SSL.
=============================================================================
"""

import os
import smtplib
import urllib.parse
import logging
from email.message import EmailMessage
from typing import Tuple
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# Ensure .env is loaded
load_dotenv()


def get_smtp_config():
    """Retrieve the latest SMTP credentials from the environment."""
    load_dotenv(override=True)
    server   = os.environ.get("SMTP_SERVER", "smtp.gmail.com").strip()
    port     = int(os.environ.get("SMTP_PORT", 465))
    email    = (os.environ.get("SMTP_EMAIL") or os.environ.get("SENDER_EMAIL") or "").strip()
    password = (os.environ.get("SMTP_PASSWORD") or os.environ.get("SENDER_PASSWORD") or "").replace(" ", "").strip()
    url      = (os.environ.get("AWS_DOOR_UNLOCK_URL") or "YOUR_AWS_API_URL_HERE").strip()
    return server, port, email, password, url


def is_smtp_configured() -> bool:
    """Check if valid sender credentials are provided."""
    _, _, email, password, _ = get_smtp_config()
    return bool(email and password and password != "your_16_digit_app_password")


def mask_email(email: str) -> str:
    """Mask email for privacy, e.g., 'john.doe@gmail.com' -> 'j***e@gmail.com'."""
    if not email or "@" not in email:
        return email or "your registered email"
    user_part, domain = email.split("@", 1)
    if len(user_part) <= 2:
        masked_user = user_part[0] + "*"
    else:
        masked_user = user_part[0] + "***" + user_part[-1]
    return f"{masked_user}@{domain}"


def send_push_approval_email(recipient_email: str, recipient_name: str, access_point: str = "Server Room", user_id: str = None, token: str = None) -> Tuple[bool, str, str]:
    """
    Send an HTML email containing a styled, clickable 'Approve & Unlock Door' button.

    Parameters
    ----------
    recipient_email : str
        Email address of the recognized user.
    recipient_name : str
        Full name of the user.
    access_point : str
        Name/location of the physical door station.
    user_id : str, optional
        ID of the recognized user.
    token : str, optional
        Unique single-use token for this authentication attempt.

    Returns
    -------
    Tuple[bool, str, str]
        (success, status_message, masked_email)
    """
    if not recipient_email:
        return False, "No registered email address found for this user.", ""

    masked = mask_email(recipient_email)
    smtp_server, smtp_port, smtp_email, smtp_password, aws_url = get_smtp_config()

    # Format the unlock URL with user details and single-use token
    unlock_url = aws_url
    if unlock_url and unlock_url != "YOUR_AWS_API_URL_HERE":
        params = {"name": recipient_name}
        if user_id:
            params["user_id"] = str(user_id)
        if token:
            params["token"] = str(token)
        sep = "&" if "?" in unlock_url else "?"
        unlock_url = f"{unlock_url}{sep}{urllib.parse.urlencode(params)}"
    elif user_id:
        unlock_url = f"/auth/approve/{user_id}?token={token}" if token else f"/auth/approve/{user_id}"

    if not is_smtp_configured():
        logger.warning(
            "SMTP is not configured in .env. DEMO MODE: Push approval simulated for '%s' (%s) -> Link: %s",
            recipient_name, masked, unlock_url
        )
        return True, f"DEMO MODE: Push email simulated to {masked}", masked

    msg = EmailMessage()
    msg["Subject"] = f"🔐 Door Access Approval Request — {access_point}"
    msg["From"]    = f"FaceAuth System <{smtp_email}>"
    msg["To"]      = recipient_email

    # Plain text fallback
    plain_text = f"""Hello {recipient_name},

A facial recognition match was detected for your account at '{access_point}'.

To authorize entry and unlock the door, please click the approval link below:
{unlock_url}

⚠️ If you did not request entry, do NOT click the link and notify building security immediately.

Best regards,
FaceAuth Security System
"""
    msg.set_content(plain_text)

    # Rich CSS-styled HTML email
    html_content = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Door Access Approval</title>
</head>
<body style="margin: 0; padding: 0; background-color: #0b0f19; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; color: #e2e8f0;">
  <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background-color: #0b0f19; padding: 40px 15px;">
    <tr>
      <td align="center">
        <table role="presentation" width="100%" style="max-width: 520px; background: #131b2e; border: 1px solid #2a3859; border-radius: 16px; overflow: hidden; box-shadow: 0 10px 30px rgba(0,0,0,0.5);">
          
          <!-- Header Banner -->
          <tr>
            <td style="padding: 28px 30px 20px; text-align: center; background: linear-gradient(135deg, #1e1b4b 0%, #312e81 100%); border-bottom: 1px solid #3730a3;">
              <div style="font-size: 34px; margin-bottom: 8px;">🔐</div>
              <h2 style="margin: 0; font-size: 20px; font-weight: 700; color: #ffffff; letter-spacing: 0.5px;">FaceAuth Security</h2>
              <p style="margin: 4px 0 0; font-size: 13px; color: #a5b4fc;">Physical Access Control &bull; {access_point}</p>
            </td>
          </tr>

          <!-- Content Body -->
          <tr>
            <td style="padding: 30px 30px 20px;">
              <p style="margin: 0 0 14px; font-size: 15px; color: #f8fafc;">Hello <strong>{recipient_name}</strong>,</p>
              <p style="margin: 0 0 24px; font-size: 14px; color: #94a3b8; line-height: 1.6;">
                A biometric facial match was detected at <strong>{access_point}</strong>. Tap the button below to approve entry and unlock the door:
              </p>

              <!-- Styled Clickable CTA Button -->
              <table role="presentation" cellspacing="0" cellpadding="0" width="100%">
                <tr>
                  <td align="center" style="padding: 10px 0 25px;">
                    <a href="{unlock_url}" target="_blank" style="display: inline-block; padding: 14px 36px; font-size: 15px; font-weight: 700; color: #ffffff; text-decoration: none; background: linear-gradient(135deg, #814BEE 0%, #6366f1 100%); border-radius: 10px; box-shadow: 0 6px 20px rgba(129, 75, 238, 0.45); letter-spacing: 0.3px;">
                      ✓ Approve &amp; Unlock Door
                    </a>
                  </td>
                </tr>
              </table>

              <div style="background: rgba(239, 68, 68, 0.08); border: 1px solid rgba(239, 68, 68, 0.25); border-radius: 8px; padding: 12px 14px; margin-top: 10px;">
                <p style="margin: 0; font-size: 12px; color: #fca5a5; line-height: 1.5;">
                  ⚠️ <strong>Security Notice:</strong> If you are not currently at the door, do NOT click the button and notify building security immediately.
                </p>
              </div>
            </td>
          </tr>

          <!-- Footer -->
          <tr>
            <td style="padding: 16px 30px 24px; text-align: center; border-top: 1px solid #1e293b; font-size: 11px; color: #64748b;">
              Automated Physical Access System &bull; FaceAuth Magic Link 2FA
            </td>
          </tr>

        </table>
      </td>
    </tr>
  </table>
</body>
</html>
"""
    msg.add_alternative(html_content, subtype="html")

    try:
        print(f"[PUSH EMAIL] Sending approval email via {smtp_server}:{smtp_port} from {smtp_email} to {recipient_email}...")
        with smtplib.SMTP_SSL(smtp_server, smtp_port, timeout=10) as server:
            server.login(smtp_email, smtp_password)
            server.send_message(msg)
        print(f"[PUSH EMAIL] Successfully delivered approval email to {recipient_email}")
        logger.info("Push approval email sent successfully to %s", recipient_email)
        return True, f"Approval email sent to {masked}", masked
    except Exception as exc:
        print(f"[PUSH EMAIL ERROR] Failed to send push approval email to {recipient_email}: {exc}")
        logger.error("Failed to send push approval email to %s: %s", recipient_email, exc)
        return False, f"Failed to deliver email: {exc}", masked
