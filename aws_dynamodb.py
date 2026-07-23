"""
aws_dynamodb.py — DynamoDB database layer for FaceAuth.

This is a drop-in replacement for database.py.  Every public function
keeps the same name and return shape so app.py requires only a one-line
import change:

    import aws_dynamodb as db      # was: import database as db

Tables
------
faceauth-users         : operator/user accounts + enrollment photo S3 key
faceauth-logs          : immutable audit trail
faceauth-access-points : physical / logical access points
"""

import uuid
import logging
from datetime import datetime, timezone
from decimal import Decimal

from boto3.dynamodb.conditions import Key, Attr
from werkzeug.security import generate_password_hash  # type: ignore[import-untyped]

from aws_config import (
    get_dynamodb,
    DYNAMO_USERS_TABLE,
    DYNAMO_LOGS_TABLE,
    DYNAMO_POINTS_TABLE,
)

logger = logging.getLogger(__name__)


# ── Internal Helpers ──────────────────────────────────────────────────────────

def _now() -> str:
    """Return current UTC time as a sortable string."""
    return datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')


def _new_id() -> str:
    """Generate a new UUID string for use as a DynamoDB primary key."""
    return str(uuid.uuid4())


def _dec(val) -> Decimal:
    """Convert a Python number to DynamoDB Decimal (required for numeric attrs)."""
    if val is None:
        return Decimal('0')
    return Decimal(str(val))


def _clean(item: dict) -> dict:
    """
    Normalise a raw DynamoDB item into a plain Python dict.

    - Converts Decimal → int or float
    - Adds an 'id' alias pointing to the table's primary key field
      so code that does user['id'] or user.id keeps working unchanged.
    """
    if item is None:
        return None

    result = {}
    for k, v in item.items():
        if isinstance(v, Decimal):
            result[k] = int(v) if v == v.to_integral_value() else float(v)
        else:
            result[k] = v

    # Provide 'id' alias for each table's PK so existing code is unaffected
    for pk in ('user_id', 'log_id', 'point_id'):
        if pk in result and 'id' not in result:
            result['id'] = result[pk]

    return result


class DynamoRow(dict):
    """
    Dict subclass that also supports attribute-style access.

    Mimics sqlite3.Row so that Jinja2 templates work without changes:
        {{ user.full_name }}   and   {{ user['full_name'] }}   both work.
    """
    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(f"'DynamoRow' has no attribute '{name}'")


def _row(item: dict):
    """Wrap a single DynamoDB item in a DynamoRow (or return None)."""
    if item is None:
        return None
    return DynamoRow(_clean(item))


def _rows(items: list) -> list:
    """Wrap a list of DynamoDB items in DynamoRow objects."""
    return [DynamoRow(_clean(i)) for i in items]


def _scan_all(table) -> list:
    """Paginated scan — fetches all items regardless of table size."""
    response = table.scan()
    items    = response.get('Items', [])
    while 'LastEvaluatedKey' in response:
        response = table.scan(ExclusiveStartKey=response['LastEvaluatedKey'])
        items.extend(response.get('Items', []))
    return items


# ── Database Initialisation ───────────────────────────────────────────────────

def init_db():
    """
    Verify that DynamoDB tables are reachable, then seed demo data if empty.

    Tables must already exist — create them first with:
        python aws_setup.py
    """
    dynamodb = get_dynamodb()

    try:
        table = dynamodb.Table(DYNAMO_USERS_TABLE)
        table.load()                        # raises ResourceNotFoundException if absent
        logger.info("DynamoDB connection OK — table '%s' is ACTIVE.", DYNAMO_USERS_TABLE)
    except Exception as exc:
        raise RuntimeError(
            f"DynamoDB table '{DYNAMO_USERS_TABLE}' not found.\n"
            f"Run:  python aws_setup.py  to create all tables.\n"
            f"Error: {exc}"
        ) from exc

    _seed_demo_data(dynamodb)


