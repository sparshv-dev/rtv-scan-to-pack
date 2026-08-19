(function () {
  "use strict";

  var S = window.RTVSheets;

  function init(shipment) {
    document.getElementById("loadingState").style.display = "none";
    document.getElementById("printShell").style.display = "block";
    document.getElementById("challanNo").textContent = shipment.challanNo;
    document.getElementById("pageTitle").textContent = shipment.vendor.name + (shipment.vendor.warehouseLabel ? " — " + shipment.vendor.warehouseLabel : "");
    document.getElementById("metaVendor").textContent = shipment.vendor.name;
    document.getElementById("metaHub").textContent = "from " + shipment.hub.name;
    document.getElementById("metaTotals").textContent = shipment.totals.boxes + " box" + (shipment.totals.boxes === 1 ? "" : "es") + " · " + shipment.totals.returns + " returns · " + shipment.totals.units + " units";

    var container = document.getElementById("sheetsContainer");
    var picker = document.getElementById("sheetPicker");
    var sheets = [];

    shipment.boxes.forEach(function (box, i) {
      sheets.push({
        key: "box" + box.boxNo,
        label: "Box " + (i + 1) + " of " + shipment.boxes.length,
        html: S.labelSheetHtml(shipment, box, i + 1, shipment.boxes.length),
      });
    });
    sheets.push({ key: "invoice-vendor", label: "Invoice — Vendor Copy", html: S.invoiceSheetHtml(shipment, "vendor") });
    sheets.push({ key: "invoice-transporter", label: "Invoice — Transporter Copy", html: S.invoiceSheetHtml(shipment, "transporter") });

    sheets.forEach(function (s, idx) {
      var wrap = document.createElement("div");
      wrap.className = "sheet-view" + (idx === 0 ? " is-active print-include" : " print-include");
      wrap.dataset.key = s.key;
      wrap.innerHTML = s.html;
      container.appendChild(wrap);

      var btn = document.createElement("button");
      btn.type = "button";
      btn.textContent = s.label;
      btn.setAttribute("aria-selected", idx === 0 ? "true" : "false");
      btn.addEventListener("click", function () {
        picker.querySelectorAll("button").forEach(function (b) { b.setAttribute("aria-selected", "false"); });
        container.querySelectorAll(".sheet-view").forEach(function (v) { v.classList.remove("is-active"); });
        btn.setAttribute("aria-selected", "true");
        wrap.classList.add("is-active");
      });
      picker.appendChild(btn);
    });

    document.getElementById("printAllBtn").addEventListener("click", function () {
      container.querySelectorAll(".sheet-view").forEach(function (v) { v.classList.add("print-include"); });
      window.print();
    });
    document.getElementById("printOneBtn").addEventListener("click", function () {
      container.querySelectorAll(".sheet-view").forEach(function (v) {
        v.classList.toggle("print-include", v.classList.contains("is-active"));
      });
      window.print();
      setTimeout(function () {
        container.querySelectorAll(".sheet-view").forEach(function (v) { v.classList.add("print-include"); });
      }, 500);
    });
  }

  var params = new URLSearchParams(window.location.search);
  var id = params.get("id");
  if (!id) {
    document.getElementById("loadingState").style.display = "none";
    document.getElementById("errorState").style.display = "block";
  } else {
    fetch("/api/shipments/" + id)
      .then(function (res) {
        if (!res.ok) throw new Error("not found");
        return res.json();
      })
      .then(init)
      .catch(function () {
        document.getElementById("loadingState").style.display = "none";
        document.getElementById("errorState").style.display = "block";
      });
  }
})();
