"""
database.py — SQLite database layer for FaceAuth.

Tables
------
users               : system operator accounts (login credentials + profile photo)
logs                : immutable audit trail for all system events
access_points       : physical / logical entry points managed by the system
webauthn_credentials: WebAuthn public-key credentials (fingerprint / Windows Hello)
"""

import sqlite3
import os
from werkzeug.security import generate_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH  = os.path.join(BASE_DIR, "faceauth.db")


# ---------------------------------------------------------------------------
# Connection helper
# ---------------------------------------------------------------------------

def get_db():
    """Return a new SQLite connection with Row factory enabled."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# ---------------------------------------------------------------------------
# Schema creation + migrations
# ---------------------------------------------------------------------------

def init_db():
    """Create tables, run any missing column migrations, then seed demo data."""
    conn = get_db()
    cur  = conn.cursor()

    # -- users ----------------------------------------------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            full_name   TEXT    NOT NULL,
            username    TEXT    NOT NULL UNIQUE,
            password    TEXT    NOT NULL,
            role        TEXT    NOT NULL DEFAULT 'Operator',
            status      TEXT    NOT NULL DEFAULT 'Active',
            email       TEXT,
            phone       TEXT,
            photo_path  TEXT,
            created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
            updated_at  TEXT    NOT NULL DEFAULT (datetime('now'))
        )
    """)

    # -- logs -----------------------------------------------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS logs (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp    TEXT    NOT NULL DEFAULT (datetime('now')),
            event_type   TEXT    NOT NULL,
            username     TEXT,
            access_point TEXT,
            status       TEXT    NOT NULL,
            details      TEXT
        )
    """)

    # -- access_points --------------------------------------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS access_points (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            name          TEXT    NOT NULL,
            location      TEXT    NOT NULL,
            type          TEXT    NOT NULL DEFAULT 'Facial Recognition',
            status        TEXT    NOT NULL DEFAULT 'Active',
            entries_today INTEGER NOT NULL DEFAULT 0,
            success_rate  REAL    NOT NULL DEFAULT 100.0,
            last_used     TEXT,
            created_at    TEXT    NOT NULL DEFAULT (datetime('now'))
        )
    """)

    # -- webauthn_credentials -------------------------------------------------
    # Stores one or more FIDO2/WebAuthn public-key credentials per user.
    # credential_id   : base64url-encoded credential ID returned by the authenticator
    # public_key      : CBOR-encoded COSE public key (stored as hex)
    # sign_count      : monotonic counter — updated on every successful assertion
    # transports      : JSON array of transport hints ("internal", "usb", etc.)
    # registered_at   : when the credential was created
    cur.execute("""
        CREATE TABLE IF NOT EXISTS webauthn_credentials (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            credential_id   TEXT    NOT NULL UNIQUE,
            public_key      TEXT    NOT NULL,
            sign_count      INTEGER NOT NULL DEFAULT 0,
            device_name     TEXT    NOT NULL DEFAULT 'Fingerprint Sensor',
            transports      TEXT,
            registered_at   TEXT    NOT NULL DEFAULT (datetime('now'))
        )
    """)

    conn.commit()

    # Run migrations on existing DB (add missing columns safely)
    _migrate(conn)
    _seed_demo_data(conn)
    conn.close()


def _migrate(conn):
    """Add columns that may not exist in databases created before this version."""
    cur = conn.cursor()
    existing = {row[1] for row in cur.execute("PRAGMA table_info(users)").fetchall()}

    if "phone" not in existing:
        cur.execute("ALTER TABLE users ADD COLUMN phone TEXT")
    if "photo_path" not in existing:
        cur.execute("ALTER TABLE users ADD COLUMN photo_path TEXT")

    conn.commit()


