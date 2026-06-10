from flask import (Flask, render_template, request, redirect,
                   url_for, session, jsonify, send_from_directory)
from flask_wtf.csrf import CSRFProtect
from werkzeug.security import check_password_hash
from werkzeug.utils import secure_filename
from dotenv import load_dotenv
import os
import uuid
from config import config
import database as db

# ---------------------------------------------------------------------------
# Allowed extensions for profile photo uploads
# ---------------------------------------------------------------------------
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------
load_dotenv()

app = Flask(__name__)

config_name = os.environ.get('FLASK_ENV', 'development')
app.config.from_object(config[config_name])

csrf = CSRFProtect(app)

UPLOAD_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config["UPLOAD_FOLDER"]       = UPLOAD_FOLDER
app.config["MAX_CONTENT_LENGTH"]  = 5 * 1024 * 1024   # 5 MB

with app.app_context():
    db.init_db()

# ---------------------------------------------------------------------------
# Template helpers
# ---------------------------------------------------------------------------

@app.context_processor
def inject_csrf_token():
    from flask_wtf.csrf import generate_csrf
    return {'csrf_token': generate_csrf}


def login_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('is_logged_in'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

# ---------------------------------------------------------------------------
# Public
# ---------------------------------------------------------------------------

@app.get("/")
def landing():
    return render_template("landing.html", active_page="home")


@app.route("/login", methods=["GET", "POST"])
def login():
    from flask_wtf.csrf import generate_csrf

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
    username  = request.form.get("username", "").strip()
    password  = request.form.get("password", "").strip()
    role      = request.form.get("role", "Operator").strip()
    status    = request.form.get("status", "Active").strip()
    email     = request.form.get("email", "").strip()
    phone     = request.form.get("phone", "").strip()

    if not all([full_name, username, password]):
        return jsonify({"success": False,
                        "error": "Full name, username and password are required."}), 400

    photo_path = None
    file = request.files.get("photo")
    if file and file.filename and allowed_file(file.filename):
        ext        = secure_filename(file.filename).rsplit(".", 1)[1].lower()
        filename   = f"{uuid.uuid4().hex}.{ext}"
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
    role      = request.form.get("role", "Operator").strip()
    status    = request.form.get("status", "Active").strip()
    email     = request.form.get("email", "").strip()
    phone     = request.form.get("phone", "").strip()

    db.update_user(user_id, full_name, role, status, email, phone)

    file = request.files.get("photo")
    if file and file.filename and allowed_file(file.filename):
        existing = db.get_user_by_id(user_id)
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
    """Live camera-based facial authentication page."""
    all_users = db.get_all_users()
    return render_template("authentication.html", active_page="authentication",
                           operator_id=session.get('operator_id', 'User'),
                           users=all_users)


@app.route("/authentication/log", methods=["POST"])
@login_required
def authentication_log():
    """Receive a face-auth event from the browser and write it to the logs table."""
    data         = request.get_json(silent=True) or {}
    event_type   = data.get("event_type",   "Access Denied")
    username     = data.get("username",     "Unknown")
    access_point = data.get("access_point", "Camera Station")
    status       = data.get("status",       "Failed")
    details      = data.get("details",      "")

    db.add_log(event_type, username, access_point, status, details)
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
    name     = request.form.get("name", "").strip()
    location = request.form.get("location", "").strip()
    ap_type  = request.form.get("type", "Facial Recognition").strip()
    status   = request.form.get("status", "Active").strip()

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
    name     = request.form.get("name", "").strip()
    location = request.form.get("location", "").strip()
    ap_type  = request.form.get("type", "Facial Recognition").strip()
    status   = request.form.get("status", "Active").strip()

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
# Serve uploaded files
# ---------------------------------------------------------------------------

@app.route("/uploads/<path:filename>")
@login_required
def uploaded_file(filename):
    """Serve user profile photos from the uploads folder."""
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename)

# ---------------------------------------------------------------------------
# Error handlers
# ---------------------------------------------------------------------------

@app.errorhandler(404)
def not_found_error(error):
    return render_template("404.html"), 404


@app.errorhandler(500)
def internal_error(error):
    return render_template("500.html"), 500

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    host = app.config['FLASK_HOST']
    port = app.config['FLASK_PORT']
    print(f"🚀 Starting FaceAuth on http://{host}:{port}")
    print(f"📋 Environment: {config_name.upper()}\n")
    from waitress import serve
    serve(app, host=host, port=port)
