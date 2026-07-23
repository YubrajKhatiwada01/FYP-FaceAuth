"""
face_recognition_service.py — Real facial recognition using the `face_recognition` library.

This module provides 1:1 face verification: given a stored enrollment photo and a
live camera frame (as raw bytes), it checks whether the face in the frame matches
the stored face.

Photo Source (Hybrid AWS Mode)
------------------------------
Enrolled photos are now stored in S3.  get_user_encoding() fetches the photo
bytes from S3 using the S3 key stored in the user's  photo_path  field
(e.g. "photos/abc123.jpg").  upload_folder is no longer used but is kept
as an optional parameter for backward-compatibility during development.

Cache strategy
--------------
Face encodings are expensive to compute (dlib CNN / HOG). We cache them in a
module-level dict keyed by (user_id, photo_path) so re-verification of the same
user within the same process does not re-encode the stored photo every time.
The cache is invalidated automatically when photo_path changes (e.g. user updates
their profile picture), because the key changes.
"""

import io
import os
import sys
import site
import logging

# ---------------------------------------------------------------------------
# Ensure user site-packages are on sys.path so packages installed with
# `pip install --user` (the default when system site-packages are read-only)
# are importable even when the app is launched via run.bat / python app.py.
# ---------------------------------------------------------------------------
_user_site = site.getusersitepackages()
if _user_site and _user_site not in sys.path:
    sys.path.insert(0, _user_site)


logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional import — graceful degradation if face_recognition is not installed
# ---------------------------------------------------------------------------
try:
    import face_recognition                  # type: ignore[import-untyped]
    FACE_RECOGNITION_AVAILABLE = True
except ImportError:
    face_recognition = None                  # type: ignore[assignment]
    FACE_RECOGNITION_AVAILABLE = False
    logger.warning(
        "face_recognition library not installed. "
        "Run: pip install face_recognition  "
        "Face authentication will be unavailable."
    )

# numpy is imported lazily inside functions to avoid crash on startup

# ---------------------------------------------------------------------------
# In-memory encoding cache
# key  : (user_id: int, photo_path: str)
# value: ndarray (128-d face encoding) or None if no face detected
# ---------------------------------------------------------------------------
_encoding_cache: dict = {}


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def is_available() -> bool:
    """Return True if the face_recognition library is installed and usable."""
    return FACE_RECOGNITION_AVAILABLE


def clear_cache(user_id: int | None = None) -> None:
    """
    Flush cached face encodings.

    Parameters
    ----------
    user_id : int, optional
        If provided, only invalidate cache entries for that user.
        If None (default), flush the entire cache.
    """
    global _encoding_cache
    if user_id is None:
        _encoding_cache.clear()
        logger.debug("Face encoding cache cleared (all users).")
    else:
        keys_to_delete = [k for k in _encoding_cache if k[0] == user_id]
        for k in keys_to_delete:
            del _encoding_cache[k]
        logger.debug("Face encoding cache cleared for user_id=%s.", user_id)


def get_user_encoding(
    user_id,
    photo_path: str,
    upload_folder: str = "",          # kept for backward-compat; ignored in AWS mode
) -> object | None:
    """
    Load and encode the stored enrollment photo for a user.

    In AWS Hybrid mode the photo is fetched from S3 using  photo_path  as
    the S3 key (e.g. 'photos/abc123.jpg').  Falls back to local disk when
    S3 is not configured or for development use.

    Returns
    -------
    np.ndarray or None
        128-d face encoding vector, or None if no face could be detected in
        the stored photo.

    Raises
    ------
    RuntimeError
        If face_recognition is not installed.
    FileNotFoundError
        If the photo cannot be found in S3 or on local disk.
    """
    if not FACE_RECOGNITION_AVAILABLE:
        raise RuntimeError("face_recognition library is not installed.")

    cache_key = (str(user_id), photo_path)

    if cache_key in _encoding_cache:
        logger.debug("Cache hit for user_id=%s, photo=%s", user_id, photo_path)
        return _encoding_cache[cache_key]

    # ── Fetch photo bytes (S3 preferred, local fallback) ─────────────────────
    photo_bytes = None
    s3_configured = bool(os.environ.get('AWS_ACCESS_KEY_ID'))

    if s3_configured and photo_path:
        try:
            from aws_s3 import get_photo_bytes
            photo_bytes, _ = get_photo_bytes(photo_path)
            logger.info(
                "Fetched enrollment photo from S3 for user_id=%s (%s)",
                user_id, photo_path,
            )
        except Exception as s3_err:
            logger.warning(
                "S3 fetch failed for user_id=%s (%s): %s — trying local disk.",
                user_id, photo_path, s3_err,
            )

    if photo_bytes is None:
        # Local disk fallback (development / pre-migration)
        filename  = os.path.basename(photo_path) if photo_path else photo_path
        abs_path  = os.path.join(upload_folder, filename) if upload_folder else filename
        if not os.path.isfile(abs_path):
            raise FileNotFoundError(
                f"Enrolled photo not found in S3 or on disk: {photo_path}"
            )
        with open(abs_path, 'rb') as fh:
            photo_bytes = fh.read()
        logger.info(
            "Loaded enrollment photo from local disk for user_id=%s.", user_id
        )

    # ── Encode the photo ──────────────────────────────────────────────────────
    logger.info("Encoding stored photo for user_id=%s (%s) …", user_id, photo_path)
    image     = face_recognition.load_image_file(io.BytesIO(photo_bytes))
    encodings = face_recognition.face_encodings(image)

    if not encodings:
        logger.warning(
            "No face detected in enrolled photo for user_id=%s (%s). "
            "Ask the user to re-upload a clear, front-facing photo.",
            user_id, photo_path,
        )
        _encoding_cache[cache_key] = None
        return None

    if len(encodings) > 1:
        logger.warning(
            "Multiple faces detected in enrolled photo for user_id=%s. "
            "Using the first face found.",
            user_id,
        )

    encoding = encodings[0]
    _encoding_cache[cache_key] = encoding
    logger.info("Encoding cached for user_id=%s.", user_id)
    return encoding