def _seed_demo_data(conn):
    """Ensure demo accounts always exist (INSERT OR IGNORE — never overwrites real data)."""
    cur = conn.cursor()

    # Use INSERT OR IGNORE so existing rows (including user-modified ones) are left alone,
    # but missing rows (e.g. accidentally deleted admin) are always re-created.
    demo_users = [
        ("John Doe",        "admin",   generate_password_hash("admin123"),  "Administrator", "Active",   "admin@faceauth.io",   "+601112345678"),
        ("Sarah Smith",     "sarah",   generate_password_hash("sarah123"),  "Operator",      "Active",   "sarah@faceauth.io",   "+601122345678"),
        ("Michael Johnson", "michael", generate_password_hash("michael123"),"Viewer",        "Active",   "michael@faceauth.io", "+601132345678"),
        ("Emily Williams",  "emily",   generate_password_hash("emily123"),  "Operator",      "Active",   "emily@faceauth.io",   "+601142345678"),
    ]
    cur.executemany(
        """INSERT OR IGNORE INTO users (full_name, username, password, role, status, email, phone)
           VALUES (?,?,?,?,?,?,?)""",
        demo_users,
    )

    cur.execute("SELECT COUNT(*) FROM access_points")
    if cur.fetchone()[0] == 0:
        demo_points = [
            ("Main Entry",      "Building A, Floor 1",  "Facial Recognition", "Active",      42,  98.5, "2 min ago"),
            ("Conference Room", "Building A, Floor 3",  "Facial Recognition", "Active",      28,  99.2, "15 min ago"),
            ("Server Room",     "Building B, Floor 2",  "Facial Recognition", "Maintenance",  5, 100.0, "1 hour ago"),
            ("Parking Garage",  "Building C, Level B2", "Facial Recognition", "Active",     156,  97.8, "30 sec ago"),
        ]
        cur.executemany(
            """INSERT INTO access_points
               (name, location, type, status, entries_today, success_rate, last_used)
               VALUES (?,?,?,?,?,?,?)""",
            demo_points,
        )

    cur.execute("SELECT COUNT(*) FROM logs")
    if cur.fetchone()[0] == 0:
        demo_logs = [
            ("2026-05-31 16:42:15", "Access Granted", "John Doe",       "Main Entry",      "Success",   "Facial match confidence 98.9%"),
            ("2026-05-31 16:41:32", "Access Denied",  "Unknown",         "Server Room",     "Failed",    "No matching face found"),
            ("2026-05-31 16:40:48", "Access Granted", "Sarah Smith",     "Conference Room", "Success",   "Facial match confidence 99.1%"),
            ("2026-05-31 16:39:12", "System Update",  "System",          "N/A",             "Completed", "Facial recognition model v3.2 deployed"),
            ("2026-05-31 16:38:55", "Access Granted", "Michael Johnson", "Parking Garage",  "Success",   "Facial match confidence 97.4%"),
        ]
        cur.executemany(
            "INSERT INTO logs (timestamp, event_type, username, access_point, status, details) VALUES (?,?,?,?,?,?)",
            demo_logs,
        )

    conn.commit()


# ---------------------------------------------------------------------------
# User queries
# ---------------------------------------------------------------------------

def get_all_users():
    conn = get_db()
    rows = conn.execute("SELECT * FROM users ORDER BY id").fetchall()
    conn.close()
    return rows


