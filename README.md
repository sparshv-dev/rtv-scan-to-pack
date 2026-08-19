# RTV Label & Invoice app

Simple web app for creating Return-to-Vendor shipments: pick the dispatching
hub and vendor warehouse, paste in the manifest, and print straight from the
browser:

- **Box label(s)** — one 4"x6" label per box, addressed to the vendor
  warehouse, with a courier/AWB barcode and a quantity summary for that box.
- **Invoice — Vendor Copy** and **Invoice — Transporter Copy** — two A5
  copies of the same delivery document (full itemized manifest, sign-off
  line), one for each party to retain as their record of handover. This is
  a delivery document for the movement of returned goods, not a GST tax
  invoice — no pricing or tax fields.

No install required — it's pure Python standard library (uses only `sqlite3`
and `http.server`, both built in). Nothing to `pip install`.

## Run it

```bash
python server.py
```

This starts the server on port 8420 and creates `rtv.db` (SQLite) next to
`server.py` on first run — that file holds all hubs, vendors, and shipments.

- On the hosting machine: http://localhost:8420
- On the network (other staff): http://192.168.0.79:8420
  (re-check the IP with `ipconfig` if this machine's address changes, e.g.
  after a reboot or reconnecting Wi-Fi — Windows may assign a different one)

Leave the terminal window open — closing it stops the server. To run on a
different port: `python server.py 9000`.

If other machines on the network can't reach it, Windows Firewall is
probably blocking inbound connections to Python on the private network —
allow it via Windows Security → Firewall & network protection, or run:

```powershell
netsh advfirewall firewall add rule name="RTV Label App" dir=in action=allow protocol=TCP localport=8420
```

## Using it

1. **Where's this going?** — pick the hub you're shipping from and the
   vendor warehouse. Use **+ New hub** / **+ New vendor** inline if either
   isn't saved yet — they're reused from a dropdown on every future
   shipment.
2. **Courier details** — courier name, AWB/tracking number, ship date.
3. **What's in the boxes?** — paste the manifest: one item per line, tab-
   or comma-separated, in the order `return ID, SKU, description, quantity,
   box number`. Hit **Parse into table**, fix anything by hand if needed.
   The box number column is what splits one shipment into multiple box
   labels.
4. **Create shipment & print** — saves the shipment (assigns an invoice
   number like `RTV-GHK-20260810-001`) and opens the print view with the
   box label(s) plus both invoice copies. Use **Print this sheet** or
   **Print all sheets**. Box labels print at true 4"x6"; invoices print at
   A5 — set your printer/print dialog to match, or "fit to page".
5. **Recent shipments** — reopen and reprint any past shipment (labels or
   either invoice copy), e.g. if a label got damaged in transit.

## Notes / known limitations

- The barcode on the box label is a **visual placeholder**, not a real
  Code128/EAN encoding — it won't scan. Fine for a human-readable label
  with manual/portal-booked couriers.
- The invoice is a plain delivery document, deliberately with no GST/tax
  fields (no HSN codes, no CGST/SGST breakup, no invoice value) — if this
  ever needs to be a real GST-compliant tax invoice or e-way bill, that's a
  separate, larger change to the data model (pricing, tax rates, HSN
  codes) and should be scoped explicitly before building.
- `rtv.db` is a single SQLite file — back it up (copy it) periodically if
  shipment history matters; there's no separate backup job.
- Concurrent writes (two people creating a shipment at the exact same
  moment) aren't load-tested, but SQLite serializes writes by default, so
  this should be safe for a handful of users.
