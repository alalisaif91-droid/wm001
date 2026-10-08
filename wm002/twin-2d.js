/* WM Twin Map, 2D fallback. The same register, flood and O&M layers on a Leaflet map when the browser has no WebGL, when the 3D
   renderer fails, or on request (?view=2d). Additive: nothing of the 3D twin changes. Leaflet 1.9.4 (BSD 2 clause) sits in leaflet/. */
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
  var esc = function (s) { return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]; }); };
  var TM = window.__twinMap; if (!TM) return;
  var SWMP = TM.swmp; var D = TM.defs || {};
  var CLASS_COLOUR = D.CLASS_COLOUR || { very_low: "#A8E6A1", low: "#CDE26B", moderate: "#F2C94C", high: "#F0883E", very_high: "#D9342B" };
  var CLASS_NAMES = ["very_low", "low", "moderate", "high", "very_high"];
  var REGISTER = [
    { key: "sectors", title: "Sectors (active versions)", color: "#4FF0FF", on: true, note: "the nine PSD sectors as published on the platform" },
    { key: "facilities", title: "Facilities", color: "#D9A968", on: true, note: "transfer stations, MRF, landfills" },
    { key: "roads", title: "Roads (main network)", color: "#F5A623", on: true, note: "OpenStreetMap through the platform, coloured by class" },
    { key: "bins", title: "Bins (register)", color: "#57C4DD", on: true, note: "test data until the bin register is loaded" },
    { key: "service-sites", title: "Service sites", color: "#E5934B", on: true, note: "customer sites" },
    { key: "routes", title: "Routes (active versions)", color: "#FFD166", on: true, note: "published route versions" },
    { key: "route-stops", title: "Route stops", color: "#FFD166", on: false, note: "stops of the active versions; off by default" }
  ];
  var OM = (TM.layers || []).filter(function (l) { return ["point", "polygon", "line"].indexOf(l.kind) >= 0 && !l.scenario && l.key !== "collection_route"; });
  var FLOOD = TM.floodLayers || [];
  var BASES = {
    imagery: ["https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}", "Esri World Imagery"],
    light: ["https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}", "Esri World Light Gray"],
    none: null
  };
  var M = { map: null, base: null, layers: {}, on: {}, counts: {}, cells: null, weather: null, mounted: false, hit: 0, tokenSeen: null, reason: "", icons: {} };
  function PREF(k, d) { try { return localStorage.getItem(k) || d; } catch (e) { return d; } }
  function setPref(k, v) { try { localStorage.setItem(k, v); } catch (e) {} }
  function status(t) { var el = $("status"); if (el) el.textContent = t; }
  function isOn(k) { if (M.on[k] != null) return M.on[k]; var l = REGISTER.concat(OM, FLOOD).find(function (x) { return x.key === k; }); return !!(l && l.on); }
  function loadScript(src, ok, fail) { var s = document.createElement("script"); s.src = src; s.onload = ok; s.onerror = fail; document.body.appendChild(s); }

  // ------------------------------------------------------------ mount
  function mount(reason) {
    if (M.mounted) return; M.mounted = true; M.reason = reason || "requested";
    var host = $("twin-host"); if (!host) return;
    host.innerHTML = '<div id="twin2d" style="position:absolute;inset:0;background:#0C1319"></div><div id="twin2d-badge" style="position:absolute;right:10px;top:10px;z-index:500;background:rgba(12,19,25,.85);border:1px solid var(--line);padding:5px 9px;border-radius:3px;font-size:12px;color:var(--ink-soft)">2D map · <a href="' + esc(location.pathname) + '" style="color:var(--accent)">open the 3D twin</a></div>';
    host.style.position = "relative"; host.style.height = "100%";
    if (!$("twin2d-css")) {
      var link = document.createElement("link"); link.rel = "stylesheet"; link.href = "leaflet/leaflet.css"; document.head.appendChild(link);
      var st = document.createElement("style"); st.id = "twin2d-css";
      st.textContent = "#twin2d .leaflet-container{background:#0C1319;font-family:var(--sans)}#twin2d .leaflet-image-layer{image-rendering:pixelated}#twin2d .leaflet-tooltip{background:#121B23;color:#E7EEF3;border:1px solid #24333F;font-size:12px}#twin2d .leaflet-control-attribution{background:rgba(12,19,25,.7);color:#9EB0BF}#twin2d .leaflet-control-attribution a{color:#57C4DD}#twin2d .leaflet-bar a{background:#121B23;color:#E7EEF3;border-bottom-color:#24333F}" +
        ".tlh{font-size:11.5px;margin:14px 0 4px;text-transform:uppercase;letter-spacing:.06em;color:var(--ink-faint)}.wsrc{border-left:3px solid var(--good);padding-left:8px}.wsrc.demo{border-color:var(--high)}.wsrc.stale{border-color:var(--extreme)}.fl-legend{display:flex;flex-wrap:wrap;gap:6px 10px;font-size:11.5px;margin:6px 0;color:var(--ink-soft)}.fl-legend i{display:inline-block;width:11px;height:11px;border-radius:2px;margin-right:4px;vertical-align:-1px}.badge{display:inline-block;padding:1px 6px;border-radius:10px;font-size:11px;font-weight:700;color:#fff;background:var(--ink-faint)}.badge.demo{background:var(--high)}.badge.cls{color:#0C1319}";
      document.head.appendChild(st);
    }
    disable3dOnly();
    renderList();
    if (window.L) build(); else loadScript("leaflet/leaflet.js", build, function () { $("twin2d").innerHTML = '<div style="padding:20px;color:#E7EEF3">Leaflet did not load (leaflet/leaflet.js is missing next to the page).</div>'; status("2D map: Leaflet missing"); });
  }
  function build() {
    var L = window.L;
    var map = L.map("twin2d", { preferCanvas: true, zoomControl: true, worldCopyJump: false, minZoom: 7, maxZoom: 19 }).setView([25.72, 55.95], 11);
    M.map = map; setBase(PREF("twinmap-2d-base", "imagery"));
    map.on("click", function (ev) { if (Date.now() - M.hit < 120) return; if (M.cells && isOn("water-risk-cells")) cellAt(ev.latlng.lng, ev.latlng.lat); });
    map.on("zoomend", zoomSwitch);
    reload().then(function () { fitSectors(); });
    setInterval(watchToken, 3000);
  }
  function setBase(k) {
    if (M.base) { M.map.removeLayer(M.base); M.base = null; }
    var b = BASES[k]; if (b) { M.base = window.L.tileLayer(b[0], { maxZoom: 19, attribution: b[1] }).addTo(M.map); M.base.bringToBack(); }
    setPref("twinmap-2d-base", k);
  }
  function disable3dOnly() {
    ["bin-capture", "area-draw", "lu-import", "rt-go", "at-apply", "at-plan", "at-export", "at-inset", "at-hl", "at-co", "at-flow", "at-zones", "at-photo"].forEach(function (id) { var b = $(id); if (b) { b.disabled = true; b.title = "needs the 3D twin"; } });
    ["bins", "areas", "landuse", "routes", "atlas"].forEach(function (t) { var sec = $("tab-" + t); if (sec && !sec.querySelector(".twin2d-note")) { var n = document.createElement("div"); n.className = "note twin2d-note"; n.innerHTML = 'This is the 2D map: viewing only. Capture, drawing, import, optimisation and the Atlas run on the <a href="' + esc(location.pathname) + '" style="color:var(--accent)">3D twin</a>.'; sec.insertBefore(n, sec.firstChild); } });
  }

  // ------------------------------------------------------------ layers
  function setLayer(key, layer, count, on) {
    if (M.layers[key]) { M.map.removeLayer(M.layers[key]); }
    M.layers[key] = layer; M.counts[key] = count; if (on) layer.addTo(M.map);
    renderList();
  }
  function roadStyle(p, col) { var road = D.ROAD_STYLE || {}; var s = road[p.highway] || [col, 3]; return { color: s[0], weight: Math.max(1.2, s[1] / 2.2), opacity: 0.9 }; }
  function floodMarkerIcon(l, p) {
    if (!D.floodIcon || !D.floodSymbolKind) return null;
    var kind = D.floodSymbolKind(l, p); var fill = (p.risk_class && CLASS_COLOUR[p.risk_class]) || l.color; var k = kind + "|" + fill;
    if (!M.icons[k]) { var cv = D.floodIcon(kind, fill); M.icons[k] = window.L.icon({ iconUrl: cv.toDataURL ? cv.toDataURL("image/png") : cv, iconSize: [30, 33], iconAnchor: [15, 33], tooltipAnchor: [0, -30] }); }
    return M.icons[k];
  }
  function floodStyle(l, p) {
    var col = (p.risk_class && CLASS_COLOUR[p.risk_class]) || l.color;
    if (l.flood === "closure") return { color: col, weight: 6, opacity: 0.95 };
    if (l.flood === "wadi") return { color: col, weight: 3, opacity: 0.9, dashArray: "8 8" };
    if (l.flood === "hazard") return { color: col, weight: 5, opacity: 0.95 };
    return { color: col, weight: 2, opacity: 0.9, fillColor: col, fillOpacity: 0.2 };
  }
  function geo(fc, l, flood) {
    var L = window.L; var col = l.color;
    var g = L.geoJSON(fc, {
      filter: function (f) { return !flood || !f.geometry || f.geometry.type !== "Point" || !D.floodProvenance || D.floodProvenance(f.properties || {}); },
      style: function (f) { var p = f.properties || {}; if (l.key === "roads") return roadStyle(p, col); if (flood) return floodStyle(l, p); var poly = /Polygon/.test(f.geometry && f.geometry.type); return { color: col, weight: l.key === "sectors" ? 2.2 : 2.5, opacity: 0.9, fillColor: col, fillOpacity: poly ? (l.key === "sectors" ? 0.04 : 0.18) : 0 }; },
      pointToLayer: function (f, ll) { var p = f.properties || {}; if (flood) { var ic = floodMarkerIcon(l, p); if (ic) return L.marker(ll, { icon: ic }); } return L.circleMarker(ll, { radius: l.key === "facilities" ? 7 : 5, color: "#0C1319", weight: 1, fillColor: (flood && p.risk_class && CLASS_COLOUR[p.risk_class]) || col, fillOpacity: 0.95 }); },
      onEachFeature: function (f, lyr) { var p = f.properties || {}; lyr.on("click", function () { M.hit = Date.now(); select(l, p, flood); }); var label = p.name || p.name_en || p.name_ar || p.code || (p.attributes && p.attributes.type); if (label) lyr.bindTooltip(String(label), { sticky: true, direction: "top", opacity: 0.92 }); }
    });
    if (flood && l.flood === "closure") {
      (fc.features || []).forEach(function (f) { var p = f.properties || {}; if (p.marker && p.marker.coordinates) { var ic = floodMarkerIcon(l, p); var m = ic ? L.marker([p.marker.coordinates[1], p.marker.coordinates[0]], { icon: ic }) : L.circleMarker([p.marker.coordinates[1], p.marker.coordinates[0]], { radius: 6, color: "#0C1319", fillColor: l.color, fillOpacity: 0.95 }); m.on("click", function () { M.hit = Date.now(); select(l, p, true); }); g.addLayer(m); } });
    }
    return g;
  }
  async function loadFc(url) { var r = await fetch(url, { headers: { Accept: "application/json" } }); if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); }
  async function loadRegister(l) {
    var fc; try { fc = await loadFc(SWMP + "/v1/layers/" + l.key); } catch (e) { M.counts[l.key] = "?"; renderList(); return; }
    setLayer(l.key, geo(fc, l, false), fc.count != null ? fc.count : (fc.features || []).length, isOn(l.key));
  }
  var DOT_ZOOM = 12; // below this zoom the flood symbols draw as small dots, so a whole emirate view is not covered by icons
  function dots(fc, l) {
    var L = window.L;
    return L.geoJSON(fc, {
      filter: function (f) { return f.geometry && f.geometry.type === "Point" && (!D.floodProvenance || D.floodProvenance(f.properties || {})); },
      pointToLayer: function (f, ll) { var p = f.properties || {}; return L.circleMarker(ll, { radius: 3.5, color: "#0C1319", weight: 1, fillColor: (p.risk_class && CLASS_COLOUR[p.risk_class]) || l.color, fillOpacity: 0.95 }); },
      onEachFeature: function (f, lyr) { var p = f.properties || {}; lyr.on("click", function () { M.hit = Date.now(); select(l, p, true); }); if (p.code || p.name) lyr.bindTooltip(String(p.code || p.name), { sticky: true, direction: "top", opacity: 0.92 }); }
    });
  }
  function zoomSwitch() {
    if (!M.map) return; var far = M.map.getZoom() < DOT_ZOOM;
    Object.keys(M.layers).forEach(function (k) { var g = M.layers[k]; if (!g || !g.twinDots) return; var show = isOn(k); var want = far ? g.twinDots : g.twinIcons, other = far ? g.twinIcons : g.twinDots; if (M.map.hasLayer(other)) M.map.removeLayer(other); if (show && !M.map.hasLayer(want)) want.addTo(M.map); });
  }
  async function loadFlood(l) {
    if (l.flood === "cells") return loadCells(l);
    var fc; try { fc = await loadFc(SWMP + "/v1/layers/" + l.key); } catch (e) { M.counts[l.key] = "?"; renderList(); return; }
    var hasPoints = (fc.features || []).some(function (f) { return f.geometry && f.geometry.type === "Point"; });
    var icons = geo(fc, l, true);
    if (!hasPoints) { setLayer(l.key, icons, fc.count != null ? fc.count : (fc.features || []).length, isOn(l.key)); return; }
    // a switch layer: icons near, dots far; the group itself carries both and is what the layer list toggles
    var group = window.L.layerGroup(); group.twinIcons = icons; group.twinDots = dots(fc, l);
    group.on("add", zoomSwitch); group.on("remove", function () { [icons, group.twinDots].forEach(function (x) { if (M.map.hasLayer(x)) M.map.removeLayer(x); }); });
    setLayer(l.key, group, fc.count != null ? fc.count : (fc.features || []).length, isOn(l.key));
  }
  async function loadOm(l) {
    var fc; try { fc = await TM.api.features(l.key); } catch (e) { M.counts[l.key] = e && e.status === 401 ? "sign in" : "?"; renderList(); return; }
    setLayer(l.key, geo(fc, l, false), (fc.features || []).length, isOn(l.key));
  }
  function mercY(lat) { var r = lat * Math.PI / 180; return Math.log(Math.tan(Math.PI / 4 + r / 2)); }
  function raster(cells) {
    // one pixel row per cell row in Web Mercator, so the overlay lands on the same ground as the 3D cells
    var L = window.L; if (!cells.length) return L.layerGroup();
    var hw = cells[0][4], hh = cells[0][5]; var minLon = Infinity, minLat = Infinity, maxLon = -Infinity, maxLat = -Infinity;
    cells.forEach(function (c) { if (c[0] < minLon) minLon = c[0]; if (c[0] > maxLon) maxLon = c[0]; if (c[1] < minLat) minLat = c[1]; if (c[1] > maxLat) maxLat = c[1]; });
    var west = minLon - hw, east = maxLon + hw, south = minLat - hh, north = maxLat + hh;
    var w = Math.round((east - west) / (2 * hw)); var yTop = mercY(north), ySpan = yTop - mercY(south); var h = Math.round(ySpan / (mercY(minLat + hh) - mercY(minLat - hh)));
    if (!(w >= 1 && h >= 1) || w * h > 30e6) return L.layerGroup();
    var cv = document.createElement("canvas"); cv.width = w; cv.height = h; var g = cv.getContext("2d"); var img = g.createImageData(w, h); var px = img.data;
    var rgb = CLASS_NAMES.map(function (k) { var v = CLASS_COLOUR[k].replace("#", ""); return [parseInt(v.slice(0, 2), 16), parseInt(v.slice(2, 4), 16), parseInt(v.slice(4, 6), 16)]; }); var alpha = [0, 0, 135, 180, 210];
    var rowOf = function (lat) { return (yTop - mercY(lat)) / ySpan * h; };
    cells.forEach(function (c) {
      var x = Math.floor((c[0] - west) / (2 * hw)); if (x < 0 || x >= w) return; var k = c[3]; if (!alpha[k]) return;
      var y0 = Math.max(0, Math.floor(rowOf(c[1] + hh))), y1 = Math.min(h - 1, Math.floor(rowOf(c[1] - hh) - 1e-9)); if (y1 < y0) y1 = y0;
      for (var y = y0; y <= y1; y++) { var i = (y * w + x) * 4; px[i] = rgb[k][0]; px[i + 1] = rgb[k][1]; px[i + 2] = rgb[k][2]; px[i + 3] = alpha[k]; }
    });
    g.putImageData(img, 0, 0);
    return L.imageOverlay(cv.toDataURL("image/png"), [[south, west], [north, east]], { opacity: 1, interactive: false });
  }
  async function loadCells(l) {
    var data; try { data = await loadFc(SWMP + "/v1/flood/risk-cells?minClass=moderate"); } catch (e) { M.counts[l.key] = "?"; renderList(); return; }
    M.cells = data.cells || []; setLayer(l.key, raster(M.cells), data.count + (data.demoOnly ? " (DEMO ONLY run)" : ""), isOn(l.key));
  }
  async function cellAt(lon, lat) {
    var best = -1, bd = Infinity, cells = M.cells; var kx = 111320 * Math.cos(lat * Math.PI / 180);
    for (var i = 0; i < cells.length; i++) { var c = cells[i]; var dx = (c[0] - lon) * kx, dy = (c[1] - lat) * 110540; var d = dx * dx + dy * dy; if (d < bd) { bd = d; best = i; } }
    if (best < 0) return; var c = cells[best]; var half = c[4] * kx; if (bd > half * half * 2) return;
    TM.showSelection({ flood: true, layerKey: "water-risk-cells", title: "Water risk cell", props: { score: c[2], risk_class: CLASS_NAMES[c[3]], source: "loading the record…" } });
    try { var fc = await loadFc(SWMP + "/v1/layers/water-risk-cells?minClass=very_low&bbox=" + [c[0] - c[4] / 2, c[1] - c[5] / 2, c[0] + c[4] / 2, c[1] + c[5] / 2].join(",")); if (fc.features && fc.features.length) TM.showSelection({ flood: true, layerKey: "water-risk-cells", title: "Water risk cell", props: fc.features[0].properties }); } catch (e) {}
  }
  function select(l, p, flood) {
    if (flood) { TM.showSelection({ flood: true, layerKey: l.key, title: l.title, props: p }); return; }
    if (l.kind) { TM.showSelection(p); return; }
    var rows = Object.keys(p).filter(function (k) { return p[k] != null && typeof p[k] !== "object"; }).map(function (k) { return "<dt>" + esc(k) + "</dt><dd>" + esc(p[k]) + "</dd>"; }).join("");
    $("sel").innerHTML = "<h2>" + esc(l.title) + "</h2><dl class=\"kv\">" + rows + "</dl><div class=\"note\">Source: Smart Waste Platform layer " + esc(l.key) + " at " + esc(SWMP) + ".</div>";
    var b = document.querySelector('nav button[data-tab="select"]'); if (b) b.click();
  }
  function fitSectors() { var s = M.layers.sectors; if (s && s.getBounds && s.getBounds().isValid()) M.map.fitBounds(s.getBounds(), { padding: [10, 10] }); }
  async function reload() {
    try { var r = await fetch(SWMP + "/v1/weather/sources"); M.weather = r.ok ? await r.json() : null; } catch (e) { M.weather = null; }
    for (var i = 0; i < REGISTER.length; i++) await loadRegister(REGISTER[i]);
    for (var j = 0; j < FLOOD.length; j++) await loadFlood(FLOOD[j]);
    M.tokenSeen = TM.token();
    if (M.tokenSeen) { for (var k = 0; k < OM.length; k++) await loadOm(OM[k]); } else { OM.forEach(function (l) { M.counts[l.key] = "sign in"; }); }
    renderList();
    var n = REGISTER.map(function (l) { return +M.counts[l.key] || 0; }).reduce(function (a, b) { return a + b; }, 0);
    status("2D map ready · " + n + " register features · flood layers loaded" + (M.tokenSeen ? " · O&M layers loaded" : " · sign in for the O&M layers"));
  }
  function watchToken() { var t = TM.token(); if (t !== M.tokenSeen && M.map) { M.tokenSeen = t; if (t) { OM.forEach(function (l) { loadOm(l); }); } else { OM.forEach(function (l) { if (M.layers[l.key]) { M.map.removeLayer(M.layers[l.key]); delete M.layers[l.key]; } M.counts[l.key] = "sign in"; }); renderList(); } } }

  // ------------------------------------------------------------ layer list (replaces the 3D list while the 2D map is up)
  function renderList() {
    var el = $("layer-list"); if (!el || !M.mounted) return;
    var row = function (l) { var k = l.key; return '<div class="layer"><input type="checkbox" data-2d="' + k + '"' + (isOn(k) ? " checked" : "") + '><span class="sw" style="background:' + l.color + '"></span><span>' + esc(l.title) + '<small>' + esc(l.note || "") + '</small></span><span class="n">' + (M.counts[k] == null ? "…" : M.counts[k]) + '</span></div>'; };
    var w = M.weather; var weather = !w ? '<div class="note">Flood platform not reachable at ' + esc(SWMP) + ' (start it with the swmp-api server); set another address with localStorage swmp-api-url.</div>'
      : '<div class="note wsrc ' + (w.data_status === "demo" ? "demo" : (w.stale ? "stale" : "ok")) + '"><b>' + (w.data_status === "demo" ? "DEMO ONLY weather data" : "Weather " + esc(w.data_status)) + '</b> via ' + esc(w.adapter) + (w.stale ? " (stale)" : "") + '. ' + esc(w.message) + '</div>';
    var why = M.reason === "no-webgl" ? "this browser has no WebGL, so the 3D twin cannot start" : M.reason === "3d-failed" ? "the 3D renderer failed to start" : "requested with ?view=2d";
    var base = PREF("twinmap-2d-base", "imagery");
    el.innerHTML = '<div class="note">2D map: ' + why + '. Register layers from the Smart Waste Platform, the flood module and, after sign in, the O&M platform features. Houses, X-ray, the Atlas and 3D measurements need the <a href="' + esc(location.pathname) + '" style="color:var(--accent)">3D twin</a>.</div>' +
      '<div class="tlh">Base map</div><div class="layer" style="grid-template-columns:1fr"><label style="margin:0">Base <select data-2d-base style="width:auto;margin-left:6px"><option value="imagery"' + (base === "imagery" ? " selected" : "") + '>Esri World Imagery</option><option value="light"' + (base === "light" ? " selected" : "") + '>Esri light grey (print look)</option><option value="none"' + (base === "none" ? " selected" : "") + '>none</option></select></label></div>' +
      '<div class="tlh">Register layers (Smart Waste Platform)</div>' + REGISTER.map(row).join("") +
      '<div class="tlh">O&amp;M platform layers</div>' + OM.map(row).join("") +
      '<div class="tlh">Flood and water accumulation</div>' + weather + FLOOD.map(row).join("") +
      '<div class="fl-legend">' + Object.keys(CLASS_COLOUR).map(function (k) { return '<span><i style="background:' + CLASS_COLOUR[k] + '"></i>' + k.replace("_", " ") + '</span>'; }).join("") + '</div>' +
      '<div class="note">Click a feature, symbol or cell: the record opens on the Selection tab with its source, data status, timestamp, confidence and verification.</div>';
    el.querySelectorAll("input[data-2d]").forEach(function (cb) { cb.onchange = function () { var k = cb.dataset["2d"]; M.on[k] = cb.checked; var l = M.layers[k]; if (!l) return; if (cb.checked) l.addTo(M.map); else M.map.removeLayer(l); }; });
    var sel = el.querySelector("select[data-2d-base]"); if (sel) sel.onchange = function () { setBase(sel.value); };
  }

  // ------------------------------------------------------------ activation
  window.__twin2dMount = mount;
  window.__twin2dReload = reload;
  window.__twin2d = { state: M, mount: mount, reload: reload, raster: raster };
  if (window.__twinMap2d) { mount(window.__twinMap2d.reason); return; }
  if (TM.want2d) { window.__twinMap2d = { reason: TM.want2d }; mount(TM.want2d); return; }
  // 3D mode: a discoverable way to the 2D map, and the runtime fallback when the 3D renderer reports a failure
  var ll = $("layer-list"); if (ll && !$("twin2d-hint")) { var hint = document.createElement("div"); hint.id = "twin2d-hint"; hint.className = "note"; hint.innerHTML = 'Slow machine or no 3D needed? <a href="?view=2d" style="color:var(--accent)">Open the 2D map</a>: the same layers on a flat map.'; ll.parentNode.insertBefore(hint, ll.nextSibling); }
  var polls = 0; var watch = setInterval(function () {
    if (++polls > 120 || (window.__wmaTwin && window.__wmaTwin.viewer)) { clearInterval(watch); return; }
    var msg = $("twin-msg"); if (msg && /failed|could not be loaded/i.test(msg.textContent || "")) { clearInterval(watch); window.__twinMap2d = { reason: "3d-failed" }; status("The 3D renderer failed: the 2D map opens instead"); mount("3d-failed"); }
  }, 1000);
})();