def get_user_by_id(user_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    return row


def get_user_by_username(username):
    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    conn.close()
    return row


def create_user(full_name, username, password_plain, role, status, email, phone, photo_path=None):
    conn = get_db()
    try:
        conn.execute(
            """INSERT INTO users (full_name, username, password, role, status, email, phone, photo_path)
               VALUES (?,?,?,?,?,?,?,?)""",
            (full_name, username, generate_password_hash(password_plain),
             role, status, email, phone, photo_path),
        )
        conn.commit()
        return True, None
    except sqlite3.IntegrityError:
        return False, "Username already exists."
    finally:
        conn.close()


def update_user(user_id, full_name, role, status, email, phone):
    conn = get_db()
    conn.execute(
        """UPDATE users
           SET full_name=?, role=?, status=?, email=?, phone=?,
               updated_at=datetime('now')
           WHERE id=?""",
        (full_name, role, status, email, phone, user_id),
    )
    conn.commit()
    conn.close()


def update_user_photo(user_id, photo_path):
    """Update only the photo_path for a user."""
    conn = get_db()
    conn.execute(
        "UPDATE users SET photo_path=?, updated_at=datetime('now') WHERE id=?",
        (photo_path, user_id),
    )
    conn.commit()
    conn.close()


def delete_user(user_id):
    conn = get_db()
    conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()


def count_active_users():
    conn = get_db()
    count = conn.execute(
        "SELECT COUNT(*) FROM users WHERE status='Active'"
    ).fetchone()[0]
    conn.close()
    return count


# ---------------------------------------------------------------------------
# Log queries
# ---------------------------------------------------------------------------

def add_log(event_type, username, access_point, status, details=""):
    conn = get_db()
    conn.execute(
        """INSERT INTO logs (timestamp, event_type, username, access_point, status, details)
           VALUES (datetime('now'), ?,?,?,?,?)""",
        (event_type, username, access_point, status, details),
    )
    conn.commit()
    conn.close()


def get_logs(event_type=None, date_from=None, date_to=None, limit=200):
    conn   = get_db()
    query  = "SELECT * FROM logs WHERE 1=1"
    params = []

    if event_type and event_type != "All Events":
        query += " AND event_type = ?"
        params.append(event_type)
    if date_from:
        query += " AND DATE(timestamp) >= ?"
        params.append(date_from)
    if date_to:
        query += " AND DATE(timestamp) <= ?"
        params.append(date_to)

    query += " ORDER BY timestamp DESC LIMIT ?"
    params.append(limit)

    rows = conn.execute(query, params).fetchall()
    conn.close()
    return rows


def count_access_granted():
    conn  = get_db()
    count = conn.execute(
        "SELECT COUNT(*) FROM logs WHERE event_type='Access Granted'"
    ).fetchone()[0]
    conn.close()
    return count


def count_access_denied():
    conn  = get_db()
    count = conn.execute(
        "SELECT COUNT(*) FROM logs WHERE event_type='Access Denied'"
    ).fetchone()[0]
    conn.close()
    return count


def get_recent_logs(limit=5):
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM logs ORDER BY timestamp DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return rows


# ---------------------------------------------------------------------------
# Access point queries
# ---------------------------------------------------------------------------

def get_all_access_points():
    conn = get_db()
    rows = conn.execute("SELECT * FROM access_points ORDER BY id").fetchall()
    conn.close()
    return rows


def get_access_point_by_id(ap_id):
    conn = get_db()
    row  = conn.execute(
        "SELECT * FROM access_points WHERE id = ?", (ap_id,)
    ).fetchone()
    conn.close()
    return row


def create_access_point(name, location, ap_type, status):
    conn = get_db()
    conn.execute(
        "INSERT INTO access_points (name, location, type, status) VALUES (?,?,?,?)",
        (name, location, ap_type, status),
    )
    conn.commit()
    conn.close()


def update_access_point(ap_id, name, location, ap_type, status):
    conn = get_db()
    conn.execute(
        "UPDATE access_points SET name=?, location=?, type=?, status=? WHERE id=?",
        (name, location, ap_type, status, ap_id),
    )
    conn.commit()
    conn.close()


def delete_access_point(ap_id):
    conn = get_db()
    conn.execute("DELETE FROM access_points WHERE id = ?", (ap_id,))
    conn.commit()
    conn.close()


def count_active_access_points():
    conn  = get_db()
    count = conn.execute(
        "SELECT COUNT(*) FROM access_points WHERE status='Active'"
    ).fetchone()[0]
    conn.close()
    return count


# ---------------------------------------------------------------------------
# WebAuthn / Fingerprint credential queries
# ---------------------------------------------------------------------------

def save_webauthn_credential(user_id, credential_id, public_key,
                              device_name="Fingerprint Sensor", transports=None):
    """Persist a newly registered WebAuthn credential."""
    conn = get_db()
    try:
        conn.execute(
            """INSERT INTO webauthn_credentials
               (user_id, credential_id, public_key, device_name, transports)
               VALUES (?,?,?,?,?)""",
            (user_id, credential_id, public_key, device_name,
             transports or "[]"),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False          # duplicate credential_id
    finally:
        conn.close()


def get_webauthn_credential(credential_id):
    """Return a single credential row by its credential_id."""
    conn = get_db()
    row  = conn.execute(
        "SELECT * FROM webauthn_credentials WHERE credential_id = ?",
        (credential_id,),
    ).fetchone()
    conn.close()
    return row


def get_webauthn_credentials_for_user(user_id):
    """Return all registered credentials for a user."""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM webauthn_credentials WHERE user_id = ? ORDER BY registered_at DESC",
        (user_id,),
    ).fetchall()
    conn.close()
    return rows


def get_all_webauthn_credentials():
    """Return all credentials joined with user info — for the admin overview table."""
    conn = get_db()
    rows = conn.execute(
        """SELECT wc.*, u.full_name, u.username, u.status AS user_status
           FROM webauthn_credentials wc
           JOIN users u ON u.id = wc.user_id
           ORDER BY wc.registered_at DESC""",
    ).fetchall()
    conn.close()
    return rows


def update_webauthn_sign_count(credential_id, new_count):
    """Increment the sign counter after a successful assertion."""
    conn = get_db()
    conn.execute(
        "UPDATE webauthn_credentials SET sign_count = ? WHERE credential_id = ?",
        (new_count, credential_id),
    )
    conn.commit()
    conn.close()


def delete_webauthn_credential(credential_id):
    conn = get_db()
    conn.execute(
        "DELETE FROM webauthn_credentials WHERE credential_id = ?",
        (credential_id,),
    )
    conn.commit()
    conn.close()


def count_webauthn_enrolled():
    """Number of users who have at least one registered credential."""
    conn   = get_db()
    count  = conn.execute(
        "SELECT COUNT(DISTINCT user_id) FROM webauthn_credentials"
    ).fetchone()[0]
    conn.close()
    return count
