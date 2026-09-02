"""
app.py — FaceAuth Flask Application
=====================================
Two-factor physical access control:
  Step 1 — Bluetooth proximity check (BLE scan via bleak)
  Step 2 — Live facial recognition (dlib via face_recognition)

Backend: AWS DynamoDB (users/logs/access-points) + S3 (photos) + Lambda (post-auth trigger)
"""

import base64
import csv
import io as _io
import logging
import os
import platform
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from functools import wraps

from dotenv import load_dotenv                                            # type: ignore[import-untyped]
from flask import (Flask, Response, jsonify, redirect,                   # type: ignore[import-untyped]
                   render_template, request, send_from_directory,
                   session, url_for)
from flask_wtf.csrf import CSRFProtect, generate_csrf                    # type: ignore[import-untyped]
from werkzeug.security import check_password_hash                        # type: ignore[import-untyped]
from werkzeug.utils import secure_filename                               # type: ignore[import-untyped]

# Internal modules
from config import config
import aws_dynamodb as db           # DynamoDB — users, logs, access-points
import aws_s3                       # S3 — enrolled face photos
import aws_lambda_client            # Lambda — post-auth event trigger
import bluetooth_scanner as bt      # BLE proximity scanner
import face_recognition_service as frs  # dlib face recognition
import push_auth_service             # 2FA Push Authentication (HTML email button)
from aws_iot_door import DoorTriggerController, trigger_door_unlock  # ESP32 door unlock via AWS IoT Core

# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

load_dotenv()  # must be first — reads .env before any os.environ.get() calls

# ── IoT door controller (module-level singleton so the consecutive-match
#    counter persists across repeated HTTP requests from the browser) ──────────
_door_controller = DoorTriggerController()
FACE_TOLERANCE = float(os.environ.get("FACE_TOLERANCE", 0.55))
_approval_events: dict = {}

# Unique instance boot ID generated each time the system runs.
# Any session from a prior run becomes immediately invalid.
SYSTEM_BOOT_ID = uuid.uuid4().hex

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# Silence noisy third-party loggers so the terminal stays clean
for _noisy in ("werkzeug", "botocore", "boto3", "urllib3", "s3transfer"):
    logging.getLogger(_noisy).setLevel(logging.ERROR)

# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------

app = Flask(__name__)

_config_name = os.environ.get("FLASK_ENV", "development") or "default"
app.config.from_object(config[_config_name])

csrf = CSRFProtect(app)

# Local upload folder (dev fallback; production uses S3)
UPLOAD_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config["UPLOAD_FOLDER"]      = UPLOAD_FOLDER
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024  # 5 MB

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}

# Bluetooth proximity thresholds (override in .env)
BT_RSSI_THRESHOLD = int(os.environ.get("BT_RSSI_THRESHOLD", "-50"))
BT_SCAN_DURATION  = float(os.environ.get("BT_SCAN_DURATION", "6.0"))

# Face recognition tolerance (lower = stricter; default 0.55)
FACE_TOLERANCE = float(os.environ.get("FACE_TOLERANCE", "0.55"))

# ---------------------------------------------------------------------------
# Database initialisation
# ---------------------------------------------------------------------------

with app.app_context():
    db.init_db()

# ---------------------------------------------------------------------------
# Face recognition model pre-warm
# ---------------------------------------------------------------------------
# dlib loads its CNN/HOG models on the very first call, which can take
# 30–90 s. Pre-warming in a background daemon thread at startup means the
# models are in memory before the first real authentication request arrives,
# preventing "Failed to fetch" timeouts on the first scan.

def _prewarm_face_recognition() -> None:
    try:
        import numpy as _np
        fr = frs._get_fr()   # triggers lazy import — loads dlib models in background
        dummy = _np.zeros((100, 100, 3), dtype=_np.uint8)
        fr.face_locations(dummy)
        fr.face_encodings(dummy)
        logger.info("[prewarm] face_recognition models loaded and ready.")
    except Exception as exc:
        logger.warning("[prewarm] face_recognition pre-warm skipped: %s", exc)


