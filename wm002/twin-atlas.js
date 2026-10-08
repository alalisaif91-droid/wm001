/* WM Twin Map · Atlas: map products from the twin. Three templates: flood impacts (monochrome base, blue extents per rainfall
   scenario, impacted houses coloured), collection days (areas by service day, no schedule hatched), scenario view (callouts on
   leader lines, water flow arrows from the terrain model, highlights, photo insets). Title, legend, scale bar, north arrow and insets
   are composed on export to a PNG. Uses window.__twinMap (twin-map.js) and the Smart Waste Platform flood API; never a feature
   that is not a record. */
(function () {
  "use strict";
  var C = window.Cesium; var TM = window.__twinMap; if (!TM) return;
  var $ = function (id) { return document.getElementById(id); };
  var esc = function (s) { return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]; }); };
  var SWMP = TM.swmp; var viewer = null;
  var DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];
  var DAY_COLOUR = { Sunday: "#F6E7A7", Monday: "#BBDDF5", Tuesday: "#F5B6BE", Wednesday: "#BFEAD8", Thursday: "#E4C9F0", Friday: "#FFE0B3", Saturday: "#D5E8B5" };
  var SCEN_BLUE = ["#1F4E9C", "#4A90D9", "#A9CBEF", "#D6E6F7"]; // the most frequent storm (smallest rainfall) darkest, as on a floodplain map
  var IMPACT = { frequent: "#7B3FA0", design: "#F28CB1" };
  var ROLE = { water: "#2F80ED", protect: "#3BAA57", plot: "#F0A05A", hazard: "#D9342B" };
  var YELLOW = "#FFE600";
  var A = { template: "flood", print: false, scen: [], selected: {}, zones: [], roads: null, borders: null, days: null, schedules: {}, story: null, items: [], insets: [], impacts: null, mode: null, draw: { pts: [], ent: null }, pending: null, legend: [], hidden: [], xrayWasOn: false };

  function status(t) { var el = $("at-out"); if (el) el.textContent = t; var s = $("status"); if (s) s.textContent = t; }
  function hint(t) { var el = $("at-hint"); if (el) el.textContent = t; }
  function fetchJson(u) { return fetch(u, { headers: { Accept: "application/json" } }).then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); }); }
  function sectorsBbox() { var g = window.__wmaTwin && window.__wmaTwin.toGeoJSON ? window.__wmaTwin.toGeoJSON() : null; var w = 180, s = 90, e = -180, n = -90; if (g) g.features.forEach(function (f) { var rings = f.geometry.type === "Polygon" ? f.geometry.coordinates : [].concat.apply([], f.geometry.coordinates); rings.forEach(function (ring) { ring.forEach(function (c) { w = Math.min(w, c[0]); e = Math.max(e, c[0]); s = Math.min(s, c[1]); n = Math.max(n, c[1]); }); }); }); return w > e ? [55.70, 24.80, 56.30, 26.10] : [w, s, e, n]; }
  function viewBbox() { var rect = viewer.camera.computeViewRectangle(viewer.scene.globe.ellipsoid); if (!rect) return null; return [C.Math.toDegrees(rect.west), C.Math.toDegrees(rect.south), C.Math.toDegrees(rect.east), C.Math.toDegrees(rect.north)]; }
  function groundPoint(pos) { var scene = viewer.scene; var cart = scene.pickPosition && scene.pickPositionSupported ? scene.pickPosition(pos) : undefined; if (!C.defined(cart)) { var ray = viewer.camera.getPickRay(pos); cart = ray ? scene.globe.pick(ray, scene) : undefined; } if (!C.defined(cart)) return null; var g = C.Cartographic.fromCartesian(cart); return { lon: C.Math.toDegrees(g.longitude), lat: C.Math.toDegrees(g.latitude) }; }
  function offset(p, dxm, dym) { return { lon: p.lon + dxm / (111320 * Math.cos(p.lat * Math.PI / 180)), lat: p.lat + dym / 110540 }; }
  function metresPerPixel() { var c = viewer.canvas; var a = groundPoint(new C.Cartesian2(c.clientWidth / 2 - 50, c.clientHeight / 2)); var b = groundPoint(new C.Cartesian2(c.clientWidth / 2 + 50, c.clientHeight / 2)); if (!a || !b) return null; var d = C.Cartesian3.distance(C.Cartesian3.fromDegrees(a.lon, a.lat), C.Cartesian3.fromDegrees(b.lon, b.lat)); return d / 100; }

  // ------------------------------------------------------------ print look: monochrome base, thin grey roads, grey houses, white sector borders
  function printLook(on) {
    A.print = on; var scene = viewer.scene;
    for (var i = 0; i < viewer.imageryLayers.length; i++) { var l = viewer.imageryLayers.get(i); if (A.zones.indexOf(l) >= 0) continue; l.saturation = on ? 0 : 1; l.brightness = on ? 1.25 : 1; l.contrast = on ? 0.95 : 1; l.gamma = on ? 1.35 : 1; }
    scene.globe.baseColor = on ? C.Color.WHITE : C.Color.fromCssColorString("#0C1319");
    var xr = document.querySelector('#twin-host button[data-act="xray"]'); var xrOn = xr && (xr.classList.contains("on") || xr.getAttribute("aria-pressed") === "true");
    if (on && xrOn) { A.xrayWasOn = true; xr.click(); } else if (!on && A.xrayWasOn && xr && !xrOn) { xr.click(); A.xrayWasOn = false; }
    var T = window.__wmaTwin;
    if (T && T.layers) Object.keys(T.layers).forEach(function (k) { if (/road|complain|survey|facilit|case|transfer|landfill|mrf|proposed/i.test(k)) { (T.layers[k].items || []).forEach(function (e) { try { if (on) { if (e.show !== false) A.hidden.push(e); e.show = false; } } catch (x) {} }); } });
    if (!on) { A.hidden.forEach(function (e) { try { e.show = true; } catch (x) {} }); A.hidden = []; }
    Object.keys(TM.ds).forEach(function (k) { var d = TM.ds[k]; if (!d || k === "roads_labelled") return; if (on) { d.show = false; } else { var l = TM.layers.find(function (x) { return (x.dsKey || x.key) === k; }) || TM.floodLayers.find(function (x) { return x.key === k; }); d.show = l ? l.on !== false : true; } });
    TM.cellPrims().forEach(function (p) { p.show = !on; });
    TM.sectorBorders(!on);
    if (on) { loadRoads(); loadBorders(); } else { if (A.roads) { A.roads = null; TM.roadsLabelled(); } if (A.borders) { viewer.dataSources.remove(A.borders, true); A.borders = null; } }
    if (!on) { clearTemplate(); TM.rebuildHouses(null); }
  }
  async function loadRoads() {
    // the same cased and named road layer as the working map, in print grey with black names on a white halo
    if (A.roads) { A.roads = null; }
    try { var ds = await TM.roadsLabelled("print"); if (ds) { ds.show = true; A.roads = ds; TM.twinRoads(false); } viewer.scene.requestRender(); } catch (e) {}
  }
  function loadBorders() {
    if (A.borders) { viewer.dataSources.remove(A.borders, true); A.borders = null; }
    var T = window.__wmaTwin; if (!T || !T.toGeoJSON) return; var ds = new C.CustomDataSource("atlas-borders");
    T.toGeoJSON().features.forEach(function (f) { var rings = f.geometry.type === "Polygon" ? f.geometry.coordinates : [].concat.apply([], f.geometry.coordinates); rings.forEach(function (ring, ri) { var flat = []; ring.forEach(function (c) { flat.push(c[0], c[1]); }); ds.entities.add({ polyline: { positions: C.Cartesian3.fromDegreesArray(flat), width: 4, material: C.Color.WHITE, clampToGround: true } }); if (ri === 0) { var lon = 0, lat = 0; ring.forEach(function (c) { lon += c[0]; lat += c[1]; }); lon /= ring.length; lat /= ring.length; ds.entities.add({ position: C.Cartesian3.fromDegrees(lon, lat), label: { text: "SECTOR " + f.properties.n, font: "bold 16px Calibri, sans-serif", fillColor: C.Color.fromCssColorString("#1B1B1B"), outlineColor: C.Color.WHITE, outlineWidth: 4, style: C.LabelStyle.FILL_AND_OUTLINE, heightReference: C.HeightReference.CLAMP_TO_GROUND, disableDepthTestDistance: Number.POSITIVE_INFINITY, scaleByDistance: new C.NearFarScalar(5000, 1.0, 120000, 0.6) } }); } }); });
    A.borders = ds; viewer.dataSources.add(ds);
  }
  function clearTemplate() {
    A.zones.forEach(function (d) { if (d instanceof C.ImageryLayer) viewer.imageryLayers.remove(d, true); else viewer.dataSources.remove(d, true); }); A.zones = [];
    if (A.days) { viewer.dataSources.remove(A.days, true); A.days = null; }
    A.legend = [];
    renderOverlay();
  }

  // ------------------------------------------------------------ flood impacts template
  async function loadScenarios() {
    try { A.scen = (await fetchJson(SWMP + "/v1/flood/scenario-runs")).items; } catch (e) { A.scen = []; $("at-scen").textContent = "flood platform not reachable at " + SWMP; return; }
    var seen = {}; A.scen = A.scen.filter(function (s) { var k = s.scenarioMm + "/" + s.scenarioHours; if (seen[k]) return false; seen[k] = 1; return true; }).sort(function (a, b) { return a.scenarioMm - b.scenarioMm; });
    A.scen.forEach(function (s) { if (!(s.id in A.selected)) A.selected[s.id] = true; });
    $("at-scen").innerHTML = A.scen.length ? A.scen.map(function (s, i) { return '<label style="margin:2px 0"><input type="checkbox" data-scen="' + s.id + '"' + (A.selected[s.id] ? " checked" : "") + '> <span class="sw" style="display:inline-block;width:12px;height:12px;background:' + SCEN_BLUE[Math.min(i, 3)] + ';vertical-align:-1px;margin-right:4px"></span>' + s.scenarioMm + ' mm in ' + s.scenarioHours + ' h (' + esc(s.code) + (s.dataStatus === "demo" ? ", DEMO ONLY" : "") + ')</label>'; }).join("") : '<span>no scenario runs yet: run the model on the platform first</span>';
    $("at-scen").querySelectorAll("input[data-scen]").forEach(function (cb) { cb.onchange = function () { A.selected[cb.dataset.scen] = cb.checked; }; });
  }
  async function applyFlood() {
    clearTemplate(); var bb = sectorsBbox(); var chosen = A.scen.filter(function (s) { return A.selected[s.id]; });
    // the band the map draws: high is the model's "avoid where possible", moderate is "review before dispatch" and covers most of the grid
    var band = ($("at-band") && $("at-band").value) || "high";
    var bandLabel = band === "high" ? "HIGH AND ABOVE (AVOID WHERE POSSIBLE)" : "MODERATE AND ABOVE (REVIEW BEFORE DISPATCH)";
    status("Drawing the " + bandLabel.toLowerCase() + " band for " + chosen.length + " scenario" + (chosen.length === 1 ? "" : "s") + "…");
    var order = chosen.slice().sort(function (a, b) { return b.scenarioMm - a.scenarioMm; }); // largest storm first, underneath
    for (var i = 0; i < order.length; i++) {
      var s = order[i]; var idx = chosen.indexOf(s); var hex = SCEN_BLUE[Math.min(idx, 3)];
      // the extent comes as a transparent image at cell resolution, draped as imagery: light on the client however many cells the storm covers
      try { var prov = await C.SingleTileImageryProvider.fromUrl(SWMP + "/v1/flood/scenario-image.png?runId=" + s.id + "&minClass=" + band + "&colour=" + hex.slice(1), { rectangle: C.Rectangle.fromDegrees(s.bounds[0], s.bounds[1], s.bounds[2], s.bounds[3]) }); var lay = viewer.imageryLayers.addImageryProvider(prov); lay.alpha = 0.85; lay.magnificationFilter = C.TextureMagnificationFilter.NEAREST; A.zones.push(lay); var cells = band === "high" ? ((s.classes.high || 0) + (s.classes.very_high || 0)) : ((s.classes.moderate || 0) + (s.classes.high || 0) + (s.classes.very_high || 0));
        A.legend.push({ colour: hex, text: "RISK " + bandLabel + ", " + s.scenarioMm + " MM IN " + s.scenarioHours + " H (" + cells.toLocaleString("en-GB") + " CELLS)" + (s.dataStatus === "demo" ? " (DEMO ONLY)" : "") }); } catch (e) { status("Extent for " + s.code + " failed: " + e.message); }
    }
    if ($("at-impacts").checked && chosen.length) {
      status("Classifying the houses inside the zones (whole emirate, about a minute)…");
      try {
        A.impacts = await fetchJson(SWMP + "/v1/flood/building-impacts?bbox=" + bb.join(",") + "&runIds=" + chosen.map(function (s) { return s.id; }).join(",") + "&minClass=" + band);
        // a house takes the most frequent storm (smallest rainfall) whose zone touches it: purple, pink, then a light violet for the extreme storm only
        var byRef = {}; A.impacts.buildings.forEach(function (b) { byRef[b.ref] = b.scenarioMm; }); var mms = chosen.map(function (s) { return s.scenarioMm; }).sort(function (a, b) { return a - b; });
        var ramp = [IMPACT.frequent, IMPACT.design, "#D9C6E8"]; var cols = ramp.map(function (h) { return C.Color.fromCssColorString(h).withAlpha(0.95); }); var grey = C.Color.fromCssColorString("#4A4A4A").withAlpha(0.9);
        await TM.rebuildHouses(function (i) { var mm = byRef["house-" + i]; if (mm == null) return grey; var k = mms.indexOf(mm); return cols[Math.min(Math.max(k, 0), 2)]; });
        mms.forEach(function (mm, k) { var n = A.impacts.buildings.filter(function (b) { return b.scenarioMm === mm; }).length; A.legend.push({ colour: ramp[Math.min(k, 2)], text: (k === 0 ? "HOUSES ON A DRAWN CELL AT " : "HOUSES REACHED ONLY AT ") + mm + " MM (" + n.toLocaleString("en-GB") + ")" }); });
        A.legend.push({ colour: "#4A4A4A", text: "OTHER HOUSES" });
      } catch (e) { status("Impacted houses failed: " + e.message); }
    } else { await TM.rebuildHouses(function () { return C.Color.fromCssColorString("#4A4A4A").withAlpha(0.9); }); }
    A.legend.push({ colour: "#FFFFFF", text: "SECTOR BOUNDARIES SHOWN IN WHITE", border: true });
    renderOverlay(); status("Risk map ready · " + chosen.length + " scenario" + (chosen.length === 1 ? "" : "s") + " at " + band + " and above" + (A.impacts ? " · " + A.impacts.count + " houses on a drawn cell" : ""));
  }

  // ------------------------------------------------------------ collection days template
  var TOKEN = function () { try { return sessionStorage.getItem("omcc-platform-token") || ""; } catch (e) { return ""; } };
  var PLAT = function () { try { return localStorage.getItem("omcc-platform-url") || "http://127.0.0.1:8100"; } catch (e) { return "http://127.0.0.1:8100"; } };
  function areas() { var out = []; ["landuse_public", "service_area"].forEach(function (k) { (TM.feats[k] || []).forEach(function (f) { if (f.geometry && /Polygon/.test(f.geometry.type)) out.push(f); }); }); return out; }
  async function loadSchedules() {
    A.schedules = {};
    try { var fc = await TM.api.features("collection_schedule"); (fc.features || []).forEach(function (f) { var a = f.properties.attributes || {}; if (a.areaId) A.schedules[a.areaId] = { id: f.id, days: a.days || [], note: a.note || "" }; }); } catch (e) {}
  }
  function renderAreas() {
    var el = $("at-areas"); var list = areas();
    if (!list.length) { el.innerHTML = '<div class="note">No drawn public or service areas loaded (sign in and load the layers).</div>'; return; }
    el.innerHTML = list.map(function (f) { var sc = A.schedules[f.id] || { days: [] }; return '<div class="layer" style="grid-template-columns:1fr;gap:4px"><b>' + esc(f.properties.name) + '</b><div class="row" style="gap:6px">' + DAYS.map(function (d) { return '<label style="margin:0;display:inline-flex;gap:3px;align-items:center;font-size:11.5px"><input type="checkbox" data-area="' + esc(f.id) + '" data-day="' + d + '"' + (sc.days.indexOf(d) >= 0 ? " checked" : "") + ' style="width:auto"> ' + d.slice(0, 3) + '</label>'; }).join("") + '<button class="btn ghost" data-save="' + esc(f.id) + '" style="margin:0;padding:3px 8px">Save</button></div></div>'; }).join("");
    el.querySelectorAll("button[data-save]").forEach(function (b) { b.onclick = function () { saveSchedule(b.dataset.save); }; });
  }
  async function saveSchedule(areaId) {
    if (!TM.can("edit")) { status("Sign in with an editing role to save a schedule"); return; }
    var f = areas().find(function (x) { return x.id === areaId; }); if (!f) return;
    var days = Array.prototype.map.call(document.querySelectorAll('input[data-area="' + areaId + '"]:checked'), function (cb) { return cb.dataset.day; });
    var old = A.schedules[areaId];
    try {
      if (old) { await fetch(PLAT() + "/v1/om/features/" + encodeURIComponent(old.id), { method: "DELETE", headers: { "Content-Type": "application/json", Authorization: "Bearer " + TOKEN() }, body: JSON.stringify({ reason: "collection schedule reassigned on the Atlas" }) }); }
      if (days.length) { await TM.api.addFeature({ "class": "collection_schedule", name: "Schedule " + f.properties.name, geometry: f.geometry, origin: "user_submitted", source: "assigned on the WM Twin Map Atlas", hAccuracyM: 5, captureDate: new Date().toISOString().slice(0, 10), surveyMethod: "schedule assignment", attributes: { areaId: areaId, areaName: f.properties.name, days: days, kind: "collection_schedule" } }); }
      await loadSchedules(); renderAreas(); await applyDays(); status("Schedule saved for " + f.properties.name + ": " + (days.join(", ") || "no collection"));
    } catch (e) { status("Schedule not saved: " + e.message); }
  }
  async function applyDays() {
    clearTemplate(); await loadSchedules(); renderAreas();
    var ds = new C.CustomDataSource("atlas-days"); var used = {};
    areas().forEach(function (f) {
      var sc = A.schedules[f.id]; var days = sc ? sc.days : []; var first = days[0];
      var mat = first ? C.Color.fromCssColorString(DAY_COLOUR[first]).withAlpha(0.85) : new C.StripeMaterialProperty({ evenColor: C.Color.fromCssColorString("#BFBFBF").withAlpha(0.7), oddColor: C.Color.WHITE.withAlpha(0.7), repeat: 24, orientation: C.StripeOrientation.VERTICAL });
      if (first) used[first] = 1;
      var rings = f.geometry.type === "Polygon" ? [f.geometry.coordinates] : f.geometry.coordinates;
      rings.forEach(function (poly, pi) { var flat = []; poly[0].forEach(function (c) { flat.push(c[0], c[1]); }); ds.entities.add({ polygon: { hierarchy: new C.PolygonHierarchy(C.Cartesian3.fromDegreesArray(flat)), material: mat, classificationType: C.ClassificationType.TERRAIN } });
        if (pi === 0) { var lon = 0, lat = 0; poly[0].forEach(function (c) { lon += c[0]; lat += c[1]; }); lon /= poly[0].length; lat /= poly[0].length; ds.entities.add({ position: C.Cartesian3.fromDegrees(lon, lat), label: { text: String(f.properties.name).toUpperCase() + (days.length ? "\n" + days.map(function (d) { return d.slice(0, 3); }).join(", ") : "\nNO SCHEDULE ASSIGNED"), font: "bold 13px Calibri, sans-serif", fillColor: C.Color.fromCssColorString("#1B1B1B"), outlineColor: C.Color.WHITE, outlineWidth: 4, style: C.LabelStyle.FILL_AND_OUTLINE, heightReference: C.HeightReference.CLAMP_TO_GROUND, disableDepthTestDistance: Number.POSITIVE_INFINITY, scaleByDistance: new C.NearFarScalar(3000, 1.0, 80000, 0.5) } }); } });
    });
    A.days = ds; viewer.dataSources.add(ds);
    A.legend = [{ colour: "hatch", text: "NO SCHEDULE ASSIGNED" }].concat(DAYS.filter(function (d) { return used[d]; }).map(function (d) { return { colour: DAY_COLOUR[d], text: d.toUpperCase() }; }));
    A.legend.push({ colour: "#FFFFFF", text: "SECTOR BOUNDARIES SHOWN IN WHITE", border: true });
    await TM.rebuildHouses(function () { return C.Color.fromCssColorString("#3A3A3A").withAlpha(0.9); });
    renderOverlay(); status("Collection day map ready · " + areas().length + " areas, " + Object.keys(A.schedules).length + " with a schedule");
  }

  // ------------------------------------------------------------ scenario view: annotations
  function storyDs() { if (!A.story) { A.story = new C.CustomDataSource("atlas-story"); viewer.dataSources.add(A.story); } return A.story; }
  function callout(anchor, text, colour) {
    var ds = storyDs(); var mpp = metresPerPixel() || 2; var tip = offset(anchor, 90 * mpp, 70 * mpp);
    ds.entities.add({ polyline: { positions: C.Cartesian3.fromDegreesArray([anchor.lon, anchor.lat, tip.lon, tip.lat]), width: 2, material: C.Color.fromCssColorString(YELLOW), clampToGround: true } });
    ds.entities.add({ position: C.Cartesian3.fromDegrees(anchor.lon, anchor.lat), point: { pixelSize: 7, color: C.Color.fromCssColorString(YELLOW), outlineColor: C.Color.BLACK, outlineWidth: 1, heightReference: C.HeightReference.CLAMP_TO_GROUND, disableDepthTestDistance: Number.POSITIVE_INFINITY } });
    ds.entities.add({ position: C.Cartesian3.fromDegrees(tip.lon, tip.lat), label: { text: String(text || "").toUpperCase(), font: "bold 13px Calibri, sans-serif", fillColor: C.Color.BLACK, showBackground: true, backgroundColor: C.Color.fromCssColorString(colour || YELLOW), backgroundPadding: new C.Cartesian2(8, 5), horizontalOrigin: C.HorizontalOrigin.LEFT, verticalOrigin: C.VerticalOrigin.BOTTOM, heightReference: C.HeightReference.CLAMP_TO_GROUND, disableDepthTestDistance: Number.POSITIVE_INFINITY } });
    A.items.push({ kind: "callout", anchor: anchor, text: text });
  }
  function highlight(ring, role, text) {
    var ds = storyDs(); var col = C.Color.fromCssColorString(ROLE[role] || ROLE.water); var flat = []; ring.forEach(function (p) { flat.push(p.lon, p.lat); });
    ds.entities.add({ polygon: { hierarchy: new C.PolygonHierarchy(C.Cartesian3.fromDegreesArray(flat)), material: col.withAlpha(0.55), classificationType: C.ClassificationType.TERRAIN } });
    var closed = flat.concat([flat[0], flat[1]]);
    ds.entities.add({ polyline: { positions: C.Cartesian3.fromDegreesArray(closed), width: 3, material: new C.PolylineDashMaterialProperty({ color: C.Color.WHITE, dashLength: 14 }), clampToGround: true } });
    var lon = 0, lat = 0; ring.forEach(function (p) { lon += p.lon; lat += p.lat; }); lon /= ring.length; lat /= ring.length;
    if (text) callout({ lon: lon, lat: lat }, text);
    A.items.push({ kind: "highlight", ring: ring, role: role, text: text });
  }
  async function flowFrom(p) {
    hint("Tracing the water path from the terrain model…");
    try {
      var f = await fetchJson(SWMP + "/v1/flood/flow-path?lon=" + p.lon + "&lat=" + p.lat); var ds = storyDs(); var flat = []; f.geometry.coordinates.forEach(function (c) { flat.push(c[0], c[1]); });
      if (flat.length >= 4) ds.entities.add({ polyline: { positions: C.Cartesian3.fromDegreesArray(flat), width: 9, material: new C.PolylineArrowMaterialProperty(C.Color.fromCssColorString("#2F80ED")), clampToGround: true } });
      var end = f.geometry.coordinates[f.geometry.coordinates.length - 1]; var pr = f.properties;
      callout({ lon: end[0], lat: end[1] }, "WATER COLLECTS HERE · " + pr.lengthM + " M, DROP " + pr.dropM + " M · " + pr.stoppedBecause + (pr.endRiskClass ? " · " + pr.endRiskClass.replace("_", " ") + " " + pr.endScore : ""), "#BBDDF5");
      A.items.push({ kind: "flow", from: p, feature: f }); hint("Flow path: " + pr.steps + " cells, " + pr.lengthM + " m, ends: " + pr.stoppedBecause + ".");
    } catch (e) { hint("No flow path: " + e.message); }
  }
  async function zonesInView() {
    var bb = viewBbox(); if (!bb) return; hint("Loading the accumulation zones in view…");
    try {
      var fc = await fetchJson(SWMP + "/v1/flood/accumulation-zones?bbox=" + bb.join(",") + "&minClass=moderate"); var ds = storyDs(); var n = 0;
      fc.features.forEach(function (f) { var col = C.Color.fromCssColorString(f.properties.risk_class === "moderate" ? "#4A90D9" : "#1F4E9C"); var polys = f.geometry.type === "Polygon" ? [f.geometry.coordinates] : f.geometry.coordinates; polys.forEach(function (poly) { var flat = []; poly[0].forEach(function (c) { flat.push(c[0], c[1]); }); ds.entities.add({ polygon: { hierarchy: new C.PolygonHierarchy(C.Cartesian3.fromDegreesArray(flat)), material: col.withAlpha(0.6), classificationType: C.ClassificationType.TERRAIN } }); ds.entities.add({ polyline: { positions: C.Cartesian3.fromDegreesArray(flat.concat([flat[0], flat[1]])), width: 2, material: new C.PolylineDashMaterialProperty({ color: C.Color.WHITE, dashLength: 10 }), clampToGround: true } }); n++; }); });
      if (fc.features.length) { var big = fc.features[0]; var polys = big.geometry.type === "Polygon" ? [big.geometry.coordinates] : big.geometry.coordinates; var ring = polys[0][0]; var lon = 0, lat = 0; ring.forEach(function (c) { lon += c[0]; lat += c[1]; }); lon /= ring.length; lat /= ring.length; callout({ lon: lon, lat: lat }, "PREDICTED ACCUMULATION · " + big.properties.risk_class.replace("_", " ") + " · " + big.properties.cells + " CELLS · " + big.properties.runCode, "#BBDDF5"); }
      A.items.push({ kind: "zones", bbox: bb }); hint(n + " zone polygons drawn from the latest model run.");
    } catch (e) { hint("Zones failed: " + e.message); }
  }
  function photoInset(p, file) {
    var reader = new FileReader(); reader.onload = function () { var img = new Image(); img.onload = function () {
      var c = document.createElement("canvas"); var S = 180; c.width = S; c.height = S; var g = c.getContext("2d");
      g.save(); g.beginPath(); g.arc(S / 2, S / 2, S / 2 - 6, 0, Math.PI * 2); g.closePath(); g.clip(); var r = Math.max(S / img.width, S / img.height); g.drawImage(img, (S - img.width * r) / 2, (S - img.height * r) / 2, img.width * r, img.height * r); g.restore();
      g.lineWidth = 6; g.strokeStyle = YELLOW; g.beginPath(); g.arc(S / 2, S / 2, S / 2 - 4, 0, Math.PI * 2); g.stroke();
      var ds = storyDs(); var mpp = metresPerPixel() || 2; var tip = offset(p, -140 * mpp, 120 * mpp);
      ds.entities.add({ polyline: { positions: C.Cartesian3.fromDegreesArray([p.lon, p.lat, tip.lon, tip.lat]), width: 2, material: C.Color.fromCssColorString(YELLOW), clampToGround: true } });
      ds.entities.add({ position: C.Cartesian3.fromDegrees(tip.lon, tip.lat), billboard: { image: c, width: 150, height: 150, heightReference: C.HeightReference.CLAMP_TO_GROUND, disableDepthTestDistance: Number.POSITIVE_INFINITY } });
      A.items.push({ kind: "photo", at: p, name: file.name }); hint("Photo inset placed: " + file.name);
    }; img.src = reader.result; }; reader.readAsDataURL(file);
  }
  function clearStory() { if (A.story) { viewer.dataSources.remove(A.story, true); A.story = null; } A.items = []; hint(""); }
  var handler = null;
  function installHandler() {
    handler = new C.ScreenSpaceEventHandler(viewer.scene.canvas);
    handler.setInputAction(function (m) {
      if (!A.mode) return; var p = groundPoint(m.position); if (!p) return;
      if (A.mode === "callout") { callout(p, $("at-callout").value || "CALLOUT"); A.mode = null; hint("Callout placed."); }
      else if (A.mode === "flow") { A.mode = null; flowFrom(p); }
      else if (A.mode === "photo") { A.pending = p; A.mode = null; $("at-photo-file").click(); }
      else if (A.mode === "draw") { A.draw.pts.push(p); if (!A.draw.ent) A.draw.ent = viewer.entities.add({ polyline: { positions: new C.CallbackProperty(function () { return A.draw.pts.map(function (q) { return C.Cartesian3.fromDegrees(q.lon, q.lat); }); }, false), width: 3, material: C.Color.fromCssColorString(YELLOW), clampToGround: true } }); hint(A.draw.pts.length + " corner" + (A.draw.pts.length > 1 ? "s" : "") + " · double click to finish"); }
    }, C.ScreenSpaceEventType.LEFT_CLICK);
    handler.setInputAction(function () { if (A.mode === "draw" && A.draw.pts.length >= 3) { var ring = A.draw.pts.slice(); viewer.entities.remove(A.draw.ent); A.draw = { pts: [], ent: null }; A.mode = null; highlight(ring, $("at-role").value, $("at-callout").value); hint("Highlight placed."); } }, C.ScreenSpaceEventType.LEFT_DOUBLE_CLICK);
  }

  // ------------------------------------------------------------ overlay (title, legend, scale, north) and export
  function overlayHost() { var scene = document.getElementById("twin-scene"); if (!scene) return null; var el = document.getElementById("atlas-overlay"); if (!el) { el = document.createElement("div"); el.id = "atlas-overlay"; scene.appendChild(el); var st = document.createElement("style"); st.textContent = "#atlas-overlay{position:absolute;inset:0;pointer-events:none;font-family:Calibri,Carlito,'Segoe UI',sans-serif;color:#111}#atlas-overlay .at-title{position:absolute;left:14px;top:14px;background:rgba(255,255,255,.94);border:1px solid #333;padding:8px 12px;max-width:46%}#atlas-overlay .at-title b{display:block;font-size:17px;letter-spacing:.02em;text-transform:uppercase}#atlas-overlay .at-title span{font-size:12px;color:#333}#atlas-overlay .at-legend{position:absolute;left:14px;bottom:44px;background:rgba(255,255,255,.94);border:1px solid #333;padding:8px 12px;font-size:12px}#atlas-overlay .at-legend b{display:block;font-size:13px;letter-spacing:.06em;margin-bottom:4px}#atlas-overlay .at-legend div{display:flex;align-items:center;gap:8px;margin:2px 0}#atlas-overlay .at-legend i{width:22px;height:14px;display:inline-block;border:1px solid #333}#atlas-overlay .at-scale{position:absolute;right:14px;bottom:44px;background:rgba(255,255,255,.9);border:1px solid #333;padding:4px 8px;font-size:11px}#atlas-overlay .at-scale i{display:block;height:6px;background:#111;margin-bottom:3px}#atlas-overlay .at-north{position:absolute;right:14px;top:14px;width:34px;height:44px;background:rgba(255,255,255,.9);border:1px solid #333;text-align:center;font-weight:700;font-size:12px;padding-top:2px}#atlas-overlay .at-src{position:absolute;left:14px;bottom:8px;font-size:10.5px;color:#222;background:rgba(255,255,255,.85);padding:2px 6px}"; document.head.appendChild(st); } return el; }
  function scaleNice(mpp) { var target = 150 * mpp; var steps = [50, 100, 200, 250, 500, 1000, 2000, 5000, 10000, 20000, 50000]; var best = steps[0]; steps.forEach(function (s) { if (Math.abs(s - target) < Math.abs(best - target)) best = s; }); return best; }
  function renderOverlay() {
    var el = overlayHost(); if (!el) return; if (!A.print && !A.items.length) { el.innerHTML = ""; return; }
    var title = $("at-title").value || (A.template === "flood" ? "Relative accumulation risk" : A.template === "days" ? "Collection days" : "Scenario view"); var sub = $("at-sub").value || "PSD Waste Management · WM Twin · " + new Date().toISOString().slice(0, 10);
    var legend = $("at-legend").checked && A.legend.length ? '<div class="at-legend"><b>' + (A.template === "flood" ? "RELATIVE ACCUMULATION RISK" : A.template === "days" ? "COLLECTION DAYS" : "LEGEND") + '</b>' + A.legend.map(function (l) { return '<div><i style="background:' + (l.colour === "hatch" ? "repeating-linear-gradient(90deg,#bfbfbf 0 3px,#fff 3px 6px)" : l.colour) + '"></i>' + esc(l.text) + '</div>'; }).join("") + '</div>' : "";
    var mpp = metresPerPixel(); var scale = $("at-scale").checked && mpp ? (function () { var m = scaleNice(mpp); return '<div class="at-scale"><i style="width:' + Math.round(m / mpp) + 'px"></i>0 &nbsp; ' + (m >= 1000 ? (m / 1000) + " km" : m + " m") + '</div>'; })() : "";
    var demo = A.legend.some(function (l) { return /DEMO/.test(l.text); }) || (A.impacts && A.impacts.scenarios && false);
    el.innerHTML = '<div class="at-title"><b>' + esc(title) + '</b><span>' + esc(sub) + '</span></div>' + legend + scale + '<div class="at-north">N<br>&#9650;</div>' + '<div class="at-src">Terrain: Copernicus DEM GLO-30 at 50 m · roads and footprints: OpenStreetMap and Microsoft open data · model: WM flood module v1, a relative risk index from eleven factors, not a modelled water depth · drainage, wadi and flood history registers are placeholders' + (demo ? " · DEMO ONLY inputs" : "") + '</div>';
  }
  var PAPER = { screen: 0, a4: 2339, a3: 3307, a2: 4677 }; // landscape widths in pixels at 200 dpi
  var PAPER_NAME = { screen: "screen size", a4: "A4 landscape at 200 dpi", a3: "A3 landscape at 200 dpi", a2: "A2 landscape at 200 dpi" };
  function tilesSettled(ms) { return new Promise(function (done) { var t0 = Date.now(); (function poll() { try { viewer.scene.requestRender(); if (viewer.scene.globe.tilesLoaded || Date.now() - t0 > ms) return setTimeout(done, 400); } catch (e) { return done(); } setTimeout(poll, 250); })(); }); }
  async function snapshot(targetW) {
    // the scene renders at up to four times the screen resolution for print; the screen resolution comes back afterwards
    var cw = viewer.canvas.clientWidth; var factor = targetW ? Math.min(4, Math.max(1, targetW / cw)) : 1; var prev = viewer.resolutionScale;
    if (factor > 1.01) { viewer.resolutionScale = factor; viewer.scene.requestRender(); await tilesSettled(12000); }
    try { viewer.scene.render(); return viewer.canvas.toDataURL("image/png"); } finally { if (factor > 1.01) { viewer.resolutionScale = prev; viewer.scene.requestRender(); } }
  }
  function loadImage(url) { return new Promise(function (res, rej) { var im = new Image(); im.onload = function () { res(im); }; im.onerror = rej; im.src = url; }); }
  async function addInset() { var bb = viewBbox(); if (!bb) return; status("Capturing the inset…"); var url = await snapshot(2 * viewer.canvas.clientWidth); A.insets.push({ dataUrl: url, bbox: bb, title: "Inset " + (A.insets.length + 1) }); renderInsets(); status("Inset " + A.insets.length + " captured; it is framed beside the main map on export."); }
  function renderInsets() { $("at-insets").innerHTML = A.insets.map(function (s, i) { return '<div><b>' + esc(s.title) + '</b> <small style="color:var(--ink-faint)">' + s.bbox.map(function (v) { return v.toFixed(3); }).join(", ") + '</small></div>'; }).join("") || '<div style="color:var(--ink-faint)">no insets</div>'; }
  function windowXY(lon, lat) { var fn = C.SceneTransforms.worldToWindowCoordinates || C.SceneTransforms.wgs84ToWindowCoordinates; return fn(viewer.scene, C.Cartesian3.fromDegrees(lon, lat)); }
  function defaultTitle() { return A.template === "flood" ? "Relative accumulation risk" : A.template === "days" ? "Collection days" : "Scenario view"; }
  async function renderExport(sizeKey) {
    var targetW = PAPER[sizeKey] || 0;
    var main = await loadImage(await snapshot(targetW)); var insets = await Promise.all(A.insets.map(function (s) { return loadImage(s.dataUrl); }));
    return compose(main, insets);
  }
  function compose(main, insetImgs) {
    // every overlay element scales with k so a print size export keeps the same layout as the screen export
    var W = main.width, H = main.height; var k = W / viewer.canvas.clientWidth; var r = function (v) { return Math.round(v * k); };
    var col = A.insets.length ? r(340) : 0; var c = document.createElement("canvas"); c.width = W + col; c.height = H; var g = c.getContext("2d");
    g.fillStyle = "#FFFFFF"; g.fillRect(0, 0, c.width, c.height); g.drawImage(main, 0, 0);
    var sx = W / viewer.canvas.clientWidth, sy = H / viewer.canvas.clientHeight;
    A.insets.forEach(function (s, i) {
      var y = r(20) + i * r(250); var iw = r(300), ih = r(200); var img = insetImgs[i];
      if (img) g.drawImage(img, W + r(20), y, iw, ih);
      g.strokeStyle = "#111"; g.lineWidth = r(2); g.strokeRect(W + r(20), y, iw, ih); g.fillStyle = "#111"; g.font = "bold " + r(14) + "px Calibri"; g.fillText(s.title.toUpperCase(), W + r(20), y + ih + r(18));
      var a = windowXY(s.bbox[0], s.bbox[1]), b = windowXY(s.bbox[2], s.bbox[3]);
      if (a && b) { g.strokeStyle = YELLOW; g.lineWidth = r(3); g.strokeRect(Math.min(a.x, b.x) * sx, Math.min(a.y, b.y) * sy, Math.abs(b.x - a.x) * sx, Math.abs(b.y - a.y) * sy); g.beginPath(); g.moveTo(Math.max(a.x, b.x) * sx, Math.min(a.y, b.y) * sy); g.lineTo(W + r(20), y); g.stroke(); }
    });
    var title = $("at-title").value || defaultTitle(); var sub = $("at-sub").value || "PSD Waste Management · WM Twin · " + new Date().toISOString().slice(0, 10);
    g.font = "bold " + r(22) + "px Calibri"; var tw = Math.max(g.measureText(title.toUpperCase()).width, r(260)) + r(28); g.fillStyle = "rgba(255,255,255,.94)"; g.fillRect(r(16), r(16), tw, r(58)); g.strokeStyle = "#333"; g.lineWidth = Math.max(1, r(1)); g.strokeRect(r(16), r(16), tw, r(58)); g.fillStyle = "#111"; g.fillText(title.toUpperCase(), r(30), r(42)); g.font = r(13) + "px Calibri"; g.fillStyle = "#333"; g.fillText(sub, r(30), r(62));
    if ($("at-legend").checked && A.legend.length) { var lh = r(30) + A.legend.length * r(20); var lw = r(340); var ly = H - r(60) - lh; g.fillStyle = "rgba(255,255,255,.94)"; g.fillRect(r(16), ly, lw, lh); g.strokeStyle = "#333"; g.strokeRect(r(16), ly, lw, lh); g.fillStyle = "#111"; g.font = "bold " + r(15) + "px Calibri"; g.fillText(A.template === "flood" ? "RELATIVE ACCUMULATION RISK" : A.template === "days" ? "COLLECTION DAYS" : "LEGEND", r(28), ly + r(20)); g.font = r(13) + "px Calibri"; A.legend.forEach(function (l, i) { var yy = ly + r(34) + i * r(20); if (l.colour === "hatch") { g.fillStyle = "#fff"; g.fillRect(r(28), yy, r(26), r(14)); g.strokeStyle = "#999"; for (var q = 0; q < r(26); q += Math.max(2, r(5))) { g.beginPath(); g.moveTo(r(28) + q, yy); g.lineTo(r(28) + q, yy + r(14)); g.stroke(); } } else { g.fillStyle = l.colour; g.fillRect(r(28), yy, r(26), r(14)); } g.strokeStyle = "#333"; g.strokeRect(r(28), yy, r(26), r(14)); g.fillStyle = "#111"; g.fillText(l.text, r(64), yy + r(12)); }); }
    var mpp = metresPerPixel(); if ($("at-scale").checked && mpp) { var m = scaleNice(mpp); var px = Math.round(m / mpp * sx); g.fillStyle = "rgba(255,255,255,.9)"; g.fillRect(W - px - r(40), H - r(62), px + r(24), r(34)); g.strokeStyle = "#333"; g.strokeRect(W - px - r(40), H - r(62), px + r(24), r(34)); g.fillStyle = "#111"; g.fillRect(W - px - r(28), H - r(54), px, r(6)); g.font = r(12) + "px Calibri"; g.fillText("0", W - px - r(28), H - r(34)); var lab = m >= 1000 ? (m / 1000) + " km" : m + " m"; g.fillText(lab, W - r(28) - g.measureText(lab).width, H - r(34)); }
    g.fillStyle = "rgba(255,255,255,.9)"; g.fillRect(W - r(54), r(16), r(38), r(48)); g.strokeStyle = "#333"; g.strokeRect(W - r(54), r(16), r(38), r(48)); g.fillStyle = "#111"; g.font = "bold " + r(14) + "px Calibri"; g.fillText("N", W - r(40), r(34)); g.beginPath(); g.moveTo(W - r(35), r(58)); g.lineTo(W - r(25), r(58)); g.lineTo(W - r(30), r(40)); g.closePath(); g.fill();
    g.font = r(11) + "px Calibri"; g.fillStyle = "#222"; g.fillText("Terrain: Copernicus DEM GLO-30 at 50 m · roads and footprints: OpenStreetMap and Microsoft open data · model: WM flood module v1, a relative risk index from eleven factors, not a modelled water depth · drainage, wadi and flood history registers are placeholders · scenario rainfall, not an observation", r(16), H - r(10));
    return c;
  }
  async function exportPng() {
    var sizeKey = ($("at-size") && $("at-size").value) || "screen";
    status("Rendering the export at " + (PAPER_NAME[sizeKey] || sizeKey) + "…");
    var c; try { c = await renderExport(sizeKey); } catch (e) { status("Export failed: " + ((e && e.message) || e)); return; }
    var title = $("at-title").value || defaultTitle();
    var a = document.createElement("a"); a.download = title.replace(/[^a-z0-9]+/gi, "_").toLowerCase() + ".png"; a.href = c.toDataURL("image/png"); document.body.appendChild(a); a.click(); a.remove();
    status("Exported " + a.download + " (" + c.width + " x " + c.height + " px, " + (PAPER_NAME[sizeKey] || sizeKey) + ")");
  }

  // ------------------------------------------------------------ wiring
  function showPanels() { var t = $("at-template").value; A.template = t; $("at-flood").style.display = t === "flood" ? "" : "none"; $("at-days").style.display = t === "days" ? "" : "none"; $("at-story").style.display = t === "story" ? "" : "none"; }
  async function apply() {
    if (!viewer) return; var t = $("at-template").value; A.template = t;
    if (t === "story") { if (A.print) printLook(false); status("Scenario view: use the annotation tools; the imagery stays in colour, X-ray off for the picture."); var xr = document.querySelector('#twin-host button[data-act="xray"]'); if (xr && (xr.classList.contains("on") || xr.getAttribute("aria-pressed") === "true")) xr.click(); renderOverlay(); return; }
    if (!A.print) printLook(true);
    if (t === "flood") await applyFlood(); else await applyDays();
  }
  function wire() {
    $("at-template").onchange = showPanels; showPanels();
    $("at-apply").onclick = apply;
    $("at-plan").onclick = function () { var b = document.querySelector('#twin-host button[data-act="plan"]'); if (b) b.click(); setTimeout(renderOverlay, 1500); };
    $("at-reset").onclick = function () { printLook(false); clearStory(); A.insets = []; renderInsets(); renderOverlay(); status("Back to the working map."); };
    $("at-hl").onclick = function () { A.mode = "draw"; A.draw = { pts: [], ent: null }; hint("Click the corners of the area on the map, double click to finish."); };
    $("at-co").onclick = function () { A.mode = "callout"; hint("Click the point the callout should point at."); };
    $("at-flow").onclick = function () { A.mode = "flow"; hint("Click a point: the water path from there follows the terrain model to where it collects."); };
    $("at-zones").onclick = zonesInView;
    $("at-photo").onclick = function () { A.mode = "photo"; hint("Click where the photo was taken, then choose the file."); };
    $("at-photo-file").onchange = function () { var f = $("at-photo-file").files[0]; if (f && A.pending) photoInset(A.pending, f); A.pending = null; $("at-photo-file").value = ""; };
    $("at-clear").onclick = clearStory;
    $("at-inset").onclick = addInset; $("at-inset-clear").onclick = function () { A.insets = []; renderInsets(); };
    $("at-export").onclick = exportPng;
    $("at-title").oninput = renderOverlay; $("at-sub").oninput = renderOverlay; $("at-legend").onchange = renderOverlay; $("at-scale").onchange = renderOverlay;
    renderInsets();
  }
  function boot() {
    var tries = 0; (function wait() { viewer = TM.viewer(); if (!viewer) { if (++tries < 400) return setTimeout(wait, 250); return; } wire(); installHandler(); loadScenarios(); viewer.camera.moveEnd.addEventListener(function () { if (A.print || A.items.length) renderOverlay(); }); setInterval(function () { if (A.print) viewer.scene.requestRender(); }, 1000); })();
  }
  window.__twinAtlas = { state: A, apply: apply, printLook: printLook, exportPng: exportPng, renderExport: renderExport, paper: PAPER, callout: callout, flowFrom: flowFrom, zonesInView: zonesInView, addInset: addInset, loadScenarios: loadScenarios };
  boot();
})();
