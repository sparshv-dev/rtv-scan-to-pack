# RTV Scan-to-Pack app

`rtv-shipment.html` + `rtv-shipment-server.py` — a separate app from
`server.py`/`public/` in this same folder (different data model, own
database file, don't confuse the two).

Scan tracking IDs to pack boxes for a return-to-vendor shipment: each
item's value (and its seller/brand, used to route it to the right
warehouse) is looked up automatically from a local database that you keep
current with a daily CSV dump. Pack boxes, close them, then print box
labels (4"x6") and delivery invoices (A4) — same output as before, just
driven by scanning instead of pasting a CSV manifest.

## Run it

```bash
python rtv-shipment-server.py
```

Starts the server on **http://localhost:8421** and creates
`rtv-shipment.db` (SQLite) next to the script on first run — that file
holds hubs, vendors/warehouses, the tracking-ID master data, and every
shipment. Bound to this machine only (127.0.0.1) — not reachable from
other computers on the network. Leave the terminal window open; closing it
stops the server. Different port: `python rtv-shipment-server.py 9000`.

## Using it

1. **Shipping details** — pick or add the hub you're shipping from, set
   the ship date.
2. **Import daily dump** — upload today's tracking dump as CSV, header
   row required: `Tracking ID, Marketplace Order ID, Return ID, Seller
   name, RTV Shipment ID, TMS Provider name, RTV Shipment Status, RTV
   Created At, RTV tracking id, Value, Order set ID`. A Tracking ID
   already in the database gets all its fields updated to the new
   values; a new one is added. Safe to re-import the same or an updated
   file — it's an upsert, not an append. Only Tracking ID and Value ever
   show up on the printed invoice (qty is always 1 per scanned tracking
   ID) — the rest is kept in the database for reference.
3. **Scan to pack** — scan (or type) a tracking ID and press Enter. If
   it's in the database and its seller name matches a saved warehouse,
   it's added to the current box immediately with value filled in and
   qty/amount updated live. If the tracking ID isn't found, or its seller
   doesn't match a saved warehouse yet, a small form opens to fill in
   value and pick/add the warehouse before adding. **Close box & start
   next** locks in the current box and starts the next one. Repeat until
   everything's packed.
4. **Finish shipment(s) & print** — groups everything you've packed by
   vendor (a box with mixed brands splits cleanly across shipments; box
   numbers are preserved per vendor), creates one shipment per vendor in
   the database (invoice number like `RTV-GHK-20260810-001`), and opens
   the print view with box labels + both invoice copies per brand.
5. **Recent shipments** — reopen and reprint any past shipment.

## Notes

- `rtv-shipment.db` is the single source of truth — back it up (copy the
  file) periodically. There's no separate backup job.
- The barcode on the box label is a visual placeholder, not a real
  Code128/EAN encoding.
- The invoice is a delivery document, not a GST tax invoice.
