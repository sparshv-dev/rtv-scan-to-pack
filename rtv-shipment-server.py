"""
RTV Scan-to-Pack app — Python stdlib HTTP server backed by Supabase Postgres.

Separate app from server.py/public/ (different data model: tracking-id-driven
scan-to-pack instead of pasted return-ID manifests) — has its own database
(Supabase project), so the two never collide.

Local run:  DATABASE_URL=postgresql://... python rtv-shipment-server.py [port]
Hosted (Render): reads DATABASE_URL and PORT from the environment.
First admin:  python rtv-shipment-server.py create-admin <username>
"""
import getpass
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import psycopg2
import psycopg2.extras

BASE_DIR = Path(__file__).resolve().parent
HTML_PATH = BASE_DIR / "rtv-shipment.html"
DEFAULT_PORT = 8421

# Seed vendor/warehouse data — same source as the old hardcoded JS block
# ("Omni RTV Locations and Tracker - Brand Details.csv"). Only used to
# populate the vendors table on first run; after that it's just DB rows the
# user can edit/add to.
SEED_WAREHOUSES = [
    {
        "seller": "Aditya Birla Lifestyle Brands Limited",
        "brands": ["Allen Solly", "Van Huesen", "LP", "PE"],
        "street": "Aditya Birla Lifestyle Brands Limited, Survey Nos. 517/2, 527, 528, 529, 530, 531, Madivala Village, Kasaba Hobli, Anekal Taluk",
        "town": "Bangalore", "city": "Bangalore", "state": "Karnataka", "pincode": "562107",
        "contact_name": "Annamma Mathew P", "contact_phone": "9743993941", "contact_email": "annamma.mathew@ablbl.adityabirla.com",
    },
    {
        "seller": "Arvind Fashions Limited",
        "brands": ["Arrow"],
        "street": "WH No. 4, Arvind Fashions Limited, Omni Return QC Center, C/O Instakart Services Pvt. Ltd., K-Square Industrial Estate, Before Padgha Toll, Bhiwandi",
        "town": "Bhiwandi", "city": "Thane", "state": "Maharashtra", "pincode": "421101",
        "contact_name": "Bhushan Patil", "contact_phone": "91121 24020", "contact_email": "omni.qccenterAR_MAH@arvindfashions.com",
    },
    {
        "seller": "Arvind Lifestyle Brands Limited",
        "brands": ["USPA"],
        "street": "WH No. 4, Arvind Lifestyle Brands Ltd., Omni NNNOW Return QC Center, C/O Instakart Services Pvt. Ltd., K-Square Industrial Estate, Before Padgha Toll, Bhiwandi",
        "town": "Bhiwandi", "city": "Thane", "state": "Maharashtra", "pincode": "421101",
        "contact_name": "Bhushan Patil", "contact_phone": "91121 24020", "contact_email": "omni.qccenter_MAH@arvindfashions.com",
    },
    {
        "seller": "Arvind Youth Brands Private Limited",
        "brands": ["Flying Machine"],
        "street": "WH No. 4, Arvind Youth Brands Pvt. Ltd., Omni Return QC Center, C/O Instakart Services Pvt. Ltd., K-Square Industrial Estate, Before Padgha Toll, Bhiwandi",
        "town": "Bhiwandi", "city": "Thane", "state": "Maharashtra", "pincode": "421101",
        "contact_name": "Bhushan Patil", "contact_phone": "91121 24020", "contact_email": "omni.qccenterFM_MAH@arvindfashions.com",
    },
    {
        "seller": "Biba Fashion Limited",
        "brands": [],
        "street": "Biba Fashion Ltd., Khasra No. 30/21/3/2/2, 35/1/2/3., Killa -2 Rakba-3. Kamal-0. Marla 1/ 2 13 MIN 7.14.15/1.15/2,60/2 Village Sikri",
        "town": "Tehsil Ballabahgarh", "city": "Faridabad", "state": "NCR", "pincode": "121004",
        "contact_name": "Mr. Praveen", "contact_phone": "9945403556", "contact_email": "praveen.kumar@bibaindia.com",
    },
    {
        "seller": "Soch Apparels Pvt Ltd",
        "brands": [],
        "street": "Mumbai Warehouse Bhiwandi, Address: Soch Apparels Private Limited Bhiwandi, Asmeeta Textile Park, Bldg No. D-3A, Unit No. 004 Ground Floor",
        "town": "Bhiwandi", "city": "", "state": "", "pincode": "421311",
        "contact_name": "Abhijeet", "contact_phone": "9702768637", "contact_email": "swb@favouriteshop.biz",
    },
    {
        "seller": "Radhamani Textile Pvt Ltd",
        "brands": ["Rare Rabbit", "Rareism"],
        "street": "Radhamani Textiles Pvt Ltd. (WH MH), Instakart Service Pvt Ltd, Vashere, Bhiwandi, Warehouse No. WE-IL, Renaissanse Integrated Industrial Area, Repro Books Ltd Plant, Vashere",
        "town": "Bhiwandi", "city": "Thane", "state": "Maharashtra", "pincode": "421302",
        "contact_name": "Chetan Mhatre", "contact_phone": "9773535457", "contact_email": "chetan.mhatre@flipkart.com",
    },
]
SEED_SHORT_NAMES = {
    "Allen Solly": "AS", "Van Huesen": "VH", "LP": "LP", "PE": "PE",
    "Arrow": "ARW", "USPA": "USPA", "Flying Machine": "FM",
    "Biba Fashion Limited": "BIBA", "Soch Apparels Pvt Ltd": "SOCH",
    "Rare Rabbit": "RR", "Rareism": "RSM",
}

# Default email draft template — editable/saveable from the app (see the
# "settings" table and /api/email-template below). {{tokens}} get filled in
# per-shipment on the frontend; kept here just as the first-run default.
DEFAULT_EMAIL_SUBJECT_TEMPLATE = "RTV Shipment {{invoiceNo}} — {{vendorName}}"
DEFAULT_EMAIL_BODY_TEMPLATE = (
    "Dear {{contactName}},\n\n"
    "Please find below the details of a Return-to-Vendor shipment dispatched from {{hubName}}.\n\n"
    "Invoice No.: {{invoiceNo}}\n"
    "Ship Date: {{shipDate}}\n"
    "Boxes: {{boxes}}\n"
    "Total Units: {{units}}\n"
    "Total Value: {{totalValue}}\n\n"
    "The box label(s) and delivery invoice are attached separately — please download them from the app and attach before sending.\n\n"
    "Regards,\n{{hubName}}"
)


def compose_address(w):
    parts = [w["street"]]
    if w.get("town"):
        parts.append(w["town"])
    if w.get("city") and w["city"] != w.get("town"):
        parts.append(w["city"])
    if w.get("state"):
        parts.append(w["state"])
    addr = ", ".join(parts)
    if w.get("pincode"):
        addr += " - " + w["pincode"]
    return addr


def parse_pincode(address):
    # Not anchored to the end — real addresses in this data put the pincode
    # anywhere ("PIN -400064 (Maharashtra)", "400 705" with a stray space,
    # trailing punctuation like "400052."). Take the last match, since a
    # pincode-shaped number nearest the end of a real address is almost
    # always the actual pincode.
    matches = re.findall(r"\b(\d{3})\s?(\d{3})\b", address or "")
    return "".join(matches[-1]) if matches else ""


# The business runs on IST — "today" for a report means the IST calendar
# day, not the UTC day timestamps happen to be stored in.
IST = timezone(timedelta(hours=5, minutes=30))


def brand_prefix(name):
    if not name:
        return name
    i = name.find("-")
    return name[:i] if i != -1 else name


def load_dotenv():
    # Local-only convenience: a gitignored .env next to this file can hold
    # DATABASE_URL / DB_SCHEMA so they don't have to be set in every shell.
    # Never overrides a variable that's already set (Render sets its own).
    path = BASE_DIR / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def db_schema():
    # Which Postgres schema holds the app's tables. "public" is the live
    # data; testing runs against a separate schema (e.g. rtv_test) in the
    # same database so live tables are never touched.
    schema = (os.environ.get("DB_SCHEMA") or "public").strip()
    if not re.fullmatch(r"[a-z_][a-z0-9_]*", schema):
        raise RuntimeError(f"DB_SCHEMA '{schema}' isn't a valid schema name")
    return schema


def database_url():
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. Point it at your Supabase Postgres connection "
            "string (Session Pooler, port 5432) before starting the server."
        )
    return url


# Connections are kept open and reused instead of opened per call — a
# fresh connection to the Supabase pooler takes ~2.5s (TLS handshake), a
# query on an open one ~0.15s, and with the sign-in check every request
# needs at least two. The semaphore caps open connections and makes callers
# wait for a free one rather than error. Idle connections older than
# POOL_RECHECK_SECONDS are pinged before reuse, since the pooler drops idle
# ones. (psycopg2's own pool isn't used: it closes every connection above
# its minimum on return, which defeats the point.)
POOL_SIZE = 8
POOL_RECHECK_SECONDS = 30
_pool_lock = threading.Lock()
_pool_slots = threading.BoundedSemaphore(POOL_SIZE)
_idle = []  # [(conn, last_used_at)]
# Every Db opened while serving a request, so anything a handler forgot to
# close (e.g. an exception mid-query) goes back to the pool at request end.
_request_dbs = threading.local()


def _connect():
    # search_path is fixed at connect time, so it can't drift and costs no
    # extra round-trip per request.
    return psycopg2.connect(database_url(), options=f"-c search_path={db_schema()}")


