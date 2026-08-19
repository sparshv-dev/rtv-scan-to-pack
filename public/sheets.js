/* Shared rendering for printable sheets — box label and the two invoice
   copies (Vendor / Transporter). One source of truth so print.js and the
   creation flow never drift on layout. */
(function () {
  "use strict";

  function escapeHtml(str) {
    return String(str == null ? "" : str).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function formatDate(iso) {
    if (!iso) return "";
    var d = new Date(iso + "T00:00:00");
    if (isNaN(d)) return iso;
    return d.toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric" });
  }

  function renderBarcode(text) {
    var seed = 0;
    for (var i = 0; i < text.length; i++) seed = (seed * 31 + text.charCodeAt(i)) >>> 0;
    function rand() { seed = (seed * 1664525 + 1013904223) >>> 0; return seed / 4294967295; }
    var bars = [];
    var x = 0;
    var totalWidth = 300;
    while (x < totalWidth - 4) {
      var w = 1.5 + rand() * 4.5;
      var isBar = rand() > 0.42;
      if (isBar) bars.push([x, w]);
      x += w;
    }
    var svg = '<svg viewBox="0 0 ' + totalWidth + ' 60" preserveAspectRatio="none" xmlns="http://www.w3.org/2000/svg">';
    bars.forEach(function (b) {
      svg += '<rect x="' + b[0].toFixed(2) + '" y="0" width="' + b[1].toFixed(2) + '" height="46" fill="#14110D"/>';
    });
    svg += '<text x="' + (totalWidth / 2) + '" y="58" font-size="8" text-anchor="middle" font-family="monospace" fill="#14110D">' + escapeHtml(text) + '</text>';
    svg += '</svg>';
    return svg;
  }

  // ---------------- box label: 4in x 6in ----------------
  function labelSheetHtml(shipment, box, boxIndex, totalBoxes) {
    var boxQty = box.rows.reduce(function (sum, r) { return sum + (r.qty || 0); }, 0);
    var boxLines = box.rows.length;

    return (
      "<div class='sheet label-sheet'>" +
        "<div class='label-topbar'>" +
          "<div><div class='brand'>Zilo Reverse Logistics</div><div class='service'>RETURN TO VENDOR</div></div>" +
          "<div class='stamp'>RTV</div>" +
        "</div>" +
        "<div class='box-count'><b>Box " + boxIndex + " of " + totalBoxes + "</b>Packed " + formatDate(shipment.shipDate) + " · " + escapeHtml(shipment.hub.name) + "</div>" +
        "<div class='addr-block'>" +
          "<div class='addr-row from'><div class='k'>From</div><div class='v'>" + escapeHtml(shipment.hub.name) + " — " + escapeHtml(shipment.hub.address) + (shipment.hub.phone ? " · " + escapeHtml(shipment.hub.phone) : "") + "</div></div>" +
          "<div class='addr-row to'><div class='k'>Deliver to warehouse</div><div class='v'>" +
            "<span class='vendor-name'>" + escapeHtml(shipment.vendor.name) + (shipment.vendor.warehouseLabel ? " — " + escapeHtml(shipment.vendor.warehouseLabel) : "") + "</span>" +
            escapeHtml(shipment.vendor.address) +
            (shipment.vendor.contactName || shipment.vendor.contactPhone ? "<br>Attn: " + escapeHtml(shipment.vendor.contactName || "") + (shipment.vendor.contactPhone ? " · " + escapeHtml(shipment.vendor.contactPhone) : "") : "") +
          "</div></div>" +
        "</div>" +
        "<div class='awb-block'>" +
          "<div class='awb-text'><div class='k'>Courier / AWB</div><div class='v mono'>" + escapeHtml(shipment.awb) + "</div><div class='courier'>" + escapeHtml(shipment.courier) + "</div></div>" +
          "<div class='barcode'>" + renderBarcode(shipment.awb) + "</div>" +
        "</div>" +
        "<div class='manifest'>" +
          "<div class='k'>Contents — this box</div>" +
          "<div class='qty-summary'>" +
            "<div class='qty-num'>" + boxQty + "</div>" +
            "<div class='qty-lbl'>unit" + (boxQty === 1 ? "" : "s") + " · " + boxLines + " return line" + (boxLines === 1 ? "" : "s") + "</div>" +
          "</div>" +
          "<div class='manifest-more'>Return ID / SKU detail is on the invoice packed with this shipment</div>" +
        "</div>" +
        "<div class='label-footer'>" +
          "<div class='handling'><div class='h-item'><span class='h-icon'>&#9730;</span>Keep dry</div><div class='h-item'><span class='h-icon'>&#9888;</span>No stacking</div></div>" +
          "<div class='pod-note'>Recipient: sign &amp; return the invoice packed with this shipment.</div>" +
        "</div>" +
      "</div>"
    );
  }

  // ---------------- invoice: A5, printed as Vendor Copy + Transporter Copy ----------------
  function invoiceSheetHtml(shipment, copyType) {
    var rowsHtml = shipment.manifest.map(function (r) {
      return "<tr><td class='mono'>" + escapeHtml(r.returnId) + "</td><td class='mono'>" + escapeHtml(r.sku) + "</td><td>" + escapeHtml(r.description) + "</td><td class='num'>" + r.qty + "</td><td class='num'>" + r.boxNo + "</td></tr>";
    }).join("");

    var isVendor = copyType === "vendor";
    var badgeText = isVendor ? "VENDOR COPY" : "TRANSPORTER COPY";
    var signLabel = isVendor ? "Received by — vendor warehouse" : "Handed over to — transporter";

    return (
      "<div class='sheet invoice-sheet'>" +
        "<div class='copy-badge'>" + badgeText + "</div>" +
        "<div class='invoice-head'>" +
          "<div class='brand-line'><div class='stamp'>RTV</div><div><div class='brand'>Zilo Reverse Logistics</div><h2>Return Delivery Invoice</h2></div></div>" +
          "<div class='meta'>" +
            "<div class='row'><span class='k'>Invoice No.</span><span class='v mono'>" + escapeHtml(shipment.challanNo) + "</span></div>" +
            "<div class='row'><span class='k'>Date</span><span class='v mono'>" + formatDate(shipment.shipDate) + "</span></div>" +
            "<div class='row'><span class='k'>AWB</span><span class='v mono'>" + escapeHtml(shipment.awb) + "</span></div>" +
          "</div>" +
        "</div>" +
        "<div class='invoice-summary'>" +
          "<div><span class='k'>From</span><span class='v'>" + escapeHtml(shipment.hub.name) + " — " + escapeHtml(shipment.hub.address) + "</span></div>" +
          "<div><span class='k'>To</span><span class='v'>" + escapeHtml(shipment.vendor.name) + (shipment.vendor.warehouseLabel ? " — " + escapeHtml(shipment.vendor.warehouseLabel) : "") + " — " + escapeHtml(shipment.vendor.address) + "</span></div>" +
          "<div><span class='k'>Courier</span><span class='v'>" + escapeHtml(shipment.courier) + " · <span class='mono'>" + escapeHtml(shipment.awb) + "</span></span></div>" +
          "<div><span class='k'>Packages</span><span class='v'>" + shipment.totals.boxes + " box" + (shipment.totals.boxes === 1 ? "" : "es") + " · " + shipment.totals.returns + " returns · " + shipment.totals.units + " units</span></div>" +
        "</div>" +
        "<div class='invoice-manifest'>" +
          "<table class='invoice-table'><thead><tr><th>Return ID</th><th>SKU</th><th>Description</th><th class='num'>Qty</th><th class='num'>Box</th></tr></thead><tbody>" + rowsHtml + "</tbody></table>" +
        "</div>" +
        "<div class='invoice-sign'>" +
          "<div class='signfield'><span>" + signLabel + " — name &amp; signature</span><span></span></div>" +
          "<div class='signfield'><span>Date / time</span><span></span></div>" +
        "</div>" +
        "<div class='invoice-foot'>Delivery document for movement of returned goods to vendor warehouse — not a GST tax invoice. Retain this copy as the " + (isVendor ? "vendor's" : "transporter's") + " record of handover.</div>" +
      "</div>"
    );
  }

  window.RTVSheets = {
    escapeHtml: escapeHtml,
    formatDate: formatDate,
    renderBarcode: renderBarcode,
    labelSheetHtml: labelSheetHtml,
    invoiceSheetHtml: invoiceSheetHtml,
  };
})();
