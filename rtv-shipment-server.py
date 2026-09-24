"""
RTV Scan-to-Pack app — Python stdlib HTTP server backed by Supabase Postgres.

Separate app from server.py/public/ (different data model: tracking-id-driven
scan-to-pack instead of pasted return-ID manifests) — has its own database
(Supabase project), so the two never collide.

Local run:  DATABASE_URL=postgresql://... python rtv-shipment-server.py [port]
Hosted (Render): reads DATABASE_URL and PORT from the environment.
"""
import json
import os
import re
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
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
    m = re.search(r"(\d{6})\s*$", address or "")
    return m.group(1) if m else ""


def database_url():
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. Point it at your Supabase Postgres connection "
            "string (Session Pooler, port 5432) before starting the server."
        )
    return url


class Db:
    """Thin wrapper so call sites can keep using the sqlite3-style
    conn.execute(sql, params).fetchone()/.fetchall() chain psycopg2 doesn't
    support directly (it requires an explicit cursor)."""

    def __init__(self):
        self._conn = psycopg2.connect(database_url())

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
        self._conn.close()


def get_db():
    return Db()


def init_db():
    conn = get_db()
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
        CREATE TABLE IF NOT EXISTS courier_booking_shipments (
            booking_id INTEGER NOT NULL REFERENCES courier_bookings(id) ON DELETE CASCADE,
            shipment_id INTEGER NOT NULL REFERENCES shipments(id) UNIQUE,
            PRIMARY KEY (booking_id, shipment_id)
        );
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
        "destinationName": row["destination_name"],
        "destinationPincode": row["vendor_pincode"],
        "destinationPhone": row["vendor_contact_phone"],
        "destinationAddress": row["vendor_address"],
        "hubName": hub_name,
        "originName": (f"Zilo {hub_name} Warehouse" if hub_name else None),
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


class Handler(BaseHTTPRequestHandler):
    server_version = "RTVScanApp/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
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
            if path == "/api/hubs":
                return self.list_hubs()
            if path == "/api/vendors":
                return self.list_vendors()
            if path == "/api/lookup":
                tracking = (qs.get("tracking") or [""])[0].strip()
                return self.lookup_tracking(tracking)
            if path == "/api/shipments":
                return self.list_shipments()
            if path == "/api/email-template":
                return self.get_email_template()
            if path == "/api/shipments/bookable":
                q = (qs.get("q") or [""])[0].strip()
                return self.list_bookable_shipments(q)
            if path == "/api/courier-bookings":
                return self.list_courier_bookings()
            if path == "/api/courier-bookings/export":
                return self.export_courier_bookings_csv()
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
            m = re.match(r"^/api/courier-bookings/(\d+)$", path)
            if m:
                return self.delete_courier_booking(int(m.group(1)))
            return self.send_json(404, {"error": "Unknown endpoint"})
        except ApiError as e:
            self.send_json(e.status, {"error": e.message})
        except Exception as e:  # noqa: BLE001
            self.send_json(500, {"error": str(e)})

    def do_PATCH(self):
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            m = re.match(r"^/api/courier-bookings/(\d+)$", path)
            if m:
                return self.update_courier_booking_tracking(int(m.group(1)))
            return self.send_json(404, {"error": "Unknown endpoint"})
        except ApiError as e:
            self.send_json(e.status, {"error": e.message})
        except Exception as e:  # noqa: BLE001
            self.send_json(500, {"error": str(e)})

    # ---------- hubs ----------
    def list_hubs(self):
        conn = get_db()
        rows = conn.execute("SELECT * FROM hubs ORDER BY name").fetchall()
        conn.close()
        self.send_json(200, [hub_to_dict(r) for r in rows])

    def create_hub(self):
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
            ORDER BY s.id DESC
            LIMIT 200
            """
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
        s = conn.execute("SELECT * FROM shipments WHERE id = %s", (shipment_id,)).fetchone()
        if not s:
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
        body = self.read_json_body()
        try:
            hub_id = int(body.get("hubId"))
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
        params = []
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
        conn = get_db()
        rows = conn.execute(
            """
            SELECT b.id, b.pickup_date, b.boxes, b.declared_price, b.weight_kg, b.created_at,
                   b.destination_name, b.tracking,
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
            GROUP BY b.id, v.name, v.warehouse_label, v.pincode, v.contact_phone, v.address,
                     h.name, h.pincode, h.contact_name, h.phone, h.address
            ORDER BY b.id DESC
            """
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
        conn = get_db()
        row = conn.execute("SELECT id FROM courier_bookings WHERE id = %s", (booking_id,)).fetchone()
        if not row:
            conn.close()
            raise ApiError(404, "Booking not found")
        conn.execute("DELETE FROM courier_bookings WHERE id = %s", (booking_id,))
        conn.commit()
        conn.close()
        self.send_json(200, {"id": booking_id})

    def update_courier_booking_tracking(self, booking_id):
        body = self.read_json_body()
        tracking = (body.get("tracking") or "").strip()
        conn = get_db()
        row = conn.execute("SELECT id FROM courier_bookings WHERE id = %s", (booking_id,)).fetchone()
        if not row:
            conn.close()
            raise ApiError(404, "Booking not found")
        conn.execute("UPDATE courier_bookings SET tracking = %s WHERE id = %s", (tracking, booking_id))
        conn.commit()
        conn.close()
        self.send_json(200, {"id": booking_id, "tracking": tracking})

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

    def export_courier_bookings_csv(self):
        conn = get_db()
        rows = conn.execute(
            """
            SELECT b.id, b.pickup_date, b.boxes, b.declared_price, b.weight_kg, b.created_at,
                   b.destination_name, b.tracking,
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
            GROUP BY b.id, v.name, v.warehouse_label, v.pincode, v.contact_phone, v.address,
                     h.name, h.pincode, h.contact_name, h.phone, h.address
            ORDER BY b.id ASC
            """
        ).fetchall()
        conn.close()

        def csv_field(v):
            s = "" if v is None else str(v)
            if any(c in s for c in (',', '"', '\n')):
                s = '"' + s.replace('"', '""') + '"'
            return s

        lines = [",".join(csv_field(label) for _, label in self.COURIER_EXPORT_COLUMNS)]
        for r in rows:
            d = courier_booking_to_dict(r)
            lines.append(",".join(csv_field(d.get(key)) for key, _ in self.COURIER_EXPORT_COLUMNS))
        body = ("\r\n".join(lines) + "\r\n").encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", "attachment; filename=dtdc-courier-bookings.csv")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    port = int(os.environ.get("PORT") or (sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PORT))
    init_db()
    server = HTTPServer(("0.0.0.0", port), Handler)
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