def _seed_demo_data(dynamodb):
    """Insert demo accounts only if the users table is completely empty."""
    users_table = dynamodb.Table(DYNAMO_USERS_TABLE)

    resp = users_table.scan(Limit=1, Select='COUNT')
    if resp.get('Count', 0) > 0:
        return  # Table already has data — never overwrite

    logger.info("Seeding demo users and access points into DynamoDB …")

    demo_users = [
        ('John Doe',        'admin',   'admin123',   'Administrator', 'admin@faceauth.io',   '+601112345678'),
        ('Sarah Smith',     'sarah',   'sarah123',   'Operator',      'sarah@faceauth.io',   '+601122345678'),
        ('Michael Johnson', 'michael', 'michael123', 'Viewer',        'michael@faceauth.io', '+601132345678'),
        ('Emily Williams',  'emily',   'emily123',   'Operator',      'emily@faceauth.io',   '+601142345678'),
    ]
    with users_table.batch_writer() as batch:
        for (full_name, username, password, role, email, phone) in demo_users:
            batch.put_item(Item={
                'user_id':    _new_id(),
                'full_name':  full_name,
                'username':   username,
                'password':   generate_password_hash(password),
                'role':       role,
                'status':     'Active',
                'email':      email,
                'phone':      phone,
                'created_at': _now(),
                'updated_at': _now(),
            })

    # Seed access points only if that table is also empty
    pts_table = dynamodb.Table(DYNAMO_POINTS_TABLE)
    if pts_table.scan(Limit=1, Select='COUNT').get('Count', 0) == 0:
        demo_points = [
            ('Main Entry',      'Building A, Floor 1',  'Facial Recognition', 'Active',      42,   98.5),
            ('Conference Room', 'Building A, Floor 3',  'Facial Recognition', 'Active',      28,   99.2),
            ('Server Room',     'Building B, Floor 2',  'Facial Recognition', 'Maintenance',  5,  100.0),
            ('Parking Garage',  'Building C, Level B2', 'Facial Recognition', 'Active',     156,   97.8),
        ]
        with pts_table.batch_writer() as batch:
            for (name, location, ap_type, status, entries, rate) in demo_points:
                batch.put_item(Item={
                    'point_id':      _new_id(),
                    'name':          name,
                    'location':      location,
                    'type':          ap_type,
                    'status':        status,
                    'entries_today': _dec(entries),
                    'success_rate':  _dec(rate),
                    'created_at':    _now(),
                })

    # Seed sample logs only if that table is also empty
    logs_table = dynamodb.Table(DYNAMO_LOGS_TABLE)
    if logs_table.scan(Limit=1, Select='COUNT').get('Count', 0) == 0:
        demo_logs = [
            ('2026-05-31 16:42:15', 'Access Granted', 'John Doe',       'Main Entry',      'Success',   'Facial match confidence 98.9%'),
            ('2026-05-31 16:41:32', 'Access Denied',  'Unknown',         'Server Room',     'Failed',    'No matching face found'),
            ('2026-05-31 16:40:48', 'Access Granted', 'Sarah Smith',     'Conference Room', 'Success',   'Facial match confidence 99.1%'),
            ('2026-05-31 16:39:12', 'System Update',  'System',          'N/A',             'Completed', 'Facial recognition model v3.2 deployed'),
            ('2026-05-31 16:38:55', 'Access Granted', 'Michael Johnson', 'Parking Garage',  'Success',   'Facial match confidence 97.4%'),
        ]
        with logs_table.batch_writer() as batch:
            for (ts, et, un, ap, st, det) in demo_logs:
                batch.put_item(Item={
                    'log_id':       _new_id(),
                    'timestamp':    ts,
                    'event_type':   et,
                    'username':     un,
                    'access_point': ap,
                    'status':       st,
                    'details':      det,
                })

    logger.info("Demo data seeded successfully.")


# ── User Queries ──────────────────────────────────────────────────────────────

def get_all_users() -> list:
    table = get_dynamodb().Table(DYNAMO_USERS_TABLE)
    items = _scan_all(table)
    return _rows(sorted(items, key=lambda x: x.get('created_at', '')))


def get_user_by_id(user_id: str):
    table    = get_dynamodb().Table(DYNAMO_USERS_TABLE)
    response = table.get_item(Key={'user_id': str(user_id)})
    return _row(response.get('Item'))


def get_user_by_username(username: str):
    table    = get_dynamodb().Table(DYNAMO_USERS_TABLE)
    response = table.query(
        IndexName='UsernameIndex',
        KeyConditionExpression=Key('username').eq(username),
    )
    items = response.get('Items', [])
    return _row(items[0]) if items else None


