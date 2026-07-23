"""
aws_s3.py — S3 photo storage helpers for FaceAuth.

Replaces the local  static/uploads/  filesystem storage.

S3 Key Convention
-----------------
All enrolled face photos are stored under the  photos/  prefix:
    photos/{uuid}.jpg

The value stored in the DynamoDB  photo_path  field is this full S3 key
(e.g. "photos/abc123.jpg").  Flask's /uploads/<path:filename> route will
receive "photos/abc123.jpg" and forward it to get_photo_bytes().
"""

import io
import logging
import mimetypes

from aws_config import get_s3, S3_BUCKET_NAME

logger = logging.getLogger(__name__)

PHOTO_PREFIX = 'photos/'


# ── Upload ────────────────────────────────────────────────────────────────────

def upload_photo(file_obj, filename: str) -> str:
    """
    Upload an open file-like object (from request.files) to S3.

    Args:
        file_obj : werkzeug FileStorage (or any file-like object)
        filename : target filename or key e.g. 'abc123.jpg' or 'users/bob/sample.jpg'

    Returns:
        S3 key string
    """
    s3_key       = filename if ('/' in filename) else (PHOTO_PREFIX + filename)
    content_type = mimetypes.guess_type(filename)[0] or 'image/jpeg'

    s3 = get_s3()
    s3.upload_fileobj(
        file_obj,
        S3_BUCKET_NAME,
        s3_key,
        ExtraArgs={'ContentType': content_type},
    )
    logger.info("Photo uploaded to S3: %s", s3_key)
    return s3_key


def upload_photo_bytes(image_bytes: bytes, filename: str) -> str:
    """
    Upload raw bytes to S3.

    Returns:
        S3 key string
    """
    s3_key       = filename if ('/' in filename) else (PHOTO_PREFIX + filename)
    content_type = mimetypes.guess_type(filename)[0] or 'image/jpeg'

    s3 = get_s3()
    s3.put_object(
        Bucket      = S3_BUCKET_NAME,
        Key         = s3_key,
        Body        = image_bytes,
        ContentType = content_type,
    )
    logger.info("Photo bytes uploaded to S3: %s", s3_key)
    return s3_key


# ── Download ──────────────────────────────────────────────────────────────────

def get_photo_bytes(s3_key: str) -> tuple[bytes, str]:
    """
    Download a photo from S3 and return its raw bytes.

    Args:
        s3_key : full S3 key, e.g. 'photos/abc123.jpg'

    Returns:
        (bytes, content_type) tuple

    Raises:
        Exception if the object does not exist or cannot be downloaded.
    """
    s3       = get_s3()
    response = s3.get_object(Bucket=S3_BUCKET_NAME, Key=s3_key)
    content_type = response.get('ContentType', 'image/jpeg')
    data     = response['Body'].read()
    return data, content_type


# ── Delete ────────────────────────────────────────────────────────────────────

def delete_photo(s3_key: str) -> bool:
    """
    Delete a photo from S3.

    Args:
        s3_key : full S3 key, e.g. 'photos/abc123.jpg'

    Returns:
        True on success, False if s3_key is empty or deletion fails.
    """
    if not s3_key:
        return False
    try:
        s3 = get_s3()
        s3.delete_object(Bucket=S3_BUCKET_NAME, Key=s3_key)
        logger.info("Photo deleted from S3: %s", s3_key)
        return True
    except Exception as exc:
        logger.error("Failed to delete S3 object '%s': %s", s3_key, exc)
        return False


# ── Presigned URL (optional helper) ──────────────────────────────────────────

def get_presigned_url(s3_key: str, expiry: int = 3600) -> str:
    """
    Generate a time-limited presigned URL for direct browser access.

    Args:
        s3_key : full S3 key
        expiry : URL lifetime in seconds (default 1 hour)

    Returns:
        HTTPS presigned URL string
    """
    s3 = get_s3()
    return s3.generate_presigned_url(
        'get_object',
        Params   = {'Bucket': S3_BUCKET_NAME, 'Key': s3_key},
        ExpiresIn= expiry,
    )