threading.Thread(
    target=_prewarm_face_recognition,
    daemon=True,
    name="fr-prewarm",
).start()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def allowed_file(filename: str) -> bool:
    """Return True if the file has a permitted image extension."""
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def login_required(f):
    """
    Decorator — redirects unauthenticated users or sessions from prior server runs
    to the administrator login page.
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        if (
            not session.get("is_logged_in")
            or session.get("boot_id") != SYSTEM_BOOT_ID
            or "admin" not in str(session.get("user_role", "")).lower()
        ):
            session.clear()
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


@app.context_processor
def inject_csrf_token():
    return {"csrf_token": generate_csrf}


def _upload_photos_to_s3(files, username: str) -> list[str]:
    """
    Read up to 10 valid image files and upload them to S3 in parallel.

    Args:
        files    : list of werkzeug FileStorage objects
        username : used to build the S3 key prefix

    Returns:
        List of S3 keys for successfully uploaded files.
    """
    valid = [f for f in files if f and f.filename and allowed_file(f.filename)][:10]
    if not valid:
        return []

    payloads = []
    for idx, f in enumerate(valid):
        safe_name = secure_filename(f.filename or "")
        ext = safe_name.rsplit(".", 1)[1].lower() if "." in safe_name else (f.filename.rsplit(".", 1)[1].lower() if f.filename and "." in f.filename else "jpg")
        s3_key = f"users/{username}/photos/sample_{idx}_{uuid.uuid4().hex[:6]}.{ext}"
        try:
            data = f.read()
            if data:
                payloads.append((data, s3_key))
        except Exception as exc:
            logger.error("Failed reading upload file bytes: %s", exc)

    if not payloads:
        return []

    def _upload(item):
        try:
            data, key = item
            return aws_s3.upload_photo_bytes(data, key)
        except Exception as exc:
            logger.error("S3 photo upload failed: %s", exc)
            return None

    with ThreadPoolExecutor(max_workers=min(len(payloads), 10)) as pool:
        res = list(pool.map(_upload, payloads))
        return [r for r in res if r]


# ===========================================================================
# Routes — Public
# ===========================================================================

@app.get("/")
def landing():
    """
    Root entry point: Every time the system is run, it must be accessed
    by the administrator logging into the system.
    """
    if (
        not session.get("is_logged_in")
        or session.get("boot_id") != SYSTEM_BOOT_ID
        or "admin" not in str(session.get("user_role", "")).lower()
    ):
        return redirect(url_for("login"))
    return redirect(url_for("dashboard"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if (
        session.get("is_logged_in")
        and session.get("boot_id") == SYSTEM_BOOT_ID
        and "admin" in str(session.get("user_role", "")).lower()
    ):
        return redirect(url_for("dashboard"))

    message = None

    if request.method == "POST":
        username = request.form.get("operator_id", "").strip()
        password = request.form.get("access_token", "").strip()
        user     = db.get_user_by_username(username)

        if user and check_password_hash(user.get("password", ""), password):
            if user.get("status") != "Active":
                message = {"type": "error",
                           "text": "Account is inactive. Contact your administrator."}
            elif "admin" not in str(user.get("role", "")).lower():
                message = {"type": "error",
                           "text": "Access denied. Only Administrators can log into and operate this system."}
            else:
                session.clear()
                session["operator_id"]  = username
                session["user_id"]      = user["id"]
                session["user_role"]    = user["role"]
                session["is_logged_in"] = True
                session["boot_id"]      = SYSTEM_BOOT_ID
                session.permanent       = False
                db.add_log("Login", username, "Dashboard", "Success",
                           f"Administrator {username} logged into system")
                return redirect(url_for("dashboard"))
        else:
            db.add_log("Login", username or "unknown", "Dashboard", "Failed",
                       "Invalid administrator credentials")
            message = {"type": "error",
                       "text": "Invalid Administrator ID or Access Token."}

    return render_template("login.html", active_page="login", message=message,
                           body_class="login-body")


@app.route("/logout")
def logout():
    username = session.get("operator_id", "unknown")
    db.add_log("Logout", username, "Dashboard", "Success",
               f"Administrator {username} logged out")
    session.clear()
    return redirect(url_for("login"))


# ===========================================================================
# Routes — Dashboard
# ===========================================================================

@app.route("/dashboard")
@login_required
def dashboard():
    # Render the shell instantly — JS will fetch /api/dashboard-stats async
    empty_stats = {
        "access_granted": 0,
        "access_denied":  0,
        "active_users":   0,
        "active_points":  0,
        "success_rate":   0.0,
    }
    return render_template(
        "dashboard.html",
        active_page="dashboard",
        operator_id=session.get("operator_id", "User"),
        stats=empty_stats,
        recent_logs=[],
        chart_7days={"labels": [], "granted": [], "denied": []},
        chart_breakdown=[],
    )


@app.route("/api/dashboard-stats")
@login_required
def api_dashboard_stats():
    """
    Return all dashboard statistics as JSON for async page loading.
    The dashboard HTML renders instantly; JS fetches this endpoint to populate numbers.
    Cached aggressively — returns stale data up to 60 s old rather than blocking.
    """
    with ThreadPoolExecutor(max_workers=7) as pool:
        f_granted   = pool.submit(db.count_access_granted)
        f_denied    = pool.submit(db.count_access_denied)
        f_users     = pool.submit(db.count_active_users)
        f_points    = pool.submit(db.count_active_access_points)
        f_logs      = pool.submit(db.get_recent_logs, limit=8)
        f_chart7    = pool.submit(db.get_chart_data_7days)
        f_breakdown = pool.submit(db.get_event_type_breakdown)

        def _safe(fut, default):
            try:
                r = fut.result(timeout=15)
                return r if r is not None else default
            except Exception as exc:
                logger.error("api_dashboard_stats error: %s", exc)
                return default

        granted     = int(_safe(f_granted, 0))
        denied      = int(_safe(f_denied,  0))
        users_cnt   = int(_safe(f_users,   0))
        points_cnt  = int(_safe(f_points,  0))
        recent_logs = _safe(f_logs, [])
        chart_7days = _safe(f_chart7, {"labels": [], "granted": [], "denied": []})
        chart_breakdown = _safe(f_breakdown, [])

    total = granted + denied
    success_rate = round((granted / total) * 100, 1) if total > 0 else 0.0

    return jsonify({
        "success": True,
        "stats": {
            "access_granted": granted,
            "access_denied":  denied,
            "active_users":   users_cnt,
            "active_points":  points_cnt,
            "success_rate":   success_rate,
        },
        "recent_logs": [
            {
                "event_type":   log.get("event_type", ""),
                "username":     log.get("username", ""),
                "access_point": log.get("access_point", ""),
                "status":       log.get("status", ""),
                "timestamp":    log.get("timestamp", ""),
            }
            for log in recent_logs
        ],
        "chart_7days":    chart_7days,
        "chart_breakdown": chart_breakdown,
    })


# ===========================================================================
# Routes — Notifications & Admin Profile API
# ===========================================================================

@app.route("/api/notifications", methods=["GET"])
@login_required
def api_notifications():
    """
    Return recent audit logs converted into structured notification items.
    Categorized into user_activity, access_log, and system_alert.
    """
    recent_logs = db.get_recent_logs(limit=25)
    notifications = []
    
    for log in recent_logs:
        event_type = log.get("event_type", "System Event")
        username = log.get("username", "System")
        details = log.get("details", "")
        access_point = log.get("access_point", "Dashboard")
        timestamp = log.get("timestamp", "")
        status = log.get("status", "Success")

        category = "system"
        icon_type = "info"
        badge_color = "cyan"
        title = event_type
        
        if event_type in ["User Created", "User Updated", "User Deleted", "BT MAC Updated"]:
            category = "user_activity"
            icon_type = "user"
            badge_color = "purple" if "Created" in event_type else ("blue" if "Updated" in event_type else "red")
        elif event_type in ["Access Granted", "Access Denied", "BT Proximity Check"]:
            category = "access_log"
            if event_type == "Access Granted":
                icon_type = "check_circle"
                badge_color = "green"
            elif event_type == "Access Denied":
                icon_type = "x_circle"
                badge_color = "red"
            else:
                icon_type = "bluetooth"
                badge_color = "cyan"
        elif event_type in ["Login", "Logout"]:
            category = "user_activity"
            icon_type = "shield"
            badge_color = "green" if event_type == "Login" else "orange"

        notifications.append({
            "id": str(log.get("id") or log.get("log_id") or uuid.uuid4()),
            "title": title,
            "username": username,
            "access_point": access_point,
            "status": status,
            "details": details,
            "timestamp": timestamp,
            "category": category,
            "icon_type": icon_type,
            "badge_color": badge_color
        })

    return jsonify({
        "success": True,
        "notifications": notifications,
        "unread_count": min(len(notifications), 5)
    })


@app.route("/api/profile", methods=["GET"])
@login_required
def api_profile():
    """Return profile and session information for the active logged-in administrator."""
    username = session.get("operator_id", "admin")
    user = db.get_user_by_username(username)
    
    user_info = {
        "username": username,
        "full_name": user.get("full_name", "System Administrator") if user else "System Administrator",
        "email": user.get("email", "admin@faceauth.sec") if user else "admin@faceauth.sec",
        "phone": user.get("phone", "+60 11-1234 5678") if user else "+60 11-1234 5678",
        "role": user.get("role", "Super Administrator") if user else "Super Administrator",
        "status": user.get("status", "Active") if user else "Active",
        "photo_path": user.get("photo_path") if user else None,
        "photos": user.get("photos", []) if user else [],
        "created_at": user.get("created_at", "2026-01-01 08:00:00") if user else "2026-01-01 08:00:00",
        "session_id": session.get("user_id", str(uuid.uuid4())[:8]),
        "user_agent": request.user_agent.string if request.user_agent else "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "ip_address": request.remote_addr or "127.0.0.1",
        "security_level": "Tier-1 Root Access (2FA + FaceAuth)",
        "db_sync": "AWS DynamoDB Active",
    }
    
    return jsonify({"success": True, "profile": user_info})




# ===========================================================================
# Routes — Users
# ===========================================================================

@app.route("/users")
@login_required
def users():
    with ThreadPoolExecutor(max_workers=2) as pool:
        f_users  = pool.submit(db.get_all_users)
        f_points = pool.submit(db.get_all_access_points)
        all_users         = f_users.result()
        all_access_points = f_points.result()
    return render_template(
        "users.html",
        active_page="users",
        operator_id=session.get("operator_id", "User"),
        users=all_users,
        access_points=all_access_points,
    )


@app.route("/users/add", methods=["POST"])
@login_required
def add_user():
    full_name   = request.form.get("full_name",  "").strip()
    username    = request.form.get("username",   "").strip()
    role        = request.form.get("role",        "General").strip()
    status      = request.form.get("status",     "Active").strip()
    email       = request.form.get("email",       "").strip()

    if not all([full_name, username, email]):
        return jsonify({"success": False,
                        "error": "Full name, username and email are required."}), 400

    files = (
        [request.files.get("photo")]
        + request.files.getlist("samples")
        + request.files.getlist("samples[]")
    )
    s3_keys     = _upload_photos_to_s3(files, username)
    primary_key = s3_keys[0] if s3_keys else None

    ok, err = db.create_user(full_name, username, role, status,
                             email, primary_key, s3_keys)
    if not ok:
        return jsonify({"success": False, "error": err}), 409

    db.add_log("User Created", session.get("operator_id"), "Users", "Success",
               f"User '{username}' created with {len(s3_keys)} photo(s) in S3")
    return jsonify({"success": True})


@app.route("/users/<user_id>/edit", methods=["POST"])
@login_required
def edit_user(user_id):
    full_name = request.form.get("full_name",  "").strip()
    role      = request.form.get("role",        "General").strip()
    status    = request.form.get("status",     "Active").strip()
    email     = request.form.get("email",       "").strip()

    if not full_name:
        return jsonify({"success": False, "error": "Full name is required."}), 400

    existing = db.get_user_by_id(user_id)
    if not existing:
        return jsonify({"success": False, "error": "User not found."}), 404

    db.update_user(user_id, full_name, role, status, email)

    # ── Surgical photo management ────────────────────────────────────────────
    # deleted_keys: S3 keys the admin explicitly removed in the edit modal
    # kept_keys   : S3 keys the admin kept (not removed)
    # samples     : new files/webcam frames to upload
    deleted_keys = request.form.getlist("deleted_keys")
    kept_keys    = request.form.getlist("kept_keys")

    # Delete only the photos the admin removed
    photos_changed = False
    for key in deleted_keys:
        if key:
            aws_s3.delete_photo(key)
            photos_changed = True

    # Upload any new photos
    main_file    = request.files.get("photo")
    sample_files = request.files.getlist("samples") + request.files.getlist("samples[]")
    new_files    = [main_file] + sample_files
    has_new      = any(f and f.filename for f in new_files)

    new_keys: list[str] = []
    if has_new:
        username = existing.get("username", "unknown")
        new_keys = _upload_photos_to_s3(new_files, username)
        photos_changed = True

    if photos_changed or kept_keys or new_keys:
        # Merge kept existing photos with any newly uploaded ones
        final_keys  = [k for k in kept_keys if k] + new_keys
        primary_key = final_keys[0] if final_keys else None
        db.update_user_photo(user_id, primary_key)
        db.update_user_photos(user_id, final_keys if final_keys else None)

        # Invalidate cached face encodings so next auth uses fresh photos
        if photos_changed:
            frs.clear_cache(user_id)

    db.add_log("User Updated", session.get("operator_id"), "Users", "Success",
               f"User ID {user_id} updated by {session.get('operator_id')}")
    return jsonify({"success": True})


@app.route("/users/<user_id>/photos", methods=["GET"])
@login_required
def get_user_photos(user_id):
    """
    Return all enrolled S3 photo keys for a user.
    Used by the Edit User modal to reliably load all photos even for users
    created before multi-photo support was added (where data-photos may be stale).
    """
    user = db.get_user_by_id(user_id)
    if not user:
        return jsonify({"success": False, "error": "User not found."}), 404

    photos = user.get("photos") or []
    if not photos and user.get("photo_path"):
        photos = [user["photo_path"]]

    return jsonify({"success": True, "photos": photos})


@app.route("/users/<user_id>/delete", methods=["POST"])
@login_required
def delete_user(user_id):
    user = db.get_user_by_id(user_id)
    if not user:
        return jsonify({"success": False, "error": "User not found."}), 404

    for key in (user.get("photos") or []):
        aws_s3.delete_photo(key)
    if not user.get("photos") and user.get("photo_path"):
        aws_s3.delete_photo(user["photo_path"])

    frs.clear_cache(user_id)
    db.delete_user(user_id)
    db.add_log("User Deleted", session.get("operator_id"), "Users", "Success",
               f"User '{user['username']}' deleted by {session.get('operator_id')}")
    return jsonify({"success": True})


@app.route("/users/<user_id>/bluetooth", methods=["POST"])
@login_required
def set_user_bluetooth(user_id):
    """Assign or clear a Bluetooth MAC address for a user."""
    data = request.get_json(silent=True) or {}
    mac  = data.get("mac", "").strip()

    if mac and not bt.validate_mac_address(mac):
        return jsonify({"success": False,
                        "error": "Invalid MAC address format. Use XX:XX:XX:XX:XX:XX"}), 400

    user = db.get_user_by_id(user_id)
    if not user:
        return jsonify({"success": False, "error": "User not found."}), 404

    db.update_user_bluetooth_mac(user_id, mac)
    db.add_log("BT MAC Updated", session.get("operator_id"), "Users", "Success",
               f"Bluetooth MAC {'set to ' + mac if mac else 'cleared'} for '{user['username']}'")
    return jsonify({"success": True, "mac": mac.upper() if mac else None})


# ===========================================================================
# Routes — Face Authentication
# ===========================================================================

@app.route("/authentication")
@login_required
def authentication():
    return render_template(
        "authentication.html",
        active_page="authentication",
        operator_id=session.get("operator_id", "User"),
        users=db.get_all_users(),
    )


@app.route("/authentication/log", methods=["POST"])
@login_required
def authentication_log():
    """Client-side log relay — write a browser-generated event to the audit log."""
    data = request.get_json(silent=True) or {}
    db.add_log(
        data.get("event_type",   "Access Denied"),
        data.get("username",     "Unknown"),
        data.get("access_point", "Camera Station"),
        data.get("status",       "Failed"),
        data.get("details",      ""),
    )
    return jsonify({"success": True})


@app.route("/authentication/recognize", methods=["POST"])
@login_required
def authentication_recognize():
    """
    Face recognition endpoint.

    Request body (JSON):
        user_id      : str   — UUID of the user to verify
        image_data   : str   — base64 data-URL (data:image/jpeg;base64,…)
        access_point : str   — label of the physical entry point

    Response (JSON):
        success    : bool
        match      : bool
        confidence : float   (0–100 %)
        face_count : int
        distance   : float
        username   : str
        full_name  : str
        error      : str | null
    """
    if not frs.is_available():
        return jsonify({
            "success": False,
            "match":   False,
            "error":   "face_recognition library is not installed. Run: pip install face_recognition",
        }), 503

    data         = request.get_json(silent=True) or {}
    user_id      = data.get("user_id")
    image_data   = data.get("image_data", "")
    access_point = data.get("access_point", "Camera Station")

    if not user_id:
        return jsonify({"success": False, "error": "user_id is required"}), 400
    if not image_data:
        return jsonify({"success": False, "error": "image_data is required"}), 400

    user = db.get_user_by_id(user_id)
    if not user:
        return jsonify({"success": False, "error": "User not found"}), 404

    if not user.get("photo_path"):
        return jsonify({
            "success":   False,
            "match":     False,
            "username":  user["username"],
            "full_name": user["full_name"],
            "error": (
                f"No enrollment photo found for '{user['full_name']}'. "
                "Please upload a face photo in User Management first."
            ),
        }), 422

    # Decode the base64 data-URL from the browser canvas
    try:
        b64         = image_data.split(",", 1)[1] if "," in image_data else image_data
        image_bytes = base64.b64decode(b64)
    except Exception as exc:
        return jsonify({"success": False, "error": f"Invalid image data: {exc}"}), 400

    # Run face recognition (blocking; dlib models pre-warmed at startup)
    result = frs.recognize_face(
        image_bytes = image_bytes,
        user_id     = user_id,
        photo_path  = user.get("photo_path"),
        photo_paths = user.get("photos"),
        tolerance   = FACE_TOLERANCE,
    )

    if result["match"]:
        # Step 2 Face Recognized! Send HTML Push Approval Email with Clickable Button
        recipient_email = user.get("email", "")
        # Generate unique token for this specific authentication attempt
        auth_token = uuid.uuid4().hex[:12]

        push_ok, push_msg, masked_email = push_auth_service.send_push_approval_email(
            recipient_email = recipient_email,
            recipient_name  = user.get("full_name") or user.get("username") or "User",
            access_point    = access_point,
            user_id         = str(user_id),
            token           = auth_token,
        )

        db.add_log(
            "Push Auth Sent",
            user["username"],
            access_point,
            "Success" if push_ok else "Warning",
            f"Face recognized ({result['confidence']}%). {push_msg}",
        )

        # Trigger post-auth Lambda notification (match=False so door does not open before email approval)
        aws_lambda_client.trigger_post_auth(
            user_id      = str(user_id),
            username     = user["username"],
            full_name    = user["full_name"],
            match        = False,
            confidence   = result["confidence"],
            access_point = access_point,
            event_type   = "Push Auth Sent",
        )

        return jsonify({
            "success":       True,
            "match":         True,
            "push_sent":     push_ok,
            "user_id":       str(user_id),
            "username":      user["username"],
            "full_name":     user["full_name"],
            "token":         auth_token,
            "masked_email":  masked_email,
            "message":       "Approval email sent. Waiting for user to click link...",
            "cooldown":      30,
            "confidence":    result["confidence"],
            "face_count":    result["face_count"],
            "distance":      result.get("distance"),
            "error":         None,
        })

    # Facial scan failed / no match
    aws_lambda_client.trigger_post_auth(
        user_id      = str(user_id),
        username     = user["username"],
        full_name    = user["full_name"],
        match        = False,
        confidence   = result["confidence"],
        access_point = access_point,
        event_type   = "Access Denied",
    )

    log_details = (
        f"match=False, confidence={result['confidence']}%, "
        f"distance={result.get('distance', 'N/A')}, faces={result['face_count']}"
    )
    if result.get("error"):
        log_details += f"; error={result['error']}"

    db.add_log(
        "Access Denied",
        user["username"],
        access_point,
        "Failed",
        log_details,
    )

    return jsonify({
        "success":    True,
        "match":      False,
        "confidence": result["confidence"],
        "face_count": result["face_count"],
        "distance":   result.get("distance"),
        "username":   user["username"],
        "full_name":  user["full_name"],
        "error":      result.get("error") or "Face not recognized.",
    })


@app.route("/authentication/resend-push", methods=["POST"])
@login_required
def authentication_resend_push():
    """Resend the HTML approval email with the clickable button."""
    data = request.get_json(silent=True) or {}
    user_id = data.get("user_id")
    access_point = data.get("access_point", "Server Room")

    if not user_id:
        return jsonify({"success": False, "error": "user_id is required"}), 400

    user = db.get_user_by_id(user_id)
    if not user:
        return jsonify({"success": False, "error": "User not found"}), 404

    token = data.get("token") or uuid.uuid4().hex[:12]

    push_ok, push_msg, masked_email = push_auth_service.send_push_approval_email(
        recipient_email = user.get("email", ""),
        recipient_name  = user.get("full_name") or user.get("username") or "User",
        access_point    = access_point,
        user_id         = str(user_id),
        token           = token,
    )

    return jsonify({
        "success":      push_ok,
        "message":      push_msg,
        "masked_email": masked_email,
        "token":        token,
    })


@app.route("/auth/approve/<user_id>")
def auth_approve_endpoint(user_id):
    """
    Handle the 'Approve & Unlock Door' button click from the email.
    Publishes JSON {"status": "GRANTED", "name": "<name>"} to AWS IoT Core
    to unlock the physical ESP32 door lock for 5 seconds.
    """
    user = db.get_user_by_id(user_id)
    if not user:
        return """<!DOCTYPE html><html><body style="background:#0b0f19;color:#ef4444;font-family:sans-serif;text-align:center;padding:50px">
        <h2>⚠️ User Not Found or Session Expired</h2></body></html>""", 404

    full_name = user.get("full_name") or user.get("username") or "User"

    # ── UNLOCK PHYSICAL DOOR VIA AWS IOT CORE ──────────────────────────────────
    door_fired = trigger_door_unlock(user_id, full_name=full_name)
    if door_fired:
        logger.info("ESP32 door unlocked via email button approval for '%s'", full_name)

    # Secondary audit log
    db.add_log(
        "Access Granted",
        user["username"],
        "Email Push Button",
        "Success",
        f"Physical door unlocked via email button approval by '{full_name}'",
    )

    # Post-auth Lambda
    aws_lambda_client.trigger_post_auth(
        user_id      = str(user_id),
        username     = user["username"],
        full_name    = full_name,
        match        = True,
        confidence   = 100.0,
        access_point = "Email Push Button",
        event_type   = "Access Granted",
    )

    # Record the approval so the auth page can detect it via polling (if local route used)
    _approval_events[str(user_id)] = {
        'granted_at': datetime.now(timezone.utc).isoformat(),
        'full_name':  full_name,
        'username':   user.get('username', ''),
        'token':      request.args.get('token', '').strip(),
    }

    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Door Access Granted — FaceAuth</title>
  <style>
    body {{
      margin: 0; padding: 0; background: #0b0f19; color: #f8fafc;
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
      display: flex; align-items: center; justify-content: center; min-height: 100vh;
    }}
    .card {{
      background: #131b2e; border: 1px solid #2a3859; border-radius: 20px;
      padding: 40px 30px; max-width: 440px; text-align: center;
      box-shadow: 0 20px 40px rgba(0,0,0,0.6);
    }}
    .icon {{ font-size: 54px; margin-bottom: 12px; }}
    h1 {{ margin: 0 0 10px; font-size: 24px; color: #10b981; }}
    p {{ color: #94a3b8; font-size: 15px; line-height: 1.6; margin: 0 0 24px; }}
    .badge {{
      display: inline-block; background: rgba(16, 185, 129, 0.12);
      color: #10b981; border: 1px solid rgba(16, 185, 129, 0.35);
      padding: 8px 20px; border-radius: 999px; font-weight: 600; font-size: 14px;
    }}
  </style>
</head>
<body>
  <div class="card">
    <div class="icon">🔓</div>
    <h1>Access Approved!</h1>
    <p>Identity verified for <strong>{full_name}</strong>. The physical door lock command has been sent to AWS IoT Core.</p>
    <div class="badge">✓ ESP32 Door Unlocked (5s)</div>
  </div>
</body>
</html>
"""


