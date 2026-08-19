"""
RTV Label & PoD app — dependency-free Python stdlib server.

Run:  python server.py [port]
Then open http://localhost:8420 (or the given port) in a browser.
Other machines on the same network can reach it at http://<this-machine-ip>:8420
"""
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "rtv.db"
PUBLIC_DIR = BASE_DIR / "public"
DEFAULT_PORT = 8420

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS hubs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            code TEXT NOT NULL,
            address TEXT NOT NULL,
            phone TEXT
        );
        CREATE TABLE IF NOT EXISTS vendors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            warehouse_label TEXT,
            address TEXT NOT NULL,
            contact_name TEXT,
            contact_phone TEXT
        );
        CREATE TABLE IF NOT EXISTS shipments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            challan_no TEXT NOT NULL UNIQUE,
            hub_id INTEGER NOT NULL REFERENCES hubs(id),
            vendor_id INTEGER NOT NULL REFERENCES vendors(id),
            courier TEXT NOT NULL,
            awb TEXT NOT NULL,
            ship_date TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS manifest_rows (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            shipment_id INTEGER NOT NULL REFERENCES shipments(id) ON DELETE CASCADE,
            return_id TEXT NOT NULL,
            sku TEXT NOT NULL,
            description TEXT,
            qty INTEGER NOT NULL,
            box_no INTEGER NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()


def hub_to_dict(row):
    return {
        "id": row["id"],
        "name": row["name"],
        "code": row["code"],
        "address": row["address"],
        "phone": row["phone"],
    }


def vendor_to_dict(row):
    return {
        "id": row["id"],
        "name": row["name"],
        "warehouseLabel": row["warehouse_label"],
        "address": row["address"],
        "contactName": row["contact_name"],
        "contactPhone": row["contact_phone"],
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
    return (letters[:3] or "HUB")


class Handler(BaseHTTPRequestHandler):
    server_version = "RTVApp/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # ---------- helpers ----------
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

    def serve_static(self, url_path):
        if url_path == "/":
            url_path = "/index.html"
        rel = url_path.lstrip("/")
        target = (PUBLIC_DIR / rel).resolve()
        if PUBLIC_DIR not in target.parents and target != PUBLIC_DIR:
            self.send_error(403)
            return
        if not target.exists() or not target.is_file():
            self.send_response(404)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Not found")
            return
        ctype = CONTENT_TYPES.get(target.suffix, "application/octet-stream")
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # ---------- routing ----------
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/api/hubs":
                return self.list_hubs()
            if path == "/api/vendors":
                return self.list_vendors()
            if path == "/api/shipments":
                return self.list_shipments()
            m = re.match(r"^/api/shipments/(\d+)$", path)
            if m:
                return self.get_shipment(int(m.group(1)))
            if path.startswith("/api/"):
                return self.send_json(404, {"error": "Unknown endpoint"})
            return self.serve_static(path)
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
        cur = conn.execute(
            "INSERT INTO hubs (name, code, address, phone) VALUES (?, ?, ?, ?)",
            (name, code, address, phone),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM hubs WHERE id = ?", (cur.lastrowid,)).fetchone()
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
        warehouse_label = (body.get("warehouseLabel") or "").strip()
        contact_name = (body.get("contactName") or "").strip()
        contact_phone = (body.get("contactPhone") or "").strip()
        conn = get_db()
        cur = conn.execute(
            "INSERT INTO vendors (name, warehouse_label, address, contact_name, contact_phone) "
            "VALUES (?, ?, ?, ?, ?)",
            (name, warehouse_label, address, contact_name, contact_phone),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM vendors WHERE id = ?", (cur.lastrowid,)).fetchone()
        conn.close()
        self.send_json(201, vendor_to_dict(row))

    # ---------- shipments ----------
    def list_shipments(self):
        conn = get_db()
        rows = conn.execute(
            """
            SELECT s.id, s.challan_no, s.courier, s.awb, s.ship_date, s.created_at,
                   h.name AS hub_name, v.name AS vendor_name, v.warehouse_label,
                   (SELECT COUNT(DISTINCT box_no) FROM manifest_rows WHERE shipment_id = s.id) AS boxes,
                   (SELECT COUNT(*) FROM manifest_rows WHERE shipment_id = s.id) AS return_lines,
                   (SELECT COALESCE(SUM(qty), 0) FROM manifest_rows WHERE shipment_id = s.id) AS units
            FROM shipments s
            JOIN hubs h ON h.id = s.hub_id
            JOIN vendors v ON v.id = s.vendor_id
            ORDER BY s.id DESC
            LIMIT 100
            """
        ).fetchall()
        conn.close()
        out = []
        for r in rows:
            out.append(
                {
                    "id": r["id"],
                    "challanNo": r["challan_no"],
                    "courier": r["courier"],
                    "awb": r["awb"],
                    "shipDate": r["ship_date"],
                    "createdAt": r["created_at"],
                    "hubName": r["hub_name"],
                    "vendorName": r["vendor_name"],
                    "warehouseLabel": r["warehouse_label"],
                    "boxes": r["boxes"],
                    "returnLines": r["return_lines"],
                    "units": r["units"],
                }
            )
        self.send_json(200, out)

    def get_shipment(self, shipment_id):
        conn = get_db()
        s = conn.execute("SELECT * FROM shipments WHERE id = ?", (shipment_id,)).fetchone()
        if not s:
            conn.close()
            raise ApiError(404, "Shipment not found")
        hub = conn.execute("SELECT * FROM hubs WHERE id = ?", (s["hub_id"],)).fetchone()
        vendor = conn.execute("SELECT * FROM vendors WHERE id = ?", (s["vendor_id"],)).fetchone()
        rows = conn.execute(
            "SELECT * FROM manifest_rows WHERE shipment_id = ? ORDER BY box_no, id",
            (shipment_id,),
        ).fetchall()
        conn.close()

        manifest = [
            {
                "returnId": r["return_id"],
                "sku": r["sku"],
                "description": r["description"],
                "qty": r["qty"],
                "boxNo": r["box_no"],
            }
            for r in rows
        ]
        box_numbers = sorted(set(r["boxNo"] for r in manifest)) or [1]
        boxes = [{"boxNo": bn, "rows": [m for m in manifest if m["boxNo"] == bn]} for bn in box_numbers]

        self.send_json(
            200,
            {
                "id": s["id"],
                "challanNo": s["challan_no"],
                "courier": s["courier"],
                "awb": s["awb"],
                "shipDate": s["ship_date"],
                "createdAt": s["created_at"],
                "hub": hub_to_dict(hub),
                "vendor": vendor_to_dict(vendor),
                "manifest": manifest,
                "boxes": boxes,
                "totals": {
                    "boxes": len(box_numbers),
                    "returns": len(manifest),
                    "units": sum(m["qty"] for m in manifest),
                },
            },
        )

    def create_shipment(self):
        body = self.read_json_body()
        try:
            hub_id = int(body.get("hubId"))
            vendor_id = int(body.get("vendorId"))
        except (TypeError, ValueError):
            raise ApiError(400, "hubId and vendorId are required")
        courier = require_str(body, "courier")
        awb = require_str(body, "awb")
        ship_date = require_str(body, "shipDate")
        manifest = body.get("manifest")
        if not isinstance(manifest, list) or not manifest:
            raise ApiError(400, "manifest must be a non-empty list")

        clean_rows = []
        for i, m in enumerate(manifest):
            if not isinstance(m, dict):
                raise ApiError(400, f"manifest row {i + 1} is invalid")
            return_id = (m.get("returnId") or "").strip()
            sku = (m.get("sku") or "").strip()
            description = (m.get("description") or "").strip()
            try:
                qty = int(m.get("qty"))
                box_no = int(m.get("boxNo"))
            except (TypeError, ValueError):
                raise ApiError(400, f"manifest row {i + 1}: qty and boxNo must be numbers")
            if not return_id or not sku:
                raise ApiError(400, f"manifest row {i + 1}: returnId and sku are required")
            if qty < 1 or box_no < 1:
                raise ApiError(400, f"manifest row {i + 1}: qty and boxNo must be >= 1")
            clean_rows.append((return_id, sku, description, qty, box_no))

        conn = get_db()
        hub = conn.execute("SELECT * FROM hubs WHERE id = ?", (hub_id,)).fetchone()
        vendor = conn.execute("SELECT * FROM vendors WHERE id = ?", (vendor_id,)).fetchone()
        if not hub or not vendor:
            conn.close()
            raise ApiError(400, "hubId or vendorId does not exist")

        date_tag = ship_date.replace("-", "")
        seq = conn.execute(
            "SELECT COUNT(*) FROM shipments WHERE hub_id = ? AND ship_date = ?",
            (hub_id, ship_date),
        ).fetchone()[0] + 1
        challan_no = f"RTV-{hub['code']}-{date_tag}-{seq:03d}"

        created_at = datetime.now(timezone.utc).isoformat()
        cur = conn.execute(
            "INSERT INTO shipments (challan_no, hub_id, vendor_id, courier, awb, ship_date, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (challan_no, hub_id, vendor_id, courier, awb, ship_date, created_at),
        )
        shipment_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO manifest_rows (shipment_id, return_id, sku, description, qty, box_no) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [(shipment_id, *row) for row in clean_rows],
        )
        conn.commit()
        conn.close()
        self.send_json(201, {"id": shipment_id, "challanNo": challan_no})


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
    init_db()
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"RTV Label app running:")
    print(f"  On this machine:  http://localhost:{port}")
    print(f"  On the network:   http://<this-machine-ip>:{port}  (run 'ipconfig' to find the IP)")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
        server.shutdown()


if __name__ == "__main__":
    main()