def recognize_face(
    image_bytes: bytes,
    user_id,
    photo_path: str | None = None,
    photo_paths: list | None = None,
    upload_folder: str = "",          # kept for backward-compat; ignored in AWS mode
    tolerance: float = 0.55,
) -> dict:
    """
    Compare a live camera frame against a user's enrolled photo(s).

    Parameters
    ----------
    image_bytes : bytes
        Raw JPEG/PNG bytes of the captured camera frame.
    user_id : int
        Database ID of the user to verify against.
    photo_path : str, optional
        S3 key or filename of the user's primary photo.
    photo_paths : list, optional
        List of S3 keys or filenames of the user's sample photos.
    upload_folder : str
        Absolute path to the uploads directory.
    tolerance : float
        Maximum face distance to consider a match. Lower = stricter.
        Default 0.55.

    Returns
    -------
    dict with keys:
        match      : bool   — True if face matches any sample
        confidence : float  — percentage 0–100 (highest match confidence)
        face_count : int    — number of faces detected in the live frame
        distance   : float  — raw face distance of the best match
        error      : str | None — human-readable error message if something failed
    """
    result: dict = {
        "match":      False,
        "confidence": 0.0,
        "face_count": 0,
        "distance":   1.0,
        "error":      None,
    }

    if not FACE_RECOGNITION_AVAILABLE:
        result["error"] = (
            "face_recognition library is not installed on the server. "
            "Run: pip install face_recognition"
        )
        return result

    # -- 1. Load known encodings for this user --------------------------------
    paths_to_check = []
    if photo_paths:
        paths_to_check = [p for p in photo_paths if p]
    elif photo_path:
        paths_to_check = [photo_path]

    if not paths_to_check:
        result["error"] = "No enrolled photos found for this user."
        return result

    known_encodings = []
    from concurrent.futures import ThreadPoolExecutor

    def load_one(path):
        try:
            return get_user_encoding(user_id, path, upload_folder)
        except Exception as exc:
            logger.warning("Failed to load encoding for path %s: %s", path, exc)
            return None

    with ThreadPoolExecutor(max_workers=min(len(paths_to_check), 10)) as executor:
        encs = list(executor.map(load_one, paths_to_check))
        for enc in encs:
            if enc is not None:
                known_encodings.append(enc)

    if not known_encodings:
        result["error"] = (
            "No faces could be detected in any of the user's enrolled photos. "
            "Please upload new, clear face photos in User Management."
        )
        return result

    # -- 2. Decode the incoming frame ----------------------------------------
    try:
        import numpy as _np                  # lazy import — user site-packages now on path
        from PIL import Image                # type: ignore[import-untyped]
        pil_image  = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        live_image = _np.array(pil_image)
    except ImportError as imp_err:
        result["error"] = (
            f"Missing dependency ({imp_err}). "
            "Run: pip install numpy Pillow"
        )
        return result
    except Exception as exc:
        result["error"] = f"Could not decode image from camera: {exc}"
        return result

    # -- 3. Detect faces in the live frame ------------------------------------
    live_locations = face_recognition.face_locations(live_image)
    result["face_count"] = len(live_locations)

    if not live_locations:
        result["error"] = (
            "No face detected in the camera frame. "
            "Ensure you are well-lit and facing the camera directly."
        )
        return result

    if len(live_locations) > 1:
        logger.info(
            "Multiple faces (%d) in live frame for user_id=%s — using largest.",
            len(live_locations), user_id,
        )
        # Pick the largest bounding box (most prominent face)
        live_locations = [_largest_face(live_locations)]

    # -- 4. Encode the live face ----------------------------------------------
    live_encodings = face_recognition.face_encodings(live_image, live_locations)
    if not live_encodings:
        result["error"] = "Could not encode face in the live frame. Please retry."
        return result

    live_encoding = live_encodings[0]

    # -- 5. Compare -----------------------------------------------------------
    distances = face_recognition.face_distance(known_encodings, live_encoding)
    if len(distances) == 0:
        result["error"] = "Comparison failed. Please retry."
        return result

    min_distance = float(min(distances))
    match        = bool(min_distance <= tolerance)

    # Convert distance to a 0–100 confidence score
    raw_conf   = max(0.0, 1.0 - min_distance)
    confidence = round(raw_conf * 100, 1)

    result["match"]      = match
    result["confidence"] = confidence
    result["distance"]   = round(min_distance, 4)

    logger.info(
        "Face recognition result for user_id=%s: match=%s, best_distance=%.4f, confidence=%.1f%% (from %d samples)",
        user_id, match, min_distance, confidence, len(known_encodings),
    )
    return result


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _largest_face(locations: list) -> tuple:
    """Return the bounding box with the largest area from a list of (top, right, bottom, left)."""
    def area(loc):
        top, right, bottom, left = loc
        return (bottom - top) * (right - left)
    return max(locations, key=area)