_consumed_log_ids: set = set()

@app.route("/auth/approval-status/<user_id>", methods=["GET", "POST"])
def auth_approval_status(user_id):
    """
    Polling endpoint: called by the authentication kiosk page every ~2 s
    after sending an approval email to detect when the user clicks the link.

    Checks:
      1. In-memory `_approval_events` (if local /auth/approve/<user_id> was triggered)
      2. DynamoDB `faceauth-logs` table (written by AWS Lambda `FaceAuth-email-unlock`
         when the user taps the button in the email on their phone / PC)
    """
    uid_str = str(user_id)
    req_token = (request.args.get("token") or "").strip()
    user = db.get_user_by_id(user_id)
    user_fullname = (user.get("full_name") or "") if user else ""
    user_username = (user.get("username") or "") if user else ""

    # 1. Check in-memory store
    event = _approval_events.get(uid_str)
    if event:
        # If token was supplied, ensure it matches
        ev_token = event.get("token")
        if not req_token or not ev_token or req_token == ev_token:
            _approval_events.pop(uid_str, None)
            return jsonify({
                "approved": True,
                "full_name": event.get("full_name") or user_fullname or "Authorized User",
                "username": event.get("username") or user_username or "user",
                "granted_at": event.get("granted_at", "")
            })

    # 2. Check DynamoDB faceauth-logs table for recent 'Access Granted' from 'Email Push Button'
    try:
        from boto3.dynamodb.conditions import Attr
        from datetime import datetime, timezone, timedelta
        from aws_config import get_dynamodb, DYNAMO_LOGS_TABLE

        table = get_dynamodb().Table(DYNAMO_LOGS_TABLE)
        # Look back up to 5 minutes
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")

        filter_expr = (
            Attr('access_point').eq('Email Push Button') &
            Attr('event_type').eq('Access Granted') &
            Attr('timestamp').gte(cutoff)
        )

        resp = table.scan(FilterExpression=filter_expr)
        items = resp.get('Items', [])
        while 'LastEvaluatedKey' in resp:
            resp = table.scan(ExclusiveStartKey=resp['LastEvaluatedKey'], FilterExpression=filter_expr)
            items.extend(resp.get('Items', []))

        # Sort newest first
        items.sort(key=lambda x: x.get('timestamp', ''), reverse=True)

        for item in items:
            log_id = item.get('log_id') or item.get('id')
            if log_id in _consumed_log_ids:
                continue

            details = str(item.get('details', ''))
            item_user = str(item.get('username', ''))
            matched = False

            # Strict matching: if token was generated for this session, it MUST match the token in details
            if req_token:
                if f"token={req_token}" in details:
                    matched = True
            else:
                # Fallback only when no token was specified (legacy)
                if f"user_id={uid_str}" in details:
                    matched = True

            if matched:
                if log_id:
                    _consumed_log_ids.add(log_id)
                return jsonify({
                    "approved": True,
                    "full_name": item_user or user_fullname or "Authorized User",
                    "username": user_username or item_user,
                    "granted_at": item.get('timestamp', '')
                })
    except Exception as exc:
        logger.warning("Error checking DynamoDB for approval status: %s", exc)

    return jsonify({"approved": False})