def _checkout():
    while True:
        with _pool_lock:
            item = _idle.pop() if _idle else None
        if item is None:
            return _connect()
        conn, last_used = item
        if conn.closed:
            continue
        if time.time() - last_used < POOL_RECHECK_SECONDS:
            return conn
        try:
            conn.cursor().execute("SELECT 1")
            conn.rollback()
            return conn
        except psycopg2.Error:
            try:
                conn.close()
            except psycopg2.Error:
                pass


def _checkin(conn):
    broken = conn.closed
    if not broken:
        try:
            conn.rollback()  # never hand on a half-finished transaction
        except psycopg2.Error:
            broken = True
    if broken:
        try:
            conn.close()
        except psycopg2.Error:
            pass
        return
    with _pool_lock:
        _idle.append((conn, time.time()))


class Db:
    """Thin wrapper so call sites can keep using the sqlite3-style
    conn.execute(sql, params).fetchone()/.fetchall() chain psycopg2 doesn't
    support directly (it requires an explicit cursor)."""

    def __init__(self):
        _pool_slots.acquire()
        try:
            self._conn = _checkout()
        except Exception:
            _pool_slots.release()
            raise
        self._open = True
        if not hasattr(_request_dbs, "items"):
            _request_dbs.items = []
        _request_dbs.items.append(self)

    def execute(self, sql, params=None):
        cur = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql, params or ())
        return cur

    def executemany(self, sql, seq_of_params):
        cur = self._conn.cursor()
        cur.executemany(sql, seq_of_params)
        return cur

    def execute_values(self, sql, values, page_size=500):
        """Bulk insert/upsert in a handful of round-trips instead of one per
        row — a dump of a few thousand tracking IDs done row-by-row over a
        remote connection is slow enough to time out the request."""
        cur = self._conn.cursor()
        psycopg2.extras.execute_values(cur, sql, values, page_size=page_size)
        return cur

    def commit(self):
        self._conn.commit()

    def close(self):
        if not self._open:
            return
        self._open = False
        _checkin(self._conn)
        _pool_slots.release()


def get_db():
    return Db()


def release_request_dbs():
    for db in getattr(_request_dbs, "items", []):
        db.close()
    _request_dbs.items = []


