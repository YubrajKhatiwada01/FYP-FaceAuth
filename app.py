import os
import uuid
import json
import base64
from functools import wraps

from flask import (Flask, render_template, request, redirect,           # type: ignore[import-untyped]
                   url_for, session, jsonify, send_from_directory)
from flask_wtf.csrf import CSRFProtect, generate_csrf                    # type: ignore[import-untyped]
from werkzeug.security import check_password_hash                        # type: ignore[import-untyped]
from werkzeug.utils import secure_filename                               # type: ignore[import-untyped]
from dotenv import load_dotenv                                           # type: ignore[import-untyped]

import webauthn                                                          # type: ignore[import-untyped]
from webauthn.helpers.structs import (                                   # type: ignore[import-untyped]
    AuthenticatorSelectionCriteria,
    UserVerificationRequirement,
    ResidentKeyRequirement,
    AuthenticatorAttachment,
    PublicKeyCredentialDescriptor,
)
from webauthn.helpers.cose import COSEAlgorithmIdentifier               # type: ignore[import-untyped]

from config import config
import database as db

# ---------------------------------------------------------------------------
# Load environment variables FIRST before anything reads them
# ---------------------------------------------------------------------------

load_dotenv()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}

RP_ID   = os.environ.get("RP_ID",   "localhost")
RP_NAME = os.environ.get("RP_NAME", "FaceAuth")
ORIGIN  = os.environ.get("ORIGIN",  "http://localhost:5000")

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = Flask(__name__)

config_name = os.environ.get('FLASK_ENV', 'development') or 'default'
app.config.from_object(config[config_name])

csrf = CSRFProtect(app)

UPLOAD_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config["UPLOAD_FOLDER"]      = UPLOAD_FOLDER
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024  # 5 MB