# ===========================================================================
# Routes — Bluetooth Proximity 2FA
# ===========================================================================

@app.route("/bluetooth")
@login_required
def bluetooth():
    return render_template(
        "bluetooth.html",
        active_page="bluetooth",
        operator_id=session.get("operator_id", "User"),
        users=db.get_all_users(),
        rssi_threshold=BT_RSSI_THRESHOLD,
        scan_duration=BT_SCAN_DURATION,
        bleak_available=bt.BLEAK_AVAILABLE,
    )


@app.route("/bluetooth/check", methods=["POST"])
@login_required
def bluetooth_check():
    """
    BLE proximity check for a single user's registered MAC address.

    Request body (JSON): { "user_id": <str> }
    """
    data    = request.get_json(silent=True) or {}
    user_id = data.get("user_id")

    if not user_id:
        return jsonify({"success": False, "error": "user_id required"}), 400

    user = db.get_user_by_id(user_id)
    if not user:
        return jsonify({"success": False, "error": "User not found"}), 404

    mac = user.get("bluetooth_mac")
    if not mac:
        return jsonify({
            "success":   False,
            "found":     False,
            "rssi":      None,
            "username":  user["username"],
            "full_name": user["full_name"],
            "mac":       None,
            "message":   f"No Bluetooth MAC registered for '{user['full_name']}'. Ask admin to set it in Users.",
        }), 422

    scan = bt.scan_for_device(
        mac_address    = mac,
        rssi_threshold = BT_RSSI_THRESHOLD,
        scan_duration  = BT_SCAN_DURATION,
    )

    db.add_log("BT Proximity Check", session.get("operator_id"),
               "Camera Station",
               "Success" if scan["found"] else "Failed",
               f"BT check for '{user['full_name']}': {scan['message']}")

    return jsonify({
        "success":         True,
        "found":           scan["found"],
        "rssi":            scan["rssi"],
        "username":        user["username"],
        "full_name":       user["full_name"],
        "mac":             mac,
        "message":         scan["message"],
        "bleak_available": scan.get("bleak_available", bt.BLEAK_AVAILABLE),
    })


