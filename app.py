"""
app.py — FaceAuth Flask Application
=====================================
Two-factor physical access control:
  Step 1 — Bluetooth proximity check (BLE scan via bleak)
  Step 2 — Live facial recognition (dlib via face_recognition)

Backend: AWS DynamoDB (users/logs/access-points) + S3 (photos) + Lambda (post-auth trigger)
"""

import base64
import logging
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
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

# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

load_dotenv()  # must be first — reads .env before any os.environ.get() calls

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

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
        import face_recognition as _fr  # type: ignore[import-untyped]
        import numpy as _np
        dummy = _np.zeros((100, 100, 3), dtype=_np.uint8)
        _fr.face_locations(dummy)
        _fr.face_encodings(dummy)
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
    """Decorator — redirects unauthenticated users to the login page."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("is_logged_in"):
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
        ext    = secure_filename(f.filename).rsplit(".", 1)[1].lower()
        s3_key = f"users/{username}/photos/sample_{idx}_{uuid.uuid4().hex[:6]}.{ext}"
        payloads.append((f.read(), s3_key))

    def _upload(item):
        data, key = item
        return aws_s3.upload_photo_bytes(data, key)

    with ThreadPoolExecutor(max_workers=len(payloads)) as pool:
        return list(pool.map(_upload, payloads))


# ===========================================================================
# Routes — Public
# ===========================================================================

@app.get("/")
def landing():
    return render_template("landing.html", active_page="home")


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("is_logged_in"):
        return redirect(url_for("dashboard"))

    message = None

    if request.method == "POST":
        username = request.form.get("operator_id", "").strip()
        password = request.form.get("access_token", "").strip()
        user     = db.get_user_by_username(username)

        if user and check_password_hash(user["password"], password):
            if user["status"] != "Active":
                message = {"type": "error",
                           "text": "Account is inactive. Contact your administrator."}
            else:
                session["operator_id"]  = username
                session["user_id"]      = user["id"]
                session["user_role"]    = user["role"]
                session["is_logged_in"] = True
                session.permanent       = True
                db.add_log("Login", username, "Dashboard", "Success",
                           f"Operator {username} logged in")
                return redirect(url_for("dashboard"))
        else:
            db.add_log("Login", username or "unknown", "Dashboard", "Failed",
                       "Invalid credentials")
            message = {"type": "error",
                       "text": "Invalid Operator ID or Access Token."}

    return render_template("login.html", active_page="login", message=message,
                           body_class="login-body")


@app.route("/logout")
def logout():
    username = session.get("operator_id", "unknown")
    db.add_log("Logout", username, "Dashboard", "Success",
               f"Operator {username} logged out")
    session.clear()
    return redirect(url_for("login"))


# ===========================================================================
# Routes — Dashboard
# ===========================================================================

@app.route("/dashboard")
@login_required
def dashboard():
    with ThreadPoolExecutor(max_workers=5) as pool:
        f_granted = pool.submit(db.count_access_granted)
        f_denied  = pool.submit(db.count_access_denied)
        f_users   = pool.submit(db.count_active_users)
        f_points  = pool.submit(db.count_active_access_points)
        f_logs    = pool.submit(db.get_recent_logs, limit=5)

        stats = {
            "access_granted": f_granted.result(),
            "access_denied":  f_denied.result(),
            "active_users":   f_users.result(),
            "active_points":  f_points.result(),
        }
        recent_logs = f_logs.result()

    return render_template(
        "dashboard.html",
        active_page="dashboard",
        operator_id=session.get("operator_id", "User"),
        stats=stats,
        recent_logs=recent_logs,
    )


# ===========================================================================
# Routes — Users
# ===========================================================================

@app.route("/users")
@login_required
def users():
    return render_template(
        "users.html",
        active_page="users",
        operator_id=session.get("operator_id", "User"),
        users=db.get_all_users(),
    )


@app.route("/users/add", methods=["POST"])
@login_required
def add_user():
    full_name = request.form.get("full_name", "").strip()
    username  = request.form.get("username",  "").strip()
    password  = request.form.get("password",  "").strip()
    role      = request.form.get("role",      "Operator").strip()
    status    = request.form.get("status",    "Active").strip()
    email     = request.form.get("email",     "").strip()
    phone     = request.form.get("phone",     "").strip()

    if not all([full_name, username, password]):
        return jsonify({"success": False,
                        "error": "Full name, username and password are required."}), 400

    files = (
        [request.files.get("photo")]
        + request.files.getlist("samples")
        + request.files.getlist("samples[]")
    )
    s3_keys     = _upload_photos_to_s3(files, username)
    primary_key = s3_keys[0] if s3_keys else None

    ok, err = db.create_user(full_name, username, password, role, status,
                             email, phone, primary_key, s3_keys)
    if not ok:
        return jsonify({"success": False, "error": err}), 409

    db.add_log("User Created", session.get("operator_id"), "Users", "Success",
               f"User '{username}' created with {len(s3_keys)} photo(s) in S3")
    return jsonify({"success": True})


@app.route("/users/<user_id>/edit", methods=["POST"])
@login_required
def edit_user(user_id):
    full_name = request.form.get("full_name", "").strip()
    role      = request.form.get("role",      "Operator").strip()
    status    = request.form.get("status",    "Active").strip()
    email     = request.form.get("email",     "").strip()
    phone     = request.form.get("phone",     "").strip()

    if not full_name:
        return jsonify({"success": False, "error": "Full name is required."}), 400

    existing = db.get_user_by_id(user_id)
    if not existing:
        return jsonify({"success": False, "error": "User not found."}), 404

    db.update_user(user_id, full_name, role, status, email, phone)

    # Re-upload photos only when new files are submitted
    main_file    = request.files.get("photo")
    sample_files = request.files.getlist("samples") + request.files.getlist("samples[]")
    new_files    = [main_file] + sample_files
    has_new      = any(f and f.filename for f in new_files)

    if has_new:
        # Delete old S3 photos first
        for old_key in (existing.get("photos") or []):
            aws_s3.delete_photo(old_key)
        if not existing.get("photos") and existing.get("photo_path"):
            aws_s3.delete_photo(existing["photo_path"])

        username    = existing.get("username", "unknown")
        s3_keys     = _upload_photos_to_s3(new_files, username)
        primary_key = s3_keys[0] if s3_keys else None
        db.update_user_photo(user_id, primary_key)
        db.update_user_photos(user_id, s3_keys)

        # Invalidate cached face encodings so next auth uses fresh photos
        frs.clear_cache(user_id)

    db.add_log("User Updated", session.get("operator_id"), "Users", "Success",
               f"User ID {user_id} updated by {session.get('operator_id')}")
    return jsonify({"success": True})


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
        tolerance   = 0.55,
    )

    # Fire the post-auth Lambda trigger (fire-and-forget, never blocks)
    aws_lambda_client.trigger_post_auth(
        user_id      = str(user_id),
        username     = user["username"],
        full_name    = user["full_name"],
        match        = result["match"],
        confidence   = result["confidence"],
        access_point = access_point,
        event_type   = "Access Granted" if result["match"] else "Access Denied",
    )

    # Write the audit log
    log_details = (
        f"match={result['match']}, confidence={result['confidence']}%, "
        f"distance={result.get('distance', 'N/A')}, faces={result['face_count']}"
    )
    if result.get("error"):
        log_details += f"; error={result['error']}"

    db.add_log(
        "Access Granted" if result["match"] else "Access Denied",
        user["username"],
        access_point,
        "Success" if result["match"] else "Failed",
        log_details,
    )

    return jsonify({
        "success":    True,
        "match":      result["match"],
        "confidence": result["confidence"],
        "face_count": result["face_count"],
        "distance":   result.get("distance"),
        "username":   user["username"],
        "full_name":  user["full_name"],
        "error":      result.get("error"),
    })


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
    return render_template(
        "settings.html",
        active_page="settings",
        operator_id=session.get("operator_id", "User"),
    )


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
    """
    if os.environ.get("AWS_ACCESS_KEY_ID"):
        try:
            photo_bytes, content_type = aws_s3.get_photo_bytes(filename)
            return Response(photo_bytes, mimetype=content_type)
        except Exception:
            pass  # fall through to local disk
    return send_from_directory(app.config["UPLOAD_FOLDER"], os.path.basename(filename))


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