def init_db():
    conn = get_db()
    schema = db_schema()
    if schema != "public":
        conn.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
        conn.commit()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS hubs (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            code TEXT NOT NULL,
            address TEXT NOT NULL,
            phone TEXT
        );
        ALTER TABLE hubs ADD COLUMN IF NOT EXISTS contact_name TEXT;
        ALTER TABLE hubs ADD COLUMN IF NOT EXISTS contact_email TEXT;
        ALTER TABLE hubs ADD COLUMN IF NOT EXISTS pincode TEXT;
        CREATE TABLE IF NOT EXISTS vendors (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            short_name TEXT,
            warehouse_label TEXT,
            address TEXT NOT NULL,
            contact_name TEXT,
            contact_phone TEXT
        );
        ALTER TABLE vendors ADD COLUMN IF NOT EXISTS contact_email TEXT;
        ALTER TABLE vendors ADD COLUMN IF NOT EXISTS pincode TEXT;
        CREATE TABLE IF NOT EXISTS master_items (
            tracking_id TEXT PRIMARY KEY,
            marketplace_order_id TEXT,
            return_id TEXT,
            seller_name TEXT,
            rtv_shipment_id TEXT,
            tms_provider_name TEXT,
            rtv_shipment_status TEXT,
            rtv_created_at TEXT,
            rtv_tracking_id TEXT,
            value REAL NOT NULL DEFAULT 0,
            order_set_id TEXT,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS shipments (
            id SERIAL PRIMARY KEY,
            invoice_no TEXT NOT NULL UNIQUE,
            hub_id INTEGER NOT NULL REFERENCES hubs(id),
            vendor_id INTEGER NOT NULL REFERENCES vendors(id),
            ship_date TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS shipment_items (
            id SERIAL PRIMARY KEY,
            shipment_id INTEGER NOT NULL REFERENCES shipments(id) ON DELETE CASCADE,
            tracking_id TEXT NOT NULL,
            value REAL NOT NULL DEFAULT 0,
            qty INTEGER NOT NULL DEFAULT 1,
            box_no INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS courier_bookings (
            id SERIAL PRIMARY KEY,
            vendor_id INTEGER NOT NULL REFERENCES vendors(id),
            pickup_date TEXT NOT NULL,
            boxes INTEGER NOT NULL,
            declared_price REAL NOT NULL,
            weight_kg REAL NOT NULL,
            created_at TEXT NOT NULL
        );
        -- Nullable even though the API requires them for new bookings —
        -- bookings created before this field existed have nothing to put
        -- here, and retrofitting NOT NULL would break those old rows.
        ALTER TABLE courier_bookings ADD COLUMN IF NOT EXISTS hub_id INTEGER REFERENCES hubs(id);
        ALTER TABLE courier_bookings ADD COLUMN IF NOT EXISTS destination_name TEXT;
        ALTER TABLE courier_bookings ADD COLUMN IF NOT EXISTS tracking TEXT;
        ALTER TABLE courier_bookings ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'Shared with DTDC';
        CREATE TABLE IF NOT EXISTS courier_booking_shipments (
            booking_id INTEGER NOT NULL REFERENCES courier_bookings(id) ON DELETE CASCADE,
            shipment_id INTEGER NOT NULL REFERENCES shipments(id) UNIQUE,
            PRIMARY KEY (booking_id, shipment_id)
        );
        -- Postgres auto-indexes primary/unique/FK-target columns but not
        -- plain FK columns or lookup keys — these back the invoice/booking
        -- lookup and the daily report, both of which join through them.
        CREATE INDEX IF NOT EXISTS idx_shipment_items_shipment_id ON shipment_items(shipment_id);
        CREATE INDEX IF NOT EXISTS idx_shipment_items_tracking_id ON shipment_items(tracking_id);
        -- Logins: one 'hub' account per hub (scoped to that hub's data), plus
        -- any number of 'admin' accounts (hub_id NULL, see every hub).
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('admin', 'hub')),
            hub_id INTEGER REFERENCES hubs(id),
            active BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TEXT NOT NULL,
            last_login_at TEXT
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_users_one_per_hub ON users(hub_id) WHERE role = 'hub';
        -- Only a hash of the session token is stored, so a leaked table
        -- can't be replayed as a login. view_hub_id is an admin's current
        -- "Viewing: <hub>" pick (NULL = all hubs).
        CREATE TABLE IF NOT EXISTS sessions (
            token_hash TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            view_hub_id INTEGER REFERENCES hubs(id) ON DELETE SET NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_sessions_user_id ON sessions(user_id);
        """
    )
    conn.commit()

    # Runs on every startup, not just an empty table: inserts any brand new
    # vendors that don't exist yet, and backfills contact_email on existing
    # rows that don't have one (e.g. vendors seeded before that field
    # existed) — without touching anything a user has since edited by hand.
    for w in SEED_WAREHOUSES:
        address = compose_address(w)
        keys = w["brands"] or [w["seller"]]
        for name in keys:
            conn.execute(
                """
                INSERT INTO vendors (name, short_name, warehouse_label, address, contact_name, contact_phone, contact_email)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (name) DO UPDATE SET contact_email = EXCLUDED.contact_email
                WHERE vendors.contact_email IS NULL OR vendors.contact_email = ''
                """,
                (name, SEED_SHORT_NAMES.get(name, ""), "", address, w["contact_name"], w["contact_phone"], w.get("contact_email", "")),
            )
    conn.commit()

    # Pincode is its own column now (needed for DTDC's Origin/Destination
    # Pincode columns), but every hub/vendor so far only has it embedded at
    # the end of the free-text address ("... - 400086"). Backfill from that
    # pattern once; leaves anything already set untouched.
    for table in ("hubs", "vendors"):
        rows = conn.execute(f"SELECT id, address FROM {table} WHERE pincode IS NULL OR pincode = ''").fetchall()
        for r in rows:
            pin = parse_pincode(r["address"])
            if pin:
                conn.execute(f"UPDATE {table} SET pincode = %s WHERE id = %s", (pin, r["id"]))
    conn.commit()

    # Bookings created before hub_id existed on courier_bookings have no
    # Origin fields to show — but the shipments linked to them still know
    # their own hub, so recover it from there instead of leaving it blank.
    orphaned = conn.execute(
        """
        SELECT b.id AS booking_id, s.hub_id
        FROM courier_bookings b
        JOIN courier_booking_shipments cbs ON cbs.booking_id = b.id
        JOIN shipments s ON s.id = cbs.shipment_id
        WHERE b.hub_id IS NULL
        GROUP BY b.id, s.hub_id
        """
    ).fetchall()
    for r in orphaned:
        conn.execute("UPDATE courier_bookings SET hub_id = %s WHERE id = %s", (r["hub_id"], r["booking_id"]))
    conn.commit()
    conn.close()


def hub_to_dict(row):
    return {
        "id": row["id"], "name": row["name"], "code": row["code"], "address": row["address"], "phone": row["phone"],
        "contactName": row["contact_name"], "contactEmail": row["contact_email"], "pincode": row["pincode"],
    }


def vendor_to_dict(row):
    return {
        "id": row["id"],
        "name": row["name"],
        "shortName": row["short_name"],
        "warehouseLabel": row["warehouse_label"],
        "address": row["address"],
        "contactName": row["contact_name"],
        "contactPhone": row["contact_phone"],
        "contactEmail": row["contact_email"],
        "pincode": row["pincode"],
    }


# Fixed for every booking (confirmed — DTDC's own bulk-booking template),
# so these never need a field, data entry, or storage of their own.
COURIER_BOOKING_CONSTANTS = {
    "serviceType": "Road",
    "courierType": "Forward",
    "contentType": "Clothes",
    "riskSurcharge": "no risk",
    "consignmentType": "Forward",
    "codToPay": "Prepaid",
}

# The lifecycle a booking moves through after it's created — fixed list, not
# stored anywhere else, so the frontend's dropdown and this validation stay
# hand in sync.
COURIER_BOOKING_STATUSES = [
    "Shared with DTDC",
    "Picked up from store",
    "In Transit",
    "Delivered to Brand",
]


def compute_origin_name(hub_name):
    if not hub_name:
        return None
    # Some hubs are already named e.g. "Zilo Parel Hub" — don't double up
    # into "Zilo Zilo Parel Hub Warehouse". Only pad out bare names like
    # "Parel" into the full "Zilo Parel Warehouse" form.
    if "zilo" in hub_name.lower():
        return hub_name
    return f"Zilo {hub_name} Warehouse"


def courier_booking_to_dict(row):
    hub_name = row["hub_name"]
    hub_contact_name = row["hub_contact_name"]
    hub_phone = row["hub_phone"]
    origin_phone = " - ".join(p for p in (hub_contact_name, hub_phone) if p) or None
    out = {
        "id": row["id"],
        "customerReferenceNumber": row["id"],
        "pickupDate": row["pickup_date"],
        "boxes": row["boxes"],
        "declaredPrice": row["declared_price"],
        "weightKg": row["weight_kg"],
        "createdAt": row["created_at"],
        "vendorName": row["vendor_name"],
        "warehouseLabel": row["warehouse_label"],
        "invoiceNumbers": row["invoice_numbers"],
        "tracking": row["tracking"],
        "status": row["status"],
        "destinationName": row["destination_name"],
        "destinationPincode": row["vendor_pincode"],
        "destinationPhone": row["vendor_contact_phone"],
        "destinationAddress": row["vendor_address"],
        "hubName": hub_name,
        "originName": compute_origin_name(hub_name),
        "originPincode": row["hub_pincode"],
        "originPhone": origin_phone,
        "originAddress": row["hub_address"],
    }
    out.update(COURIER_BOOKING_CONSTANTS)
    return out


def master_to_dict(row):
    return {
        "trackingId": row["tracking_id"],
        "marketplaceOrderId": row["marketplace_order_id"],
        "returnId": row["return_id"],
        # Exposed as "brand" (not "sellerName") so the existing brand->vendor
        # matching logic on the frontend needs no changes.
        "brand": row["seller_name"],
        "rtvShipmentId": row["rtv_shipment_id"],
        "tmsProviderName": row["tms_provider_name"],
        "rtvShipmentStatus": row["rtv_shipment_status"],
        "rtvCreatedAt": row["rtv_created_at"],
        "rtvTrackingId": row["rtv_tracking_id"],
        "value": row["value"],
        "orderSetId": row["order_set_id"],
    }


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def require_str(body, key, allow_empty=False):
    val = body.get(key)
    if not isinstance(val, str) or (not allow_empty and not val.strip()):
        raise ApiError(400, f"'{key}' is required")
    return val.strip()


def slugify_code(name):
    letters = re.sub(r"[^A-Za-z]", "", name).upper()
    return letters[:3] or "HUB"


# ---------- auth ----------
SESSION_COOKIE = "rtv_session"
SESSION_DAYS = 7
PBKDF2_ITERATIONS = 240000
MIN_PASSWORD_LENGTH = 8
USERNAME_RE = re.compile(r"^[a-z0-9._-]{3,40}$")

# Failed sign-ins per username, in memory: after LOCKOUT_ATTEMPTS misses
# within LOCKOUT_SECONDS the account refuses further tries until the window
# passes. Resets on restart — it's a brake on password guessing, not an audit.
LOCKOUT_ATTEMPTS = 5
LOCKOUT_SECONDS = 15 * 60
_failed_logins = {}


def hash_password(password):
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password, stored):
    try:
        _algo, iterations, salt_hex, hash_hex = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations))
    except (ValueError, AttributeError):
        return False
    return hmac.compare_digest(dk.hex(), hash_hex)


def hash_token(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def validate_password(password):
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
        raise ApiError(400, f"Password must be at least {MIN_PASSWORD_LENGTH} characters")
    return password


def normalize_username(body):
    username = (body.get("username") or "").strip().lower()
    if not USERNAME_RE.match(username):
        raise ApiError(400, "Username must be 3-40 characters: letters, numbers, dot, dash or underscore")
    return username


def login_locked(username):
    times = [t for t in _failed_logins.get(username, []) if time.time() - t < LOCKOUT_SECONDS]
    _failed_logins[username] = times
    return len(times) >= LOCKOUT_ATTEMPTS


def user_to_dict(row):
    return {
        "id": row["id"],
        "username": row["username"],
        "role": row["role"],
        "hubId": row["hub_id"],
        "hubName": row["hub_name"],
        "hubCode": row["hub_code"],
        "active": row["active"],
        "createdAt": row["created_at"],
        "lastLoginAt": row["last_login_at"],
    }


USER_SELECT_SQL = (
    "SELECT u.id, u.username, u.role, u.hub_id, u.active, u.created_at, u.last_login_at, "
    "h.name AS hub_name, h.code AS hub_code "
    "FROM users u LEFT JOIN hubs h ON h.id = u.hub_id "
)


class Handler(BaseHTTPRequestHandler):
    server_version = "RTVScanApp/1.0"

    def handle_one_request(self):
        # The signed-in user is cached per request; never carry it over.
        self.__dict__.pop("_user", None)
        try:
            super().handle_one_request()
        finally:
            release_request_dbs()

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def send_json(self, status, payload, cookie=None):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if cookie is not None:
            self.set_session_cookie(*cookie)
        self.end_headers()
        self.wfile.write(body)

    def read_json_body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ApiError(400, "Malformed JSON body")

    # ---------- session / access helpers ----------
    def session_token(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie") or "")
        except Exception:  # noqa: BLE001 — a mangled Cookie header just means "not signed in"
            return None
        morsel = cookie.get(SESSION_COOKIE)
        return morsel.value if morsel else None

    def current_user(self):
        if hasattr(self, "_user"):
            return self._user
        self._user = None
        token = self.session_token()
        if token:
            conn = get_db()
            row = conn.execute(
                """
                SELECT u.id, u.username, u.role, u.hub_id, u.active, s.view_hub_id,
                       h.name AS hub_name, h.code AS hub_code
                FROM sessions s
                JOIN users u ON u.id = s.user_id
                LEFT JOIN hubs h ON h.id = u.hub_id
                WHERE s.token_hash = %s AND s.expires_at > %s
                """,
                (hash_token(token), datetime.now(timezone.utc).isoformat()),
            ).fetchone()
            conn.close()
            if row and row["active"]:
                self._user = dict(row)
        return self._user

    def require_user(self):
        user = self.current_user()
        if not user:
            raise ApiError(401, "Sign in to continue")
        return user

    def require_admin(self):
        user = self.require_user()
        if user["role"] != "admin":
            raise ApiError(403, "Only admins can do that")
        return user

    def scope_hub_id(self):
        """The hub whose data this request may see: a hub account is always
        locked to its own hub; an admin sees whichever hub they've picked in
        the "Viewing" switcher, or every hub (None)."""
        user = self.require_user()
        if user["role"] == "hub":
            return user["hub_id"]
        return user["view_hub_id"]

    def hub_filter(self, column):
        hub_id = self.scope_hub_id()
        if hub_id is None:
            return "", []
        return f" AND {column} = %s", [hub_id]

    def check_hub_access(self, hub_id):
        scoped = self.scope_hub_id()
        if scoped is not None and hub_id != scoped:
            raise ApiError(404, "Not found")

    def set_session_cookie(self, token, max_age):
        secure = "; Secure" if (self.headers.get("X-Forwarded-Proto") or "").lower() == "https" else ""
        self.send_header(
            "Set-Cookie",
            f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={max_age}{secure}",
        )

    def serve_html(self):
        if not HTML_PATH.exists():
            self.send_response(404)
            self.end_headers()
            return
        data = HTML_PATH.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # ---------- routing ----------
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        try:
            if path == "/" or path == "/rtv-shipment.html":
                return self.serve_html()
            if path.startswith("/api/"):
                self.require_user()
            if path == "/api/me":
                return self.get_me()
            if path == "/api/users":
                return self.list_users()
            if path == "/api/hubs":
                return self.list_hubs()
            if path == "/api/vendors":
                return self.list_vendors()
            if path == "/api/lookup":
                tracking = (qs.get("tracking") or [""])[0].strip()
                return self.lookup_tracking(tracking)
            if path == "/api/shipments":
                return self.list_shipments()
            if path == "/api/shipments/archive":
                q = (qs.get("q") or [""])[0].strip()
                return self.list_shipments_archive(q)
            if path == "/api/email-template":
                return self.get_email_template()
            if path == "/api/shipments/bookable":
                q = (qs.get("q") or [""])[0].strip()
                return self.list_bookable_shipments(q)
            if path == "/api/courier-bookings":
                return self.list_courier_bookings()
            if path == "/api/courier-bookings/export":
                status_filter = (qs.get("status") or [""])[0].strip()
                q_filter = (qs.get("q") or [""])[0].strip()
                return self.export_courier_bookings_csv(status_filter or None, q_filter or None)
            if path == "/api/courier-bookings/report":
                date_str = (qs.get("date") or [""])[0].strip()
                return self.get_courier_bookings_report(date_str)
            if path == "/api/courier-lookup":
                q = (qs.get("q") or [""])[0].strip()
                return self.courier_lookup(q)
            m = re.match(r"^/api/courier-bookings/(\d+)/report$", path)
            if m:
                return self.get_courier_booking_report_csv(int(m.group(1)))
            m = re.match(r"^/api/shipments/(\d+)$", path)
            if m:
                return self.get_shipment(int(m.group(1)))
            if path.startswith("/api/"):
                return self.send_json(404, {"error": "Unknown endpoint"})
            self.send_response(404)
            self.end_headers()
        except ApiError as e:
            self.send_json(e.status, {"error": e.message})
        except Exception as e:  # noqa: BLE001
            self.send_json(500, {"error": str(e)})

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/api/login":
                return self.login()
            if path == "/api/logout":
                return self.logout()
            self.require_user()
            if path == "/api/view-hub":
                return self.set_view_hub()
            if path == "/api/users":
                return self.create_user()
            if path == "/api/hubs":
                return self.create_hub()
            if path == "/api/vendors":
                return self.create_vendor()
            if path == "/api/dump/import":
                return self.import_dump()
            if path == "/api/shipments":
                return self.create_shipment()
            if path == "/api/email-template":
                return self.save_email_template()
            if path == "/api/courier-bookings":
                return self.create_courier_booking()
            return self.send_json(404, {"error": "Unknown endpoint"})
        except ApiError as e:
            self.send_json(e.status, {"error": e.message})
        except Exception as e:  # noqa: BLE001
            self.send_json(500, {"error": str(e)})

    def do_DELETE(self):
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            self.require_user()
            m = re.match(r"^/api/courier-bookings/(\d+)$", path)
            if m:
                return self.delete_courier_booking(int(m.group(1)))
            m = re.match(r"^/api/hubs/(\d+)$", path)
            if m:
                return self.delete_hub(int(m.group(1)))
            return self.send_json(404, {"error": "Unknown endpoint"})
        except ApiError as e:
            self.send_json(e.status, {"error": e.message})
        except Exception as e:  # noqa: BLE001
            self.send_json(500, {"error": str(e)})

    def do_PATCH(self):
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            self.require_user()
            m = re.match(r"^/api/users/(\d+)$", path)
            if m:
                return self.update_user(int(m.group(1)))
            m = re.match(r"^/api/courier-bookings/(\d+)$", path)
            if m:
                return self.update_courier_booking(int(m.group(1)))
            return self.send_json(404, {"error": "Unknown endpoint"})
        except ApiError as e:
            self.send_json(e.status, {"error": e.message})
        except Exception as e:  # noqa: BLE001
            self.send_json(500, {"error": str(e)})

    # ---------- sign-in / accounts ----------
    def me_payload(self, user):
        return {
            "user": {
                "id": user["id"], "username": user["username"], "role": user["role"],
                "hubId": user["hub_id"], "hubName": user["hub_name"], "hubCode": user["hub_code"],
            },
            "viewHubId": user["view_hub_id"] if user["role"] == "admin" else user["hub_id"],
        }

    def get_me(self):
        self.send_json(200, self.me_payload(self.require_user()))

    def login(self):
        body = self.read_json_body()
        username = (body.get("username") or "").strip().lower()
        password = body.get("password") or ""
        if not username or not password:
            raise ApiError(400, "Enter your username and password")
        if login_locked(username):
            raise ApiError(429, "Too many wrong attempts. Try again in 15 minutes.")
        conn = get_db()
        row = conn.execute("SELECT * FROM users WHERE username = %s", (username,)).fetchone()
        if not row or not verify_password(password, row["password_hash"]):
            conn.close()
            _failed_logins.setdefault(username, []).append(time.time())
            raise ApiError(401, "Wrong username or password")
        if not row["active"]:
            conn.close()
            raise ApiError(403, "This account is disabled. Ask your admin.")
        _failed_logins.pop(username, None)

        token = secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc)
        conn.execute("DELETE FROM sessions WHERE expires_at < %s", (now.isoformat(),))
        conn.execute(
            "INSERT INTO sessions (token_hash, user_id, created_at, expires_at) VALUES (%s, %s, %s, %s)",
            (hash_token(token), row["id"], now.isoformat(), (now + timedelta(days=SESSION_DAYS)).isoformat()),
        )
        conn.execute("UPDATE users SET last_login_at = %s WHERE id = %s", (now.isoformat(), row["id"]))
        conn.commit()
        hub = conn.execute("SELECT name, code FROM hubs WHERE id = %s", (row["hub_id"],)).fetchone() if row["hub_id"] else None
        conn.close()
        user = {
            "id": row["id"], "username": row["username"], "role": row["role"], "hub_id": row["hub_id"],
            "view_hub_id": None, "hub_name": hub["name"] if hub else None, "hub_code": hub["code"] if hub else None,
        }
        self.send_json(200, self.me_payload(user), cookie=(token, SESSION_DAYS * 24 * 3600))

    def logout(self):
        token = self.session_token()
        if token:
            conn = get_db()
            conn.execute("DELETE FROM sessions WHERE token_hash = %s", (hash_token(token),))
            conn.commit()
            conn.close()
        self.send_json(200, {"ok": True}, cookie=("", 0))

    def set_view_hub(self):
        user = self.require_admin()
        body = self.read_json_body()
        hub_id = body.get("hubId")
        conn = get_db()
        if hub_id is not None:
            try:
                hub_id = int(hub_id)
            except (TypeError, ValueError):
                conn.close()
                raise ApiError(400, "hubId must be a number or null")
            if not conn.execute("SELECT id FROM hubs WHERE id = %s", (hub_id,)).fetchone():
                conn.close()
                raise ApiError(404, "Hub not found")
        conn.execute(
            "UPDATE sessions SET view_hub_id = %s WHERE token_hash = %s",
            (hub_id, hash_token(self.session_token())),
        )
        conn.commit()
        conn.close()
        user["view_hub_id"] = hub_id
        self.send_json(200, self.me_payload(user))

    def list_users(self):
        self.require_admin()
        conn = get_db()
        rows = conn.execute(USER_SELECT_SQL + "ORDER BY u.role, h.name NULLS LAST, u.username").fetchall()
        conn.close()
        self.send_json(200, [user_to_dict(r) for r in rows])

    def create_user(self):
        self.require_admin()
        body = self.read_json_body()
        username = normalize_username(body)
        password = validate_password(body.get("password"))
        role = body.get("role")
        if role not in ("admin", "hub"):
            raise ApiError(400, "role must be 'admin' or 'hub'")
        conn = get_db()
        hub_id = None
        if role == "hub":
            try:
                hub_id = int(body.get("hubId"))
            except (TypeError, ValueError):
                conn.close()
                raise ApiError(400, "Pick the hub this account belongs to")
            if not conn.execute("SELECT id FROM hubs WHERE id = %s", (hub_id,)).fetchone():
                conn.close()
                raise ApiError(404, "Hub not found")
            if conn.execute("SELECT id FROM users WHERE role = 'hub' AND hub_id = %s", (hub_id,)).fetchone():
                conn.close()
                raise ApiError(409, "That hub already has an account. Reset its password instead.")
        if conn.execute("SELECT id FROM users WHERE username = %s", (username,)).fetchone():
            conn.close()
            raise ApiError(409, f"Username '{username}' is already taken")
        new_id = conn.execute(
            "INSERT INTO users (username, password_hash, role, hub_id, created_at) VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (username, hash_password(password), role, hub_id, datetime.now(timezone.utc).isoformat()),
        ).fetchone()["id"]
        conn.commit()
        row = conn.execute(USER_SELECT_SQL + "WHERE u.id = %s", (new_id,)).fetchone()
        conn.close()
        self.send_json(201, user_to_dict(row))

    def update_user(self, user_id):
        admin = self.require_admin()
        body = self.read_json_body()
        conn = get_db()
        if not conn.execute("SELECT id FROM users WHERE id = %s", (user_id,)).fetchone():
            conn.close()
            raise ApiError(404, "Account not found")
        if "password" in body:
            password = validate_password(body.get("password"))
            conn.execute("UPDATE users SET password_hash = %s WHERE id = %s", (hash_password(password), user_id))
            # A reset signs the account out everywhere, so whoever had the old
            # password loses access straight away.
            conn.execute("DELETE FROM sessions WHERE user_id = %s", (user_id,))
        if "active" in body:
            active = bool(body.get("active"))
            if not active and user_id == admin["id"]:
                conn.close()
                raise ApiError(400, "You can't disable your own account")
            conn.execute("UPDATE users SET active = %s WHERE id = %s", (active, user_id))
            if not active:
                conn.execute("DELETE FROM sessions WHERE user_id = %s", (user_id,))
        conn.commit()
        row = conn.execute(USER_SELECT_SQL + "WHERE u.id = %s", (user_id,)).fetchone()
        conn.close()
        self.send_json(200, user_to_dict(row))

    # ---------- hubs ----------
    def list_hubs(self):
        # Admins' hub switcher needs every hub, so only hub accounts get a filtered list.
        user = self.require_user()
        hub_sql, params = ("", []) if user["role"] == "admin" else (" AND id = %s", [user["hub_id"]])
        conn = get_db()
        rows = conn.execute("SELECT * FROM hubs WHERE TRUE" + hub_sql + " ORDER BY name", tuple(params)).fetchall()
        conn.close()
        self.send_json(200, [hub_to_dict(r) for r in rows])

    def create_hub(self):
        self.require_admin()
        body = self.read_json_body()
        name = require_str(body, "name")
        address = require_str(body, "address")
        phone = (body.get("phone") or "").strip()
        code = (body.get("code") or "").strip().upper() or slugify_code(name)
        contact_name = (body.get("contactName") or "").strip()
        contact_email = (body.get("contactEmail") or "").strip()
        pincode = (body.get("pincode") or "").strip() or parse_pincode(address)
        conn = get_db()
        row = conn.execute(
            "INSERT INTO hubs (name, code, address, phone, contact_name, contact_email, pincode) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING *",
            (name, code, address, phone, contact_name, contact_email, pincode),
        ).fetchone()
        conn.commit()
        conn.close()
        self.send_json(201, hub_to_dict(row))

    def delete_hub(self, hub_id):
        self.require_admin()
        conn = get_db()
        row = conn.execute("SELECT id FROM hubs WHERE id = %s", (hub_id,)).fetchone()
        if not row:
            conn.close()
            raise ApiError(404, "Hub not found")
        in_use = conn.execute(
            "SELECT COUNT(*) AS count FROM shipments WHERE hub_id = %s", (hub_id,)
        ).fetchone()["count"]
        if in_use:
            conn.close()
            raise ApiError(409, f"Can't delete — {in_use} shipment(s) still reference this hub")
        if conn.execute("SELECT id FROM users WHERE hub_id = %s", (hub_id,)).fetchone():
            conn.close()
            raise ApiError(409, "Can't delete — this hub has a login account. Disable the account instead.")
        conn.execute("DELETE FROM hubs WHERE id = %s", (hub_id,))
        conn.commit()
        conn.close()
        self.send_json(200, {"id": hub_id})

    # ---------- vendors ----------
    def list_vendors(self):
        conn = get_db()
        rows = conn.execute("SELECT * FROM vendors ORDER BY name").fetchall()
        conn.close()
        self.send_json(200, [vendor_to_dict(r) for r in rows])

    def create_vendor(self):
        body = self.read_json_body()
        name = require_str(body, "name")
        address = require_str(body, "address")
        short_name = (body.get("shortName") or "").strip()
        warehouse_label = (body.get("warehouseLabel") or "").strip()
        contact_name = (body.get("contactName") or "").strip()
        contact_phone = (body.get("contactPhone") or "").strip()
        contact_email = (body.get("contactEmail") or "").strip()
        pincode = (body.get("pincode") or "").strip() or parse_pincode(address)
        conn = get_db()
        existing = conn.execute("SELECT * FROM vendors WHERE name = %s", (name,)).fetchone()
        if existing:
            conn.close()
            raise ApiError(409, f"A warehouse for '{name}' already exists")
        row = conn.execute(
            "INSERT INTO vendors (name, short_name, warehouse_label, address, contact_name, contact_phone, contact_email, pincode) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING *",
            (name, short_name, warehouse_label, address, contact_name, contact_phone, contact_email, pincode),
        ).fetchone()
        conn.commit()
        conn.close()
        self.send_json(201, vendor_to_dict(row))

    # ---------- email template (shared, applies to every future draft) ----------
    def get_email_template(self):
        conn = get_db()
        rows = conn.execute(
            "SELECT key, value FROM settings WHERE key IN ('email_subject_template', 'email_body_template')"
        ).fetchall()
        conn.close()
        values = {r["key"]: r["value"] for r in rows}
        self.send_json(
            200,
            {
                "subject": values.get("email_subject_template", DEFAULT_EMAIL_SUBJECT_TEMPLATE),
                "body": values.get("email_body_template", DEFAULT_EMAIL_BODY_TEMPLATE),
            },
        )

    def save_email_template(self):
        body = self.read_json_body()
        subject = require_str(body, "subject")
        body_text = require_str(body, "body")
        conn = get_db()
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('email_subject_template', %s) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (subject,),
        )
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('email_body_template', %s) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (body_text,),
        )
        conn.commit()
        conn.close()
        self.send_json(200, {"subject": subject, "body": body_text})

    # ---------- master items (tracking -> sku/value lookup) ----------
    def lookup_tracking(self, tracking_id):
        if not tracking_id:
            raise ApiError(400, "tracking is required")
        conn = get_db()
        row = conn.execute("SELECT * FROM master_items WHERE tracking_id = %s", (tracking_id,)).fetchone()
        conn.close()
        if not row:
            raise ApiError(404, "Tracking ID not found in database")
        self.send_json(200, master_to_dict(row))

    def import_dump(self):
        self.require_admin()
        body = self.read_json_body()
        rows = body.get("rows")
        if not isinstance(rows, list) or not rows:
            raise ApiError(400, "rows must be a non-empty list")

        # Dedupe within this dump first — last occurrence for a given tracking
        # ID wins, matching "if there's a duplicate, the new data is used".
        by_tracking = {}
        for r in rows:
            if not isinstance(r, dict):
                continue
            tracking = (r.get("trackingId") or "").strip()
            if not tracking:
                continue
            try:
                value = float(r.get("value") or 0)
            except (TypeError, ValueError):
                value = 0
            by_tracking[tracking] = {
                "trackingId": tracking,
                "marketplaceOrderId": (r.get("marketplaceOrderId") or "").strip(),
                "returnId": (r.get("returnId") or "").strip(),
                "sellerName": (r.get("sellerName") or "").strip(),
                "rtvShipmentId": (r.get("rtvShipmentId") or "").strip(),
                "tmsProviderName": (r.get("tmsProviderName") or "").strip(),
                "rtvShipmentStatus": (r.get("rtvShipmentStatus") or "").strip(),
                "rtvCreatedAt": (r.get("rtvCreatedAt") or "").strip(),
                "rtvTrackingId": (r.get("rtvTrackingId") or "").strip(),
                "value": max(value, 0),
                "orderSetId": (r.get("orderSetId") or "").strip(),
            }

        if not by_tracking:
            raise ApiError(400, "No valid rows with a Tracking ID found")

        now = datetime.now(timezone.utc).isoformat()
        conn = get_db()
        existing_ids = set(
            r["tracking_id"] for r in conn.execute("SELECT tracking_id FROM master_items").fetchall()
        )
        inserted = sum(1 for t in by_tracking if t not in existing_ids)
        updated = len(by_tracking) - inserted

        values = [
            (
                item["trackingId"], item["marketplaceOrderId"], item["returnId"], item["sellerName"],
                item["rtvShipmentId"], item["tmsProviderName"], item["rtvShipmentStatus"],
                item["rtvCreatedAt"], item["rtvTrackingId"], item["value"], item["orderSetId"], now,
            )
            for item in by_tracking.values()
        ]
        conn.execute_values(
            """
            INSERT INTO master_items (
                tracking_id, marketplace_order_id, return_id, seller_name, rtv_shipment_id,
                tms_provider_name, rtv_shipment_status, rtv_created_at, rtv_tracking_id, value,
                order_set_id, updated_at
            )
            VALUES %s
            ON CONFLICT (tracking_id) DO UPDATE SET
                marketplace_order_id=excluded.marketplace_order_id, return_id=excluded.return_id,
                seller_name=excluded.seller_name, rtv_shipment_id=excluded.rtv_shipment_id,
                tms_provider_name=excluded.tms_provider_name, rtv_shipment_status=excluded.rtv_shipment_status,
                rtv_created_at=excluded.rtv_created_at, rtv_tracking_id=excluded.rtv_tracking_id,
                value=excluded.value, order_set_id=excluded.order_set_id, updated_at=excluded.updated_at
            """,
            values,
        )
        conn.commit()
        conn.close()
        self.send_json(200, {"total": len(by_tracking), "inserted": inserted, "updated": updated})

    # ---------- shipments ----------
    def list_shipments(self):
        hub_sql, hub_params = self.hub_filter("s.hub_id")
        conn = get_db()
        rows = conn.execute(
            """
            SELECT s.id, s.invoice_no, s.ship_date, s.created_at,
                   h.name AS hub_name, v.name AS vendor_name, v.warehouse_label,
                   (SELECT COUNT(DISTINCT box_no) FROM shipment_items WHERE shipment_id = s.id) AS boxes,
                   (SELECT COUNT(*) FROM shipment_items WHERE shipment_id = s.id) AS lines,
                   (SELECT COALESCE(SUM(qty), 0) FROM shipment_items WHERE shipment_id = s.id) AS units,
                   (SELECT COALESCE(SUM(qty * value), 0) FROM shipment_items WHERE shipment_id = s.id) AS amount
            FROM shipments s
            JOIN hubs h ON h.id = s.hub_id
            JOIN vendors v ON v.id = s.vendor_id
            WHERE TRUE {hub_sql}
            ORDER BY s.id DESC
            LIMIT 200
            """.format(hub_sql=hub_sql),
            tuple(hub_params),
        ).fetchall()
        conn.close()
        out = [
            {
                "id": r["id"],
                "invoiceNo": r["invoice_no"],
                "shipDate": r["ship_date"],
                "createdAt": r["created_at"],
                "hubName": r["hub_name"],
                "vendorName": r["vendor_name"],
                "warehouseLabel": r["warehouse_label"],
                "boxes": r["boxes"],
                "lines": r["lines"],
                "units": r["units"],
                "amount": r["amount"],
            }
            for r in rows
        ]
        self.send_json(200, out)

    # Full history for the Shipment Archive tab — unlike list_shipments()
    # (capped at 200, backs the small "recent shipments" widget), this has no
    # practical cap and supports a free-text search across invoice/vendor/hub.
    def list_shipments_archive(self, q):
        hub_sql, params = self.hub_filter("s.hub_id")
        conn = get_db()
        where_sql = "WHERE TRUE" + hub_sql
        if q:
            where_sql += " AND (s.invoice_no ILIKE %s OR v.name ILIKE %s OR h.name ILIKE %s)"
            like = f"%{q}%"
            params += [like, like, like]
        rows = conn.execute(
            f"""
            SELECT s.id, s.invoice_no, s.ship_date, s.created_at,
                   h.name AS hub_name, v.name AS vendor_name, v.warehouse_label,
                   (SELECT COUNT(DISTINCT box_no) FROM shipment_items WHERE shipment_id = s.id) AS boxes,
                   (SELECT COUNT(*) FROM shipment_items WHERE shipment_id = s.id) AS lines,
                   (SELECT COALESCE(SUM(qty), 0) FROM shipment_items WHERE shipment_id = s.id) AS units,
                   (SELECT COALESCE(SUM(qty * value), 0) FROM shipment_items WHERE shipment_id = s.id) AS amount
            FROM shipments s
            JOIN hubs h ON h.id = s.hub_id
            JOIN vendors v ON v.id = s.vendor_id
            {where_sql}
            ORDER BY s.id DESC
            LIMIT 5000
            """,
            tuple(params),
        ).fetchall()
        conn.close()
        out = [
            {
                "id": r["id"],
                "invoiceNo": r["invoice_no"],
                "shipDate": r["ship_date"],
                "createdAt": r["created_at"],
                "hubName": r["hub_name"],
                "vendorName": r["vendor_name"],
                "warehouseLabel": r["warehouse_label"],
                "boxes": r["boxes"],
                "lines": r["lines"],
                "units": r["units"],
                "amount": r["amount"],
            }
            for r in rows
        ]
        self.send_json(200, out)

    def get_shipment(self, shipment_id):
        conn = get_db()
        user = self.require_user()
        s = conn.execute("SELECT * FROM shipments WHERE id = %s", (shipment_id,)).fetchone()
        if not s or (user["role"] == "hub" and s["hub_id"] != user["hub_id"]):
            conn.close()
            raise ApiError(404, "Shipment not found")
        hub = conn.execute("SELECT * FROM hubs WHERE id = %s", (s["hub_id"],)).fetchone()
        vendor = conn.execute("SELECT * FROM vendors WHERE id = %s", (s["vendor_id"],)).fetchone()
        rows = conn.execute(
            "SELECT * FROM shipment_items WHERE shipment_id = %s ORDER BY box_no, id", (shipment_id,)
        ).fetchall()
        conn.close()
        manifest = [
            {
                "tracking": r["tracking_id"],
                "mrp": r["value"],
                "qty": r["qty"],
                "boxNo": r["box_no"],
            }
            for r in rows
        ]
        self.send_json(
            200,
            {
                "id": s["id"],
                "invoiceNo": s["invoice_no"],
                "shipDate": s["ship_date"],
                "createdAt": s["created_at"],
                "hub": hub_to_dict(hub),
                "vendor": vendor_to_dict(vendor),
                "manifest": manifest,
            },
        )

    def create_shipment(self):
        user = self.require_user()
        body = self.read_json_body()
        try:
            hub_id = user["hub_id"] if user["role"] == "hub" else int(body.get("hubId"))
            vendor_id = int(body.get("vendorId"))
        except (TypeError, ValueError):
            raise ApiError(400, "hubId and vendorId are required")
        ship_date = require_str(body, "shipDate")
        items = body.get("items")
        if not isinstance(items, list) or not items:
            raise ApiError(400, "items must be a non-empty list")

        clean_rows = []
        for i, m in enumerate(items):
            if not isinstance(m, dict):
                raise ApiError(400, f"item {i + 1} is invalid")
            tracking = (m.get("tracking") or "").strip()
            try:
                value = float(m.get("mrp") or 0)
                qty = int(m.get("qty"))
                box_no = int(m.get("boxNo"))
            except (TypeError, ValueError):
                raise ApiError(400, f"item {i + 1}: value, qty and boxNo must be numbers")
            if not tracking:
                raise ApiError(400, f"item {i + 1}: tracking is required")
            if qty < 1 or box_no < 1:
                raise ApiError(400, f"item {i + 1}: qty and boxNo must be >= 1")
            clean_rows.append((tracking, max(value, 0), qty, box_no))

        conn = get_db()
        hub = conn.execute("SELECT * FROM hubs WHERE id = %s", (hub_id,)).fetchone()
        vendor = conn.execute("SELECT * FROM vendors WHERE id = %s", (vendor_id,)).fetchone()
        if not hub or not vendor:
            conn.close()
            raise ApiError(400, "hubId or vendorId does not exist")

        date_tag = ship_date.replace("-", "")
        seq = conn.execute(
            "SELECT COUNT(*) AS count FROM shipments WHERE hub_id = %s AND ship_date = %s", (hub_id, ship_date)
        ).fetchone()["count"] + 1
        invoice_no = f"RTV-{hub['code']}-{date_tag}-{seq:03d}"

        created_at = datetime.now(timezone.utc).isoformat()
        shipment_row = conn.execute(
            "INSERT INTO shipments (invoice_no, hub_id, vendor_id, ship_date, created_at) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (invoice_no, hub_id, vendor_id, ship_date, created_at),
        ).fetchone()
        shipment_id = shipment_row["id"]
        conn.executemany(
            "INSERT INTO shipment_items (shipment_id, tracking_id, value, qty, box_no) VALUES (%s, %s, %s, %s, %s)",
            [(shipment_id, *row) for row in clean_rows],
        )
        conn.commit()
        conn.close()
        self.send_json(201, {"id": shipment_id, "invoiceNo": invoice_no})

    # ---------- courier bookings (DTDC pickups bundling one or more invoices) ----------
    def list_bookable_shipments(self, q):
        conn = get_db()
        sql = (
            "SELECT s.id, s.invoice_no, s.ship_date, s.vendor_id, s.hub_id, "
            "v.name AS vendor_name, v.warehouse_label, h.name AS hub_name, "
            "(SELECT COUNT(DISTINCT box_no) FROM shipment_items WHERE shipment_id = s.id) AS boxes, "
            "(SELECT COALESCE(SUM(qty * value), 0) FROM shipment_items WHERE shipment_id = s.id) AS amount "
            "FROM shipments s JOIN vendors v ON v.id = s.vendor_id JOIN hubs h ON h.id = s.hub_id "
            "WHERE s.id NOT IN (SELECT shipment_id FROM courier_booking_shipments)"
        )
        hub_sql, params = self.hub_filter("s.hub_id")
        sql += hub_sql
        if q:
            sql += " AND (s.invoice_no ILIKE %s OR v.name ILIKE %s)"
            like = f"%{q}%"
            params.extend([like, like])
        sql += " ORDER BY s.id DESC LIMIT 50"
        rows = conn.execute(sql, tuple(params)).fetchall()
        conn.close()
        out = [
            {
                "id": r["id"],
                "invoiceNo": r["invoice_no"],
                "shipDate": r["ship_date"],
                "vendorId": r["vendor_id"],
                "vendorName": r["vendor_name"],
                "warehouseLabel": r["warehouse_label"],
                "hubId": r["hub_id"],
                "hubName": r["hub_name"],
                "boxes": r["boxes"],
                "amount": r["amount"],
            }
            for r in rows
        ]
        self.send_json(200, out)

    def list_courier_bookings(self):
        hub_sql, hub_params = self.hub_filter("b.hub_id")
        conn = get_db()
        rows = conn.execute(
            """
            SELECT b.id, b.pickup_date, b.boxes, b.declared_price, b.weight_kg, b.created_at,
                   b.destination_name, b.tracking, b.status,
                   v.name AS vendor_name, v.warehouse_label, v.pincode AS vendor_pincode,
                   v.contact_phone AS vendor_contact_phone, v.address AS vendor_address,
                   h.name AS hub_name, h.pincode AS hub_pincode, h.contact_name AS hub_contact_name,
                   h.phone AS hub_phone, h.address AS hub_address,
                   string_agg(s.invoice_no, ', ' ORDER BY s.invoice_no) AS invoice_numbers
            FROM courier_bookings b
            JOIN vendors v ON v.id = b.vendor_id
            LEFT JOIN hubs h ON h.id = b.hub_id
            JOIN courier_booking_shipments cbs ON cbs.booking_id = b.id
            JOIN shipments s ON s.id = cbs.shipment_id
            WHERE TRUE {hub_sql}
            GROUP BY b.id, v.name, v.warehouse_label, v.pincode, v.contact_phone, v.address,
                     h.name, h.pincode, h.contact_name, h.phone, h.address
            ORDER BY b.id DESC
            """.format(hub_sql=hub_sql),
            tuple(hub_params),
        ).fetchall()
        conn.close()
        self.send_json(200, [courier_booking_to_dict(r) for r in rows])

    def create_courier_booking(self):
        body = self.read_json_body()
        shipment_ids = body.get("shipmentIds")
        if not isinstance(shipment_ids, list) or not shipment_ids:
            raise ApiError(400, "shipmentIds must be a non-empty list")
        try:
            shipment_ids = [int(x) for x in shipment_ids]
        except (TypeError, ValueError):
            raise ApiError(400, "shipmentIds must be numbers")
        pickup_date = require_str(body, "pickupDate")
        destination_name = require_str(body, "destinationName")
        try:
            boxes = int(body.get("boxes"))
            declared_price = float(body.get("declaredPrice"))
            weight_kg = float(body.get("weightKg"))
        except (TypeError, ValueError):
            raise ApiError(400, "boxes, declaredPrice and weightKg must be numbers")
        if boxes < 1:
            raise ApiError(400, "boxes must be >= 1")
        if weight_kg <= 0:
            raise ApiError(400, "weightKg must be greater than 0")

        conn = get_db()
        placeholders = ",".join(["%s"] * len(shipment_ids))
        rows = conn.execute(f"SELECT * FROM shipments WHERE id IN ({placeholders})", tuple(shipment_ids)).fetchall()
        if len(rows) != len(set(shipment_ids)):
            conn.close()
            raise ApiError(400, "One or more shipmentIds don't exist")
        vendor_ids = set(r["vendor_id"] for r in rows)
        if len(vendor_ids) > 1:
            conn.close()
            raise ApiError(400, "All invoices in one booking must be for the same store")
        vendor_id = vendor_ids.pop()
        hub_ids = set(r["hub_id"] for r in rows)
        if len(hub_ids) > 1:
            conn.close()
            raise ApiError(400, "All invoices in one booking must be picked up from the same hub")
        hub_id = hub_ids.pop()
        try:
            self.check_hub_access(hub_id)
        except ApiError:
            conn.close()
            raise ApiError(400, "One or more shipmentIds don't exist")

        already_booked = conn.execute(
            f"SELECT shipment_id FROM courier_booking_shipments WHERE shipment_id IN ({placeholders})",
            tuple(shipment_ids),
        ).fetchall()
        if already_booked:
            conn.close()
            raise ApiError(409, "One or more invoices are already in another courier booking")

        created_at = datetime.now(timezone.utc).isoformat()
        booking_row = conn.execute(
            "INSERT INTO courier_bookings (vendor_id, hub_id, destination_name, pickup_date, boxes, declared_price, weight_kg, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (vendor_id, hub_id, destination_name, pickup_date, boxes, declared_price, weight_kg, created_at),
        ).fetchone()
        booking_id = booking_row["id"]
        conn.executemany(
            "INSERT INTO courier_booking_shipments (booking_id, shipment_id) VALUES (%s, %s)",
            [(booking_id, sid) for sid in shipment_ids],
        )
        conn.commit()
        conn.close()
        self.send_json(201, {"id": booking_id})

    def delete_courier_booking(self, booking_id):
        hub_sql, hub_params = self.hub_filter("hub_id")
        conn = get_db()
        row = conn.execute("SELECT id FROM courier_bookings WHERE id = %s" + hub_sql, (booking_id, *hub_params)).fetchone()
        if not row:
            conn.close()
            raise ApiError(404, "Booking not found")
        conn.execute("DELETE FROM courier_bookings WHERE id = %s", (booking_id,))
        conn.commit()
        conn.close()
        self.send_json(200, {"id": booking_id})

    def update_courier_booking(self, booking_id):
        body = self.read_json_body()
        hub_sql, hub_params = self.hub_filter("hub_id")
        conn = get_db()
        row = conn.execute("SELECT id FROM courier_bookings WHERE id = %s" + hub_sql, (booking_id, *hub_params)).fetchone()
        if not row:
            conn.close()
            raise ApiError(404, "Booking not found")

        updates = {}
        if "tracking" in body:
            updates["tracking"] = (body.get("tracking") or "").strip()
        if "status" in body:
            status = (body.get("status") or "").strip()
            if status not in COURIER_BOOKING_STATUSES:
                conn.close()
                raise ApiError(400, "status must be one of: " + ", ".join(COURIER_BOOKING_STATUSES))
            updates["status"] = status
        if not updates:
            conn.close()
            raise ApiError(400, "Nothing to update — send 'tracking' and/or 'status'")

        set_sql = ", ".join(f"{col} = %s" for col in updates)
        conn.execute(f"UPDATE courier_bookings SET {set_sql} WHERE id = %s", tuple(updates.values()) + (booking_id,))
        conn.commit()
        conn.close()
        self.send_json(200, dict(updates, id=booking_id))

    # Column order matches DTDC's own bulk-booking template exactly, so this
    # file can go straight to them without reshuffling anything.
    COURIER_EXPORT_COLUMNS = [
        ("customerReferenceNumber", "Customer Reference Number"),
        ("serviceType", "Service Type"),
        ("courierType", "Courier Type"),
        ("declaredPrice", "Declared Price (non-document)"),
        ("boxes", "Number of Pieces (non-document)"),
        ("weightKg", "Weight(KG) (non-document)"),
        ("originPincode", "Origin Pincode"),
        ("originName", "Origin Name"),
        ("originPhone", "Origin Phone"),
        ("originAddress", "Origin Address Line 1"),
        ("destinationPincode", "Destination Pincode"),
        ("destinationName", "Destination Name"),
        ("destinationPhone", "Destination Phone"),
        ("destinationAddress", "Destination Address Line 1"),
        ("contentType", "Content Type"),
        ("riskSurcharge", "Risk Surcharge (YES/NO) (non-document)"),
        ("consignmentType", "Consignment Type"),
        ("tracking", "Tracking"),
        ("codToPay", "COD/To Pay"),
        ("pickupDate", "Pick Date (Tentative)"),
    ]

    # status/q mirror the same filters the Bookings table applies on screen,
    # so "export what I'm looking at" (e.g. only "Shared with DTDC" rows)
    # does what it looks like it should. Filtered in Python, not SQL — the
    # invoice list is a string_agg across a GROUP BY, so it's simpler and
    # just as fast at this table's size to filter the already-built dicts.
    def export_courier_bookings_csv(self, status=None, q=None):
        hub_sql, hub_params = self.hub_filter("b.hub_id")
        conn = get_db()
        rows = conn.execute(
            """
            SELECT b.id, b.pickup_date, b.boxes, b.declared_price, b.weight_kg, b.created_at,
                   b.destination_name, b.tracking, b.status,
                   v.name AS vendor_name, v.warehouse_label, v.pincode AS vendor_pincode,
                   v.contact_phone AS vendor_contact_phone, v.address AS vendor_address,
                   h.name AS hub_name, h.pincode AS hub_pincode, h.contact_name AS hub_contact_name,
                   h.phone AS hub_phone, h.address AS hub_address,
                   string_agg(s.invoice_no, ', ' ORDER BY s.invoice_no) AS invoice_numbers
            FROM courier_bookings b
            JOIN vendors v ON v.id = b.vendor_id
            LEFT JOIN hubs h ON h.id = b.hub_id
            JOIN courier_booking_shipments cbs ON cbs.booking_id = b.id
            JOIN shipments s ON s.id = cbs.shipment_id
            WHERE TRUE {hub_sql}
            GROUP BY b.id, v.name, v.warehouse_label, v.pincode, v.contact_phone, v.address,
                     h.name, h.pincode, h.contact_name, h.phone, h.address
            ORDER BY b.id ASC
            """.format(hub_sql=hub_sql),
            tuple(hub_params),
        ).fetchall()
        conn.close()

        def csv_field(v):
            s = "" if v is None else str(v)
            if any(c in s for c in (',', '"', '\n')):
                s = '"' + s.replace('"', '""') + '"'
            return s

        dicts = [courier_booking_to_dict(r) for r in rows]
        if status:
            dicts = [d for d in dicts if d.get("status") == status]
        if q:
            q_lower = q.lower()
            search_fields = ("pickupDate", "vendorName", "destinationName", "invoiceNumbers", "status", "tracking")
            dicts = [
                d for d in dicts
                if any(d.get(f) and q_lower in str(d[f]).lower() for f in search_fields)
            ]

        lines = [",".join(csv_field(label) for _, label in self.COURIER_EXPORT_COLUMNS)]
        for d in dicts:
            lines.append(",".join(csv_field(d.get(key)) for key, _ in self.COURIER_EXPORT_COLUMNS))
        body = ("\r\n".join(lines) + "\r\n").encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", "attachment; filename=dtdc-courier-bookings.csv")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # Item-level: one row per scanned tracking ID within each invoice
    # bundled into a booking made on the given IST calendar day (default
    # today) — a booking's own invoice list is invoice-level, but Marketplace
    # ID only exists per tracking ID, so this has to join down to that level.
    # Shared by the "today" report, the per-booking report, and its CSV
    # download — one row per scanned tracking ID, since Marketplace ID only
    # exists at that granularity (a booking's own invoice list is coarser).
    REPORT_ROW_SQL = (
        "SELECT v.name AS vendor_name, s.invoice_no, si.tracking_id, mi.marketplace_order_id, "
        "       h.name AS hub_name, s.ship_date, b.id AS booking_id, b.pickup_date "
        "FROM courier_bookings b "
        "JOIN vendors v ON v.id = b.vendor_id "
        "LEFT JOIN hubs h ON h.id = b.hub_id "
        "JOIN courier_booking_shipments cbs ON cbs.booking_id = b.id "
        "JOIN shipments s ON s.id = cbs.shipment_id "
        "JOIN shipment_items si ON si.shipment_id = s.id "
        "LEFT JOIN master_items mi ON mi.tracking_id = si.tracking_id "
    )
    REPORT_COLUMNS = [
        ("brand", "Brand"),
        ("invoiceNo", "Invoice Number"),
        ("marketplaceOrderId", "Marketplace ID"),
        ("trackingId", "Tracking Number"),
        ("hubName", "Hub Location"),
        ("shipDate", "Ship Date"),
    ]

    def fetch_report_rows(self, where_sql, params):
        hub_sql, hub_params = self.hub_filter("b.hub_id")
        conn = get_db()
        rows = conn.execute(
            self.REPORT_ROW_SQL + where_sql + hub_sql + " ORDER BY v.name, s.invoice_no, si.tracking_id",
            tuple(params) + tuple(hub_params),
        ).fetchall()
        conn.close()
        return [
            {
                "brand": brand_prefix(r["vendor_name"]),
                "vendorName": r["vendor_name"],
                "invoiceNo": r["invoice_no"],
                "marketplaceOrderId": r["marketplace_order_id"],
                "trackingId": r["tracking_id"],
                "hubName": r["hub_name"],
                "shipDate": r["ship_date"],
                "bookingId": r["booking_id"],
                "pickupDate": r["pickup_date"],
            }
            for r in rows
        ]

    @staticmethod
    def csv_escape(v):
        s = "" if v is None else str(v)
        if any(c in s for c in (',', '"', '\n')):
            s = '"' + s.replace('"', '""') + '"'
        return s

    def send_report_csv(self, out, filename):
        lines = [",".join(self.csv_escape(label) for _, label in self.REPORT_COLUMNS)]
        for r in out:
            lines.append(",".join(self.csv_escape(r.get(key)) for key, _ in self.REPORT_COLUMNS))
        body = ("\r\n".join(lines) + "\r\n").encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", f"attachment; filename={filename}")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def get_courier_bookings_report(self, date_str):
        if not date_str:
            date_str = datetime.now(IST).strftime("%Y-%m-%d")
        try:
            day = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=IST)
        except ValueError:
            raise ApiError(400, "date must be YYYY-MM-DD")
        start_utc = day.astimezone(timezone.utc).isoformat()
        end_utc = (day + timedelta(days=1)).astimezone(timezone.utc).isoformat()
        out = self.fetch_report_rows("WHERE b.created_at >= %s AND b.created_at < %s", (start_utc, end_utc))
        self.send_json(200, {"date": date_str, "rows": out})

    def get_courier_booking_report_csv(self, booking_id):
        out = self.fetch_report_rows("WHERE b.id = %s", (booking_id,))
        if not out:
            raise ApiError(404, "Booking not found")
        self.send_report_csv(out, f"courier-booking-{booking_id}-report.csv")

    # Single entry point for "give me everything about X" — X can be a
    # courier booking id (numeric) or an invoice number. Works even for an
    # invoice that hasn't been courier-booked yet (booking comes back null).
    def courier_lookup(self, q):
        q = (q or "").strip()
        if not q:
            raise ApiError(400, "q is required")
        hub_sql, hub_params = self.hub_filter("hub_id")
        conn = get_db()

        booking_id = None
        if q.isdigit():
            row = conn.execute(
                "SELECT id FROM courier_bookings WHERE id = %s" + hub_sql, (int(q), *hub_params)
            ).fetchone()
            if row:
                booking_id = row["id"]

        if booking_id is None:
            srow = conn.execute(
                "SELECT id FROM shipments WHERE invoice_no = %s" + hub_sql, (q, *hub_params)
            ).fetchone()
            if not srow:
                conn.close()
                raise ApiError(404, f"No courier booking or invoice found for '{q}'")
            brow = conn.execute(
                "SELECT booking_id FROM courier_booking_shipments WHERE shipment_id = %s", (srow["id"],)
            ).fetchone()
            booking_id = brow["booking_id"] if brow else None

        booking = None
        if booking_id is not None:
            brow = conn.execute(
                """
                SELECT b.id, b.pickup_date, b.boxes, b.declared_price, b.weight_kg, b.created_at,
                       b.destination_name, b.tracking, b.status,
                       v.name AS vendor_name, v.warehouse_label, v.pincode AS vendor_pincode,
                       v.contact_phone AS vendor_contact_phone, v.address AS vendor_address,
                       h.name AS hub_name, h.pincode AS hub_pincode, h.contact_name AS hub_contact_name,
                       h.phone AS hub_phone, h.address AS hub_address,
                       string_agg(s.invoice_no, ', ' ORDER BY s.invoice_no) AS invoice_numbers
                FROM courier_bookings b
                JOIN vendors v ON v.id = b.vendor_id
                LEFT JOIN hubs h ON h.id = b.hub_id
                JOIN courier_booking_shipments cbs ON cbs.booking_id = b.id
                JOIN shipments s ON s.id = cbs.shipment_id
                WHERE b.id = %s
                GROUP BY b.id, v.name, v.warehouse_label, v.pincode, v.contact_phone, v.address,
                         h.name, h.pincode, h.contact_name, h.phone, h.address
                """,
                (booking_id,),
            ).fetchone()
            booking = courier_booking_to_dict(brow)
            shipment_rows = conn.execute(
                "SELECT s.id, s.invoice_no, s.ship_date, s.hub_id, s.vendor_id "
                "FROM shipments s JOIN courier_booking_shipments cbs ON cbs.shipment_id = s.id "
                "WHERE cbs.booking_id = %s ORDER BY s.invoice_no",
                (booking_id,),
            ).fetchall()
        else:
            shipment_rows = conn.execute(
                "SELECT id, invoice_no, ship_date, hub_id, vendor_id FROM shipments WHERE invoice_no = %s", (q,)
            ).fetchall()

        invoices = []
        for srow in shipment_rows:
            hub = conn.execute("SELECT * FROM hubs WHERE id = %s", (srow["hub_id"],)).fetchone()
            vendor = conn.execute("SELECT * FROM vendors WHERE id = %s", (srow["vendor_id"],)).fetchone()
            items = conn.execute(
                """
                SELECT si.tracking_id, si.value, si.qty, si.box_no, mi.marketplace_order_id,
                       mi.seller_name, mi.return_id, mi.rtv_shipment_status
                FROM shipment_items si
                LEFT JOIN master_items mi ON mi.tracking_id = si.tracking_id
                WHERE si.shipment_id = %s
                ORDER BY si.box_no, si.tracking_id
                """,
                (srow["id"],),
            ).fetchall()
            invoices.append({
                "invoiceNo": srow["invoice_no"],
                "shipDate": srow["ship_date"],
                "hub": hub_to_dict(hub) if hub else None,
                "vendor": vendor_to_dict(vendor) if vendor else None,
                "items": [
                    {
                        "trackingId": it["tracking_id"],
                        "value": it["value"],
                        "qty": it["qty"],
                        "boxNo": it["box_no"],
                        "marketplaceOrderId": it["marketplace_order_id"],
                        "sellerName": it["seller_name"],
                        "returnId": it["return_id"],
                        "rtvShipmentStatus": it["rtv_shipment_status"],
                    }
                    for it in items
                ],
            })
        conn.close()
        self.send_json(200, {"booking": booking, "invoices": invoices})


def create_admin(username):
    username = normalize_username({"username": username})
    password = getpass.getpass(f"New password for admin '{username}': ")
    if getpass.getpass("Type it again: ") != password:
        sys.exit("Passwords don't match — nothing saved.")
    validate_password(password)
    init_db()
    conn = get_db()
    if conn.execute("SELECT id FROM users WHERE username = %s", (username,)).fetchone():
        conn.close()
        sys.exit(f"Username '{username}' already exists — reset its password from the Hub accounts screen.")
    conn.execute(
        "INSERT INTO users (username, password_hash, role, created_at) VALUES (%s, %s, 'admin', %s)",
        (username, hash_password(password), datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()
    print(f"Admin '{username}' created in schema '{db_schema()}'.")


def main():
    load_dotenv()
    schema = db_schema()
    # Safety net while testing: off Render, refuse to touch the live
    # ("public") tables unless that's explicitly asked for.
    if schema == "public" and not os.environ.get("RENDER") and os.environ.get("ALLOW_PUBLIC_SCHEMA") != "1":
        sys.exit(
            "Refusing to start against the live 'public' schema from this machine. "
            "Set DB_SCHEMA=rtv_test to test, or ALLOW_PUBLIC_SCHEMA=1 if you really mean live data."
        )
    if len(sys.argv) > 1 and sys.argv[1] == "create-admin":
        if len(sys.argv) != 3:
            sys.exit("Usage: python rtv-shipment-server.py create-admin <username>")
        try:
            create_admin(sys.argv[2])
        except ApiError as e:
            sys.exit(e.message)
        return
    port = int(os.environ.get("PORT") or (sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PORT))
    init_db()
    print(f"Database schema: {schema}")
    # Threaded: browsers open idle pre-connections, which would block a
    # single-threaded server for every other user until they time out.
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print("RTV Scan-to-Pack app running:")
    print(f"  listening on 0.0.0.0:{port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
        server.shutdown()


if __name__ == "__main__":
    main()