@app.route("/bluetooth/scan-devices", methods=["POST"])
@login_required
def bluetooth_scan_devices():
    """
    Discover ALL nearby BLE-advertising devices.
    Used by the admin UI device-picker.

    Optional body: { "duration": <float> }  (clamped 3–15 s, default 8 s)
    """
    data     = request.get_json(silent=True) or {}
    duration = float(data.get("duration", 8.0))
    duration = max(3.0, min(duration, 15.0))
    return jsonify(bt.scan_nearby_devices(scan_duration=duration))


@app.route("/bluetooth/scan-all", methods=["POST"])
@login_required
def bluetooth_scan_all():
    """Scan all users with registered MACs and return presence results."""
    mac_users = [u for u in db.get_all_users() if u.get("bluetooth_mac")]
    results   = []

    for user in mac_users:
        scan = bt.scan_for_device(
            mac_address    = user["bluetooth_mac"],
            rssi_threshold = BT_RSSI_THRESHOLD,
            scan_duration  = 2.0,   # short scan for bulk check
        )
        results.append({
            "user_id":   user["id"],
            "username":  user["username"],
            "full_name": user["full_name"],
            "mac":       user["bluetooth_mac"],
            "found":     scan["found"],
            "rssi":      scan["rssi"],
            "message":   scan["message"],
        })

    return jsonify({"success": True, "results": results})