def create_user(full_name, username, password_plain, role, status,
                email, phone, photo_path=None, photos=None):
    """Create a new user.  Returns (True, None) or (False, error_message)."""
    if get_user_by_username(username):
        return False, "Username already exists."

    table = get_dynamodb().Table(DYNAMO_USERS_TABLE)
    item  = {
        'user_id':    _new_id(),
        'full_name':  full_name,
        'username':   username,
        'password':   generate_password_hash(password_plain),
        'role':       role,
        'status':     status,
        'created_at': _now(),
        'updated_at': _now(),
    }
    # Only store optional fields when they have a value
    if email:      item['email']      = email
    if phone:      item['phone']      = phone
    if photo_path: item['photo_path'] = photo_path
    if photos:     item['photos']     = photos

    try:
        table.put_item(Item=item)
        return True, None
    except Exception as exc:
        logger.error("create_user DynamoDB error: %s", exc)
        return False, str(exc)


def update_user(user_id, full_name, role, status, email, phone):
    table = get_dynamodb().Table(DYNAMO_USERS_TABLE)

    # role and status are reserved-ish; use ExpressionAttributeNames
    expr_parts = ['full_name = :fn', '#r = :role', '#s = :status', 'updated_at = :ua']
    expr_vals  = {':fn': full_name, ':role': role, ':status': status, ':ua': _now()}
    expr_names = {'#r': 'role', '#s': 'status'}

    if email is not None:
        expr_parts.append('email = :email')
        expr_vals[':email'] = email
    if phone is not None:
        expr_parts.append('phone = :phone')
        expr_vals[':phone'] = phone

    table.update_item(
        Key={'user_id': str(user_id)},
        UpdateExpression='SET ' + ', '.join(expr_parts),
        ExpressionAttributeValues=expr_vals,
        ExpressionAttributeNames=expr_names,
    )


def update_user_photo(user_id, photo_path: str | None):
    """Update the primary S3 photo key for a user. Pass None to remove it."""
    table = get_dynamodb().Table(DYNAMO_USERS_TABLE)
    if photo_path:
        table.update_item(
            Key={'user_id': str(user_id)},
            UpdateExpression='SET photo_path = :pp, updated_at = :ua',
            ExpressionAttributeValues={':pp': photo_path, ':ua': _now()},
        )
    else:
        table.update_item(
            Key={'user_id': str(user_id)},
            UpdateExpression='REMOVE photo_path SET updated_at = :ua',
            ExpressionAttributeValues={':ua': _now()},
        )


def update_user_photos(user_id, photos: list | None):
    """Update the list of S3 photo keys for a user. Pass None/[] to remove them."""
    table = get_dynamodb().Table(DYNAMO_USERS_TABLE)
    if photos:
        table.update_item(
            Key={'user_id': str(user_id)},
            UpdateExpression='SET photos = :pts, updated_at = :ua',
            ExpressionAttributeValues={':pts': photos, ':ua': _now()},
        )
    else:
        table.update_item(
            Key={'user_id': str(user_id)},
            UpdateExpression='REMOVE photos SET updated_at = :ua',
            ExpressionAttributeValues={':ua': _now()},
        )


def update_user_bluetooth_mac(user_id: str, mac: str) -> bool:
    table   = get_dynamodb().Table(DYNAMO_USERS_TABLE)
    mac_val = mac.upper().strip() if mac else None

    if mac_val:
        table.update_item(
            Key={'user_id': str(user_id)},
            UpdateExpression='SET bluetooth_mac = :mac, updated_at = :ua',
            ExpressionAttributeValues={':mac': mac_val, ':ua': _now()},
        )
    else:
        # Clear the field entirely (DynamoDB doesn't store None)
        table.update_item(
            Key={'user_id': str(user_id)},
            UpdateExpression='REMOVE bluetooth_mac SET updated_at = :ua',
            ExpressionAttributeValues={':ua': _now()},
        )
    return True


def get_user_bluetooth_mac(user_id: str) -> str | None:
    user = get_user_by_id(user_id)
    return user.get('bluetooth_mac') if user else None


def delete_user(user_id):
    table = get_dynamodb().Table(DYNAMO_USERS_TABLE)
    table.delete_item(Key={'user_id': str(user_id)})


