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

import collections
import importlib
import importlib.util
import io
import os
import sys
import site
import logging
import threading

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
# Lazy availability check — do NOT import face_recognition at module level.
# Importing it at startup forces dlib to load its CNN/HOG models which takes
# 15–45 s and blocks the server from accepting connections.
# Instead we check whether the package is present using importlib.util and
# defer the actual import to the first function call that needs it.
# ---------------------------------------------------------------------------

FACE_RECOGNITION_AVAILABLE: bool = importlib.util.find_spec('face_recognition') is not None
if not FACE_RECOGNITION_AVAILABLE:
    logger.warning(
        "face_recognition library not installed. "
        "Run: pip install face_recognition  "
        "Face authentication will be unavailable."
    )

# Module-level reference filled lazily on first use
_fr = None  # will hold the face_recognition module


def _get_fr():
    """
    Return the face_recognition module, importing it the first time it is
    needed.  This keeps it out of the startup critical path.
    """
    global _fr, FACE_RECOGNITION_AVAILABLE
    if _fr is not None:
        return _fr
    try:
        import face_recognition as _face_recognition  # type: ignore[import-untyped]
        _fr = _face_recognition
        FACE_RECOGNITION_AVAILABLE = True
    except ImportError:
        FACE_RECOGNITION_AVAILABLE = False
        raise RuntimeError("face_recognition library is not installed.")
    return _fr

# numpy is imported lazily inside functions to avoid crash on startup

# ---------------------------------------------------------------------------
# Thread-safe LRU in-memory encoding cache
# key  : (user_id: str, photo_path: str)
# value: ndarray (128-d face encoding) or None if no face detected
#
# Max size is bounded to _CACHE_MAX_SIZE entries to prevent unbounded memory
# growth on long-running servers.  Least-recently-used entries are evicted
# when the limit is reached.
# ---------------------------------------------------------------------------
_CACHE_MAX_SIZE = 256
_encoding_cache: collections.OrderedDict = collections.OrderedDict()
_cache_lock = threading.Lock()


def _cache_get(key):
    """Return (True, value) on hit, (False, None) on miss — O(1) LRU lookup."""
    with _cache_lock:
        if key not in _encoding_cache:
            return False, None
        # Move to end (most-recently-used)
        _encoding_cache.move_to_end(key)
        return True, _encoding_cache[key]