with app.app_context():
    db.init_db()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def _b64url_decode(s):
    """Decode a base64url string with correct padding (no over-padding)."""
    s = s.replace("+", "-").replace("/", "_")   # normalise to url-safe alphabet
    padding = (4 - len(s) % 4) % 4
    return base64.urlsafe_b64decode(s + "=" * padding)


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('is_logged_in'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


@app.context_processor
def inject_csrf_token():
    return {'csrf_token': generate_csrf}

# ---------------------------------------------------------------------------
# Public routes
# ---------------------------------------------------------------------------

@app.get("/")
def landing():
    return render_template("landing.html", active_page="home")


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get('is_logged_in'):
        return redirect(url_for('dashboard'))

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
                session['operator_id']  = username
                session['user_id']      = user["id"]
                session['user_role']    = user["role"]
                session['is_logged_in'] = True
                session.permanent       = True
                db.add_log("Login", username, "Dashboard", "Success",
                           f"Operator {username} logged in")
                return redirect(url_for('dashboard'))
        else:
            db.add_log("Login", username or "unknown", "Dashboard", "Failed",
                       "Invalid credentials")
            message = {"type": "error",
                       "text": "Invalid Operator ID or Access Token."}

    return render_template("login.html", active_page="login", message=message,
                           csrf_token=generate_csrf(), body_class="login-body")


@app.route("/logout")
def logout():
    username = session.get('operator_id', 'unknown')
    db.add_log("Logout", username, "Dashboard", "Success",
               f"Operator {username} logged out")
    session.clear()
    return redirect(url_for('login'))

# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@app.route("/dashboard")
@login_required
def dashboard():
    stats = {
        "access_granted": db.count_access_granted(),
        "access_denied":  db.count_access_denied(),
        "active_users":   db.count_active_users(),
        "active_points":  db.count_active_access_points(),
    }
    recent_logs = db.get_recent_logs(limit=5)
    return render_template("dashboard.html", active_page="dashboard",
                           operator_id=session.get('operator_id', 'User'),
                           stats=stats, recent_logs=recent_logs)

# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

@app.route("/users")
@login_required
def users():
    all_users = db.get_all_users()
    return render_template("users.html", active_page="users",
                           operator_id=session.get('operator_id', 'User'),
                           users=all_users)


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

    photo_path = None
    file = request.files.get("photo")
    if file and file.filename and allowed_file(file.filename):
        ext      = secure_filename(file.filename).rsplit(".", 1)[1].lower()
        filename = f"{uuid.uuid4().hex}.{ext}"
        file.save(os.path.join(app.config["UPLOAD_FOLDER"], filename))
        photo_path = filename

    ok, err = db.create_user(full_name, username, password, role, status,
                             email, phone, photo_path)
    if not ok:
        return jsonify({"success": False, "error": err}), 409

    db.add_log("User Created", session.get('operator_id'), "Users", "Success",
               f"User '{username}' created by {session.get('operator_id')}")
    return jsonify({"success": True})


@app.route("/users/<int:user_id>/edit", methods=["POST"])
@login_required
def edit_user(user_id):
    full_name = request.form.get("full_name", "").strip()
    role      = request.form.get("role",      "Operator").strip()
    status    = request.form.get("status",    "Active").strip()
    email     = request.form.get("email",     "").strip()
    phone     = request.form.get("phone",     "").strip()

    if not full_name:
        return jsonify({"success": False,
                        "error": "Full name is required."}), 400

    # Ensure the user actually exists before updating
    existing = db.get_user_by_id(user_id)
    if not existing:
        return jsonify({"success": False, "error": "User not found."}), 404

    db.update_user(user_id, full_name, role, status, email, phone)

    file = request.files.get("photo")
    if file and file.filename and allowed_file(file.filename):
        if existing and existing["photo_path"]:
            old = os.path.join(app.config["UPLOAD_FOLDER"], existing["photo_path"])
            if os.path.exists(old):
                os.remove(old)
        ext      = secure_filename(file.filename).rsplit(".", 1)[1].lower()
        filename = f"{uuid.uuid4().hex}.{ext}"
        file.save(os.path.join(app.config["UPLOAD_FOLDER"], filename))
        db.update_user_photo(user_id, filename)

    db.add_log("User Updated", session.get('operator_id'), "Users", "Success",
               f"User ID {user_id} updated by {session.get('operator_id')}")
    return jsonify({"success": True})


@app.route("/users/<int:user_id>/delete", methods=["POST"])
@login_required
def delete_user(user_id):
    user = db.get_user_by_id(user_id)
    if not user:
        return jsonify({"success": False, "error": "User not found."}), 404

    if user["photo_path"]:
        photo_file = os.path.join(app.config["UPLOAD_FOLDER"], user["photo_path"])
        if os.path.exists(photo_file):
            os.remove(photo_file)

    db.delete_user(user_id)
    db.add_log("User Deleted", session.get('operator_id'), "Users", "Success",
               f"User '{user['username']}' deleted by {session.get('operator_id')}")
    return jsonify({"success": True})

# ---------------------------------------------------------------------------
# Face Authentication
# ---------------------------------------------------------------------------

@app.route("/authentication")
@login_required
def authentication():
    all_users = db.get_all_users()
    return render_template("authentication.html", active_page="authentication",
                           operator_id=session.get('operator_id', 'User'),
                           users=all_users)


@app.route("/authentication/log", methods=["POST"])
@login_required
def authentication_log():
    data         = request.get_json(silent=True) or {}
    event_type   = data.get("event_type",   "Access Denied")
    username     = data.get("username",     "Unknown")
    access_point = data.get("access_point", "Camera Station")
    status       = data.get("status",       "Failed")
    details      = data.get("details",      "")

    db.add_log(event_type, username, access_point, status, details)
    return jsonify({"success": True})

# ---------------------------------------------------------------------------
# Fingerprint / WebAuthn
# ---------------------------------------------------------------------------

@app.route("/fingerprint")
@login_required
def fingerprint():
    all_users   = db.get_all_users()
    credentials = db.get_all_webauthn_credentials()
    enrolled    = db.count_webauthn_enrolled()
    return render_template(
        "fingerprint.html",
        active_page="fingerprint",
        operator_id=session.get('operator_id', 'User'),
        all_users=all_users,
        credentials=credentials,
        enrolled_count=enrolled,
    )


@app.route("/fingerprint/register/begin", methods=["POST"])
@login_required
def fp_register_begin():
    data    = request.get_json(silent=True) or {}
    user_id = data.get("user_id")
    if not user_id:
        return jsonify({"error": "user_id required"}), 400

    user = db.get_user_by_id(int(user_id))
    if not user:
        return jsonify({"error": "User not found"}), 404

    existing = db.get_webauthn_credentials_for_user(user["id"])
    exclude_credentials = [
        PublicKeyCredentialDescriptor(id=_b64url_decode(c["credential_id"]))
        for c in existing
    ]

    options = webauthn.generate_registration_options(
        rp_id=RP_ID,
        rp_name=RP_NAME,
        user_id=str(user["id"]).encode(),
        user_name=user["username"],
        user_display_name=user["full_name"],
        authenticator_selection=AuthenticatorSelectionCriteria(
            authenticator_attachment=AuthenticatorAttachment.PLATFORM,
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
        supported_pub_key_algs=[
            COSEAlgorithmIdentifier.ECDSA_SHA_256,
            COSEAlgorithmIdentifier.RSASSA_PKCS1_v1_5_SHA_256,
        ],
        exclude_credentials=exclude_credentials,
    )

    session["fp_reg_challenge"] = base64.b64encode(options.challenge).decode()
    session["fp_reg_user_id"]   = user["id"]

    return jsonify(webauthn.options_to_json(options))


@app.route("/fingerprint/register/complete", methods=["POST"])
@login_required
def fp_register_complete():
    data      = request.get_json(silent=True) or {}
    challenge = session.get("fp_reg_challenge")
    user_id   = session.get("fp_reg_user_id")

    if not challenge or not user_id:
        return jsonify({"success": False, "error": "No pending registration."}), 400

    device_name = data.pop("device_name", "Fingerprint Sensor")

    try:
        credential = webauthn.verify_registration_response(
            credential=data,
            expected_challenge=base64.b64decode(challenge),
            expected_rp_id=RP_ID,
            expected_origin=ORIGIN,
            require_user_verification=True,
        )
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 400

    cred_id    = base64.urlsafe_b64encode(credential.credential_id).rstrip(b"=").decode()
    pub_key    = credential.credential_public_key.hex()
    transports = json.dumps(data.get("response", {}).get("transports", []))

    ok = db.save_webauthn_credential(user_id, cred_id, pub_key, device_name, transports)
    if not ok:
        return jsonify({"success": False, "error": "Credential already registered."}), 409

    user = db.get_user_by_id(user_id)
    db.add_log("Fingerprint Enrolled", session.get('operator_id'), "Fingerprint",
               "Success", f"Credential registered for user '{user['username']}'")

    session.pop("fp_reg_challenge", None)
    session.pop("fp_reg_user_id",   None)
    return jsonify({"success": True})


@app.route("/fingerprint/auth/begin", methods=["POST"])
@login_required
def fp_auth_begin():
    data    = request.get_json(silent=True) or {}
    user_id = data.get("user_id")

    allow_credentials = []
    if user_id:
        creds = db.get_webauthn_credentials_for_user(int(user_id))
        allow_credentials = [
            PublicKeyCredentialDescriptor(
                id=_b64url_decode(c["credential_id"])
            )
            for c in creds
        ]
        if not allow_credentials:
            return jsonify({"error": "No fingerprint enrolled for this user."}), 404

    options = webauthn.generate_authentication_options(
        rp_id=RP_ID,
        allow_credentials=allow_credentials,
        user_verification=UserVerificationRequirement.REQUIRED,
    )

    session["fp_auth_challenge"] = base64.b64encode(options.challenge).decode()
    return jsonify(webauthn.options_to_json(options))


@app.route("/fingerprint/auth/complete", methods=["POST"])
@login_required
def fp_auth_complete():
    data      = request.get_json(silent=True) or {}
    challenge = session.get("fp_auth_challenge")
    if not challenge:
        return jsonify({"success": False, "error": "No pending challenge."}), 400

    raw_id   = data.get("rawId", "")
    cred_row = db.get_webauthn_credential(raw_id)

    if not cred_row:
        cred_row = db.get_webauthn_credential(raw_id.replace("+", "-").replace("/", "_"))

    if not cred_row:
        return jsonify({"success": False, "error": "Unknown credential."}), 400

    try:
        verification = webauthn.verify_authentication_response(
            credential=data,
            expected_challenge=base64.b64decode(challenge),
            expected_rp_id=RP_ID,
            expected_origin=ORIGIN,
            credential_public_key=bytes.fromhex(cred_row["public_key"]),
            credential_current_sign_count=cred_row["sign_count"],
            require_user_verification=True,
        )
    except Exception as exc:
        db.add_log("Fingerprint Auth", session.get('operator_id'), "Fingerprint",
                   "Failed", str(exc))
        return jsonify({"success": False, "error": str(exc)}), 400

    db.update_webauthn_sign_count(cred_row["credential_id"], verification.new_sign_count)

    user     = db.get_user_by_id(cred_row["user_id"])
    username = user["username"] if user else "unknown"

    if user and user["status"] != "Active":
        return jsonify({"success": False, "error": "Account is inactive."}), 403

    db.add_log("Fingerprint Auth", session.get('operator_id'), "Fingerprint",
               "Success", f"Fingerprint verified for '{username}'")

    session.pop("fp_auth_challenge", None)
    return jsonify({"success": True, "username": username,
                    "full_name": user["full_name"] if user else username})


@app.route("/fingerprint/credential/<cred_id>/delete", methods=["POST"])
@login_required
def fp_delete_credential(cred_id):
    cred = db.get_webauthn_credential(cred_id)
    if not cred:
        return jsonify({"success": False, "error": "Not found."}), 404
    db.delete_webauthn_credential(cred_id)
    db.add_log("Fingerprint Removed", session.get('operator_id'), "Fingerprint",
               "Success", f"Credential {cred_id[:16]}… deleted")
    return jsonify({"success": True})

# ---------------------------------------------------------------------------
# Access Control
# ---------------------------------------------------------------------------

@app.route("/access-control")
@login_required
def access_control():
    points = db.get_all_access_points()
    return render_template("access_control.html", active_page="access_control",
                           operator_id=session.get('operator_id', 'User'),
                           access_points=points)


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
    db.add_log("Access Point Created", session.get('operator_id'), name,
               "Success", f"Access point '{name}' created")
    return jsonify({"success": True})


@app.route("/access-control/<int:ap_id>/edit", methods=["POST"])
@login_required
def edit_access_point(ap_id):
    name     = request.form.get("name",     "").strip()
    location = request.form.get("location", "").strip()
    ap_type  = request.form.get("type",     "Facial Recognition").strip()
    status   = request.form.get("status",   "Active").strip()

    db.update_access_point(ap_id, name, location, ap_type, status)
    db.add_log("Access Point Updated", session.get('operator_id'), name,
               "Success", f"Access point ID {ap_id} updated")
    return jsonify({"success": True})


@app.route("/access-control/<int:ap_id>/delete", methods=["POST"])
@login_required
def delete_access_point(ap_id):
    ap = db.get_access_point_by_id(ap_id)
    if not ap:
        return jsonify({"success": False, "error": "Access point not found."}), 404

    db.delete_access_point(ap_id)
    db.add_log("Access Point Deleted", session.get('operator_id'), ap["name"],
               "Success", f"Access point '{ap['name']}' deleted")
    return jsonify({"success": True})

# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------

@app.route("/logs")
@login_required
def logs():
    event_type = request.args.get("event_type", "")
    date_from  = request.args.get("date_from",  "")
    date_to    = request.args.get("date_to",    "")

    log_rows = db.get_logs(
        event_type=event_type or None,
        date_from=date_from   or None,
        date_to=date_to       or None,
    )

    event_types = [
        "All Events", "Access Granted", "Access Denied",
        "Login", "Logout", "System Update",
        "User Created", "User Updated", "User Deleted",
        "Access Point Created", "Access Point Updated", "Access Point Deleted",
    ]

    return render_template("logs.html", active_page="logs",
                           operator_id=session.get('operator_id', 'User'),
                           logs=log_rows, event_types=event_types,
                           selected_event=event_type,
                           date_from=date_from, date_to=date_to)

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

@app.route("/settings")
@login_required
def settings():
    return render_template("settings.html", active_page="settings",
                           operator_id=session.get('operator_id', 'User'))

# ---------------------------------------------------------------------------
# File serving
# ---------------------------------------------------------------------------

@app.route("/uploads/<path:filename>")
@login_required
def uploaded_file(filename):
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename)

# ---------------------------------------------------------------------------
# Error handlers
# ---------------------------------------------------------------------------

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

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    try:
        from waitress import serve
        host = app.config['FLASK_HOST']
        port = app.config['FLASK_PORT']
        print(f"Starting FaceAuth on http://{host}:{port}")
        serve(app, host=host, port=port)
    except ImportError:
        # Fallback to Flask dev server if waitress is not installed
        app.run(
            host=app.config.get('FLASK_HOST', '127.0.0.1'),
            port=app.config.get('FLASK_PORT', 5000),
            debug=app.config.get('DEBUG', False),
        )
