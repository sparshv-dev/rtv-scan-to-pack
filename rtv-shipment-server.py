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
        "contact_name": "Annamma Mathew P", "contact_phone": "9743993941",
    },
    {
        "seller": "Arvind Fashions Limited",
        "brands": ["Arrow"],
        "street": "WH No. 4, Arvind Fashions Limited, Omni Return QC Center, C/O Instakart Services Pvt. Ltd., K-Square Industrial Estate, Before Padgha Toll, Bhiwandi",
        "town": "Bhiwandi", "city": "Thane", "state": "Maharashtra", "pincode": "421101",
        "contact_name": "Bhushan Patil", "contact_phone": "91121 24020",
    },
    {
        "seller": "Arvind Lifestyle Brands Limited",
        "brands": ["USPA"],
        "street": "WH No. 4, Arvind Lifestyle Brands Ltd., Omni NNNOW Return QC Center, C/O Instakart Services Pvt. Ltd., K-Square Industrial Estate, Before Padgha Toll, Bhiwandi",
        "town": "Bhiwandi", "city": "Thane", "state": "Maharashtra", "pincode": "421101",
        "contact_name": "Bhushan Patil", "contact_phone": "91121 24020",
    },
    {
        "seller": "Arvind Youth Brands Private Limited",
        "brands": ["Flying Machine"],
        "street": "WH No. 4, Arvind Youth Brands Pvt. Ltd., Omni Return QC Center, C/O Instakart Services Pvt. Ltd., K-Square Industrial Estate, Before Padgha Toll, Bhiwandi",
        "town": "Bhiwandi", "city": "Thane", "state": "Maharashtra", "pincode": "421101",
        "contact_name": "Bhushan Patil", "contact_phone": "91121 24020",
    },
    {
        "seller": "Biba Fashion Limited",
        "brands": [],
        "street": "Biba Fashion Ltd., Khasra No. 30/21/3/2/2, 35/1/2/3., Killa -2 Rakba-3. Kamal-0. Marla 1/ 2 13 MIN 7.14.15/1.15/2,60/2 Village Sikri",
        "town": "Tehsil Ballabahgarh", "city": "Faridabad", "state": "NCR", "pincode": "121004",
        "contact_name": "Mr. Praveen", "contact_phone": "9945403556",
    },
    {
        "seller": "Soch Apparels Pvt Ltd",
        "brands": [],
        "street": "Mumbai Warehouse Bhiwandi, Address: Soch Apparels Private Limited Bhiwandi, Asmeeta Textile Park, Bldg No. D-3A, Unit No. 004 Ground Floor",
        "town": "Bhiwandi", "city": "", "state": "", "pincode": "421311",
        "contact_name": "Abhijeet", "contact_phone": "9702768637",
    },
    {
        "seller": "Radhamani Textile Pvt Ltd",
        "brands": ["Rare Rabbit", "Rareism"],
        "street": "Radhamani Textiles Pvt Ltd. (WH MH), Instakart Service Pvt Ltd, Vashere, Bhiwandi, Warehouse No. WE-IL, Renaissanse Integrated Industrial Area, Repro Books Ltd Plant, Vashere",
        "town": "Bhiwandi", "city": "Thane", "state": "Maharashtra", "pincode": "421302",
        "contact_name": "Chetan Mhatre", "contact_phone": "9773535457",
    },
]
SEED_SHORT_NAMES = {
    "Allen Solly": "AS", "Van Huesen": "VH", "LP": "LP", "PE": "PE",
    "Arrow": "ARW", "USPA": "USPA", "Flying Machine": "FM",
    "Biba Fashion Limited": "BIBA", "Soch Apparels Pvt Ltd": "SOCH",
    "Rare Rabbit": "RR", "Rareism": "RSM",
}


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
        CREATE TABLE IF NOT EXISTS vendors (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            short_name TEXT,
            warehouse_label TEXT,
            address TEXT NOT NULL,
            contact_name TEXT,
            contact_phone TEXT
        );
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
        """
    )
    conn.commit()

    seeded = conn.execute("SELECT COUNT(*) AS count FROM vendors").fetchone()["count"]
    if seeded == 0:
        now = datetime.now(timezone.utc).isoformat()
        for w in SEED_WAREHOUSES:
            address = compose_address(w)
            keys = w["brands"] or [w["seller"]]
            for name in keys:
                conn.execute(
                    "INSERT INTO vendors (name, short_name, warehouse_label, address, contact_name, contact_phone) "
                    "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (name) DO NOTHING",
                    (name, SEED_SHORT_NAMES.get(name, ""), "", address, w["contact_name"], w["contact_phone"]),
                )
        conn.commit()
    conn.close()


def hub_to_dict(row):
    return {"id": row["id"], "name": row["name"], "code": row["code"], "address": row["address"], "phone": row["phone"]}


def vendor_to_dict(row):
    return {
        "id": row["id"],
        "name": row["name"],
        "shortName": row["short_name"],
        "warehouseLabel": row["warehouse_label"],
        "address": row["address"],
        "contactName": row["contact_name"],
        "contactPhone": row["contact_phone"],
    }


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
        conn = get_db()
        row = conn.execute(
            "INSERT INTO hubs (name, code, address, phone) VALUES (%s, %s, %s, %s) RETURNING *",
            (name, code, address, phone),
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
        conn = get_db()
        existing = conn.execute("SELECT * FROM vendors WHERE name = %s", (name,)).fetchone()
        if existing:
            conn.close()
            raise ApiError(409, f"A warehouse for '{name}' already exists")
        row = conn.execute(
            "INSERT INTO vendors (name, short_name, warehouse_label, address, contact_name, contact_phone) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING *",
            (name, short_name, warehouse_label, address, contact_name, contact_phone),
        ).fetchone()
        conn.commit()
        conn.close()
        self.send_json(201, vendor_to_dict(row))

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
        inserted = 0
        updated = 0
        for item in by_tracking.values():
            if item["trackingId"] in existing_ids:
                updated += 1
            else:
                inserted += 1
            conn.execute(
                """
                INSERT INTO master_items (
                    tracking_id, marketplace_order_id, return_id, seller_name, rtv_shipment_id,
                    tms_provider_name, rtv_shipment_status, rtv_created_at, rtv_tracking_id, value,
                    order_set_id, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (tracking_id) DO UPDATE SET
                    marketplace_order_id=excluded.marketplace_order_id, return_id=excluded.return_id,
                    seller_name=excluded.seller_name, rtv_shipment_id=excluded.rtv_shipment_id,
                    tms_provider_name=excluded.tms_provider_name, rtv_shipment_status=excluded.rtv_shipment_status,
                    rtv_created_at=excluded.rtv_created_at, rtv_tracking_id=excluded.rtv_tracking_id,
                    value=excluded.value, order_set_id=excluded.order_set_id, updated_at=excluded.updated_at
                """,
                (
                    item["trackingId"], item["marketplaceOrderId"], item["returnId"], item["sellerName"],
                    item["rtvShipmentId"], item["tmsProviderName"], item["rtvShipmentStatus"],
                    item["rtvCreatedAt"], item["rtvTrackingId"], item["value"], item["orderSetId"], now,
                ),
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