# ===========================================================================
# Routes — Access Control
# ===========================================================================

@app.route("/access-control")
@login_required
def access_control():
    return render_template(
        "access_control.html",
        active_page="access_control",
        operator_id=session.get("operator_id", "User"),
        access_points=db.get_all_access_points(),
    )


@app.route("/access-control/add", methods=["POST"])
@login_required
def add_access_point():
    name     = request.form.get("name",     "").strip()
    location = request.form.get("location", "").strip()
    ap_type  = request.form.get("type",     "Facial Recognition").strip()
    status   = request.form.get("status",   "Active").strip()

    if not all([name, location]):
        return jsonify({"success": False,
                        "error": "Name and location are required."}), 400

    db.create_access_point(name, location, ap_type, status)
    db.add_log("Access Point Created", session.get("operator_id"), name,
               "Success", f"Access point '{name}' created")
    return jsonify({"success": True})


@app.route("/access-control/<ap_id>/edit", methods=["POST"])
@login_required
def edit_access_point(ap_id):
    name     = request.form.get("name",     "").strip()
    location = request.form.get("location", "").strip()
    ap_type  = request.form.get("type",     "Facial Recognition").strip()
    status   = request.form.get("status",   "Active").strip()

    db.update_access_point(ap_id, name, location, ap_type, status)
    db.add_log("Access Point Updated", session.get("operator_id"), name,
               "Success", f"Access point ID {ap_id} updated")
    return jsonify({"success": True})


