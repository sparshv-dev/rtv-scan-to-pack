"""
RTV Scan-to-Pack app — Python stdlib HTTP server backed by Supabase Postgres.

Separate app from server.py/public/ (different data model: tracking-id-driven
scan-to-pack instead of pasted return-ID manifests) — has its own database
(Supabase project), so the two never collide.

Local run:  DATABASE_URL=postgresql://... python rtv-shipment-server.py [port]
Hosted (Render): reads DATABASE_URL and PORT from the environment.
First admin:  python rtv-shipment-server.py create-admin <username>
"""
import base64
import csv
import getpass
import hashlib
import io
import hmac
import json
import os
import re
import secrets
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from xml.etree import ElementTree

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
        -- 'Local' (Mumbai, POD photo required on delivery) or 'Non-Local'.
        ALTER TABLE vendors ADD COLUMN IF NOT EXISTS return_location TEXT;
        -- Removed brands drop out of the brand picker but stay on the old
        -- shipments and bookings that used them.
        ALTER TABLE vendors ADD COLUMN IF NOT EXISTS active BOOLEAN NOT NULL DEFAULT TRUE;
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
        ALTER TABLE courier_bookings ADD COLUMN IF NOT EXISTS created_by TEXT;
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
        -- Daily full returns dump (every return, consolidated or not). One row
        -- per Return Tracking ID + SKU: a single return can carry several SKUs.
        -- Re-importing is an upsert, and a row only changes when the file's
        -- "Updated At" is newer than what's stored, so re-uploads and older
        -- files can never duplicate or roll back data.
        ALTER TABLE hubs ADD COLUMN IF NOT EXISTS dump_name TEXT;
        CREATE TABLE IF NOT EXISTS returns (
            return_tracking_id TEXT NOT NULL,
            sku TEXT NOT NULL DEFAULT '',
            forward_tracking_id TEXT,
            marketplace_order_id TEXT,
            return_type TEXT,
            return_reason TEXT,
            return_status TEXT,
            sale_price NUMERIC,
            hub_name TEXT,
            hub_id INTEGER REFERENCES hubs(id) ON DELETE SET NULL,
            return_created_at TIMESTAMPTZ,
            picked_up_at TIMESTAMPTZ,
            checked_in_at TIMESTAMPTZ,
            consolidated_at TIMESTAMPTZ,
            dispatched_at TIMESTAMPTZ,
            rtv_delivered_at TIMESTAMPTZ,
            source_updated_at TIMESTAMPTZ,
            rtv_shipment_status TEXT,
            seller_name TEXT,
            return_location_name TEXT,
            first_import_id INTEGER,
            last_import_id INTEGER,
            PRIMARY KEY (return_tracking_id, sku)
        );
        CREATE INDEX IF NOT EXISTS idx_returns_forward_tracking_id ON returns(forward_tracking_id);
        CREATE INDEX IF NOT EXISTS idx_returns_hub_id ON returns(hub_id);
        CREATE TABLE IF NOT EXISTS dump_imports (
            id SERIAL PRIMARY KEY,
            source TEXT NOT NULL,
            filename TEXT,
            uploaded_by TEXT,
            created_at TEXT NOT NULL,
            total_rows INTEGER NOT NULL DEFAULT 0,
            blank_rows INTEGER NOT NULL DEFAULT 0,
            invalid_rows INTEGER NOT NULL DEFAULT 0,
            duplicate_rows INTEGER NOT NULL DEFAULT 0,
            inserted INTEGER NOT NULL DEFAULT 0,
            updated INTEGER NOT NULL DEFAULT 0,
            unchanged INTEGER NOT NULL DEFAULT 0,
            stale INTEGER NOT NULL DEFAULT 0,
            unmatched_hubs TEXT,
            error TEXT
        );
        ALTER TABLE shipment_items ADD COLUMN IF NOT EXISTS sku TEXT;
        -- Pendency's second terminal (the first is being packed in the app):
        -- a return item closed by the team with a reason and remarks.
        -- Issues a store raises with the admins (Raised issues / My tickets).
        CREATE TABLE IF NOT EXISTS tickets (
            id SERIAL PRIMARY KEY,
            hub_id INTEGER REFERENCES hubs(id) ON DELETE SET NULL,
            category TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            reference TEXT,
            photo_url TEXT,
            status TEXT NOT NULL DEFAULT 'Open' CHECK (status IN ('Open', 'In progress', 'Solved')),
            admin_reply TEXT,
            raised_by TEXT NOT NULL,
            raised_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            updated_by TEXT,
            solved_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_tickets_hub_id ON tickets(hub_id);
        CREATE TABLE IF NOT EXISTS return_closures (
            return_tracking_id TEXT NOT NULL,
            sku TEXT NOT NULL DEFAULT '',
            reason TEXT NOT NULL CHECK (reason IN ('Lost', 'Rider Miss', 'Store Miss')),
            remarks TEXT NOT NULL,
            hub_id INTEGER REFERENCES hubs(id) ON DELETE SET NULL,
            closed_by TEXT NOT NULL,
            closed_at TEXT NOT NULL,
            PRIMARY KEY (return_tracking_id, sku)
        );
        -- Gate-out and delivery are per invoice: one scan of the invoice
        -- barcode dispatches every box on it.
        ALTER TABLE shipments ADD COLUMN IF NOT EXISTS dispatched_at TEXT;
        ALTER TABLE shipments ADD COLUMN IF NOT EXISTS dispatched_by TEXT;
        ALTER TABLE shipments ADD COLUMN IF NOT EXISTS delivered_on TEXT;
        ALTER TABLE shipments ADD COLUMN IF NOT EXISTS delivered_at TEXT;
        ALTER TABLE shipments ADD COLUMN IF NOT EXISTS delivered_by TEXT;
        ALTER TABLE shipments ADD COLUMN IF NOT EXISTS delivery_note TEXT;
        ALTER TABLE shipments ADD COLUMN IF NOT EXISTS pod_url TEXT;
        ALTER TABLE shipments ADD COLUMN IF NOT EXISTS pod_file_id TEXT;
        ALTER TABLE shipments ADD COLUMN IF NOT EXISTS pod_updated_at TEXT;
        ALTER TABLE shipments ADD COLUMN IF NOT EXISTS pod_updated_by TEXT;
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

    # Invoices packed before gate-out existed were never scanned out, so the
    # open-shipment lists only count invoices from this date on. Set once,
    # the first time this version starts; admins can move it.
    conn.execute(
        "INSERT INTO settings (key, value) VALUES ('dispatch_tracking_from', %s) ON CONFLICT (key) DO NOTHING",
        (datetime.now(IST).strftime("%Y-%m-%d"),),
    )
    conn.commit()

    # Fill Return location from the seller sheet for any brand that doesn't
    # have one yet — never overwrites a value someone has set by hand.
    for r in conn.execute("SELECT id, name, warehouse_label FROM vendors WHERE return_location IS NULL").fetchall():
        loc = guess_return_location(r["name"], r["warehouse_label"])
        if loc:
            conn.execute("UPDATE vendors SET return_location = %s WHERE id = %s", (loc, r["id"]))
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
        "dumpName": row.get("dump_name"),
    }


# ---------- daily returns dump ----------
# Only these columns are kept from the HMS returns export; anything else in
# the file (Pickup Due By, Arrived At, Bin Assigned At, Bin ID, ...) is
# ignored. Header matching ignores case, spaces and punctuation.
DUMP_COLUMNS = [
    # (db column, header in the file, kind)
    ("marketplace_order_id", "Marketplace Order ID", "text"),
    ("forward_tracking_id", "Forward Tracking ID", "text"),
    ("return_tracking_id", "Return Tracking ID", "text"),
    ("return_type", "Return Type", "text"),
    ("return_reason", "Return_Reason", "text"),
    ("return_status", "Return Status", "text"),
    ("sku", "SKU", "text"),
    ("sale_price", "Sale Price", "number"),
    ("hub_name", "Hub Name", "text"),
    ("return_created_at", "Return Created At", "time"),
    ("picked_up_at", "Picked Up At", "time"),
    ("checked_in_at", "Checked In At", "time"),
    ("consolidated_at", "Consolidated At", "time"),
    ("dispatched_at", "Dispatched At", "time"),
    ("rtv_delivered_at", "RTV Delivered At", "time"),
    ("source_updated_at", "Updated At", "time"),
    ("rtv_shipment_status", "rtv_shipment_status", "text"),
    ("seller_name", "Seller Name", "text"),
    ("return_location_name", "Return Location Name", "text"),
]
DUMP_REQUIRED_HEADERS = ["Return Tracking ID", "SKU", "Return Type", "Hub Name", "Return Created At", "Picked Up At", "Updated At"]
MAX_UPLOAD_BYTES = 60 * 1024 * 1024
HUB_NAME_NOISE = {"zilo", "hub", "return", "returns", "warehouse", "the", "store"}


def norm_header(h):
    return re.sub(r"[^a-z0-9]", "", (h or "").lower())


def hub_tokens(name):
    return {w for w in re.findall(r"[a-z]+", (name or "").lower()) if w not in HUB_NAME_NOISE}


def parse_time(v):
    v = (v or "").strip()
    if not v:
        return None
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)  # the HMS export is IST
    return dt