def count_active_users() -> int:
    table    = get_dynamodb().Table(DYNAMO_USERS_TABLE)
    response = table.scan(FilterExpression=Attr('status').eq('Active'), Select='COUNT')
    return response.get('Count', 0)


# ── Log Queries ───────────────────────────────────────────────────────────────

def add_log(event_type, username, access_point, status, details=""):
    table = get_dynamodb().Table(DYNAMO_LOGS_TABLE)
    table.put_item(Item={
        'log_id':       _new_id(),
        'timestamp':    _now(),
        'event_type':   event_type   or '',
        'username':     username     or 'unknown',
        'access_point': access_point or '',
        'status':       status       or '',
        'details':      details      or '',
    })


def get_logs(event_type=None, date_from=None, date_to=None, limit=200) -> list:
    table         = get_dynamodb().Table(DYNAMO_LOGS_TABLE)
    filter_exprs  = []

    if event_type and event_type != 'All Events':
        filter_exprs.append(Attr('event_type').eq(event_type))
    if date_from:
        filter_exprs.append(Attr('timestamp').gte(date_from))
    if date_to:
        filter_exprs.append(Attr('timestamp').lte(date_to + ' 23:59:59'))

    kwargs = {}
    if filter_exprs:
        combined = filter_exprs[0]
        for expr in filter_exprs[1:]:
            combined = combined & expr
        kwargs['FilterExpression'] = combined

    response = table.scan(**kwargs)
    items    = response.get('Items', [])
    while 'LastEvaluatedKey' in response and len(items) < limit * 2:
        response = table.scan(ExclusiveStartKey=response['LastEvaluatedKey'], **kwargs)
        items.extend(response.get('Items', []))

    items.sort(key=lambda x: x.get('timestamp', ''), reverse=True)
    return _rows(items[:limit])


def count_access_granted() -> int:
    table    = get_dynamodb().Table(DYNAMO_LOGS_TABLE)
    response = table.scan(
        FilterExpression=Attr('event_type').eq('Access Granted'), Select='COUNT'
    )
    return response.get('Count', 0)


def count_access_denied() -> int:
    table    = get_dynamodb().Table(DYNAMO_LOGS_TABLE)
    response = table.scan(
        FilterExpression=Attr('event_type').eq('Access Denied'), Select='COUNT'
    )
    return response.get('Count', 0)


def get_recent_logs(limit=5) -> list:
    return get_logs(limit=limit)


# ── Access Point Queries ──────────────────────────────────────────────────────

def get_all_access_points() -> list:
    table = get_dynamodb().Table(DYNAMO_POINTS_TABLE)
    items = _scan_all(table)
    return _rows(sorted(items, key=lambda x: x.get('created_at', '')))


def get_access_point_by_id(ap_id: str):
    table    = get_dynamodb().Table(DYNAMO_POINTS_TABLE)
    response = table.get_item(Key={'point_id': str(ap_id)})
    return _row(response.get('Item'))


def create_access_point(name, location, ap_type, status):
    table = get_dynamodb().Table(DYNAMO_POINTS_TABLE)
    table.put_item(Item={
        'point_id':      _new_id(),
        'name':          name,
        'location':      location,
        'type':          ap_type,
        'status':        status,
        'entries_today': _dec(0),
        'success_rate':  _dec(100.0),
        'created_at':    _now(),
    })


def update_access_point(ap_id, name, location, ap_type, status):
    table = get_dynamodb().Table(DYNAMO_POINTS_TABLE)
    table.update_item(
        Key={'point_id': str(ap_id)},
        UpdateExpression='SET #n = :name, #l = :loc, #t = :type, #s = :status',
        ExpressionAttributeNames={
            '#n': 'name', '#l': 'location', '#t': 'type', '#s': 'status',
        },
        ExpressionAttributeValues={
            ':name': name, ':loc': location, ':type': ap_type, ':status': status,
        },
    )


def delete_access_point(ap_id):
    table = get_dynamodb().Table(DYNAMO_POINTS_TABLE)
    table.delete_item(Key={'point_id': str(ap_id)})


def count_active_access_points() -> int:
    table    = get_dynamodb().Table(DYNAMO_POINTS_TABLE)
    response = table.scan(FilterExpression=Attr('status').eq('Active'), Select='COUNT')
    return response.get('Count', 0)