@app.route("/access-control/<ap_id>/delete", methods=["POST"])
@login_required
def delete_access_point(ap_id):
    ap = db.get_access_point_by_id(ap_id)
    if not ap:
        return jsonify({"success": False, "error": "Access point not found."}), 404

    db.delete_access_point(ap_id)
    db.add_log("Access Point Deleted", session.get("operator_id"), ap["name"],
               "Success", f"Access point '{ap['name']}' deleted")
    return jsonify({"success": True})


# ===========================================================================
# Routes — Logs
# ===========================================================================

@app.route("/logs")
@login_required
def logs():
    event_type = request.args.get("event_type", "")
    date_from  = request.args.get("date_from",  "")
    date_to    = request.args.get("date_to",    "")

    log_rows = db.get_logs(
        event_type = event_type or None,
        date_from  = date_from  or None,
        date_to    = date_to    or None,
    )

    event_types = [
        "All Events", "Access Granted", "Access Denied",
        "Login", "Logout", "System Update",
        "User Created", "User Updated", "User Deleted",
        "Access Point Created", "Access Point Updated", "Access Point Deleted",
        "BT Proximity Check", "BT MAC Updated",
    ]

    return render_template(
        "logs.html",
        active_page="logs",
        operator_id=session.get("operator_id", "User"),
        logs=log_rows,
        event_types=event_types,
        selected_event=event_type,
        date_from=date_from,
        date_to=date_to,
    )


# ===========================================================================
# Routes — Settings
# ===========================================================================

@app.route("/settings")
@login_required
def settings():
    username = session.get("operator_id", "admin")
    admin    = db.get_user_by_username(username) or {}

    system_info = {
        "python_version":  platform.python_version(),
        "platform":        platform.system() + " " + platform.release(),
        "flask_env":       os.environ.get("FLASK_ENV", "development"),
        "aws_region":      os.environ.get("AWS_REGION", "us-east-1"),
        "s3_bucket":       os.environ.get("S3_BUCKET_NAME", "—"),
        "dynamo_users":    os.environ.get("DYNAMO_USERS_TABLE", "—"),
        "dynamo_logs":     os.environ.get("DYNAMO_LOGS_TABLE", "—"),
        "dynamo_points":   os.environ.get("DYNAMO_POINTS_TABLE", "—"),
        "iot_endpoint":    os.environ.get("IOT_ENDPOINT", "Not configured") or "Not configured",
        "face_lib":        "Available" if frs.is_available() else "Not installed",
        "bleak_available": bt.BLEAK_AVAILABLE,
    }

    return render_template(
        "settings.html",
        active_page="settings",
        operator_id=username,
        admin=admin,
        bt_rssi=BT_RSSI_THRESHOLD,
        bt_scan=BT_SCAN_DURATION,
        face_tolerance=FACE_TOLERANCE,
        system_info=system_info,
    )


@app.route("/settings/update-thresholds", methods=["POST"])
@login_required
def settings_update_thresholds():
    """Update runtime thresholds for BT proximity and face recognition."""
    global BT_RSSI_THRESHOLD, BT_SCAN_DURATION, FACE_TOLERANCE

    data = request.get_json(silent=True) or {}

    try:
        if "bt_rssi" in data:
            BT_RSSI_THRESHOLD = int(data["bt_rssi"])
        if "bt_scan" in data:
            BT_SCAN_DURATION = float(data["bt_scan"])
        if "face_tolerance" in data:
            val = float(data["face_tolerance"])
            if not (0.3 <= val <= 0.9):
                return jsonify({"success": False, "error": "Face tolerance must be between 0.3 and 0.9"}), 400
            FACE_TOLERANCE = val
    except (ValueError, TypeError) as exc:
        return jsonify({"success": False, "error": str(exc)}), 400

    db.add_log("System Update", session.get("operator_id"), "Settings", "Success",
               f"Thresholds updated: RSSI={BT_RSSI_THRESHOLD}, Scan={BT_SCAN_DURATION}s, FaceTol={FACE_TOLERANCE}")
    return jsonify({
        "success":        True,
        "bt_rssi":        BT_RSSI_THRESHOLD,
        "bt_scan":        BT_SCAN_DURATION,
        "face_tolerance": FACE_TOLERANCE,
    })


@app.route("/settings/change-password", methods=["POST"])
@login_required
def settings_change_password():
    """Securely change the admin's own password."""
    from werkzeug.security import generate_password_hash, check_password_hash

    data         = request.get_json(silent=True) or {}
    current_pw   = data.get("current_password", "").strip()
    new_pw       = data.get("new_password",     "").strip()
    confirm_pw   = data.get("confirm_password", "").strip()

    if not all([current_pw, new_pw, confirm_pw]):
        return jsonify({"success": False, "error": "All password fields are required."}), 400
    if new_pw != confirm_pw:
        return jsonify({"success": False, "error": "New passwords do not match."}), 400
    if len(new_pw) < 8:
        return jsonify({"success": False, "error": "Password must be at least 8 characters."}), 400

    username = session.get("operator_id") or "admin"
    user_id  = session.get("user_id")
    user     = (user_id and db.get_user_by_id(user_id)) or (username and db.get_user_by_username(username))
    if not user:
        return jsonify({"success": False, "error": "User not found."}), 404

    if not check_password_hash(user["password"], current_pw):
        return jsonify({"success": False, "error": "Current password is incorrect."}), 403

    new_hash = generate_password_hash(new_pw)
    # Update password field in DynamoDB
    from aws_config import get_dynamodb, DYNAMO_USERS_TABLE
    table = get_dynamodb().Table(DYNAMO_USERS_TABLE)
    uid = str(user.get("user_id") or user.get("id"))
    table.update_item(
        Key={"user_id": uid},
        UpdateExpression="SET #pw = :pw, updated_at = :ua",
        ExpressionAttributeNames={"#pw": "password"},
        ExpressionAttributeValues={
            ":pw": new_hash,
            ":ua": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        },
    )
    db.clear_users_cache()
    db.add_log("Password Changed", username, "Settings", "Success",
               f"Admin '{username}' changed their password")
    return jsonify({"success": True, "message": "Password updated successfully."})