def parse_number(v):
    v = re.sub(r"[^0-9.\-]", "", v or "")
    try:
        return float(v) if v else None
    except ValueError:
        return None


def xlsx_rows(data):
    """First worksheet of an .xlsx as lists of strings — stdlib only, so the
    upload can be a spreadsheet as well as a CSV."""
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            root = ElementTree.fromstring(z.read("xl/sharedStrings.xml"))
            for si in root.findall("m:si", ns):
                shared.append("".join(t.text or "" for t in si.iter("{%s}t" % ns["m"])))
        sheets = sorted(n for n in z.namelist() if re.match(r"xl/worksheets/sheet\d+\.xml$", n))
        if not sheets:
            raise ApiError(400, "That spreadsheet has no sheets")
        root = ElementTree.fromstring(z.read(sheets[0]))
    rows = []
    for row in root.iter("{%s}row" % ns["m"]):
        out = {}
        for c in row.findall("m:c", ns):
            ref = c.get("r", "")
            letters = re.match(r"[A-Z]+", ref)
            col = 0
            for ch in (letters.group(0) if letters else ""):
                col = col * 26 + (ord(ch) - 64)
            col = col - 1 if col else len(out)
            t = c.get("t")
            v = c.find("m:v", ns)
            if t == "s" and v is not None:
                val = shared[int(v.text)]
            elif t == "inlineStr":
                val = "".join(x.text or "" for x in c.iter("{%s}t" % ns["m"]))
            else:
                val = v.text if v is not None and v.text is not None else ""
            out[col] = val
        rows.append([out.get(i, "") for i in range(max(out) + 1)] if out else [])
    return rows


