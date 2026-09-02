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
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal

from boto3.dynamodb.conditions import Key, Attr

from aws_config import (
    get_dynamodb,
    DYNAMO_USERS_TABLE,
    DYNAMO_LOGS_TABLE,
    DYNAMO_POINTS_TABLE,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Simple TTL in-memory cache
# ---------------------------------------------------------------------------

class _TTLCache:
    """
    Thread-safe key→value store with per-entry expiry.

    Usage:
        cache = _TTLCache(ttl=30)
        cache.set('key', value)
        hit, value = cache.get('key')   # hit=False when missing or expired
        cache.invalidate('key')         # evict a single key
        cache.clear()                   # evict everything
    """
    def __init__(self, ttl: float = 30.0):
        self._ttl   = ttl
        self._store: dict = {}          # key → (value, expiry_timestamp)
        self._lock  = threading.Lock()

    def get(self, key):
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return False, None
            value, expiry = entry
            if time.monotonic() > expiry:
                del self._store[key]
                return False, None
            return True, value

    def set(self, key, value):
        with self._lock:
            self._store[key] = (value, time.monotonic() + self._ttl)

    def invalidate(self, key):
        with self._lock:
            self._store.pop(key, None)

    def clear(self):
        with self._lock:
            self._store.clear()


# Module-level cache instances (shared across all requests)
_stats_cache        = _TTLCache(ttl=60)    # dashboard count stats (granted/denied/users/points)
_users_cache        = _TTLCache(ttl=30)    # full user list
_access_points_cache = _TTLCache(ttl=60)   # access points list
_chart_cache        = _TTLCache(ttl=120)   # 7-day chart data (expensive full scan)
_breakdown_cache    = _TTLCache(ttl=120)   # event-type breakdown (expensive full scan)
_recent_logs_cache  = _TTLCache(ttl=15)    # recent logs for dashboard / notifications


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
        ('John Doe',        'admin',   'Administrator', 'admin@faceauth.io'),
        ('Sarah Smith',     'sarah',   'Operator',      'sarah@faceauth.io'),
        ('Michael Johnson', 'michael', 'Viewer',        'michael@faceauth.io'),
        ('Emily Williams',  'emily',   'Operator',      'emily@faceauth.io'),
    ]
    with users_table.batch_writer() as batch:
        for (full_name, username, role, email) in demo_users:
            batch.put_item(Item={
                'user_id':    _new_id(),
                'full_name':  full_name,
                'username':   username,
                'role':       role,
                'status':     'Active',
                'email':      email,
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
    hit, cached = _users_cache.get('all_users')
    if hit:
        return cached
    table  = get_dynamodb().Table(DYNAMO_USERS_TABLE)
    items  = _scan_all(table)
    result = _rows(sorted(items, key=lambda x: x.get('created_at', '')))
    _users_cache.set('all_users', result)
    return result


def clear_users_cache():
    _users_cache.clear()


def get_user_by_id(user_id: str):
    table    = get_dynamodb().Table(DYNAMO_USERS_TABLE)
    response = table.get_item(Key={'user_id': str(user_id)})
    return _row(response.get('Item'))


def get_user_by_username(username: str):
    if not username:
        return None
    try:
        table    = get_dynamodb().Table(DYNAMO_USERS_TABLE)
        response = table.query(
            IndexName='UsernameIndex',
            KeyConditionExpression=Key('username').eq(username),
        )
        items = response.get('Items', [])
        if items:
            return _row(items[0])
    except Exception as exc:
        logger.warning("UsernameIndex query failed, falling back to scan: %s", exc)

    for u in get_all_users():
        if u.get('username') == username:
            return u
    return None


def create_user(full_name, username, role, status,
                email, photo_path=None, photos=None):
    """Create a new user.  Returns (True, None) or (False, error_message)."""
    if get_user_by_username(username):
        return False, "Username already exists."

    table = get_dynamodb().Table(DYNAMO_USERS_TABLE)
    item  = {
        'user_id':    _new_id(),
        'full_name':  full_name,
        'username':   username,
        'role':       role,
        'status':     status,
        'created_at': _now(),
        'updated_at': _now(),
    }
    # Only store optional fields when they have a value
    if email:      item['email']      = email
    if photo_path: item['photo_path'] = photo_path
    if photos:     item['photos']     = photos

    try:
        table.put_item(Item=item)
        _users_cache.clear()           # invalidate cached user list
        return True, None
    except Exception as exc:
        logger.error("create_user DynamoDB error: %s", exc)
        return False, str(exc)


def update_user(user_id, full_name, role, status, email):
    table = get_dynamodb().Table(DYNAMO_USERS_TABLE)

    # role and status are reserved-ish; use ExpressionAttributeNames
    expr_parts = ['full_name = :fn', '#r = :role', '#s = :status', 'updated_at = :ua']
    expr_vals  = {':fn': full_name, ':role': role, ':status': status, ':ua': _now()}
    expr_names = {'#r': 'role', '#s': 'status'}

    if email is not None:
        expr_parts.append('email = :email')
        expr_vals[':email'] = email

    table.update_item(
        Key={'user_id': str(user_id)},
        UpdateExpression='SET ' + ', '.join(expr_parts),
        ExpressionAttributeValues=expr_vals,
        ExpressionAttributeNames=expr_names,
    )
    _users_cache.clear()               # invalidate cached user list


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
    _users_cache.clear()               # invalidate cached user list


def count_active_users() -> int:
    hit, val = _stats_cache.get('active_users')
    if hit:
        return val
    table    = get_dynamodb().Table(DYNAMO_USERS_TABLE)
    response = table.scan(FilterExpression=Attr('status').eq('Active'), Select='COUNT')
    result   = response.get('Count', 0)
    _stats_cache.set('active_users', result)
    return result


# ── Log Queries ───────────────────────────────────────────────────────────────

def add_log(event_type, username, access_point, status, details=""):
    """
    Write an audit-log entry to DynamoDB.

    Fire-and-forget: the write runs in a daemon thread so the caller
    (and the HTTP response) is never blocked by DynamoDB network latency.
    Invalidates cached log/chart data so the next read reflects the new entry.
    """
    # Eagerly invalidate read caches so the new entry appears promptly
    _recent_logs_cache.clear()
    _chart_cache.clear()
    _breakdown_cache.clear()
    # stats cache is intentionally left to expire naturally (60 s)

    def _write():
        try:
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
        except Exception as exc:
            logger.warning("add_log background write failed: %s", exc)

    threading.Thread(target=_write, daemon=True, name="log-write").start()


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


def _fetch_access_counts() -> tuple[int, int]:
    """
    Internal helper: fetch Access Granted + Access Denied counts in a
    *single* DynamoDB scan instead of two separate round-trips.

    Results are written into _stats_cache under 'access_granted' and
    'access_denied' so individual callers remain cache-aware.
    """
    table    = get_dynamodb().Table(DYNAMO_LOGS_TABLE)
    response = table.scan(
        FilterExpression=(
            Attr('event_type').eq('Access Granted') |
            Attr('event_type').eq('Access Denied')
        ),
        ProjectionExpression='event_type',
    )
    items = response.get('Items', [])
    while 'LastEvaluatedKey' in response:
        response = table.scan(
            ExclusiveStartKey=response['LastEvaluatedKey'],
            FilterExpression=(
                Attr('event_type').eq('Access Granted') |
                Attr('event_type').eq('Access Denied')
            ),
            ProjectionExpression='event_type',
        )
        items.extend(response.get('Items', []))

    granted = sum(1 for i in items if i.get('event_type') == 'Access Granted')
    denied  = sum(1 for i in items if i.get('event_type') == 'Access Denied')
    _stats_cache.set('access_granted', granted)
    _stats_cache.set('access_denied',  denied)
    return granted, denied


def count_access_granted() -> int:
    hit, val = _stats_cache.get('access_granted')
    if hit:
        return val
    granted, _ = _fetch_access_counts()
    return granted


def count_access_denied() -> int:
    hit, val = _stats_cache.get('access_denied')
    if hit:
        return val
    _, denied = _fetch_access_counts()
    return denied


def get_recent_logs(limit=5) -> list:
    cache_key = f'recent_logs_{limit}'
    hit, cached = _recent_logs_cache.get(cache_key)
    if hit:
        return cached
    result = get_logs(limit=limit)
    _recent_logs_cache.set(cache_key, result)
    return result


# ── Access Point Queries ──────────────────────────────────────────────────────

def get_all_access_points() -> list:
    hit, cached = _access_points_cache.get('all_access_points')
    if hit:
        return cached
    table  = get_dynamodb().Table(DYNAMO_POINTS_TABLE)
    items  = _scan_all(table)
    result = _rows(sorted(items, key=lambda x: x.get('created_at', '')))
    _access_points_cache.set('all_access_points', result)
    return result


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
    _access_points_cache.clear()
    _stats_cache.invalidate('active_points')


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
    _access_points_cache.clear()
    _stats_cache.invalidate('active_points')


def delete_access_point(ap_id):
    table = get_dynamodb().Table(DYNAMO_POINTS_TABLE)
    table.delete_item(Key={'point_id': str(ap_id)})
    _access_points_cache.clear()
    _stats_cache.invalidate('active_points')


def count_active_access_points() -> int:
    hit, val = _stats_cache.get('active_points')
    if hit:
        return val
    # Reuse cached access points list if available — avoids extra scan
    ap_hit, ap_list = _access_points_cache.get('all_access_points')
    if ap_hit:
        result = sum(1 for ap in ap_list if ap.get('status') == 'Active')
    else:
        table    = get_dynamodb().Table(DYNAMO_POINTS_TABLE)
        response = table.scan(FilterExpression=Attr('status').eq('Active'), Select='COUNT')
        result   = response.get('Count', 0)
    _stats_cache.set('active_points', result)
    return result


def get_chart_data_7days() -> dict:
    """
    Return daily Access Granted / Access Denied counts for the last 7 days.
    Used for the dashboard bar chart.
    Results are cached for 120 s — chart data does not need to be real-time.

    Returns:
        {
          "labels": ["Mon", "Tue", …],   # 7 day-name labels (oldest → newest)
          "granted": [3, 1, 5, …],
          "denied":  [0, 2, 1, …],
        }
    """
    hit, cached = _chart_cache.get('chart_7days')
    if hit:
        return cached

    from datetime import timedelta

    today  = datetime.now(timezone.utc).date()
    days   = [(today - timedelta(days=i)) for i in range(6, -1, -1)]  # oldest first
    labels = [d.strftime('%a %d') for d in days]

    granted_by_day: dict[str, int] = {d.strftime('%Y-%m-%d'): 0 for d in days}
    denied_by_day:  dict[str, int] = {d.strftime('%Y-%m-%d'): 0 for d in days}

    cutoff       = days[0].strftime('%Y-%m-%d')   # 6 days ago
    filter_expr  = (
        Attr('timestamp').gte(cutoff) &
        (Attr('event_type').eq('Access Granted') | Attr('event_type').eq('Access Denied'))
    )
    table    = get_dynamodb().Table(DYNAMO_LOGS_TABLE)
    response = table.scan(
        FilterExpression=filter_expr,
        ProjectionExpression='#ts, event_type',
        ExpressionAttributeNames={'#ts': 'timestamp'},
    )
    items = response.get('Items', [])
    while 'LastEvaluatedKey' in response:
        response = table.scan(
            ExclusiveStartKey=response['LastEvaluatedKey'],
            FilterExpression=filter_expr,
            ProjectionExpression='#ts, event_type',
            ExpressionAttributeNames={'#ts': 'timestamp'},
        )
        items.extend(response.get('Items', []))

    for item in items:
        ts  = str(item.get('timestamp', ''))[:10]   # "YYYY-MM-DD"
        evt = item.get('event_type', '')
        if ts in granted_by_day:
            if evt == 'Access Granted':
                granted_by_day[ts] += 1
            elif evt == 'Access Denied':
                denied_by_day[ts]  += 1

    result = {
        'labels':  labels,
        'granted': [granted_by_day[d.strftime('%Y-%m-%d')] for d in days],
        'denied':  [denied_by_day[d.strftime('%Y-%m-%d')]  for d in days],
    }
    _chart_cache.set('chart_7days', result)
    return result


def get_event_type_breakdown() -> list:
    """
    Return a list of (event_type, count) for all log entries.
    Used for the dashboard donut chart.
    Results are cached for 120 s — breakdown data does not need to be real-time.

    Returns:
        [{"label": "Access Granted", "count": 42}, …]  sorted by count desc
    """
    hit, cached = _breakdown_cache.get('event_breakdown')
    if hit:
        return cached

    table    = get_dynamodb().Table(DYNAMO_LOGS_TABLE)
    response = table.scan(ProjectionExpression='event_type')
    items    = response.get('Items', [])
    while 'LastEvaluatedKey' in response:
        response = table.scan(
            ExclusiveStartKey=response['LastEvaluatedKey'],
            ProjectionExpression='event_type',
        )
        items.extend(response.get('Items', []))

    counts: dict[str, int] = {}
    for item in items:
        et = item.get('event_type', 'Unknown')
        counts[et] = counts.get(et, 0) + 1

    result = sorted(
        [{'label': k, 'count': v} for k, v in counts.items()],
        key=lambda x: x['count'],
        reverse=True,
    )
    _breakdown_cache.set('event_breakdown', result)
    return result