def _cache_set(key, value):
    """Insert/update a cache entry, evicting the LRU entry if over capacity."""
    with _cache_lock:
        if key in _encoding_cache:
            _encoding_cache.move_to_end(key)
        _encoding_cache[key] = value
        if len(_encoding_cache) > _CACHE_MAX_SIZE:
            _encoding_cache.popitem(last=False)  # evict oldest


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
    with _cache_lock:
        if user_id is None:
            _encoding_cache.clear()
            logger.debug("Face encoding cache cleared (all users).")
        else:
            uid_str = str(user_id)
            keys_to_delete = [k for k in _encoding_cache if k[0] == uid_str]
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

    hit, cached_enc = _cache_get(cache_key)
    if hit:
        logger.debug("Cache hit for user_id=%s, photo=%s", user_id, photo_path)
        return cached_enc

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
    fr        = _get_fr()
    image     = fr.load_image_file(io.BytesIO(photo_bytes))
    
    # Downscale enrolled photo to max 480px — keeps dlib memory usage low
    # (640 caused 'bad allocation' on machines with limited RAM; 480 is safe)
    small_image, _ = _resize_for_detection(image, max_side=480)
    encodings = fr.face_encodings(small_image)

    if not encodings:
        logger.warning(
            "No face detected in enrolled photo for user_id=%s (%s). "
            "Ask the user to re-upload a clear, front-facing photo.",
            user_id, photo_path,
        )
        _cache_set(cache_key, None)
        return None

    if len(encodings) > 1:
        logger.warning(
            "Multiple faces detected in enrolled photo for user_id=%s. "
            "Using the first face found.",
            user_id,
        )

    encoding = encodings[0]
    _cache_set(cache_key, encoding)
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

    def load_one(path):
        """
        Load encoding for a single path.
        On 'bad allocation' (dlib OOM), retry once at a smaller resolution.
        Running multiple of these concurrently is the primary cause of
        'bad allocation' — keep max_workers=1 (sequential) to avoid it.
        """
        try:
            return get_user_encoding(user_id, path, upload_folder)
        except MemoryError as exc:
            logger.warning(
                "MemoryError loading encoding for path %s: %s — skipping.",
                path, exc,
            )
            return None
        except Exception as exc:
            exc_str = str(exc)
            # dlib raises std::bad_alloc which surfaces as a generic Exception
            # with 'bad allocation' in the message
            if 'bad allocation' in exc_str or 'std::bad_alloc' in exc_str:
                logger.warning(
                    "bad allocation loading encoding for path %s — "
                    "retrying at 320px resolution.", path,
                )
                try:
                    # Re-encode at half the normal size to reduce peak RAM usage
                    fr_mod = _get_fr()
                    from aws_s3 import get_photo_bytes as _gpb
                    pb, _ = _gpb(path)
                    import io as _io
                    img = fr_mod.load_image_file(_io.BytesIO(pb))
                    small, _ = _resize_for_detection(img, max_side=320)
                    encs = fr_mod.face_encodings(small)
                    enc = encs[0] if encs else None
                    _cache_set((str(user_id), path), enc)
                    return enc
                except Exception as retry_exc:
                    logger.warning(
                        "Retry also failed for path %s: %s — skipping.",
                        path, retry_exc,
                    )
                    return None
            logger.warning("Failed to load encoding for path %s: %s", path, exc)
            return None

    # ── Sequential loading (max_workers=1) ──────────────────────────────────
    # dlib's CNN face encoder uses ~200-500 MB RAM per call. Running multiple
    # simultaneously with ThreadPoolExecutor caused 'bad allocation' crashes.
    # Sequential processing is safer and only marginally slower for ≤5 samples.
    for path in paths_to_check:
        enc = load_one(path)
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

    # -- 3. Detect and encode faces on downscaled frame for high speed -------
    fr = _get_fr()
    small_image, _ = _resize_for_detection(live_image, max_side=640)
    small_locations = fr.face_locations(small_image)
    result["face_count"] = len(small_locations)

    if not small_locations:
        result["error"] = (
            "No face detected in the camera frame. "
            "Ensure you are well-lit and facing the camera directly."
        )
        return result

    if len(small_locations) > 1:
        logger.info(
            "Multiple faces (%d) in live frame for user_id=%s — using largest.",
            len(small_locations), user_id,
        )
        small_locations = [_largest_face(small_locations)]

    # -- 4. Encode the live face directly on downscaled frame -----------------
    live_encodings = fr.face_encodings(small_image, small_locations)
    if not live_encodings:
        result["error"] = "Could not encode face in the live frame. Please retry."
        return result

    live_encoding = live_encodings[0]

    # -- 5. Compare -----------------------------------------------------------
    distances = fr.face_distance(known_encodings, live_encoding)
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


def _resize_for_detection(image, max_side: int = 640):
    """
    Downscale *image* (numpy RGB array) so its longest dimension is at most
    *max_side* pixels, while preserving the aspect ratio.

    Returns (resized_image, scale_factor) where scale_factor is the ratio
    original / resized.  When the image is already small enough the original
    is returned unchanged with scale_factor=1.0.

    The caller uses scale_factor to map bounding-box coordinates found on the
    resized image back to the original resolution for the encoding step.
    """
    import numpy as _np
    h, w = image.shape[:2]
    longest = max(h, w)
    if longest <= max_side:
        return image, 1.0

    scale    = max_side / longest
    new_w    = max(1, int(w * scale))
    new_h    = max(1, int(h * scale))

    try:
        from PIL import Image as _PILImage
        pil = _PILImage.fromarray(image)
        pil = pil.resize((new_w, new_h), _PILImage.BILINEAR)
        return _np.array(pil), 1.0 / scale   # scale_factor: resized→original
    except ImportError:
        # PIL not available — skip resize (graceful degradation)
        return image, 1.0
