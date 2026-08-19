(function () {
  "use strict";

  var state = { hubs: [], vendors: [], manifest: [] };

  var toastEl = document.getElementById("toast");
  var toastTimer = null;
  function toast(message, isError) {
    toastEl.textContent = message;
    toastEl.classList.toggle("is-error", !!isError);
    toastEl.classList.add("is-visible");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { toastEl.classList.remove("is-visible"); }, 3200);
  }

  async function api(method, path, body) {
    var res = await fetch(path, {
      method: method,
      headers: body ? { "Content-Type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    });
    var data = null;
    try { data = await res.json(); } catch (e) { /* no body */ }
    if (!res.ok) throw new Error((data && data.error) || ("Request failed (" + res.status + ")"));
    return data;
  }

  function escapeHtml(str) {
    return String(str).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function fillSelect(select, items, labelFn) {
    var current = select.value;
    select.innerHTML = "";
    if (items.length === 0) {
      var opt = document.createElement("option");
      opt.textContent = "None saved yet — add one";
      opt.value = "";
      select.appendChild(opt);
      return;
    }
    items.forEach(function (item) {
      var opt = document.createElement("option");
      opt.value = item.id;
      opt.textContent = labelFn(item);
      select.appendChild(opt);
    });
    if (current) select.value = current;
  }

  async function loadHubs() {
    state.hubs = await api("GET", "/api/hubs");
    fillSelect(document.getElementById("hubSelect"), state.hubs, function (h) { return h.name + " (" + h.code + ")"; });
  }
  async function loadVendors() {
    state.vendors = await api("GET", "/api/vendors");
    fillSelect(document.getElementById("vendorSelect"), state.vendors, function (v) {
      return v.name + (v.warehouseLabel ? " — " + v.warehouseLabel : "");
    });
  }

  // ---------- inline hub/vendor add ----------
  function wireToggle(btnId, formId) {
    document.getElementById(btnId).addEventListener("click", function () {
      document.getElementById(formId).classList.toggle("is-open");
    });
  }
  wireToggle("toggleNewHub", "newHubForm");
  wireToggle("toggleNewVendor", "newVendorForm");
  document.getElementById("cancelHub").addEventListener("click", function () {
    document.getElementById("newHubForm").classList.remove("is-open");
  });
  document.getElementById("cancelVendor").addEventListener("click", function () {
    document.getElementById("newVendorForm").classList.remove("is-open");
  });

  document.getElementById("saveHub").addEventListener("click", async function () {
    try {
      var hub = await api("POST", "/api/hubs", {
        name: document.getElementById("nh-name").value,
        code: document.getElementById("nh-code").value,
        address: document.getElementById("nh-address").value,
        phone: document.getElementById("nh-phone").value,
      });
      await loadHubs();
      document.getElementById("hubSelect").value = hub.id;
      document.getElementById("newHubForm").classList.remove("is-open");
      ["nh-name", "nh-code", "nh-address", "nh-phone"].forEach(function (id) { document.getElementById(id).value = ""; });
      toast("Hub saved: " + hub.name);
    } catch (e) { toast(e.message, true); }
  });

  document.getElementById("saveVendor").addEventListener("click", async function () {
    try {
      var vendor = await api("POST", "/api/vendors", {
        name: document.getElementById("nv-name").value,
        warehouseLabel: document.getElementById("nv-warehouse").value,
        address: document.getElementById("nv-address").value,
        contactPhone: document.getElementById("nv-contact-phone").value,
      });
      await loadVendors();
      document.getElementById("vendorSelect").value = vendor.id;
      document.getElementById("newVendorForm").classList.remove("is-open");
      ["nv-name", "nv-warehouse", "nv-address", "nv-contact-phone"].forEach(function (id) { document.getElementById(id).value = ""; });
      toast("Vendor saved: " + vendor.name);
    } catch (e) { toast(e.message, true); }
  });

  // ---------- manifest table ----------
  var manifestBody = document.getElementById("manifestBody");

  function renderManifest() {
    manifestBody.innerHTML = "";
    document.getElementById("manifestEmpty").style.display = state.manifest.length ? "none" : "block";
    state.manifest.forEach(function (row, idx) {
      var tr = document.createElement("tr");
      tr.innerHTML =
        "<td><input data-field='returnId' data-idx='" + idx + "' value='" + escapeHtml(row.returnId) + "'></td>" +
        "<td><input data-field='sku' data-idx='" + idx + "' value='" + escapeHtml(row.sku) + "'></td>" +
        "<td><input data-field='description' data-idx='" + idx + "' value='" + escapeHtml(row.description) + "'></td>" +
        "<td class='num-col'><input data-field='qty' data-idx='" + idx + "' type='number' min='1' value='" + row.qty + "'></td>" +
        "<td class='num-col'><input data-field='boxNo' data-idx='" + idx + "' type='number' min='1' value='" + row.boxNo + "'></td>" +
        "<td><button type='button' class='row-del' data-idx='" + idx + "' title='Remove row'>&times;</button></td>";
      manifestBody.appendChild(tr);
    });
    updateSummary();
  }

  manifestBody.addEventListener("input", function (e) {
    var field = e.target.dataset.field;
    var idx = e.target.dataset.idx;
    if (!field || idx === undefined) return;
    var row = state.manifest[idx];
    if (field === "qty" || field === "boxNo") {
      row[field] = parseInt(e.target.value, 10) || 0;
    } else {
      row[field] = e.target.value;
    }
    updateSummary();
  });
  manifestBody.addEventListener("click", function (e) {
    if (!e.target.classList.contains("row-del")) return;
    var idx = parseInt(e.target.dataset.idx, 10);
    state.manifest.splice(idx, 1);
    renderManifest();
  });

  function updateSummary() {
    var boxes = new Set(state.manifest.map(function (r) { return r.boxNo; }));
    var units = state.manifest.reduce(function (sum, r) { return sum + (r.qty || 0); }, 0);
    document.getElementById("statBoxes").textContent = boxes.size;
    document.getElementById("statReturns").textContent = state.manifest.length;
    document.getElementById("statUnits").textContent = units;
  }

  document.getElementById("addRowBtn").addEventListener("click", function () {
    var lastBox = state.manifest.length ? state.manifest[state.manifest.length - 1].boxNo : 1;
    state.manifest.push({ returnId: "", sku: "", description: "", qty: 1, boxNo: lastBox });
    renderManifest();
  });

  function parsePastedManifest(text) {
    var lines = text.split(/\r?\n/).map(function (l) { return l.trim(); }).filter(Boolean);
    var rows = [];
    lines.forEach(function (line) {
      var parts = line.indexOf("\t") !== -1 ? line.split("\t") : line.split(",");
      parts = parts.map(function (p) { return p.trim(); });
      if (parts.length < 4) return;
      var first = (parts[0] || "").toLowerCase();
      if (["return", "return id", "return_id", "returnid"].indexOf(first) !== -1) return;
      var returnId = parts[0] || "";
      var sku = parts[1] || "";
      var description = parts.length >= 5 ? parts[2] : "";
      var qtyRaw = parts.length >= 5 ? parts[3] : parts[2];
      var boxRaw = parts.length >= 5 ? parts[4] : parts[3];
      var qty = parseInt(qtyRaw, 10);
      var boxNo = parseInt(boxRaw, 10);
      if (!returnId || !sku) return;
      rows.push({
        returnId: returnId, sku: sku, description: description || "",
        qty: isNaN(qty) || qty < 1 ? 1 : qty,
        boxNo: isNaN(boxNo) || boxNo < 1 ? 1 : boxNo,
      });
    });
    return rows;
  }

  document.getElementById("parseBtn").addEventListener("click", function () {
    var text = document.getElementById("pasteArea").value;
    var rows = parsePastedManifest(text);
    if (rows.length === 0) { toast("Couldn't find any valid rows to parse", true); return; }
    state.manifest = state.manifest.concat(rows);
    renderManifest();
    document.getElementById("pasteArea").value = "";
    toast("Added " + rows.length + " row" + (rows.length === 1 ? "" : "s") + " from paste");
  });

  // ---------- create shipment ----------
  document.getElementById("createBtn").addEventListener("click", async function () {
    var errEl = document.getElementById("formError");
    errEl.textContent = "";
    var hubId = document.getElementById("hubSelect").value;
    var vendorId = document.getElementById("vendorSelect").value;
    var courier = document.getElementById("courierInput").value.trim();
    var awb = document.getElementById("awbInput").value.trim();
    var shipDate = document.getElementById("shipDateInput").value;

    if (!hubId) { errEl.textContent = "Pick or add a hub."; return; }
    if (!vendorId) { errEl.textContent = "Pick or add a vendor warehouse."; return; }
    if (!courier) { errEl.textContent = "Courier name is required."; return; }
    if (!awb) { errEl.textContent = "AWB / tracking number is required."; return; }
    if (!shipDate) { errEl.textContent = "Ship date is required."; return; }
    if (state.manifest.length === 0) { errEl.textContent = "Add at least one manifest row."; return; }
    for (var i = 0; i < state.manifest.length; i++) {
      var r = state.manifest[i];
      if (!r.returnId || !r.sku || !r.qty || !r.boxNo) {
        errEl.textContent = "Row " + (i + 1) + " is missing Return ID, SKU, Qty, or Box #.";
        return;
      }
    }

    var btn = document.getElementById("createBtn");
    btn.disabled = true;
    try {
      var result = await api("POST", "/api/shipments", {
        hubId: hubId, vendorId: vendorId, courier: courier, awb: awb, shipDate: shipDate,
        manifest: state.manifest,
      });
      toast("Shipment created: " + result.challanNo);
      window.location.href = "/print.html?id=" + result.id;
    } catch (e) {
      errEl.textContent = e.message;
    } finally {
      btn.disabled = false;
    }
  });

  // ---------- recent shipments ----------
  async function loadRecent() {
    var body = document.getElementById("recentBody");
    var list = await api("GET", "/api/shipments");
    body.innerHTML = "";
    document.getElementById("recentEmpty").style.display = list.length ? "none" : "block";
    list.forEach(function (s) {
      var tr = document.createElement("tr");
      tr.innerHTML =
        "<td><a class='recent-row-link mono' href='/print.html?id=" + s.id + "'>" + escapeHtml(s.challanNo) + "</a></td>" +
        "<td>" + escapeHtml(s.shipDate) + "</td>" +
        "<td>" + escapeHtml(s.hubName) + "</td>" +
        "<td>" + escapeHtml(s.vendorName) + (s.warehouseLabel ? " — " + escapeHtml(s.warehouseLabel) : "") + "</td>" +
        "<td>" + s.boxes + "</td>" +
        "<td>" + s.units + "</td>";
      body.appendChild(tr);
    });
  }

  // ---------- init ----------
  document.getElementById("shipDateInput").value = new Date().toISOString().slice(0, 10);
  Promise.all([loadHubs(), loadVendors(), loadRecent()]).catch(function (e) { toast(e.message, true); });
})();