@app.route("/settings/update-profile", methods=["POST"])
@login_required
def settings_update_profile():
    """Update the admin's own full_name, email, and phone in DynamoDB."""
    data      = request.get_json(silent=True) or {}
    full_name = data.get("full_name", "").strip()
    email     = data.get("email",     "").strip()
    phone     = data.get("phone",     "").strip()

    if not full_name:
        return jsonify({"success": False, "error": "Full name is required."}), 400

    username = session.get("operator_id") or "admin"
    user_id  = session.get("user_id")
    user     = (user_id and db.get_user_by_id(user_id)) or (username and db.get_user_by_username(username))
    if not user:
        return jsonify({"success": False, "error": "User not found."}), 404

    from aws_config import get_dynamodb, DYNAMO_USERS_TABLE
    table = get_dynamodb().Table(DYNAMO_USERS_TABLE)
    uid   = str(user.get("user_id") or user.get("id"))
    table.update_item(
        Key={"user_id": uid},
        UpdateExpression="SET full_name = :fn, email = :em, phone = :ph, updated_at = :ua",
        ExpressionAttributeValues={
            ":fn": full_name,
            ":em": email,
            ":ph": phone,
            ":ua": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        },
    )
    db.clear_users_cache()
    db.add_log("Profile Updated", username, "Settings", "Success",
               f"Admin '{username}' updated their profile")
    return jsonify({"success": True, "message": "Profile updated successfully."})


@app.route("/settings/export-logs")
@login_required
def settings_export_logs():
    """Download all audit logs as a CSV file."""
    logs   = db.get_logs(limit=500)
    output = _io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Timestamp", "Event Type", "Username", "Access Point", "Status", "Details"])
    for row in logs:
        writer.writerow([
            row.get("timestamp", ""),
            row.get("event_type", ""),
            row.get("username", ""),
            row.get("access_point", ""),
            row.get("status", ""),
            row.get("details", ""),
        ])
    csv_data = output.getvalue()
    filename = f"faceauth_audit_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    return Response(
        csv_data,
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.route("/api/settings/status")
@login_required
def api_settings_status():
    """Return live runtime values for settings page polling."""
    return jsonify({
        "success":        True,
        "bt_rssi":        BT_RSSI_THRESHOLD,
        "bt_scan":        BT_SCAN_DURATION,
        "face_tolerance": FACE_TOLERANCE,
        "face_lib":       frs.is_available(),
        "bleak_available": bt.BLEAK_AVAILABLE,
    })


# ===========================================================================
# Routes — Static file serving
# ===========================================================================

@app.route("/uploads/<path:filename>")
@login_required
def uploaded_file(filename):
    """
    Serve enrolled photos.
    In AWS mode the filename is an S3 key (e.g. 'users/bob/photos/x.jpg').
    Falls back to the local uploads folder for development.
    Photos are served with a 7-day browser cache to avoid repeated S3 round-trips.
    """
    cache_headers = {
        "Cache-Control": "private, max-age=604800",   # 7 days
    }
    if os.environ.get("AWS_ACCESS_KEY_ID"):
        try:
            photo_bytes, content_type = aws_s3.get_photo_bytes(filename)
            resp = Response(photo_bytes, mimetype=content_type)
            for k, v in cache_headers.items():
                resp.headers[k] = v
            return resp
        except Exception:
            pass  # fall through to local disk
    resp = send_from_directory(app.config["UPLOAD_FOLDER"], os.path.basename(filename))
    for k, v in cache_headers.items():
        resp.headers[k] = v
    return resp


# ===========================================================================
# Error handlers
# ===========================================================================

@app.errorhandler(404)
def not_found_error(error):
    try:
        return render_template("404.html"), 404
    except Exception:
        return "<h1>404 – Page Not Found</h1>", 404


@app.errorhandler(500)
def internal_error(error):
    try:
        return render_template("500.html"), 500
    except Exception:
        return "<h1>500 – Internal Server Error</h1>", 500


@app.errorhandler(413)
def request_entity_too_large(error):
    return jsonify({"success": False,
                    "error": "File too large. Maximum upload size is 5 MB."}), 413


# ===========================================================================
# Entry point
# ===========================================================================

if __name__ == "__main__":
    host = app.config["FLASK_HOST"]
    port = app.config["FLASK_PORT"]
    env  = os.environ.get("FLASK_ENV", "development")

    # Optional SSL — place cert.pem + key.pem in the project root, or set
    # SSL_CERT / SSL_KEY in .env.  Required for getUserMedia on non-localhost.
    _cert    = os.environ.get("SSL_CERT", "cert.pem")
    _key     = os.environ.get("SSL_KEY",  "key.pem")
    _use_ssl = os.path.exists(_cert) and os.path.exists(_key)
    scheme   = "https" if _use_ssl else "http"

    # Suppress waitress's own "Serving on …" INFO line — we print our own banner.
    logging.getLogger("waitress").setLevel(logging.WARNING)

    # ── Startup banner ────────────────────────────────────────────────────────
    print()
    print("  +------------------------------------------+")
    print("  |       FaceAuth  --  Access Control        |")
    print("  +------------------------------------------+")
    print(f"  |  URL  : {scheme}://{host}:{port:<24}  |")
    print(f"  |  Mode : {env:<34} |")
    if _use_ssl:
        print(f"  |  SSL  : {_cert:<34} |")
    print("  +------------------------------------------+")
    print()
    # ─────────────────────────────────────────────────────────────────────────

    try:
        from waitress import serve  # type: ignore[import-untyped]
        if _use_ssl:
            # waitress has no SSL support — use Flask dev server for HTTPS
            app.run(host=host, port=port, debug=False,
                    ssl_context=(_cert, _key), threaded=True)
        else:
            # channel_timeout: raised above the default 30 s so that slow
            # first-run dlib model loads (up to 90 s) are never killed.
            # threads=8: lets BLE scans and face recognition run concurrently.
            serve(app, host=host, port=port, threads=8, channel_timeout=180)
    except ImportError:
        # waitress not installed — fall back to Flask dev server.
        # threaded=True is required so BLE/recognition don't block all requests.
        ssl_ctx = (_cert, _key) if _use_ssl else None
        app.run(host=host, port=port,
                debug=app.config.get("DEBUG", False),
                ssl_context=ssl_ctx,
                threaded=True)