def file_rows(data, filename):
    if data[:2] == b"PK" or (filename or "").lower().endswith(".xlsx"):
        try:
            return xlsx_rows(data)
        except zipfile.BadZipFile:
            raise ApiError(400, "That file looks like a spreadsheet but couldn't be opened")
    for enc in ("utf-8-sig", "cp1252"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    return list(csv.reader(io.StringIO(text)))


def prepare_dump(rows):
    """Header check, column pick, blank/invalid skip and in-file de-duplication.
    Returns (records keyed by (return_tracking_id, sku), stats)."""
    if not rows:
        raise ApiError(400, "The file is empty")
    header = [norm_header(h) for h in rows[0]]
    index = {}
    for col, label, _kind in DUMP_COLUMNS:
        key = norm_header(label)
        if key in header:
            index[col] = header.index(key)
    missing = [h for h in DUMP_REQUIRED_HEADERS if norm_header(h) not in header]
    if missing:
        raise ApiError(400, "This doesn't look like the returns export — missing column(s): " + ", ".join(missing))

    stats = {"total_rows": 0, "blank_rows": 0, "invalid_rows": 0, "duplicate_rows": 0}
    records = {}
    for raw in rows[1:]:
        stats["total_rows"] += 1
        if not any((c or "").strip() for c in raw):
            stats["blank_rows"] += 1
            continue
        rec = {}
        for col, _label, kind in DUMP_COLUMNS:
            i = index.get(col)
            v = (raw[i] if i is not None and i < len(raw) else "").strip()
            rec[col] = parse_time(v) if kind == "time" else parse_number(v) if kind == "number" else (v or None)
        if not rec["return_tracking_id"]:
            stats["invalid_rows"] += 1
            continue
        rec["sku"] = rec["sku"] or ""
        key = (rec["return_tracking_id"], rec["sku"])
        prev = records.get(key)
        if prev is not None:
            stats["duplicate_rows"] += 1
            # Same item twice in one file: keep the most recently updated
            # copy (a later row wins a tie).
            prev_t, new_t = prev["source_updated_at"], rec["source_updated_at"]
            if prev_t is not None and (new_t is None or new_t < prev_t):
                continue
        records[key] = rec
    return records, stats


# Seller -> brands -> movement, from the ops sheet ("Seller / Brand /
# Movement"). Brand records are mostly one per store ("ARROW-InfiMala",
# "JackJone-PhoeMark"), so each is matched by its company (warehouse label)
# or by the brand at the start of its name. Extra spellings seen in the
# saved records are listed alongside the sheet's brands.
RETURN_LOCATION_RULES = [
    ("Aditya Birla Lifestyle Brands Limited", ["Allen Solly", "Van Huesen", "Van Heusen", "Louis Phillipe", "Louis Philippe", "Peter England", "LP", "PE"], "Non-Local"),
    ("Ochre and Black", ["AND", "GD", "Globaldesi", "Global Desi"], "Local"),
    ("ARVIND FASHIONS LIMITED", ["Arrow"], "Non-Local"),
    ("ARVIND LIFESTYLE BRANDS LIMITED", ["USPA"], "Non-Local"),
    ("ARVIND YOUTH BRANDS PRIVATE LIMITED", ["Flying Machine"], "Non-Local"),
    ("Bear House Clothing Private Limited", ["The Bear House"], "Local"),
    ("Biba Fashion Limited", ["Biba"], "Non-Local"),
    ("Inc. 5 Shoes Private Limited", ["Inc5"], "Local"),
    ("Best United Ltd", ["Jack & Jones", "JackJone", "Jack and Jones Junior"], "Local"),
    ("SELECTED RETAIL PRIVATE LIMITED", ["Selected", "Selected Home", "SeleHomm"], "Local"),
    ("ONLY RETAIL PRIVATE LIMITED", ["Only"], "Local"),
    ("VERO MODA RETAIL PRIVATE LIMITED", ["Vero Moda"], "Local"),
    ("Just in Time", ["Just in Time"], "Local"),
    ("Metro Brands Limited", ["Metro", "Mochi", "Fila", "New Era"], "Local"),
    ("Neemans Private Limited", ["Neeman's"], "Local"),
    ("Nishorama", ["Nishorama"], "Local"),
    ("Pepe Jeans India Limited", ["Pepe"], "Local"),
    ("VAMAS FASHION PRIVATE LIMITED", ["Vamas"], "Local"),
    ("SOCH APPARELS PVT LTD", ["SOCH"], "Non-Local"),
    ("Iconic Group", ["GANT", "True Religion"], "Non-Local"),
    ("RADHAMANI TEXTILE PVT LTD", ["Rare Rabbit", "Rareism"], "Non-Local"),
    ("Chumbak Design Private Limited", ["Chumbak", "Teal by Chumbak"], "Local"),
    ("Sooperbuy Private Limited", ["Puma"], "Local"),
    ("REGAL BRANDS PRIVATE LIMITED", ["Regal", "Rocia", "Imperio", "Ragal"], "Local"),
    ("BATA", ["Bata Group of Brands", "Bata"], "Non-Local"),
    # Not on the ops sheet; confirmed Local separately.
    ("Fabindia Limited", ["Fabindia"], "Local"),
    ("Kraus Casuals Private Limited", ["Kraus Jeans"], "Local"),
    ("Me N Moms Private Limited", ["MeeMee"], "Local"),
    ("Estele Accessories Private Limited", ["Estele"], "Local"),
    ("Dhnishk Venture Private Limited", ["Freyaa"], "Local"),
    ("One Life Creations", ["KidOne"], "Local"),
    ("Zivore Apparels Pvt. Ltd.", ["Libas"], "Local"),
    ("Crocs", ["Crocs"], "Local"),
    ("Phoenix Palladium Mall", ["Phoenix Palladium Mall"], "Local"),
    ("Aditya Birla Fashion and Retail", ["W", "Wishful"], "Non-Local"),  # Gurugram / Bangalore
    ("American Eagle", ["American Eagle"], "Non-Local"),  # Bangalore
]
RETURN_LOCATIONS = ("Local", "Non-Local")
_COMPANY_NOISE = {"private", "pvt", "limited", "ltd", "the", "india", "group", "of", "brands", "and", "co"}


def _company_key(s):
    return "".join(w for w in re.findall(r"[a-z0-9]+", (s or "").lower()) if w not in _COMPANY_NOISE)


def _brand_key(s):
    # "and"/"the" are filler between words ("Jack and Jones"), but a leading
    # AND is the brand itself.
    words = re.findall(r"[a-z0-9]+", (s or "").lower().replace("&", " and "))
    return "".join(w for i, w in enumerate(words) if w != "the" and (i == 0 or w != "and"))


def _common_prefix(a, b):
    n = 0
    while n < min(len(a), len(b)) and a[n] == b[n]:
        n += 1
    return n


def guess_return_location(name, warehouse_label):
    """'Local' / 'Non-Local' when exactly one movement fits, else None."""
    label, name_key = _company_key(warehouse_label), _company_key(name)
    prefix = _brand_key(re.split(r"[-(]", name or "")[0])
    words = re.findall(r"[a-z0-9]+", (name or "").lower())
    first = words[0] if words else ""
    full = _brand_key(name)
    found = set()
    for seller, brands, movement in RETURN_LOCATION_RULES:
        sk = _company_key(seller)
        if sk and (sk in label or (len(label) > 5 and label in sk) or sk in name_key):
            found.add(movement)
            continue
        for b in (_brand_key(x) for x in brands):
            if not b or not prefix:
                continue
            if ((len(b) <= 3 and (prefix == b or first == b))
                    or (len(prefix) >= 3 and len(b) > 3 and (b.startswith(prefix) or prefix.startswith(b)))
                    or (len(b) > 5 and _common_prefix(prefix, b) >= 6)
                    or (len(b) >= 6 and b in full)):
                found.add(movement)
                break
    return found.pop() if len(found) == 1 else None


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
        "returnLocation": row.get("return_location"),
        "active": row.get("active", True),
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
# hand in sync. A hub raises a booking as Requested; an admin shares it with
# DTDC; gate-out and delivery of every invoice on it move it to Picked up /
# Delivered automatically (see sync_booking_status).
COURIER_BOOKING_STATUSES = [
    "Requested",
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
        "createdBy": row.get("created_by"),
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


MAX_POD_BYTES = 12 * 1024 * 1024
CLOSURE_REASONS = ("Lost", "Rider Miss", "Store Miss")
TICKET_CATEGORIES = (
    "Packing / scanning", "Daily data", "Gate-out", "Delivery / POD", "Courier booking",
    "Pending to Scan / closures", "Login / account", "Other",
)
TICKET_STATUSES = ("Open", "In progress", "Solved")


def ticket_to_dict(r):
    return {
        "id": r["id"], "number": f"TKT-{r['id']:05d}", "hubId": r["hub_id"], "hub": r["hub_name"] or "—",
        "category": r["category"], "title": r["title"], "description": r["description"], "reference": r["reference"],
        "photoUrl": r["photo_url"], "status": r["status"], "adminReply": r["admin_reply"],
        "raisedBy": r["raised_by"], "raisedAt": r["raised_at"], "updatedAt": r["updated_at"],
        "updatedBy": r["updated_by"], "solvedAt": r["solved_at"],
    }


TICKET_SELECT_SQL = "SELECT t.*, h.name AS hub_name FROM tickets t LEFT JOIN hubs h ON h.id = t.hub_id "


def shipment_status(row):
    if row["delivered_at"]:
        return "Delivered"
    if row["dispatched_at"]:
        return "Dispatched"
    return "Packed"


# Shared SELECT for every list that shows invoice-level dispatch status.
SHIPMENT_LIST_SQL = """
    SELECT s.id, s.invoice_no, s.ship_date, s.created_at, s.hub_id,
           s.dispatched_at, s.dispatched_by, s.delivered_on, s.delivered_at, s.delivered_by,
           s.delivery_note, s.pod_url, s.pod_updated_at, s.pod_updated_by,
           h.name AS hub_name, v.name AS vendor_name, v.warehouse_label, v.return_location,
           (SELECT COUNT(DISTINCT box_no) FROM shipment_items WHERE shipment_id = s.id) AS boxes,
           (SELECT COUNT(*) FROM shipment_items WHERE shipment_id = s.id) AS lines,
           (SELECT COALESCE(SUM(qty), 0) FROM shipment_items WHERE shipment_id = s.id) AS units,
           (SELECT COALESCE(SUM(qty * value), 0) FROM shipment_items WHERE shipment_id = s.id) AS amount
    FROM shipments s
    JOIN hubs h ON h.id = s.hub_id
    JOIN vendors v ON v.id = s.vendor_id
"""


def shipment_list_dict(r):
    return {
        "id": r["id"],
        "invoiceNo": r["invoice_no"],
        "shipDate": r["ship_date"],
        "createdAt": r["created_at"],
        "hubName": r["hub_name"],
        "vendorName": r["vendor_name"],
        "warehouseLabel": r["warehouse_label"],
        "returnLocation": r["return_location"],
        "boxes": r["boxes"],
        "lines": r["lines"],
        "units": r["units"],
        "amount": r["amount"],
        "status": shipment_status(r),
        "dispatchedAt": r["dispatched_at"],
        "dispatchedBy": r["dispatched_by"],
        "deliveredOn": r["delivered_on"],
        "deliveredAt": r["delivered_at"],
        "deliveredBy": r["delivered_by"],
        "deliveryNote": r["delivery_note"],
        "podUrl": r["pod_url"],
        "podUpdatedAt": r["pod_updated_at"],
        "podUpdatedBy": r["pod_updated_by"],
    }


def upload_pod_to_drive(data, mime_type, filename, month):
    """Send a POD photo to the Drive Apps Script (apps-script/pod-upload.gs);
    returns (file_id, url)."""
    url = (os.environ.get("POD_UPLOAD_URL") or "").strip()
    secret = (os.environ.get("POD_UPLOAD_SECRET") or "").strip()
    if not url or not secret:
        raise ApiError(503, "Photo upload isn't set up on the server yet (POD_UPLOAD_URL / POD_UPLOAD_SECRET)")
    body = json.dumps({
        "secret": secret, "filename": filename, "mimeType": mime_type, "month": month,
        "dataBase64": base64.b64encode(data).decode("ascii"),
    }).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": "application/json"})
    try:
        # Apps Script answers with a redirect to the result, which urllib follows.
        with urllib.request.urlopen(req, timeout=90) as res:
            reply = json.loads(res.read())
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        raise ApiError(502, f"Couldn't save the photo to Google Drive: {e}")
    if not reply.get("ok"):
        raise ApiError(502, "Google Drive refused the photo: " + str(reply.get("error")))
    return reply.get("id"), reply.get("url")


def sync_booking_status(conn, shipment_id):
    """After a gate-out, undo or delivery: bring the courier booking holding
    this invoice in line. Every invoice delivered -> Delivered to Brand;
    every invoice dispatched -> Picked up from store; a pickup undone ->
    back to Shared with DTDC. Manual statuses further along are kept."""
    b = conn.execute(
        """
        SELECT b.id, b.status,
               bool_and(s.dispatched_at IS NOT NULL) AS all_dispatched,
               bool_and(s.delivered_at IS NOT NULL) AS all_delivered
        FROM courier_booking_shipments x
        JOIN courier_bookings b ON b.id = x.booking_id
        JOIN courier_booking_shipments y ON y.booking_id = b.id
        JOIN shipments s ON s.id = y.shipment_id
        WHERE x.shipment_id = %s
        GROUP BY b.id, b.status
        """,
        (shipment_id,),
    ).fetchone()
    if not b:
        return
    status = b["status"]
    if b["all_delivered"]:
        new = "Delivered to Brand"
    elif b["all_dispatched"]:
        new = status if status in ("Picked up from store", "In Transit") else "Picked up from store"
    elif status == "Picked up from store":
        new = "Shared with DTDC"
    else:
        new = status
    if new != status:
        conn.execute("UPDATE courier_bookings SET status = %s WHERE id = %s", (new, b["id"]))


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
            if path == "/api/dump/imports":
                return self.list_dump_imports()
            if path == "/api/hubs":
                return self.list_hubs()
            if path == "/api/vendors":
                return self.list_vendors()
            if path == "/api/lookup":
                tracking = (qs.get("tracking") or [""])[0].strip()
                return self.lookup_tracking(tracking)
            if path == "/api/pendency":
                return self.list_pendency()
            if path == "/api/dashboard":
                return self.dashboard()
            if path == "/api/tickets":
                return self.list_tickets()
            if path == "/api/tickets/open-count":
                return self.open_tickets_count()
            if path == "/api/pendency/closed":
                return self.list_closed_returns()
            if path == "/api/shipments/open":
                return self.list_open_shipments()
            if path == "/api/shipments/to-mark-delivered":
                return self.list_to_mark_delivered()
            if path == "/api/settings/tracking-from":
                return self.send_json(200, {"trackingFrom": self.tracking_from()})
            if path == "/api/shipments/by-status":
                return self.list_shipments_by_status(
                    (qs.get("status") or [""])[0].strip(), (qs.get("q") or [""])[0].strip(),
                    (qs.get("since") or [""])[0].strip())
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
            if path == "/api/courier-bookings/requested-count":
                return self.requested_bookings_count()
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
            if path == "/api/dump/upload":
                return self.upload_dump()  # reads the file before checking sign-in, see upload_dump
            if path == "/api/login":
                return self.login()
            if path == "/api/logout":
                return self.logout()
            self.require_user()
            if path == "/api/view-hub":
                return self.set_view_hub()
            if path == "/api/gate-out":
                return self.gate_out()
            if path == "/api/tickets":
                return self.create_ticket()
            m = re.match(r"^/api/tickets/(\d+)/status$", path)
            if m:
                return self.update_ticket_status(int(m.group(1)))
            if path == "/api/pendency/close":
                return self.close_returns()
            if path == "/api/pendency/reopen":
                return self.reopen_returns()
            if path == "/api/settings/tracking-from":
                return self.set_tracking_from()
            m = re.match(r"^/api/shipments/(\d+)/undo-gate-out$", path)
            if m:
                return self.undo_gate_out(int(m.group(1)))
            m = re.match(r"^/api/shipments/(\d+)/pod$", path)
            if m:
                return self.edit_pod(int(m.group(1)))
            m = re.match(r"^/api/shipments/(\d+)/deliver$", path)
            if m:
                return self.mark_delivered(int(m.group(1)))
            if path == "/api/users":
                return self.create_user()
            if path == "/api/hubs":
                return self.create_hub()
            if path == "/api/vendors":
                return self.create_vendor()
            if path == "/api/shipments":
                return self.create_shipment()
            if path == "/api/email-template":
                return self.save_email_template()
            if path == "/api/courier-bookings":
                return self.create_courier_booking()
            if path == "/api/courier-bookings/share":
                return self.share_courier_bookings()
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
            m = re.match(r"^/api/hubs/(\d+)$", path)
            if m:
                return self.update_hub(int(m.group(1)))
            m = re.match(r"^/api/vendors/(\d+)$", path)
            if m:
                return self.update_vendor(int(m.group(1)))
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
        return_location = (body.get("returnLocation") or "").strip() or guess_return_location(name, warehouse_label)
        if return_location and return_location not in RETURN_LOCATIONS:
            raise ApiError(400, "returnLocation must be 'Local' or 'Non-Local'")
        conn = get_db()
        existing = conn.execute("SELECT * FROM vendors WHERE name = %s", (name,)).fetchone()
        if existing:
            conn.close()
            raise ApiError(409, f"A warehouse for '{name}' already exists")
        row = conn.execute(
            "INSERT INTO vendors (name, short_name, warehouse_label, address, contact_name, contact_phone, contact_email, pincode, return_location) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *",
            (name, short_name, warehouse_label, address, contact_name, contact_phone, contact_email, pincode, return_location),
        ).fetchone()
        conn.commit()
        conn.close()
        self.send_json(201, vendor_to_dict(row))

    def update_vendor(self, vendor_id):
        self.require_admin()
        body = self.read_json_body()
        updates = {}
        if "returnLocation" in body:
            loc = (body.get("returnLocation") or "").strip() or None
            if loc and loc not in RETURN_LOCATIONS:
                raise ApiError(400, "returnLocation must be 'Local' or 'Non-Local'")
            updates["return_location"] = loc
        if "active" in body:
            updates["active"] = bool(body.get("active"))
        if not updates:
            raise ApiError(400, "Nothing to update — send 'returnLocation' and/or 'active'")
        conn = get_db()
        set_sql = ", ".join(f"{c} = %s" for c in updates)
        row = conn.execute(f"UPDATE vendors SET {set_sql} WHERE id = %s RETURNING *", (*updates.values(), vendor_id)).fetchone()
        conn.commit()
        conn.close()
        if not row:
            raise ApiError(404, "Brand not found")
        self.send_json(200, vendor_to_dict(row))

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
        rows = conn.execute(
            "SELECT r.*, h.name AS hub_display, c.reason AS closed_reason, c.closed_at, c.closed_by "
            "FROM returns r LEFT JOIN hubs h ON h.id = r.hub_id "
            "LEFT JOIN return_closures c ON c.return_tracking_id = r.return_tracking_id AND c.sku = r.sku "
            "WHERE r.return_tracking_id = %s ORDER BY r.sku",
            (tracking_id,),
        ).fetchall()
        if rows:
            conn.close()
            return self.send_json(200, {
                "kind": "return",
                "trackingId": tracking_id,
                "items": [
                    {
                        "sku": r["sku"], "value": float(r["sale_price"] or 0), "brand": r["seller_name"],
                        "returnType": r["return_type"], "returnStatus": r["return_status"],
                        "hubId": r["hub_id"], "hubName": r["hub_display"] or r["hub_name"],
                        "closedReason": r["closed_reason"], "closedAt": r["closed_at"], "closedBy": r["closed_by"],
                    }
                    for r in rows
                ],
            })
        fwd = conn.execute(
            "SELECT DISTINCT return_tracking_id FROM returns WHERE forward_tracking_id = %s ORDER BY 1", (tracking_id,)
        ).fetchall()
        if fwd:
            conn.close()
            ids = [r["return_tracking_id"] for r in fwd]
            return self.send_json(409, {
                "kind": "forward",
                "error": "That's a Forward Tracking ID. Scan the Return Tracking ID on the item instead.",
                "returnIds": ids,
            })
        # Items from before the full daily dump existed still resolve from the
        # old tracking dump.
        row = conn.execute("SELECT * FROM master_items WHERE tracking_id = %s", (tracking_id,)).fetchone()
        conn.close()
        if not row:
            raise ApiError(404, "Tracking ID not found in database")
        self.send_json(200, {"kind": "legacy", "trackingId": tracking_id,
                             "items": [{"sku": None, "value": row["value"], "brand": row["seller_name"]}]})

    def read_upload(self):
        """The uploaded file's bytes (sent as the raw request body) and its name."""
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length <= 0:
            raise ApiError(400, "No file received")
        if length > MAX_UPLOAD_BYTES:
            raise ApiError(413, "File is too large (max 60 MB)")
        return self.rfile.read(length), self.headers.get("X-Filename") or ""

    def upload_dump(self):
        # Read the body before any auth answer, so a refused client gets the
        # error message instead of a connection reset mid-upload.
        data, filename = self.read_upload()
        user = self.require_admin()
        source, uploaded_by = "manual", user["username"]

        records, stats = prepare_dump(file_rows(data, filename))
        if not records:
            raise ApiError(400, "No rows with a Return Tracking ID found in that file")

        conn = get_db()
        hubs = conn.execute("SELECT id, name, dump_name FROM hubs").fetchall()
        hub_map, unmatched = {}, []
        for name in sorted({r["hub_name"] for r in records.values() if r["hub_name"]}):
            hub = next((h for h in hubs if (h["dump_name"] or "").lower() == name.lower()), None)
            if hub is None:
                want = hub_tokens(name)
                cands = [h for h in hubs if not h["dump_name"] and want and want <= hub_tokens(h["name"])]
                if len(cands) == 1:
                    hub = cands[0]
                    conn.execute("UPDATE hubs SET dump_name = %s WHERE id = %s", (name, hub["id"]))
                    hub["dump_name"] = name
            if hub is None:
                unmatched.append(name)
            else:
                hub_map[name] = hub["id"]

        import_id = conn.execute(
            "INSERT INTO dump_imports (source, filename, uploaded_by, created_at) VALUES (%s, %s, %s, %s) RETURNING id",
            (source, filename or None, uploaded_by, datetime.now(timezone.utc).isoformat()),
        ).fetchone()["id"]

        cols = [c for c, _l, _k in DUMP_COLUMNS] + ["hub_id"]
        buf = io.StringIO()
        writer = csv.writer(buf)
        for r in records.values():
            row = []
            for c in cols:
                v = hub_map.get(r["hub_name"]) if c == "hub_id" else r[c]
                row.append("" if v is None else v.isoformat() if isinstance(v, datetime) else v)
            writer.writerow(row)
        buf.seek(0)

        conn.execute("CREATE TEMP TABLE incoming (LIKE returns INCLUDING DEFAULTS) ON COMMIT DROP")
        cur = conn._conn.cursor()
        cur.copy_expert(
            f"COPY incoming ({', '.join(cols)}) FROM STDIN WITH (FORMAT csv, FORCE_NOT_NULL (return_tracking_id, sku))",
            buf,
        )
        counts = conn.execute(
            """
            SELECT
              COUNT(*) FILTER (WHERE r.return_tracking_id IS NULL) AS inserted,
              COUNT(*) FILTER (WHERE r.return_tracking_id IS NOT NULL AND
                  (r.source_updated_at IS NULL OR i.source_updated_at > r.source_updated_at)) AS updated,
              COUNT(*) FILTER (WHERE r.return_tracking_id IS NOT NULL AND
                  i.source_updated_at IS NOT DISTINCT FROM r.source_updated_at) AS unchanged,
              COUNT(*) FILTER (WHERE r.return_tracking_id IS NOT NULL AND
                  r.source_updated_at IS NOT NULL AND (i.source_updated_at IS NULL OR i.source_updated_at < r.source_updated_at)) AS stale
            FROM incoming i
            LEFT JOIN returns r ON r.return_tracking_id = i.return_tracking_id AND r.sku = i.sku
            """
        ).fetchone()
        set_cols = [c for c in cols if c not in ("return_tracking_id", "sku")]
        conn.execute(
            f"""
            INSERT INTO returns ({', '.join(cols)}, first_import_id, last_import_id)
            SELECT {', '.join(cols)}, %s, %s FROM incoming
            ON CONFLICT (return_tracking_id, sku) DO UPDATE SET
              {', '.join(f"{c} = excluded.{c}" for c in set_cols)}, last_import_id = excluded.last_import_id
            WHERE returns.source_updated_at IS NULL OR excluded.source_updated_at > returns.source_updated_at
            """,
            (import_id, import_id),
        )
        result = dict(stats, **{k: counts[k] for k in ("inserted", "updated", "unchanged", "stale")})
        conn.execute(
            """
            UPDATE dump_imports SET total_rows = %s, blank_rows = %s, invalid_rows = %s, duplicate_rows = %s,
                   inserted = %s, updated = %s, unchanged = %s, stale = %s, unmatched_hubs = %s
            WHERE id = %s
            """,
            (result["total_rows"], result["blank_rows"], result["invalid_rows"], result["duplicate_rows"],
             result["inserted"], result["updated"], result["unchanged"], result["stale"],
             ", ".join(unmatched) or None, import_id),
        )
        conn.commit()
        conn.close()
        self.send_json(200, dict(result, id=import_id, source=source, filename=filename, unmatchedHubs=unmatched))

    def list_dump_imports(self):
        self.require_admin()
        conn = get_db()
        rows = conn.execute("SELECT * FROM dump_imports ORDER BY id DESC LIMIT 50").fetchall()
        total = conn.execute("SELECT COUNT(*) AS n FROM returns").fetchone()["n"]
        no_hub = conn.execute(
            "SELECT hub_name, COUNT(*) AS n FROM returns WHERE hub_id IS NULL GROUP BY hub_name ORDER BY 2 DESC"
        ).fetchall()
        conn.close()
        self.send_json(200, {
            "totalReturns": total,
            "returnsWithoutHub": [{"hubName": r["hub_name"], "count": r["n"]} for r in no_hub],
            "imports": [
                {
                    "id": r["id"], "source": r["source"], "filename": r["filename"], "uploadedBy": r["uploaded_by"],
                    "createdAt": r["created_at"], "totalRows": r["total_rows"], "blankRows": r["blank_rows"],
                    "invalidRows": r["invalid_rows"], "duplicateRows": r["duplicate_rows"], "inserted": r["inserted"],
                    "updated": r["updated"], "unchanged": r["unchanged"], "stale": r["stale"],
                    "unmatchedHubs": r["unmatched_hubs"],
                }
                for r in rows
            ],
        })

    def update_hub(self, hub_id):
        self.require_admin()
        body = self.read_json_body()
        if "dumpName" not in body:
            raise ApiError(400, "Nothing to update — send 'dumpName'")
        dump_name = (body.get("dumpName") or "").strip() or None
        conn = get_db()
        row = conn.execute("UPDATE hubs SET dump_name = %s WHERE id = %s RETURNING *", (dump_name, hub_id)).fetchone()
        if not row:
            conn.close()
            raise ApiError(404, "Hub not found")
        if dump_name:
            # Back-fill returns already imported under that name.
            conn.execute("UPDATE returns SET hub_id = %s WHERE lower(hub_name) = lower(%s)", (hub_id, dump_name))
        conn.commit()
        conn.close()
        self.send_json(200, hub_to_dict(row))

    # ---------- shipments ----------
    # Full history for the Shipment Archive tab — no practical cap, with
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
        rows = conn.execute(SHIPMENT_LIST_SQL + where_sql + " ORDER BY s.id DESC LIMIT 5000", tuple(params)).fetchall()
        conn.close()
        self.send_json(200, [shipment_list_dict(r) for r in rows])

    # status: packed | dispatched | delivered. since (YYYY-MM-DD, IST) limits
    # to invoices dispatched/delivered on or after that day.
    def list_shipments_by_status(self, status, q, since):
        conds = {
            "packed": "s.dispatched_at IS NULL",
            "dispatched": "s.dispatched_at IS NOT NULL AND s.delivered_at IS NULL",
            "delivered": "s.delivered_at IS NOT NULL",
        }
        if status not in conds:
            raise ApiError(400, "status must be packed, dispatched or delivered")
        hub_sql, params = self.hub_filter("s.hub_id")
        where_sql = "WHERE " + conds[status] + hub_sql
        if since:
            try:
                day = datetime.strptime(since, "%Y-%m-%d").replace(tzinfo=IST)
            except ValueError:
                raise ApiError(400, "since must be YYYY-MM-DD")
            col = "s.delivered_at" if status == "delivered" else "s.dispatched_at" if status == "dispatched" else "s.created_at"
            where_sql += f" AND {col} >= %s"
            params.append(day.astimezone(timezone.utc).isoformat())
        if q:
            where_sql += " AND (s.invoice_no ILIKE %s OR v.name ILIKE %s)"
            params += [f"%{q}%", f"%{q}%"]
        order = {"packed": "s.id", "dispatched": "s.dispatched_at", "delivered": "s.delivered_at"}[status]
        conn = get_db()
        rows = conn.execute(SHIPMENT_LIST_SQL + where_sql + f" ORDER BY {order} DESC LIMIT 2000", tuple(params)).fetchall()
        conn.close()
        self.send_json(200, [shipment_list_dict(r) for r in rows])

    def tracking_from(self):
        conn = get_db()
        row = conn.execute("SELECT value FROM settings WHERE key = 'dispatch_tracking_from'").fetchone()
        conn.close()
        return row["value"] if row else None

    def set_tracking_from(self):
        self.require_admin()
        value = require_str(self.read_json_body(), "trackingFrom")
        try:
            datetime.strptime(value, "%Y-%m-%d")
        except ValueError:
            raise ApiError(400, "trackingFrom must be YYYY-MM-DD")
        conn = get_db()
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('dispatch_tracking_from', %s) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (value,),
        )
        conn.commit()
        conn.close()
        self.send_json(200, {"trackingFrom": value})

    # Returns in the daily data that are at the hub and still open. A return
    # leaves pendency only two ways: it's packed in the app, or the team closes
    # it (Lost / Rider Miss / Store Miss). The clock starts at Return Created
    # At for RTO / Home Trial returns and at Picked Up At for everything else
    # (no pickup yet = not at the hub, so not pending).
    PENDENCY_SQL = """
        WITH r AS (
            SELECT r.*,
                   CASE WHEN r.return_type ILIKE 'rto%%' OR r.return_type ILIKE '%%home trial%%'
                        THEN r.return_created_at ELSE r.picked_up_at END AS clock_start,
                   CASE WHEN r.return_type ILIKE 'rto%%' OR r.return_type ILIKE '%%home trial%%'
                        THEN 'Return Created At' ELSE 'Picked Up At' END AS clock_column
            FROM returns r
            WHERE NOT EXISTS (
                  SELECT 1 FROM return_closures c
                  WHERE c.return_tracking_id = r.return_tracking_id AND c.sku = r.sku
              )
              AND NOT EXISTS (
                  SELECT 1 FROM shipment_items si
                  WHERE si.tracking_id = r.return_tracking_id AND (si.sku IS NULL OR si.sku = r.sku)
              )
        )
        SELECT r.return_tracking_id, r.sku, r.forward_tracking_id, r.return_type, r.return_status,
               r.rtv_shipment_status, r.sale_price, r.seller_name, r.return_location_name,
               r.hub_id, COALESCE(h.name, r.hub_name) AS hub, r.clock_start, r.clock_column,
               EXTRACT(EPOCH FROM (now() - r.clock_start)) / 3600.0 AS age_hours
        FROM r LEFT JOIN hubs h ON h.id = r.hub_id
    """

    # One call for the admin dashboard: every hub's numbers from each screen.
    def dashboard(self):
        self.require_admin()
        now = datetime.now(timezone.utc)
        today_ist = now.astimezone(IST).replace(hour=0, minute=0, second=0, microsecond=0)
        today_start = today_ist.astimezone(timezone.utc).isoformat()
        three_days_ago = (now - timedelta(days=3)).isoformat()
        week_ago = (now - timedelta(days=7)).isoformat()
        conn = get_db()
        since = (conn.execute("SELECT value FROM settings WHERE key = 'dispatch_tracking_from'").fetchone() or {"value": "1970-01-01"})["value"]
        keys = ["pend1", "pend2", "pend3", "waitingGateOut", "waitingDelivery", "waiting3d", "mismatchInvoices",
                "mismatchReturns", "requestedBookings", "openTickets", "packedToday", "dispatchedToday", "deliveredToday"]
        hubs = {}

        def hub(hid, name="Hub not matched"):
            if hid not in hubs:
                hubs[hid] = dict({"id": hid, "name": name}, **{k: 0 for k in keys})
            return hubs[hid]

        for r in conn.execute("SELECT id, name FROM hubs ORDER BY name").fetchall():
            hub(r["id"], r["name"])

        for r in conn.execute(self.PENDENCY_SQL).fetchall():
            if r["clock_start"] is None:
                continue
            hours = float(r["age_hours"])
            hub(r["hub_id"])["pend1" if hours < 24 else "pend2" if hours < 48 else "pend3"] += 1

        for r in conn.execute(
            """
            SELECT hub_id,
                   COUNT(*) FILTER (WHERE dispatched_at IS NULL) AS packed,
                   COUNT(*) FILTER (WHERE dispatched_at IS NOT NULL) AS dispatched,
                   COUNT(*) FILTER (WHERE COALESCE(dispatched_at, created_at) < %s) AS old3
            FROM shipments WHERE delivered_at IS NULL AND ship_date >= %s GROUP BY hub_id
            """,
            (three_days_ago, since),
        ).fetchall():
            h = hub(r["hub_id"])
            h["waitingGateOut"], h["waitingDelivery"], h["waiting3d"] = r["packed"], r["dispatched"], r["old3"]

        for r in conn.execute(
            """
            SELECT s.hub_id, COUNT(DISTINCT s.id) AS invoices, COUNT(DISTINCT si.tracking_id) AS returns
            FROM shipments s
            JOIN shipment_items si ON si.shipment_id = s.id
            LEFT JOIN LATERAL (
                SELECT * FROM returns r
                WHERE r.return_tracking_id = si.tracking_id AND (si.sku IS NULL OR r.sku = si.sku)
                ORDER BY r.rtv_delivered_at NULLS FIRST LIMIT 1
            ) r ON TRUE
            WHERE s.delivered_at IS NOT NULL AND r.rtv_delivered_at IS NULL
              AND COALESCE(r.rtv_shipment_status, '') NOT ILIKE '%%deliver%%'
            GROUP BY s.hub_id
            """
        ).fetchall():
            h = hub(r["hub_id"])
            h["mismatchInvoices"], h["mismatchReturns"] = r["invoices"], r["returns"]

        bookings = {st: 0 for st in COURIER_BOOKING_STATUSES}
        for r in conn.execute("SELECT hub_id, status, COUNT(*) AS n FROM courier_bookings GROUP BY hub_id, status").fetchall():
            bookings[r["status"]] = bookings.get(r["status"], 0) + r["n"]
            if r["status"] == "Requested":
                hub(r["hub_id"])["requestedBookings"] += r["n"]

        tickets = {st: 0 for st in TICKET_STATUSES}
        for r in conn.execute("SELECT hub_id, status, COUNT(*) AS n FROM tickets GROUP BY hub_id, status").fetchall():
            tickets[r["status"]] += r["n"]
            if r["status"] != "Solved":
                hub(r["hub_id"])["openTickets"] += r["n"]

        for r in conn.execute(
            """
            SELECT hub_id,
                   COUNT(*) FILTER (WHERE created_at >= %s) AS packed,
                   COUNT(*) FILTER (WHERE dispatched_at >= %s) AS dispatched,
                   COUNT(*) FILTER (WHERE delivered_at >= %s) AS delivered
            FROM shipments
            WHERE created_at >= %s OR dispatched_at >= %s OR delivered_at >= %s
            GROUP BY hub_id
            """,
            (today_start,) * 6,
        ).fetchall():
            h = hub(r["hub_id"])
            h["packedToday"], h["dispatchedToday"], h["deliveredToday"] = r["packed"], r["dispatched"], r["delivered"]

        closures = {rs: 0 for rs in CLOSURE_REASONS}
        for r in conn.execute("SELECT reason, COUNT(*) AS n FROM return_closures WHERE closed_at >= %s GROUP BY reason", (week_ago,)).fetchall():
            closures[r["reason"]] = r["n"]

        last = conn.execute("SELECT * FROM dump_imports ORDER BY id DESC LIMIT 1").fetchone()
        conn.close()

        rows = sorted(hubs.values(), key=lambda h: (h["name"] == "Hub not matched", h["name"]))
        rows = [h for h in rows if h["id"] is not None or any(h[k] for k in keys)]
        totals = {k: sum(h[k] for h in rows) for k in keys}
        self.send_json(200, {
            "asOf": now.isoformat(), "trackingFrom": since, "hubs": rows, "totals": totals,
            "bookings": bookings, "tickets": tickets, "closures7d": closures,
            "lastImport": {
                "createdAt": last["created_at"], "by": last["uploaded_by"], "filename": last["filename"],
                "rows": last["total_rows"] - last["blank_rows"], "inserted": last["inserted"], "updated": last["updated"],
            } if last else None,
        })

    def list_pendency(self):
        scoped = self.scope_hub_id()
        where, params = "", []
        if scoped is not None:
            where, params = " WHERE r.hub_id = %s", [scoped]
        conn = get_db()
        rows = conn.execute(self.PENDENCY_SQL + where, tuple(params)).fetchall()
        conn.close()

        def bucket(hours):
            return "1 day" if hours < 24 else "2 days" if hours < 48 else "3+ days"

        items, not_picked_up = [], 0
        for r in rows:
            if r["clock_start"] is None:
                not_picked_up += 1
                continue
            hours = float(r["age_hours"])
            items.append({
                "returnId": r["return_tracking_id"], "sku": r["sku"], "forwardId": r["forward_tracking_id"],
                "returnType": r["return_type"], "returnStatus": r["return_status"],
                "rtvStatus": r["rtv_shipment_status"], "salePrice": float(r["sale_price"] or 0),
                "seller": r["seller_name"], "returnLocation": r["return_location_name"],
                "hubId": r["hub_id"], "hub": r["hub"] or "Hub not matched",
                "clockStart": r["clock_start"].isoformat(), "clockColumn": r["clock_column"],
                "ageHours": round(hours, 1), "bucket": bucket(hours),
            })
        items.sort(key=lambda x: -x["ageHours"])
        self.send_json(200, {"items": items, "notPickedUp": not_picked_up})

    def _closure_items(self, body):
        items = body.get("items")
        if not isinstance(items, list) or not items:
            raise ApiError(400, "Pick at least one return")
        if len(items) > 2000:
            raise ApiError(400, "Too many at once — close at most 2000 at a time")
        out = []
        for it in items:
            rid = (it.get("returnId") or "").strip() if isinstance(it, dict) else ""
            if not rid:
                raise ApiError(400, "Each item needs a returnId")
            out.append((rid, (it.get("sku") or "").strip()))
        return list(dict.fromkeys(out))

    def close_returns(self):
        user = self.require_user()
        body = self.read_json_body()
        reason = (body.get("reason") or "").strip()
        if reason not in CLOSURE_REASONS:
            raise ApiError(400, "Reason must be Lost, Rider Miss or Store Miss")
        remarks = (body.get("remarks") or "").strip()
        if not remarks:
            raise ApiError(400, "Write a remark saying what happened")
        items = self._closure_items(body)
        scoped = self.scope_hub_id()
        conn = get_db()
        placeholders = ",".join(["(%s, %s)"] * len(items))
        found = conn.execute(
            f"SELECT return_tracking_id, sku, hub_id FROM returns WHERE (return_tracking_id, sku) IN ({placeholders})",
            tuple(v for pair in items for v in pair),
        ).fetchall()
        by_key = {(r["return_tracking_id"], r["sku"]): r for r in found}
        missing = [f"{rid} {sku}".strip() for rid, sku in items if (rid, sku) not in by_key]
        if missing:
            conn.close()
            raise ApiError(404, "Not in the daily data: " + ", ".join(missing[:5]) + ("…" if len(missing) > 5 else ""))
        if scoped is not None and any(r["hub_id"] != scoped for r in found):
            conn.close()
            raise ApiError(403, "You can only close your own hub's returns")
        now = datetime.now(timezone.utc).isoformat()
        rows = [(rid, sku, reason, remarks[:1000], by_key[(rid, sku)]["hub_id"], user["username"], now) for rid, sku in items]
        conn.execute_values(
            "INSERT INTO return_closures (return_tracking_id, sku, reason, remarks, hub_id, closed_by, closed_at) VALUES %s "
            "ON CONFLICT (return_tracking_id, sku) DO NOTHING",
            rows,
        )
        conn.commit()
        conn.close()
        self.send_json(200, {"closed": len(rows), "reason": reason})

    def reopen_returns(self):
        self.require_admin()
        items = self._closure_items(self.read_json_body())
        conn = get_db()
        placeholders = ",".join(["(%s, %s)"] * len(items))
        gone = conn.execute(
            f"DELETE FROM return_closures WHERE (return_tracking_id, sku) IN ({placeholders}) RETURNING return_tracking_id",
            tuple(v for pair in items for v in pair),
        ).fetchall()
        conn.commit()
        conn.close()
        self.send_json(200, {"reopened": len(gone)})

    def list_closed_returns(self):
        scoped = self.scope_hub_id()
        conn = get_db()
        rows = conn.execute(
            """
            SELECT c.*, COALESCE(h.name, r.hub_name, 'Hub not matched') AS hub, r.return_type, r.seller_name, r.sale_price,
                   EXISTS (SELECT 1 FROM shipment_items si
                           WHERE si.tracking_id = c.return_tracking_id AND (si.sku IS NULL OR si.sku = c.sku)) AS packed_later
            FROM return_closures c
            LEFT JOIN returns r ON r.return_tracking_id = c.return_tracking_id AND r.sku = c.sku
            LEFT JOIN hubs h ON h.id = c.hub_id
            """ + (" WHERE c.hub_id = %s" if scoped is not None else "") + " ORDER BY c.closed_at DESC LIMIT 5000",
            (scoped,) if scoped is not None else (),
        ).fetchall()
        conn.close()
        self.send_json(200, [
            {
                "returnId": r["return_tracking_id"], "sku": r["sku"], "reason": r["reason"], "remarks": r["remarks"],
                "hub": r["hub"], "closedBy": r["closed_by"], "closedAt": r["closed_at"], "returnType": r["return_type"],
                "seller": r["seller_name"], "salePrice": float(r["sale_price"] or 0), "packedLater": r["packed_later"],
            }
            for r in rows
        ])

    # Packed-but-not-dispatched and dispatched-but-not-delivered invoices
    # from the tracking start date on, oldest first (the ones to chase).
    def list_open_shipments(self):
        since = self.tracking_from() or "1970-01-01"
        hub_sql, params = self.hub_filter("s.hub_id")
        conn = get_db()
        rows = conn.execute(
            SHIPMENT_LIST_SQL + "WHERE s.delivered_at IS NULL AND s.ship_date >= %s" + hub_sql +
            " ORDER BY COALESCE(s.dispatched_at, s.created_at) ASC LIMIT 5000",
            (since, *params),
        ).fetchall()
        conn.close()
        self.send_json(200, {"trackingFrom": since, "rows": [shipment_list_dict(r) for r in rows]})

    # Delivered in the app, but the daily data still has no RTV Delivered At
    # for at least one of the invoice's returns: the store must mark those in
    # the source system. A return drops off once the dump shows it delivered.
    def list_to_mark_delivered(self):
        hub_sql, params = self.hub_filter("s.hub_id")
        conn = get_db()
        items = conn.execute(
            """
            SELECT s.id AS shipment_id, si.tracking_id, si.sku, si.box_no,
                   r.return_tracking_id IS NOT NULL AS in_data,
                   r.rtv_delivered_at, r.rtv_shipment_status, r.return_status
            FROM shipments s
            JOIN shipment_items si ON si.shipment_id = s.id
            LEFT JOIN LATERAL (
                SELECT * FROM returns r
                WHERE r.return_tracking_id = si.tracking_id AND (si.sku IS NULL OR r.sku = si.sku)
                ORDER BY r.rtv_delivered_at NULLS FIRST LIMIT 1
            ) r ON TRUE
            WHERE s.delivered_at IS NOT NULL
            """ + hub_sql + " ORDER BY s.id, si.box_no, si.tracking_id",
            tuple(params),
        ).fetchall()
        pending = {}
        for it in items:
            delivered_in_data = bool(it["rtv_delivered_at"]) or "deliver" in (it["rtv_shipment_status"] or "").lower()
            if delivered_in_data:
                continue
            pending.setdefault(it["shipment_id"], []).append({
                "trackingId": it["tracking_id"], "sku": it["sku"], "boxNo": it["box_no"],
                "inData": it["in_data"], "dataStatus": it["rtv_shipment_status"] or it["return_status"],
            })
        out = []
        if pending:
            placeholders = ",".join(["%s"] * len(pending))
            rows = conn.execute(
                SHIPMENT_LIST_SQL + f"WHERE s.id IN ({placeholders}) ORDER BY s.delivered_at ASC",
                tuple(pending.keys()),
            ).fetchall()
            for r in rows:
                d = shipment_list_dict(r)
                d["pendingItems"] = pending[r["id"]]
                d["notInData"] = sum(1 for x in pending[r["id"]] if not x["inData"])
                out.append(d)
        conn.close()
        self.send_json(200, out)

    def find_scoped_shipment(self, conn, shipment_id=None, invoice_no=None):
        if invoice_no is not None:
            row = conn.execute(SHIPMENT_LIST_SQL + "WHERE upper(s.invoice_no) = upper(%s)", (invoice_no,)).fetchone()
        else:
            row = conn.execute(SHIPMENT_LIST_SQL + "WHERE s.id = %s", (shipment_id,)).fetchone()
        scoped = self.scope_hub_id()
        if row and scoped is not None and row["hub_id"] != scoped:
            # Say so plainly — a hub scanning another hub's box should know why.
            raise ApiError(403, f"{row['invoice_no']} belongs to {row['hub_name']}, not your hub")
        return row

    def gate_out(self):
        user = self.require_user()
        body = self.read_json_body()
        code = (body.get("code") or "").strip()
        if not code:
            raise ApiError(400, "Scan an invoice barcode")
        conn = get_db()
        row = self.find_scoped_shipment(conn, invoice_no=code)
        if not row:
            conn.close()
            raise ApiError(404, f"No invoice {code} — scan the barcode on the box label or invoice")
        if row["dispatched_at"]:
            conn.close()
            raise ApiError(409, f"{row['invoice_no']} was already dispatched ({row['dispatched_at']}, by {row['dispatched_by']})")
        now = datetime.now(timezone.utc).isoformat()
        # The WHERE guard makes a double scan from two devices dispatch once.
        updated = conn.execute(
            "UPDATE shipments SET dispatched_at = %s, dispatched_by = %s WHERE id = %s AND dispatched_at IS NULL RETURNING id",
            (now, user["username"], row["id"]),
        ).fetchone()
        if updated:
            sync_booking_status(conn, row["id"])
        conn.commit()
        if not updated:
            conn.close()
            raise ApiError(409, f"{row['invoice_no']} was just dispatched by someone else")
        row = self.find_scoped_shipment(conn, shipment_id=row["id"])
        conn.close()
        self.send_json(200, shipment_list_dict(row))

    def undo_gate_out(self, shipment_id):
        self.require_user()
        conn = get_db()
        row = self.find_scoped_shipment(conn, shipment_id=shipment_id)
        if not row:
            conn.close()
            raise ApiError(404, "Shipment not found")
        if row["delivered_at"]:
            conn.close()
            raise ApiError(409, "Already marked delivered — can't undo the dispatch")
        if not row["dispatched_at"]:
            conn.close()
            raise ApiError(409, "Not dispatched yet")
        conn.execute("UPDATE shipments SET dispatched_at = NULL, dispatched_by = NULL WHERE id = %s", (shipment_id,))
        sync_booking_status(conn, shipment_id)
        conn.commit()
        conn.close()
        self.send_json(200, {"id": shipment_id, "status": "Packed"})

    # Admin correction of a delivered invoice: replace the POD photo and/or
    # change the delivery date or note. The old photo stays in Drive (the
    # upload script can only add files).
    def edit_pod(self, shipment_id):
        user = self.require_admin()
        if int(self.headers.get("Content-Length", 0) or 0) > MAX_POD_BYTES * 2:
            raise ApiError(413, "Photo is too large")
        body = self.read_json_body()
        conn = get_db()
        row = self.find_scoped_shipment(conn, shipment_id=shipment_id)
        conn.close()
        if not row:
            raise ApiError(404, "Shipment not found")
        if not row["delivered_at"]:
            raise ApiError(409, "Only a delivered invoice has a POD to edit")
        delivered_on = (body.get("deliveredOn") or row["delivered_on"] or "").strip()
        try:
            datetime.strptime(delivered_on, "%Y-%m-%d")
        except ValueError:
            raise ApiError(400, "Pick the delivery date")
        if delivered_on < row["ship_date"]:
            raise ApiError(400, "Delivery date can't be before the invoice's ship date")
        note = (body.get("note") if "note" in body else row["delivery_note"]) or ""
        note = note.strip()[:500] or None
        file_id, pod_url = None, row["pod_url"]
        photo_b64 = body.get("photoBase64") or ""
        if photo_b64:
            try:
                data = base64.b64decode(photo_b64, validate=True)
            except (ValueError, TypeError):
                raise ApiError(400, "The photo didn't upload properly — try again")
            if len(data) > MAX_POD_BYTES:
                raise ApiError(413, "Photo is too large")
            stamp = datetime.now(IST).strftime("%Y%m%d-%H%M")
            file_id, pod_url = upload_pod_to_drive(data, "image/jpeg", f"{row['invoice_no']}-POD-{delivered_on}-edited-{stamp}.jpg", delivered_on[:7])
        if row["return_location"] != "Non-Local" and not pod_url:
            raise ApiError(400, "A proof-of-delivery photo is required for Local deliveries")
        conn = get_db()
        conn.execute(
            "UPDATE shipments SET delivered_on = %s, delivery_note = %s, pod_url = %s, "
            "pod_file_id = COALESCE(%s, pod_file_id), pod_updated_at = %s, pod_updated_by = %s WHERE id = %s",
            (delivered_on, note, pod_url, file_id, datetime.now(timezone.utc).isoformat(), user["username"], shipment_id),
        )
        conn.commit()
        row = self.find_scoped_shipment(conn, shipment_id=shipment_id)
        conn.close()
        self.send_json(200, shipment_list_dict(row))

    def mark_delivered(self, shipment_id):
        user = self.require_user()
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length > MAX_POD_BYTES * 2:  # base64 inflates the photo by ~4/3
            raise ApiError(413, "Photo is too large")
        body = self.read_json_body()
        delivered_on = (body.get("deliveredOn") or "").strip()
        try:
            datetime.strptime(delivered_on, "%Y-%m-%d")
        except ValueError:
            raise ApiError(400, "Pick the delivery date")
        note = (body.get("note") or "").strip()[:500] or None
        photo_b64 = body.get("photoBase64") or ""
        mime = (body.get("photoMimeType") or "image/jpeg").strip()
        if photo_b64 and not mime.startswith("image/"):
            raise ApiError(400, "The proof of delivery must be a photo")

        conn = get_db()
        row = self.find_scoped_shipment(conn, shipment_id=shipment_id)
        if not row:
            conn.close()
            raise ApiError(404, "Shipment not found")
        if not row["dispatched_at"]:
            conn.close()
            raise ApiError(409, "Gate-out this invoice before marking it delivered")
        if row["delivered_at"]:
            conn.close()
            raise ApiError(409, f"{row['invoice_no']} is already marked delivered")
        if delivered_on < row["ship_date"]:
            conn.close()
            raise ApiError(400, "Delivery date can't be before the invoice's ship date")
        # Local brands (and any brand without a return location yet) need the photo.
        if row["return_location"] != "Non-Local" and not photo_b64:
            conn.close()
            raise ApiError(400, "A proof-of-delivery photo is required for Local deliveries")
        conn.close()  # don't hold a connection while the photo goes to Drive

        file_id = pod_url = None
        if photo_b64:
            try:
                data = base64.b64decode(photo_b64, validate=True)
            except (ValueError, TypeError):
                raise ApiError(400, "The photo didn't upload properly — try again")
            if len(data) > MAX_POD_BYTES:
                raise ApiError(413, "Photo is too large")
            ext = "png" if mime == "image/png" else "jpg"
            file_id, pod_url = upload_pod_to_drive(data, mime, f"{row['invoice_no']}-POD-{delivered_on}.{ext}", delivered_on[:7])

        conn = get_db()
        updated = conn.execute(
            "UPDATE shipments SET delivered_on = %s, delivered_at = %s, delivered_by = %s, delivery_note = %s, "
            "pod_url = %s, pod_file_id = %s WHERE id = %s AND delivered_at IS NULL RETURNING id",
            (delivered_on, datetime.now(timezone.utc).isoformat(), user["username"], note, pod_url, file_id, shipment_id),
        ).fetchone()
        if updated:
            sync_booking_status(conn, shipment_id)
        conn.commit()
        row = self.find_scoped_shipment(conn, shipment_id=shipment_id)
        conn.close()
        if not updated:
            raise ApiError(409, "Already marked delivered")
        self.send_json(200, shipment_list_dict(row))

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
                "sku": r["sku"],
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
            sku = (m.get("sku") or "").strip() or None
            clean_rows.append((tracking, max(value, 0), qty, box_no, sku))

        conn = get_db()
        hub = conn.execute("SELECT * FROM hubs WHERE id = %s", (hub_id,)).fetchone()
        vendor = conn.execute("SELECT * FROM vendors WHERE id = %s", (vendor_id,)).fetchone()
        if not hub or not vendor:
            conn.close()
            raise ApiError(400, "hubId or vendorId does not exist")
        if not vendor["active"]:
            conn.close()
            raise ApiError(400, f"'{vendor['name']}' has been removed from the brand list")

        date_tag = ship_date.replace("-", "")
        # Several brands finished together arrive as parallel requests; the
        # lock (held until commit) makes them take turns, so each gets its
        # own next number instead of two requests reading the same count.
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"invoice-seq:{hub_id}:{ship_date}",))
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
            "INSERT INTO shipment_items (shipment_id, tracking_id, value, qty, box_no, sku) VALUES (%s, %s, %s, %s, %s, %s)",
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
                   b.destination_name, b.tracking, b.status, b.created_by,
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
        user = self.require_user()
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
            "INSERT INTO courier_bookings (vendor_id, hub_id, destination_name, pickup_date, boxes, declared_price, weight_kg, created_at, status, created_by) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'Requested', %s) RETURNING id",
            (vendor_id, hub_id, destination_name, pickup_date, boxes, declared_price, weight_kg, created_at, user["username"]),
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
        user = self.require_user()
        hub_sql, hub_params = self.hub_filter("hub_id")
        conn = get_db()
        row = conn.execute("SELECT id, status FROM courier_bookings WHERE id = %s" + hub_sql, (booking_id, *hub_params)).fetchone()
        if not row:
            conn.close()
            raise ApiError(404, "Booking not found")
        if user["role"] != "admin" and row["status"] != "Requested":
            conn.close()
            raise ApiError(403, "This booking is already with DTDC — ask an admin to cancel it")
        conn.execute("DELETE FROM courier_bookings WHERE id = %s", (booking_id,))
        conn.commit()
        conn.close()
        self.send_json(200, {"id": booking_id})

    # ---------- tickets ----------
    def create_ticket(self):
        user = self.require_user()
        if int(self.headers.get("Content-Length", 0) or 0) > MAX_POD_BYTES * 2:
            raise ApiError(413, "Photo is too large")
        body = self.read_json_body()
        category = (body.get("category") or "").strip()
        if category not in TICKET_CATEGORIES:
            raise ApiError(400, "Pick a category")
        title = (body.get("title") or "").strip()[:150]
        description = (body.get("description") or "").strip()[:4000]
        if not title:
            raise ApiError(400, "Give the issue a short title")
        if not description:
            raise ApiError(400, "Describe the issue")
        reference = (body.get("reference") or "").strip()[:200] or None
        # A hub account raises for its own hub; an admin raises for the hub
        # they're viewing (or none).
        hub_id = user["hub_id"] if user["role"] == "hub" else user["view_hub_id"]
        photo_b64 = body.get("photoBase64") or ""
        now = datetime.now(timezone.utc).isoformat()
        conn = get_db()
        tid = conn.execute(
            "INSERT INTO tickets (hub_id, category, title, description, reference, raised_by, raised_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (hub_id, category, title, description, reference, user["username"], now, now),
        ).fetchone()["id"]
        conn.commit()
        conn.close()
        if photo_b64:
            try:
                data = base64.b64decode(photo_b64, validate=True)
            except (ValueError, TypeError):
                raise ApiError(400, "The photo didn't upload properly — try again")
            try:
                _fid, url = upload_pod_to_drive(data, "image/jpeg", f"TKT-{tid:05d}-{now[:10]}.jpg", now[:7])
            except ApiError as e:
                # Keep the ticket; just say the photo didn't make it.
                conn = get_db()
                conn.execute("UPDATE tickets SET description = description || %s WHERE id = %s",
                             ("\n\n(Photo couldn't be saved: " + e.message + ")", tid))
                conn.commit()
                conn.close()
                url = None
            if url:
                conn = get_db()
                conn.execute("UPDATE tickets SET photo_url = %s WHERE id = %s", (url, tid))
                conn.commit()
                conn.close()
        conn = get_db()
        row = conn.execute(TICKET_SELECT_SQL + "WHERE t.id = %s", (tid,)).fetchone()
        conn.close()
        self.send_json(201, ticket_to_dict(row))

    def list_tickets(self):
        hub_sql, params = self.hub_filter("t.hub_id")
        conn = get_db()
        rows = conn.execute(TICKET_SELECT_SQL + "WHERE TRUE" + hub_sql + " ORDER BY t.id DESC LIMIT 5000", tuple(params)).fetchall()
        conn.close()
        self.send_json(200, [ticket_to_dict(r) for r in rows])

    def open_tickets_count(self):
        hub_sql, params = self.hub_filter("hub_id")
        conn = get_db()
        n = conn.execute("SELECT COUNT(*) AS n FROM tickets WHERE status <> 'Solved'" + hub_sql, tuple(params)).fetchone()["n"]
        conn.close()
        self.send_json(200, {"open": n})

    # Admins move a ticket along (with a reply the store sees); a store can
    # only reopen its own solved ticket, saying why.
    def update_ticket_status(self, ticket_id):
        user = self.require_user()
        body = self.read_json_body()
        status = (body.get("status") or "").strip()
        reply = (body.get("reply") or "").strip()[:4000]
        if status not in TICKET_STATUSES:
            raise ApiError(400, "status must be Open, In progress or Solved")
        hub_sql, params = self.hub_filter("t.hub_id")
        conn = get_db()
        row = conn.execute(TICKET_SELECT_SQL + "WHERE t.id = %s" + hub_sql, (ticket_id, *params)).fetchone()
        if not row:
            conn.close()
            raise ApiError(404, "Ticket not found")
        now = datetime.now(timezone.utc).isoformat()
        if user["role"] != "admin":
            if not (row["status"] == "Solved" and status == "Open"):
                conn.close()
                raise ApiError(403, "Only admins update tickets — you can reopen a solved one")
            if not reply:
                conn.close()
                raise ApiError(400, "Say why you're reopening it")
            conn.execute(
                "UPDATE tickets SET status = 'Open', solved_at = NULL, updated_at = %s, updated_by = %s, "
                "description = description || %s WHERE id = %s",
                (now, user["username"], f"\n\nReopened by {user['username']} ({now[:10]}): {reply}", ticket_id),
            )
        else:
            if status == "Solved" and not (reply or row["admin_reply"]):
                conn.close()
                raise ApiError(400, "Add a reply saying how it was solved")
            conn.execute(
                "UPDATE tickets SET status = %s, admin_reply = COALESCE(NULLIF(%s, ''), admin_reply), updated_at = %s, updated_by = %s, "
                "solved_at = CASE WHEN %s = 'Solved' THEN %s ELSE NULL END WHERE id = %s",
                (status, reply, now, user["username"], status, now, ticket_id),
            )
        conn.commit()
        row = conn.execute(TICKET_SELECT_SQL + "WHERE t.id = %s", (ticket_id,)).fetchone()
        conn.close()
        self.send_json(200, ticket_to_dict(row))

    def share_courier_bookings(self):
        self.require_admin()
        ids = self.read_json_body().get("ids")
        if not isinstance(ids, list) or not ids:
            raise ApiError(400, "Pick at least one booking")
        try:
            ids = [int(x) for x in ids]
        except (TypeError, ValueError):
            raise ApiError(400, "ids must be numbers")
        conn = get_db()
        placeholders = ",".join(["%s"] * len(ids))
        rows = conn.execute(
            f"UPDATE courier_bookings SET status = 'Shared with DTDC' WHERE id IN ({placeholders}) AND status = 'Requested' RETURNING id",
            tuple(ids),
        ).fetchall()
        conn.commit()
        conn.close()
        self.send_json(200, {"shared": len(rows)})

    def requested_bookings_count(self):
        hub_sql, params = self.hub_filter("hub_id")
        conn = get_db()
        n = conn.execute("SELECT COUNT(*) AS n FROM courier_bookings WHERE status = 'Requested'" + hub_sql, tuple(params)).fetchone()["n"]
        conn.close()
        self.send_json(200, {"requested": n})

    def update_courier_booking(self, booking_id):
        self.require_admin()
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
                   b.destination_name, b.tracking, b.status, b.created_by,
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
