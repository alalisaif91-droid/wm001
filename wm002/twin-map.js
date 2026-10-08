/* WM Twin Map: the WM001 twin module plus the platform layers (bins, land use, generators, routes).
   Talks to the O&M platform at /v1/om (sign in required for writes). Not part of the published claude.ai copy. */
(function () {
  "use strict";
  var C = window.Cesium;
  var PLAT = (function () { try { return localStorage.getItem("omcc-platform-url") || "http://127.0.0.1:8100"; } catch (e) { return "http://127.0.0.1:8100"; } })();
  var TOKEN = (function () { try { return sessionStorage.getItem("omcc-platform-token") || ""; } catch (e) { return ""; } })();
  var USER = null;
  var $ = function (id) { return document.getElementById(id); };
  var status = function (t) { $("status").textContent = t; };
  var esc = function (s) { return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]; }); };

  // ------------------------------------------------------------ platform client
  async function call(method, path, body, form) {
    var h = { "X-Correlation-Id": "twinmap-" + Date.now().toString(36) };
    if (TOKEN) h.Authorization = "Bearer " + TOKEN;
    if (!form && body !== undefined) h["Content-Type"] = "application/json";
    var r = await fetch(PLAT + path, { method: method, headers: h, body: form ? form : body === undefined ? undefined : JSON.stringify(body) });
    var text = await r.text(); var j = null; try { j = JSON.parse(text); } catch (e) { j = null; }
    if (!r.ok) { var e2 = new Error((j && j.error && j.error.message) || r.statusText || ("HTTP " + r.status)); e2.status = r.status; e2.error = (j && j.error) || {}; throw e2; }
    return j;
  }
  var api = {
    health: function () { var opt = {}; try { if (AbortSignal && AbortSignal.timeout) opt.signal = AbortSignal.timeout(8000); } catch (e) {} return fetch(PLAT + "/v1/om/health", opt).then(function (r) { return r.json(); }); },
    me: function () { return call("GET", "/v1/om/me"); },
    login: async function (email, pw) { var j = await call("POST", "/v1/om/auth/login", { email: email, password: pw }); TOKEN = j.token; try { sessionStorage.setItem("omcc-platform-token", TOKEN); } catch (e) {} USER = j.user; try { USER = await call("GET", "/v1/om/me"); } catch (e) {} return USER; },
    logout: async function () { try { await call("POST", "/v1/om/auth/logout"); } catch (e) {} TOKEN = ""; USER = null; try { sessionStorage.removeItem("omcc-platform-token"); } catch (e) {} },
    features: function (cls, bbox, scenario) { return call("GET", "/v1/om/features?class=" + encodeURIComponent(cls) + (bbox ? "&bbox=" + bbox.join(",") : "") + (scenario ? "&scenario=" + encodeURIComponent(scenario) : "")); },
    feature: function (id) { return call("GET", "/v1/om/features/" + encodeURIComponent(id)); },
    addFeature: function (f) { return call("POST", "/v1/om/features", f); },
    summary: function () { return call("GET", "/v1/om/twin/summary"); },
    importLanduse: function (bbox) { return call("POST", "/v1/om/twin/landuse/import", { bbox: bbox }); },
    optimise: function (body) { return call("POST", "/v1/om/twin/routes/optimise", body); },
    design: function (body) { return call("POST", "/v1/om/twin/routes/design", body); },
    plans: function () { return call("GET", "/v1/om/twin/routes/plans"); },
    plan: function (id) { return call("GET", "/v1/om/twin/routes/plans/" + encodeURIComponent(id)); },
    quality: function () { return call("GET", "/v1/om/spatial/quality"); }
  };
  function can(perm) { return !!(USER && USER.perms && USER.perms[perm]); }

  // ------------------------------------------------------------ flood and water accumulation (Smart Waste Platform API, read only)
  var SWMP = (function () { try { return localStorage.getItem("swmp-api-url") || "http://localhost:8000"; } catch (e) { return "http://localhost:8000"; } })();
  var CLASS_COLOUR = { very_low: "#A8E6A1", low: "#CDE26B", moderate: "#F2C94C", high: "#F0883E", very_high: "#D9342B" };
  var WATER = "#2F80ED";
  var FLOOD_LAYERS = [
    { key: "water-risk-cells", title: "Water risk cells, moderate and above", color: "#F0883E", flood: "cells", on: false, note: "17,811 cells at the design storm: tick to load, they are not built at page start. Yellow moderate, orange high, red very high; low classes stay in the record, not on the map; click a cell for its factors" },
    { key: "water-points", title: "Water accumulation points", color: WATER, flood: "symbol", on: true, note: "predicted low points and zones, historical, reports, closures, drainage, wadi, underpass" },
    { key: "road-hazards", title: "Road hazards (flood risk on a road)", color: "#F0883E", flood: "hazard", on: true, note: "roads within 100 m of a high or very high cell, and registered hazards" },
    { key: "road-closures", title: "Road closures (official)", color: "#D9342B", flood: "closure", on: true, note: "closed roads are never routable" },
    { key: "standing-water-reports", title: "Standing water reports", color: WATER, flood: "symbol", on: true, note: "unverified reports pulse and penalise the nearest road until verified" },
    { key: "topology-errors", title: "Road data quality (topology)", color: "#E0B25A", flood: "symbol", on: false, note: "duplicates, invalid crossings, unsnapped ends, one way dead ends; off by default" },
    { key: "building-conflicts", title: "Building and road conflicts", color: "#E0B25A", flood: "symbol", on: false, note: "roads through footprints, footprints on the carriageway; off by default" },
    { key: "weather-stations", title: "Weather stations", color: "#9EB0BF", flood: "symbol", on: true, note: "DEMO ONLY stations until the NCM interface is approved" },
    { key: "wadi-segments", title: "Wadi lines", color: "#5DADE2", flood: "wadi", on: true, note: "placeholder until the drainage register is loaded" },
    { key: "drainage-assets", title: "Drainage assets", color: "#7F8C8D", flood: "symbol", on: true, note: "condition clear, partially blocked, blocked" }
  ];
  var FLOOD_WEATHER = null; var FLOOD_ICONS = {};
  function floodIcon(kind, fill) {
    var k = kind + "|" + fill; if (FLOOD_ICONS[k]) return FLOOD_ICONS[k];
    var c = document.createElement("canvas"); c.width = 36; c.height = 40; var g = c.getContext("2d");
    g.lineWidth = 2; g.strokeStyle = "#0C1319"; g.lineJoin = "round";
    function drop(f) { g.beginPath(); g.moveTo(18, 4); g.bezierCurveTo(26, 14, 32, 20, 32, 26); g.arc(18, 26, 14, 0, Math.PI, false); g.bezierCurveTo(4, 20, 10, 14, 18, 4); g.closePath(); g.fillStyle = f; g.fill(); g.stroke(); }
    function text(t, col) { g.fillStyle = col || "#0C1319"; g.font = "bold 14px Calibri, sans-serif"; g.textAlign = "center"; g.textBaseline = "middle"; g.fillText(t, 18, 27); }
    if (kind === "predicted_low_point" || kind === "predicted_accumulation_zone") { drop(fill); g.fillStyle = WATER; g.beginPath(); g.arc(18, 27, 5, 0, Math.PI * 2); g.fill(); }
    else if (kind === "historical_flood_location") { drop(fill); g.beginPath(); g.arc(18, 27, 6, 0, Math.PI * 2); g.stroke(); g.beginPath(); g.moveTo(18, 27); g.lineTo(18, 22); g.moveTo(18, 27); g.lineTo(22, 27); g.stroke(); }
    else if (kind === "active_water_report" || kind === "report") { drop(fill); text("!", "#FFFFFF"); }
    else if (kind === "officially_closed_road" || kind === "closure") { g.beginPath(); for (var i = 0; i < 8; i++) { var a = Math.PI / 8 + i * Math.PI / 4; g.lineTo(18 + 15 * Math.cos(a), 22 + 15 * Math.sin(a)); } g.closePath(); g.fillStyle = "#D9342B"; g.fill(); g.strokeStyle = "#FFFFFF"; g.stroke(); g.fillStyle = "#FFFFFF"; g.fillRect(9, 20, 18, 4); }
    else if (kind === "drainage_blockage" || kind === "drainage") { g.fillStyle = fill; g.fillRect(6, 10, 24, 24); g.strokeRect(6, 10, 24, 24); for (var y = 15; y < 34; y += 5) { g.beginPath(); g.moveTo(8, y); g.lineTo(28, y); g.stroke(); } if (kind === "drainage_blockage") { g.strokeStyle = "#D9342B"; g.lineWidth = 3; g.beginPath(); g.moveTo(9, 13); g.lineTo(27, 31); g.moveTo(27, 13); g.lineTo(9, 31); g.stroke(); } }
    else if (kind === "wadi_crossing") { g.strokeStyle = fill; g.lineWidth = 3; for (var r = 0; r < 3; r++) { g.beginPath(); for (var x = 4; x <= 32; x += 4) { g.lineTo(x, 12 + r * 8 + (Math.floor(x / 4) % 2 ? -3 : 3)); } g.stroke(); } g.strokeStyle = "#0C1319"; g.lineWidth = 2; g.beginPath(); g.moveTo(10, 36); g.lineTo(18, 30); g.lineTo(26, 36); g.stroke(); }
    else if (kind === "underpass_tunnel_risk") { g.beginPath(); g.moveTo(6, 34); g.lineTo(6, 18); g.arc(18, 18, 12, Math.PI, 0, false); g.lineTo(30, 34); g.closePath(); g.fillStyle = fill; g.fill(); g.stroke(); g.strokeStyle = WATER; g.lineWidth = 3; g.beginPath(); g.moveTo(8, 30); g.quadraticCurveTo(13, 26, 18, 30); g.quadraticCurveTo(23, 34, 28, 30); g.stroke(); }
    else if (kind === "quality") { g.beginPath(); g.moveTo(18, 4); g.lineTo(33, 34); g.lineTo(3, 34); g.closePath(); g.fillStyle = fill; g.fill(); g.stroke(); g.setLineDash([3, 3]); g.beginPath(); g.moveTo(9, 27); g.lineTo(27, 27); g.stroke(); g.setLineDash([]); text("!", "#0C1319"); }
    else if (kind === "station") { g.fillStyle = fill; g.beginPath(); g.arc(18, 22, 12, 0, Math.PI * 2); g.fill(); g.stroke(); g.beginPath(); g.moveTo(18, 12); g.lineTo(18, 22); g.lineTo(25, 22); g.stroke(); }
    else { drop(fill); }
    FLOOD_ICONS[k] = c; return c;
  }
  function floodSymbolKind(layer, props) {
    if (layer.key === "water-points") return props.kind;
    if (layer.key === "standing-water-reports") return "report";
    if (layer.key === "topology-errors" || layer.key === "building-conflicts") return "quality";
    if (layer.key === "weather-stations") return "station";
    if (layer.key === "drainage-assets") return props.condition === "blocked" ? "drainage_blockage" : "drainage";
    return "drop";
  }
  function floodProvenance(props) { var ts = props.timestamp || props.calculated_at; return props.source != null && ts != null && props.confidence != null && (props.verification != null || props.field_verification_status != null); }
  function styleFlood(e, layer, props) {
    var cls = props.risk_class; var col = C.Color.fromCssColorString(cls && CLASS_COLOUR[cls] ? CLASS_COLOUR[cls] : layer.color);
    if (layer.flood === "cells" && e.polygon) {
      e.polygon.material = col.withAlpha(cls === "moderate" ? 0.3 : cls === "high" ? 0.5 : cls === "very_high" ? 0.62 : 0.0); e.polygon.outline = true; e.polygon.outlineColor = col.withAlpha(cls === "moderate" ? 0.55 : 0.85);
      e.polygon.classificationType = C.ClassificationType.TERRAIN; e.polygon.height = undefined; e.polygon.perPositionHeight = false;
    } else if ((layer.flood === "hazard" || layer.flood === "closure" || layer.flood === "wadi") && e.polyline) {
      e.polyline.material = layer.flood === "wadi" ? new C.PolylineDashMaterialProperty({ color: col.withAlpha(0.9), dashLength: 12 }) : col.withAlpha(0.95);
      e.polyline.width = layer.flood === "closure" ? 7 : 6; e.polyline.clampToGround = true;
      if (layer.flood === "closure" && props.marker && props.marker.coordinates && floodProvenance(props)) {
        var m = props.marker.coordinates;
        var marker = new C.Entity({ position: C.Cartesian3.fromDegrees(m[0], m[1]), billboard: new C.BillboardGraphics({ image: floodIcon("closure", layer.color), width: 30, height: 33, heightReference: C.HeightReference.CLAMP_TO_GROUND, disableDepthTestDistance: Number.POSITIVE_INFINITY, verticalOrigin: C.VerticalOrigin.CENTER }) });
        marker.twinLayer = layer.key; marker.twinProps = { flood: true, layerKey: layer.key, title: layer.title, props: props }; e.entityCollection.add(marker);
      }
    } else if (e.position && !e.polygon && !e.polyline) {
      if (!floodProvenance(props)) { e.show = false; return; }
      var kind = floodSymbolKind(layer, props); if (e.billboard) e.billboard = undefined;
      var pulse = layer.key === "standing-water-reports" && props.status === "unverified";
      e.billboard = new C.BillboardGraphics({ image: floodIcon(kind, cls && CLASS_COLOUR[cls] ? CLASS_COLOUR[cls] : layer.color), width: pulse ? 34 : 30, height: pulse ? 38 : 33, heightReference: C.HeightReference.CLAMP_TO_GROUND, disableDepthTestDistance: Number.POSITIVE_INFINITY, verticalOrigin: C.VerticalOrigin.BOTTOM, scaleByDistance: new C.NearFarScalar(3000, 1.0, 60000, 0.35) });
      if (pulse) e.billboard.scale = 1.15;
      // labels are never clamped: Cesium clamps a label glyph by glyph, one terrain pick per character, which cost the
      // page 83 s of frozen main thread for about 10,000 characters. They already draw over the terrain
      // (disableDepthTestDistance) and sit on their icon by pixel offset, so the clamp bought nothing.
      if (props.code) e.label = new C.LabelGraphics({ text: props.code, font: "11px Calibri, sans-serif", fillColor: C.Color.fromCssColorString("#E7EEF3"), outlineColor: C.Color.fromCssColorString("#0C1319"), outlineWidth: 3, style: C.LabelStyle.FILL_AND_OUTLINE, pixelOffset: new C.Cartesian2(0, -36), heightReference: C.HeightReference.NONE, disableDepthTestDistance: Number.POSITIVE_INFINITY, scaleByDistance: new C.NearFarScalar(2000, 1.0, 20000, 0.0) });
    }
    e.twinLayer = layer.key; e.twinProps = { flood: true, layerKey: layer.key, title: layer.title, props: props };
  }
  var CELL_PRIMS = []; var CELLS = null;
  var CLASS_NAMES = ["very_low", "low", "moderate", "high", "very_high"];
  async function loadFloodCells(layer) {
    var data;
    try { var r = await fetch(SWMP + "/v1/flood/risk-cells?minClass=moderate", { headers: { Accept: "application/json" } }); if (!r.ok) throw new Error("HTTP " + r.status); data = await r.json(); } catch (e) { COUNTS[layer.key] = "?"; return; }
    CELL_PRIMS.forEach(function (p) { viewer.scene.groundPrimitives.remove(p); }); CELL_PRIMS = []; CELLS = data.cells;
    var cols = CLASS_NAMES.map(function (k) { return C.Color.fromCssColorString(CLASS_COLOUR[k]); });
    var alpha = [0.0, 0.0, 0.32, 0.5, 0.62];
    for (var b = 0; b < data.cells.length; b += 5000) {
      var inst = [];
      data.cells.slice(b, b + 5000).forEach(function (c, i) {
        try { inst.push(new C.GeometryInstance({ geometry: new C.RectangleGeometry({ rectangle: C.Rectangle.fromDegrees(c[0] - c[4], c[1] - c[5], c[0] + c[4], c[1] + c[5]), vertexFormat: C.PerInstanceColorAppearance.VERTEX_FORMAT }), attributes: { color: C.ColorGeometryInstanceAttribute.fromColor(cols[c[3]].withAlpha(alpha[c[3]])) }, id: "cell|" + (b + i) })); } catch (e) {}
      });
      if (inst.length) { var p = viewer.scene.groundPrimitives.add(new C.GroundPrimitive({ geometryInstances: inst, appearance: new C.PerInstanceColorAppearance({ translucent: true, flat: true }), asynchronous: true, allowPicking: false, show: layer.on && cameraHeight() < LOD.cells })); CELL_PRIMS.push(p); }
      await new Promise(function (r) { setTimeout(r, 0); });
    }
    COUNTS[layer.key] = data.count + (data.demoOnly ? " (DEMO ONLY run)" : ""); applyLod(); viewer.scene.requestRender();
  }
  async function floodCellAt(lon, lat) {
    // the nearest drawn cell to a clicked ground point (cells are not pickable, which keeps the scene fast)
    var best = -1, bd = Infinity; for (var i = 0; i < CELLS.length; i++) { var c = CELLS[i]; var dx = (c[0] - lon) * 111320 * Math.cos(lat * Math.PI / 180), dy = (c[1] - lat) * 110540; var d = dx * dx + dy * dy; if (d < bd) { bd = d; best = i; } }
    if (best >= 0 && bd <= (CELLS[best][4] * 111320 * Math.cos(lat * Math.PI / 180)) * (CELLS[best][4] * 111320 * Math.cos(lat * Math.PI / 180)) * 2) floodCellSelect(best);
  }
  async function floodCellSelect(i) {
    var c = CELLS && CELLS[i]; if (!c) return;
    var quick = { flood: true, layerKey: "water-risk-cells", title: "Water risk cell", props: { score: c[2], risk_class: CLASS_NAMES[c[3]], source: "loading the record…" } };
    showSelection(quick);
    try {
      var bbox = [c[0] - c[4] / 2, c[1] - c[5] / 2, c[0] + c[4] / 2, c[1] + c[5] / 2].join(",");
      var r = await fetch(SWMP + "/v1/layers/water-risk-cells?minClass=very_low&bbox=" + bbox); var fc = await r.json();
      if (fc.features && fc.features.length) showSelection({ flood: true, layerKey: "water-risk-cells", title: "Water risk cell", props: fc.features[0].properties });
    } catch (e) {}
  }
  // Icons and trees stand on sampled terrain instead of asking the globe for a height each. A clamped billboard costs
  // a terrain pick every time it is placed, and 1,070 icons with 792 trees cost the load 10 s of frozen page. One
  // sampleTerrain call per layer replaces them all; the offset each entity carried is kept, so a re-settle when the
  // terrain changes (the Esri terrain arriving after the ellipsoid) puts everything back on the new ground.
  function settleOnGround(entities) {
    var now = C.JulianDate.now(), list = [], cartos = [];
    entities.forEach(function (e) {
      var g = e.billboard || e.cylinder || e.point || e.box || e.ellipse; if (!g) return;
      var offset;
      if (e.__settle) offset = e.__settle.offset;
      else {
        var hr = g.heightReference && g.heightReference.getValue ? g.heightReference.getValue(now) : g.heightReference;
        if (hr !== C.HeightReference.CLAMP_TO_GROUND && hr !== C.HeightReference.RELATIVE_TO_GROUND) return;
        var pos0 = e.position && e.position.getValue(now); if (!pos0) return;
        var c0 = C.Cartographic.fromCartesian(pos0); if (!c0) return;
        var len = e.cylinder ? (e.cylinder.length && e.cylinder.length.getValue ? e.cylinder.length.getValue(now) : e.cylinder.length) : 0;
        offset = hr === C.HeightReference.CLAMP_TO_GROUND ? (e.cylinder ? len / 2 : 0) : c0.height;   // a cylinder is placed by its centre
        e.__settle = { lon: c0.longitude, lat: c0.latitude, offset: offset };
        g.heightReference = C.HeightReference.NONE;
      }
      list.push(e); cartos.push(new C.Cartographic(e.__settle.lon, e.__settle.lat));
    });
    if (!list.length) return Promise.resolve(0);
    return C.sampleTerrain(viewer.terrainProvider, 12, cartos).then(function () {
      list.forEach(function (e, i) {
        e.position = new C.ConstantPositionProperty(C.Cartesian3.fromRadians(e.__settle.lon, e.__settle.lat, (cartos[i].height || 0) + e.__settle.offset));
      });
      viewer.scene.requestRender(); return list.length;
    }).catch(function () { return 0; });   // no terrain to sample: the entities keep the height they have
  }
  function resettleAll() {
    Object.keys(DS).forEach(function (k) { if (DS[k]) settleOnGround(DS[k].entities.values); });
    if (FARM_DS) settleOnGround(FARM_DS.entities.values);
    settleTwin(true);
  }
  // The twin's own objects (facilities, sectors, the complaint points) arrive clamped to the ground, each graphic with a
  // callback on the terrain, a label with one per letter. Measured 29 Sep 2026: 11,664 callbacks at 785 points, 784 of the
  // points not even shown, and 11 of 18 seconds of a drag spent in the terrain picks they ask for. They are placed once
  // on sampled terrain instead, the way this page's own layers are, half a metre up so an icon never sinks into the
  // ground; an object the twin makes later is caught by the next pass. Only a position that does not move is touched.
  var TWIN_SETTLING = false;
  function settleTwin(again) {
    if (!viewer || TWIN_SETTLING) return Promise.resolve(0);
    var now = C.JulianDate.now(), NONE = C.HeightReference.NONE, CLAMP = C.HeightReference.CLAMP_TO_GROUND, list = [], cartos = [];
    viewer.entities.values.forEach(function (e) {
      if (!e.position || (e.position.isConstant === false)) return;
      var fresh = false, off = null;
      ["billboard", "label", "point"].forEach(function (k) {
        var g = e[k]; if (!g || !g.heightReference) return;
        var hr = g.heightReference.getValue ? g.heightReference.getValue(now) : g.heightReference;
        if (hr === undefined || hr === NONE) return;
        if (off === null) { var p0 = e.position.getValue(now); if (!p0) return; var c0 = C.Cartographic.fromCartesian(p0); if (!c0) return;
          off = hr === CLAMP ? 0.5 : c0.height; e.__settle = { lon: c0.longitude, lat: c0.latitude, offset: off }; }
        g.heightReference = NONE; fresh = true;
      });
      if ((fresh || (again && e.__settle && e.__settleTwin)) && e.__settle) { e.__settleTwin = true; list.push(e); cartos.push(new C.Cartographic(e.__settle.lon, e.__settle.lat)); }
    });
    if (!list.length) { releaseHeights(); return Promise.resolve(0); }
    TWIN_SETTLING = true;
    return C.sampleTerrain(viewer.terrainProvider, 12, cartos).then(function () {
      list.forEach(function (e, i) { e.position = new C.ConstantPositionProperty(C.Cartesian3.fromRadians(e.__settle.lon, e.__settle.lat, (cartos[i].height || 0) + e.__settle.offset)); });
      TWIN_SETTLING = false; viewer.scene.requestRender(); setTimeout(releaseHeights, 1200); return list.length;
    }).catch(function () { TWIN_SETTLING = false; return 0; });
  }
  // what is left on the terrain once nothing is clamped belongs to nothing: a label gives its letters back to a pool
  // without taking their callbacks off, so they pile up (416 at one facility). Each is released through its own remove
  // function, and only when no object of the page is clamped, so a callback in use is never taken away.
  function releaseHeights() {
    try {
      var now = C.JulianDate.now(), NONE = C.HeightReference.NONE, held = false;
      var clamped = function (e) { return ["billboard", "label", "point", "cylinder", "box", "ellipse", "model"].some(function (k) { var g = e[k]; if (!g || !g.heightReference) return false; var hr = g.heightReference.getValue ? g.heightReference.getValue(now) : g.heightReference; return hr !== undefined && hr !== NONE; }); };
      held = viewer.entities.values.some(clamped);
      for (var i = 0; i < viewer.dataSources.length && !held; i++) held = viewer.dataSources.get(i).entities.values.some(clamped);
      if (held) return 0;
      var surf = viewer.scene.globe._surface; if (!surf || !surf.forEachLoadedTile) return 0;
      var all = []; surf.forEachLoadedTile(function (t) { var cd = t.customData; if (!cd) return; (cd.forEach ? cd : Array.prototype.slice.call(cd)).forEach(function (d) { all.push(d); }); });
      var n = 0; all.forEach(function (d) { if (d && typeof d.removeFunc === "function") { try { d.removeFunc(); n++; } catch (e) {} } });
      if (n) viewer.scene.requestRender();
      return n;
    } catch (e) { return 0; }
  }
  // the readout under the pointer asks the terrain for a point on every mouse move, a drag included: it rests while a
  // button is down and answers ten times a second otherwise
  function easeReadout() {
    try {
      var h = viewer.screenSpaceEventHandler, T = C.ScreenSpaceEventType.MOUSE_MOVE, was = h.getInputAction(T); if (!was || was.__eased) return;
      var last = 0, down = false;
      viewer.canvas.addEventListener("pointerdown", function () { down = true; }, true);
      window.addEventListener("pointerup", function () { down = false; }, true);
      window.addEventListener("pointercancel", function () { down = false; }, true);
      var eased = function (m) { var t = Date.now(); if (down || t - last < 100) return; last = t; was(m); };
      eased.__eased = true; h.setInputAction(eased, T);
    } catch (e) {}
  }
  async function loadFloodLayer(layer) {
    if (!viewer) return; var fc;
    if (layer.flood === "cells") return loadFloodCells(layer);
    try { var r = await fetch(SWMP + "/v1/layers/" + layer.key, { headers: { Accept: "application/json" } }); if (!r.ok) throw new Error("HTTP " + r.status); fc = await r.json(); } catch (e) { COUNTS[layer.key] = "?"; return; }
    if (DS[layer.key]) { viewer.dataSources.remove(DS[layer.key], true); DS[layer.key] = null; }
    var ds = await C.GeoJsonDataSource.load(fc, { clampToGround: true }); var byId = {}; fc.features.forEach(function (x) { byId[x.id] = x; });
    ds.entities.values.slice().forEach(function (e) { var f = byId[e.id]; styleFlood(e, layer, f ? f.properties : {}); });
    ds.show = layer.on; DS[layer.key] = ds; await viewer.dataSources.add(ds); settleOnGround(ds.entities.values); COUNTS[layer.key] = fc.count;
  }
  async function loadFloodLayers() {
    if (!document.getElementById("twin-flood-css")) { var st = document.createElement("style"); st.id = "twin-flood-css"; st.textContent = ".tlh{font-size:11.5px;margin:14px 0 4px;text-transform:uppercase;letter-spacing:.06em;color:var(--ink-faint)}.wsrc{border-left:3px solid var(--good);padding-left:8px}.wsrc.demo{border-color:var(--high)}.wsrc.stale{border-color:var(--extreme)}.fl-legend{display:flex;flex-wrap:wrap;gap:6px 10px;font-size:11.5px;margin:6px 0;color:var(--ink-soft)}.fl-legend i{display:inline-block;width:11px;height:11px;border-radius:2px;margin-right:4px;vertical-align:-1px}.badge{display:inline-block;padding:1px 6px;border-radius:10px;font-size:11px;font-weight:700;color:#fff;background:var(--ink-faint)}.badge.demo{background:var(--high)}.badge.cls{color:#0C1319}"; document.head.appendChild(st); }
    try { var r = await fetch(SWMP + "/v1/weather/sources"); FLOOD_WEATHER = r.ok ? await r.json() : null; } catch (e) { FLOOD_WEATHER = null; }
    for (var i = 0; i < FLOOD_LAYERS.length; i++) { var fl = FLOOD_LAYERS[i]; if (fl.flood === "cells" && !fl.on && !CELL_PRIMS.length) { COUNTS[fl.key] = "tick to load"; continue; } await loadFloodLayer(fl); }
    renderLayers();
  }
  function floodSectionHtml() {
    var w = FLOOD_WEATHER;
    var weather = !w ? '<div class="note">Flood platform not reachable at ' + esc(SWMP) + ' (start it with the swmp-api server); set another address with localStorage swmp-api-url.</div>'
      : '<div class="note wsrc ' + (w.data_status === "demo" ? "demo" : (w.stale ? "stale" : "ok")) + '"><b>' + (w.data_status === "demo" ? "DEMO ONLY weather data" : "Weather " + esc(w.data_status)) + '</b> via ' + esc(w.adapter) + (w.stale ? " (stale)" : "") + '. ' + esc(w.message) + '</div>';
    return '<div class="tlh">Flood and water accumulation</div>' + weather +
      FLOOD_LAYERS.map(function (l) { return '<div class="layer"><input type="checkbox" data-layer="' + l.key + '"' + (l.on ? " checked" : "") + '><span class="sw" style="background:' + l.color + '"></span><span>' + esc(l.title) + '<small>' + esc(l.note) + '</small>' + (l.legend ? '<small style="display:block;margin-top:3px">' + l.legend.map(function (g) { return '<span style="display:inline-block;width:9px;height:9px;border-radius:2px;background:' + esc(g.colour) + ';margin:0 4px 0 0;vertical-align:-1px"></span>' + esc(g.label) + ' '; }).join('') + '</small>' : '') + '</span><span class="n">' + (COUNTS[l.key] == null ? "…" : COUNTS[l.key]) + '</span></div>'; }).join("") +
      '<div class="fl-legend">' + Object.keys(CLASS_COLOUR).map(function (k) { return '<span><i style="background:' + CLASS_COLOUR[k] + '"></i>' + k.replace("_", " ") + '</span>'; }).join("") + '</div>' +
      '<div class="tlh">Performance</div><div class="layer" style="grid-template-columns:1fr"><label style="margin:0">Rendering <select data-pref="twinmap-render" style="width:auto;margin-left:6px"><option value="change"' + (PREF("twinmap-render", "change") === "change" ? " selected" : "") + '>only on change (fast, default)</option><option value="always"' + (PREF("twinmap-render", "change") === "always" ? " selected" : "") + '>continuous (animations of the twin module)</option></select></label></div>' +
      '<div class="layer" style="grid-template-columns:1fr"><label style="margin:0">Building detail <select data-pref="twinmap-detail" style="width:auto;margin-left:6px"><option value="max"' + (PREF("twinmap-detail", "max") === "max" ? " selected" : "") + '>realistic with cast shadows (default)</option><option value="real"' + (PREF("twinmap-detail", "max") === "real" ? " selected" : "") + '>realistic without shadows (lighter)</option><option value="plain"' + (PREF("twinmap-detail", "max") === "plain" ? " selected" : "") + '>plain blocks (fastest, for a weak graphics card)</option></select></label></div>' +
      '<div class="layer" style="grid-template-columns:1fr"><label style="margin:0">Homes <select data-pref="twinmap-homes" style="width:auto;margin-left:6px"><option value="all"' + (PREF("twinmap-homes", "all") === "all" ? " selected" : "") + '>every building in its own colour</option><option value="homes"' + (PREF("twinmap-homes", "all") === "homes" ? " selected" : "") + '>homes only, everything else grey</option></select></label></div>' +
      '<div class="layer" style="grid-template-columns:1fr"><label style="margin:0">Building heights <select data-pref="twinmap-heights" style="width:auto;margin-left:6px"><option value="emphasised"' + (PREF("twinmap-heights", REAL ? "estimated" : "emphasised") === "emphasised" ? " selected" : "") + '>emphasised, 1.5 times the recorded or estimated height, for reading the map (default for the diagram look)</option><option value="estimated"' + (PREF("twinmap-heights", REAL ? "estimated" : "emphasised") === "estimated" ? " selected" : "") + '>as recorded or estimated (true scale, default for the real look)</option><option value="flat"' + (PREF("twinmap-heights", REAL ? "estimated" : "emphasised") === "flat" ? " selected" : "") + '>flat 6 m boxes (faster)</option></select></label></div>' +
      '<div class="layer" style="grid-template-columns:1fr"><label style="margin:0">Farm tree symbols <select data-pref="twinmap-trees" style="width:auto;margin-left:6px"><option value="3d"' + (PREF("twinmap-trees", "3d") === "3d" ? " selected" : "") + '>3D cylinders (default)</option><option value="flat"' + (PREF("twinmap-trees", "3d") === "flat" ? " selected" : "") + '>flat symbols (faster)</option></select></label></div>' +
      '<div class="note">Symbols: water drop predicted accumulation, drop with clock historical flood, drop with ! active report (pulsing while unverified), red octagon official closure, grate with red cross drainage blockage, waves wadi crossing, arch underpass risk, amber triangle road data quality error. Click a symbol or cell: source, timestamp, confidence and verification status open on the Selection tab.</div>';
  }
  var FLOOD_FIELDS = { code: "Code", kind: "Kind", status: "Status", risk_class: "Risk class", score: "Score (0 to 100)", confidence: "Confidence", confidence_score: "Confidence score", run_code: "Model run", rainfall_scenario: "Rainfall scenario",
    elevation_m: "Elevation (m)", slope_pct: "Slope (%)", flow_accumulation_class: "Flow accumulation", flow_accumulation_cells: "Flow accumulation (cells)", is_depression: "Depression", nearest_road_name: "Nearest road", nearest_road_distance_m: "Road distance (m)", road_impact: "Road impact",
    building_intersection: "Building intersection", expected_depth_m: "Expected depth (m)", field_verification_status: "Field verification", field_verified_at: "Verified at", verification: "Verification", source: "Source", data_status: "Data status",
    timestamp: "Timestamp", calculated_at: "Calculated at", road_name: "Road", other_name: "Other road", reason: "Reason", authority: "Authority", ends_at: "Ends", reporter_kind: "Reported by", description: "Description", depth_estimate_m: "Depth estimate (m)",
    verification_due_at: "Verification due", expires_at: "Penalty expires", network_release: "Network release", overlap_m2: "Overlap (m2)", overlap_length_m: "Crossing length (m)", is_error: "Error", name: "Name", operator: "Operator", condition: "Condition",
    f_rainfall_total: "Factor rainfall total", f_rainfall_intensity: "Factor rainfall intensity", f_elevation: "Factor elevation", f_slope: "Factor slope", f_flow_accumulation: "Factor flow accumulation", f_depression: "Factor depression", f_drainage: "Factor drainage",
    f_impervious: "Factor impervious", f_history: "Factor history", f_wadi: "Factor wadi", f_road_underpass: "Factor road and underpass" };
  function floodSelectionHtml(sel) {
    var p = sel.props || {};
    var rows = Object.keys(FLOOD_FIELDS).filter(function (k) { return p[k] != null && p[k] !== "" && typeof p[k] !== "object"; }).map(function (k) {
      var v = p[k];
      if (k === "risk_class") v = '<span class="badge cls" style="background:' + (CLASS_COLOUR[v] || "#ccc") + '">' + esc(String(v).replace("_", " ")) + '</span>';
      else if (k === "data_status") v = '<span class="badge' + (v === "demo" ? " demo" : "") + '">' + esc(v === "demo" ? "DEMO ONLY" : v) + '</span>';
      else if (typeof v === "number") v = esc(Math.round(v * 1000) / 1000);
      else v = esc(v);
      return "<dt>" + FLOOD_FIELDS[k] + "</dt><dd>" + v + "</dd>";
    }).join("");
    if (p.detail && typeof p.detail === "object") rows += "<dt>Detail</dt><dd>" + esc(JSON.stringify(p.detail)) + "</dd>";
    return '<h2>' + esc(sel.title) + (p.demo_only ? ' <span class="badge demo">DEMO ONLY</span>' : "") + '</h2><dl class="kv">' + rows + '</dl><div class="note">Source: Smart Waste Platform flood module at ' + esc(SWMP) + '.</div>';
  }

  // ------------------------------------------------------------ layers over the twin
  var LAYERS = [
    { key: "sector_borders", dsKey: "sector_borders", title: "Sector borders (bold, labelled)", color: "#4FF0FF", kind: "custom", on: true, note: "the nine PSD sectors, glowing outline and name" },
    { key: "roads_labelled", dsKey: "roads_labelled", title: "Roads, cased and named (main network)", color: "#F5A623", kind: "roads", on: true, note: "motorways and trunks amber, primaries yellow, secondaries and tertiaries white, all on a dark casing, names in a halo; OpenStreetMap through the platform" },
    { key: "container", title: "Bins and collection points", color: "#57C4DD", kind: "point", on: true, note: "user submitted or surveyed" },
    { key: "container", dsKey: "container_proposed", scenario: "SC-BINS-HOUSES-20260914", title: "Proposed bins, one 1,100 L per house", color: "#8ECAE6", kind: "point", on: true, note: "scenario, estimated positions at the house; confirm on survey" },
    { key: "houses", dsKey: "houses", title: "Houses and buildings (3D; one 1,100 L bin per house)", color: "#C9D1D9", kind: "houses", on: false, note: "every building the department holds as a flat footprint out to 30 km, and solids on top wherever the ground near the point you are looking at holds up to 20,000 of them, which is about 2 to 3 km up over the city and well over 10 km over the farms, so no view can be missing one. Every building footprint in the nine sectors (Overture Maps: OpenStreetMap, Microsoft and Google footprints merged) at its source height where one is recorded, else its floor count, else an estimate from the footprint and class; houses grey, red where the density is heavy, other buildings blue grey, structures under 40 m2 dark; only houses carry a bin" },
    { key: "landuse_public", title: "Public areas", color: "#F2C94C", kind: "polygon", on: true, note: "yellow: public areas; red: heavy public areas (drawn by the department)" },
    { key: "landuse_commercial", title: "Commercial zones", color: "#E5934B", kind: "polygon", on: true, note: "OpenStreetMap reference" },
    { key: "landuse_industrial", title: "Commercial and industrial", color: "#E060D8", kind: "polygon", on: true, note: "pink: commercial and industrial areas (drawn by the department) and OpenStreetMap reference" },
    { key: "landuse_residential", title: "Residential zones", color: "#B0B7C3", kind: "polygon", on: false, note: "OpenStreetMap reference" },
    { key: "landuse_agriculture", title: "Nurseries, agriculture, farms (3D tree symbols)", color: "#4FBF8C", kind: "polygon", on: true, note: "green: nurseries, agriculture and farms (drawn by the department); tree symbols every 140 m inside each area" },
    { key: "generator_medical", title: "Medical generators", color: "#E8646A", kind: "point", on: true, note: "hospitals, clinics, pharmacies" },
    { key: "generator_commercial", title: "Commercial generators", color: "#F2C14E", kind: "point", on: true, note: "malls, markets, supermarkets" },
    { key: "contract_zone", title: "Contract zones (RAKEZ)", color: "#B14BD9", kind: "polygon", on: true, note: "purple: RAKEZ contract zones (Al Jazeera, Al Ghail) and other contracts" },
    { key: "service_area", title: "Service areas (drawn)", color: "#C77DFF", kind: "polygon", on: true, note: "drawn by the department; orange: colour class still to confirm" },
    { key: "collection_route", title: "Planned routes (selected plan)", color: "#FFD166", kind: "line", on: true, note: "proposed scenarios only" },
    { key: "area_priority", dsKey: "area_priority", title: "Collection areas by priority (RPT-13)", color: "#D9342B", kind: "priority", on: false, note: "where the department starts: 79 of the 133 named areas ranked on full container reports per 1,000 houses, houses per km2, and the share of houses under 350 m2, equally weighted. Click an area for its rank, score, the three indicator ranks behind it and the containers its own analysis requires." },
    { key: "bin_register", dsKey: "bin_register", title: "Bins on the ground (asset register, positions)", color: "#F59E0B", kind: "binreg", on: false, note: "every container the department holds a position for, at that position: a magenta box at the real size of that container within 900 m, where a 1.37 m container still reads as an object, and a dot coloured by size above that. Magenta because nothing on the ground is that colour, so a container is never missed. Click one for its identifier, size, body type, what it serves and whether it is free of cost or paid. Recorded 2024, before the 1.1 m3 programme, and not survey verified." },
    { key: "bin_programme", dsKey: "bin_programme", title: "Bin programme: placed, planned and owed", color: "#22C55E", kind: "binprog", on: false, note: "the section's mobilisation framework drawn on the collection areas: green complete, amber in progress, red pending. Click an area for the bins placed there and the programme for its sub-sector. Counts are reported by the section, not surveyed positions." },
    { key: "smart_bins", dsKey: "smart_bins", title: "Smart bins (RFID, Sector 4B): last collection read", color: "#22C55E", kind: "smartbins", on: false, note: "the bins the smart bin system holds an RFID record for, at the position registered at deployment, coloured by the days from the last collection read to the export: green within 4 days, yellow 5 to 6, amber 7 to 13, red 14 to 29, dark red 30 or more. A box at the real size of the bin within 900 m, a dot above that. Click one for its number, tag, deployment, last read and last washing. A snapshot: the age is counted to the export, not to today." },
    { key: "msw_subsector_areas", title: "MSW sub-sector areas (named)", color: "#4FF0FF", kind: "local", file: "omcc-source/rak_subsectors.geojson", pick: "area", on: false, note: "133 named collection areas by sub-sector (1A to 9A), drawn in the department map's own colours; Google map exported 15 Sep 2026" },
    { key: "sand_tracks", dsKey: "sand_tracks", title: "Sand tracks and unpaved access (dotted)", color: "#E9D8A6", kind: "local", file: "omcc-source/rak_tracks.geojson", pick: "track", on: false, note: "OpenStreetMap tracks and roads tagged unpaved, drawn dotted because there is no built road; where a housing area shows no track at all, none is mapped yet" },
    { key: "msw_route_areas", title: "MSW collection route areas (R codes)", color: "#F5A623", kind: "local", file: "omcc-source/rak_subsectors.geojson", pick: "route", on: false, note: "42 route areas (R01 onwards) inside the sub-sectors; the same department Google map" }
  ];
  var ROUTE_COLORS = ["#FFD166", "#06D6A0", "#EF476F", "#A786FF", "#F78C6B", "#8ECAE6", "#C77DFF", "#90BE6D"];
  var DS = {}; var COUNTS = {}; var FEATS = {}; var viewer = null; var currentPlan = null;
  // housesFlat: the roofs painted on the ground show below it (30 km until 29 Sep 2026, when a town from 12 km read as empty ground);
  // solidFar: solids are built below it, the nearest first up to the cap; from there a house is under a pixel and its painted roof carries it
  var LOD = { houses: 2000, housesFlat: 60000, solidFar: 14000, cells: 45000, trees: 12000, bins: 900, ids: 700, roadSurf: 8000, villa: 1200 };
  // 2D fallback (progressive enhancement): requested with ?view=2d, or forced when the browser has no WebGL; twin-2d.js draws the same layers on a Leaflet map
  var WANT2D = (function () { try { if (/[?&]view=2d(&|$)/.test(location.search)) return "requested"; var c = document.createElement("canvas"); if (!(c.getContext("webgl2") || c.getContext("webgl") || c.getContext("experimental-webgl"))) return "no-webgl"; } catch (e) { return "no-webgl"; } return ""; })();
  function PREF(k, d) { try { return localStorage.getItem(k) || d; } catch (e) { return d; } }
  function setPref(k, v) { try { localStorage.setItem(k, v); } catch (e) {} }
  function cameraHeight() { try { return viewer.camera.positionCartographic.height; } catch (e) { return 0; } }
  function applyLod() {
    var h = cameraHeight(); var hl = LAYERS.find(function (x) { return x.kind === "houses"; }); var cl = FLOOD_LAYERS.find(function (x) { return x.flood === "cells"; });
    var solid = !!(hl && hl.on) && !!(HOUSE_BUILT && HOUSE_BUILT.solid) && h < LOD.solidFar;
    // the count has crossed the cap since the last build, one way or the other: the other draw is wanted now
    if (hl && hl.on && HOUSE_BUILT && h < LOD.housesFlat && solidWanted() !== !!HOUSE_BUILT.solid) housesFollowCamera(true);
    else if (hl && hl.on && HOUSE_BUILT && HOUSE_BUILT.solid && ID_ON && (h < LOD.ids) !== !!HOUSE_BUILT.ids) housesFollowCamera(true);   // crossed 700 m: the roof codes come or go
    else if (hl && hl.on && HOUSE_BUILT && HOUSE_BUILT.solid && REAL && (h < LOD.villa) !== !!HOUSE_BUILT.villa) housesFollowCamera(true);   // crossed 1.2 km: the villa detail comes or goes
    cardSoon();
    HOUSE_PRIMS.forEach(function (p) { p.show = solid; });
    if (HOUSE_FLAT) HOUSE_FLAT.show = !!(hl && hl.on) && h < LOD.housesFlat;   // always under the solids: what is beyond them is a footprint, never absent
    // the count belongs to whichever draw is showing, so it is refreshed when the band changes rather than left standing
    if (hl && hl.on && HOUSE_BUILT) { var lbl = houseCountLabel(); if (COUNTS.houses !== lbl) { COUNTS.houses = lbl; renderLayers(); } }
    CELL_PRIMS.forEach(function (p) { p.show = !!(cl && cl.on) && h < LOD.cells; });
    var surf = REAL && !!ROAD_LAYER && h < LOD.roadSurf;   // below 8 km the painted roads carry the network; above it the line roads do
    if (ROAD_LAYER) ROAD_LAYER.show = REAL;
    var rl = LAYERS.find(function (x) { return x.kind === "roads"; });
    if (rl && ROAD_LAYER) { ROAD_PRIMS.forEach(function (p) { p.show = rl.on && !surf; }); twinRoads(!rl.on && !surf); }
    platformRoads(!surf);
    var bl = LAYERS.find(function (x) { return x.kind === "binreg"; });
    if (bl && (BINREG_PRIM || BINREG_BOX)) {
      if (BINREG_PRIM) BINREG_PRIM.show = !!bl.on && h >= LOD.bins;   // dots far
      if (BINREG_BOX) BINREG_BOX.show = !!bl.on && h < LOD.bins;     // boxes near
    }
    var sbl = LAYERS.find(function (x) { return x.kind === "smartbins"; });
    if (sbl && (SB_PRIM || SB_BOX)) { if (SB_PRIM) SB_PRIM.show = !!sbl.on && h >= LOD.bins; if (SB_BOX) SB_BOX.show = !!sbl.on && h < LOD.bins; }   // dots far, boxes near, as the register
    if (FARM_DS) FARM_DS.show = h < LOD.trees && !!(LAYERS.find(function (x) { return x.key === "landuse_agriculture"; }) || {}).on;
  }
  // the nine sectors with a margin: where the camera goes when it has nowhere to be
  var HOME = { west: 55.70, south: 24.84, east: 56.30, north: 26.10 };
  function flyHome(why) {
    if (!viewer) return;
    var b = sectorsBbox();
    var rect = C.Rectangle.fromDegrees(b[0], b[1], b[2], b[3]);
    if (!isFinite(rect.west) || rect.east <= rect.west) rect = C.Rectangle.fromDegrees(HOME.west, HOME.south, HOME.east, HOME.north);
    viewer.camera.lookAtTransform(C.Matrix4.IDENTITY);
    viewer.camera.flyTo({ destination: rect, duration: why ? 0 : 1.5 });
    viewer.scene.requestRender();
    if (why) status("The view was reset: " + why);
  }

  // a white canvas is recoverable, so recover from it: a lost WebGL context, a render error, or a camera with nothing
  // under it all end as a blank globe, and the user is left with no way back except a reload
  function renderGuard() {
    var scene = viewer.scene, canvas = scene.canvas;
    canvas.addEventListener("webglcontextlost", function (e) {
      e.preventDefault();
      status("The graphics context was lost. Reload the page to restore the 3D view.");
    }, false);
    canvas.addEventListener("webglcontextrestored", function () { flyHome("the graphics context came back"); }, false);
    scene.renderError.addEventListener(function (s, err) {
      status("The 3D view hit an error: " + (err && err.message ? err.message : err) + ". Reload the page.");
    });
    // the camera can be left outside the globe by a bad flyTo or a stale saved view: put it back rather than show white
    setInterval(function () {
      if (!viewer || viewer.isDestroyed()) return;
      var p = viewer.camera.positionCartographic;
      if (!p) return;
      var lon = C.Math.toDegrees(p.longitude), lat = C.Math.toDegrees(p.latitude);
      var off = lon < HOME.west - 6 || lon > HOME.east + 6 || lat < HOME.south - 6 || lat > HOME.north + 6;
      if (off || p.height > 9.0e6 || p.height < -200) flyHome(off ? "the camera had left the emirate" : "the camera was out of range");
    }, 4000);
  }

  // a stable pseudo random in 0..1 from the row index: the same building varies the same way on every load
  function jitter(i) { var x = Math.sin(i * 12.9898) * 43758.5453; return x - Math.floor(x); }

  // scene quality. The buildings were drawn without antialiasing, without contact shading and under a flat light, so they
  // read as printed blocks. Cost is measured on the machine, not assumed: see the change record.
  function applyDetail() {
    if (!viewer) return;
    var lvl = PREF("twinmap-detail", "max"), scene = viewer.scene, pp = scene.postProcessStages;
    var plain = lvl === "plain";
    try { scene.msaaSamples = plain ? 1 : 4; } catch (e) {}
    try { pp.fxaa.enabled = !plain; } catch (e) {}
    try { if (pp.ambientOcclusion) { pp.ambientOcclusion.enabled = !plain; if (!plain) { pp.ambientOcclusion.uniforms.intensity = 2.2; pp.ambientOcclusion.uniforms.bias = 0.12; pp.ambientOcclusion.uniforms.lengthCap = 0.28; } } } catch (e) {}
    // a sun angle rather than a head on lamp: the faces of a building then differ and the massing reads
    try { scene.light = plain ? new C.DirectionalLight({ direction: new C.Cartesian3(0.35, -0.5, -0.79) })
                             : (REAL ? new C.DirectionalLight({ direction: C.Cartesian3.normalize(new C.Cartesian3(-0.873, -0.482, 0.078), new C.Cartesian3()), intensity: 1.0, color: new C.Color(1.0, 0.96, 0.88) })   // 50 degrees up from the south west over Ras Al Khaimah, warm
                                   : new C.DirectionalLight({ direction: C.Cartesian3.normalize(new C.Cartesian3(0.62, -0.30, -0.72), new C.Cartesian3()), intensity: 2.1 })); } catch (e) {}
    try { viewer.shadows = lvl === "max"; viewer.terrainShadows = lvl === "max" ? C.ShadowMode.ENABLED : C.ShadowMode.DISABLED; } catch (e) {}
    // the real look: shadows lighter in the shade, and a little haze with distance. Soft shadows at 4096 were tried and drew
    // rings of shadow acne across every large flat roof, so the map keeps hard shadows at 2048, which are clean and cheaper
    try { var sm = viewer.shadowMap; sm.softShadows = false; sm.size = 2048; sm.darkness = REAL ? 0.42 : 0.3; sm.maximumDistance = 5000; } catch (e) {}
    try { scene.fog.enabled = true; scene.fog.density = REAL ? 0.00032 : 0.0002; } catch (e) {}
    scene.requestRender();
  }

  var MOVING_T = null, AT_REST = true;
  function quality(rest) {
    if (AT_REST === rest || !viewer) return; AT_REST = rest;
    var lvl = PREF("twinmap-detail", "max"), pp = viewer.scene.postProcessStages;
    try { viewer.shadows = rest && lvl === "max"; } catch (e) {}
    try { if (pp.ambientOcclusion) pp.ambientOcclusion.enabled = rest && lvl !== "plain"; } catch (e) {}
    viewer.scene.requestRender();
  }
  function fastScene() {
    // render only when something changed: navigation stays fluid and the laptop stays cool; loads and animations ask for a frame
    var scene = viewer.scene; scene.requestRenderMode = PREF("twinmap-render", "change") === "change"; scene.maximumRenderTimeChange = 0.5; scene.globe.maximumScreenSpaceError = 2.4;
    if (viewer.scene.terrainProviderChanged) viewer.scene.terrainProviderChanged.addEventListener(function () { setTimeout(resettleAll, 1500); });   // the Esri terrain arrives after the ellipsoid
    // shadows and ambient occlusion cost 19 ms of a 37 ms frame: they rest while the camera moves and return 400 ms after it stops
    viewer.camera.changed.addEventListener(function () { quality(false); if (MOVING_T) clearTimeout(MOVING_T); MOVING_T = setTimeout(function () { MOVING_T = null; quality(true); }, 400); });
    viewer.camera.percentageChanged = 0.05; viewer.camera.changed.addEventListener(applyLod);
    // the changed event hands its listeners the fraction the camera moved by: passed straight on, that number was read as
    // housesFollowCamera's force argument, and every pan rebuilt every solid whether or not the view had left what was
    // built (29 Sep 2026: 36 rebuilds in twenty pans of 300 m, the page frozen 42 of 62 seconds). Nothing is passed on.
    viewer.camera.changed.addEventListener(function () { housesFollowCamera(); }); viewer.camera.moveEnd.addEventListener(function () { housesFollowCamera(); });
    viewer.dataSources.dataSourceAdded.addEventListener(function () { scene.requestRender(); }); viewer.dataSources.dataSourceRemoved.addEventListener(function () { scene.requestRender(); });
    setInterval(function () { if (SIM.running) scene.requestRender(); }, 100);
  }

  function styleEntity(e, layer, props) {
    var hint = props.attributes && props.attributes.colourHint;
    var col = C.Color.fromCssColorString(hint || layer.color);
    if (e.polygon && layer.kind === "polygon3d") {
      var dens = props.attributes && props.attributes.density; var hc = C.Color.fromCssColorString(dens === "heavy" ? "#D9342B" : layer.color);
      e.polygon.material = hc.withAlpha(0.75); e.polygon.outline = true; e.polygon.outlineColor = C.Color.fromCssColorString("#0C1319").withAlpha(0.8);
      e.polygon.classificationType = undefined; e.polygon.perPositionHeight = false; e.polygon.height = 0; e.polygon.heightReference = C.HeightReference.CLAMP_TO_GROUND;
      e.polygon.extrudedHeight = (props.attributes && props.attributes.heightM) || 6; e.polygon.extrudedHeightReference = C.HeightReference.RELATIVE_TO_GROUND;
    } else if (e.polygon) { e.polygon.material = col.withAlpha(0.14); e.polygon.outline = true; e.polygon.outlineColor = col.withAlpha(0.9); e.polygon.classificationType = C.ClassificationType.TERRAIN; e.polygon.height = undefined; e.polygon.perPositionHeight = false; }
    if (e.polyline) { var ra = props.attributes || {}, vi = (ra.dayGroups > 1 ? ra.dayGroup : ra.vehicle) || 1; e.polyline.material = C.Color.fromCssColorString(ROUTE_COLORS[(vi - 1) % ROUTE_COLORS.length]).withAlpha(0.95); e.polyline.width = 5; e.polyline.clampToGround = true; }
    if (e.billboard) e.billboard = undefined;
    if (e.position && !e.polygon && !e.polyline) {
      var origin = props.origin || "";
      e.point = new C.PointGraphics({ pixelSize: layer.key === "container" ? 9 : 8, color: col, outlineColor: origin === "surveyed" ? C.Color.WHITE : C.Color.fromCssColorString("#0C1319"), outlineWidth: origin === "surveyed" ? 2 : 1, heightReference: C.HeightReference.CLAMP_TO_GROUND, disableDepthTestDistance: Number.POSITIVE_INFINITY });
      if (layer.kind === "point" && layer.key !== "container") e.label = new C.LabelGraphics({ text: props.name || "", font: "11px Calibri, sans-serif", fillColor: C.Color.fromCssColorString("#E7EEF3"), outlineColor: C.Color.fromCssColorString("#0C1319"), outlineWidth: 3, style: C.LabelStyle.FILL_AND_OUTLINE, pixelOffset: new C.Cartesian2(0, -14), heightReference: C.HeightReference.NONE, disableDepthTestDistance: Number.POSITIVE_INFINITY, scaleByDistance: new C.NearFarScalar(2000, 1.0, 30000, 0.0) });
    }
    e.twinLayer = layer.key; e.twinProps = props;
  }
  var FARM_DS = null; var SECTOR_DS = null;
  var NOT_COLLECTION = /complain|survey|proposed|aj_|grid|cell|leachate|well|buildings/i;
  function collectionOnly() {
    var T = window.__wmaTwin; if (!T || !T.layers) return;
    Object.keys(T.layers).forEach(function (k) { if (NOT_COLLECTION.test(k)) { var L2 = T.layers[k]; (L2.items || []).forEach(function (e) { try { e.show = false; } catch (x) {} }); L2.on = false; var cb = document.querySelector('input[data-layer="' + k + '"]'); if (cb && cb.checked) { cb.checked = false; } } });
  }
  function sectorBorders(on) {
    var T = window.__wmaTwin; if (!viewer || !T || !T.toGeoJSON) return;
    if (SECTOR_DS) { viewer.dataSources.remove(SECTOR_DS, true); SECTOR_DS = null; }
    if (on === false) { COUNTS.sector_borders = 9; renderLayers(); return; }
    var ds = new C.CustomDataSource("sector-borders"); var fc = T.toGeoJSON(); var glow = new C.PolylineGlowMaterialProperty({ glowPower: 0.3, taperPower: 1, color: C.Color.fromCssColorString("#4FF0FF") });
    fc.features.forEach(function (f) {
      var rings = f.geometry.type === "Polygon" ? f.geometry.coordinates : [].concat.apply([], f.geometry.coordinates);
      rings.forEach(function (ring, ri) {
        var flat = []; ring.forEach(function (c) { flat.push(c[0], c[1]); });
        ds.entities.add({ polyline: { positions: C.Cartesian3.fromDegreesArray(flat), width: 10, material: glow, clampToGround: true } });
        if (ri === 0) { var lon = 0, lat = 0; ring.forEach(function (c) { lon += c[0]; lat += c[1]; }); lon /= ring.length; lat /= ring.length; var p = f.properties || {};
          ds.entities.add({ position: C.Cartesian3.fromDegrees(lon, lat), label: { text: "SECTOR " + p.n + (p.areas ? "\n" + (Array.isArray(p.areas) ? p.areas.join(", ") : p.areas) : ""), font: "bold 18px Calibri, sans-serif", fillColor: C.Color.fromCssColorString("#4FF0FF"), outlineColor: C.Color.fromCssColorString("#0C1319"), outlineWidth: 4, style: C.LabelStyle.FILL_AND_OUTLINE, showBackground: true, backgroundColor: C.Color.fromCssColorString("#0C1319").withAlpha(0.55), heightReference: C.HeightReference.NONE, disableDepthTestDistance: Number.POSITIVE_INFINITY, scaleByDistance: new C.NearFarScalar(5000, 1.1, 90000, 0.55) } }); }
      });
    });
    SECTOR_DS = ds; viewer.dataSources.add(ds); COUNTS.sector_borders = fc.features.length; renderLayers();
  }
  function pointInRing(x, y, ring) { var inside = false; for (var i = 0, j = ring.length - 1; i < ring.length; j = i++) { var xi = ring[i][0], yi = ring[i][1], xj = ring[j][0], yj = ring[j][1]; if (((yi > y) !== (yj > y)) && (x < (xj - xi) * (y - yi) / (yj - yi) + xi)) inside = !inside; } return inside; }
  function farmSymbols(fc) {
    if (!viewer) return; if (FARM_DS) { viewer.dataSources.remove(FARM_DS, true); FARM_DS = null; }
    var ds = new C.CustomDataSource("farm-symbols"); var n = 0; var flat = PREF("twinmap-trees", "3d") === "flat";
    var trunk = C.Color.fromCssColorString("#6B4A2B"), canopy = C.Color.fromCssColorString("#2E9E63").withAlpha(0.95);
    var ddc = new C.DistanceDisplayCondition(0, 12000);
    var treeImg = (function () { var c = document.createElement("canvas"); c.width = 24; c.height = 32; var g = c.getContext("2d"); g.fillStyle = "#6B4A2B"; g.fillRect(10, 20, 4, 11); g.fillStyle = "#2E9E63"; g.beginPath(); g.moveTo(12, 1); g.lineTo(23, 22); g.lineTo(1, 22); g.closePath(); g.fill(); return c; })();
    fc.features.forEach(function (f) { var g = f.geometry; if (!g) return; var rings = g.type === "Polygon" ? [g.coordinates[0]] : g.coordinates.map(function (p) { return p[0]; });
      rings.forEach(function (ring) { var xs = ring.map(function (c) { return c[0]; }), ys = ring.map(function (c) { return c[1]; }); var step = 0.0013; // about 140 m
        for (var y = Math.min.apply(null, ys) + step / 2; y < Math.max.apply(null, ys); y += step) for (var x = Math.min.apply(null, xs) + step / 2; x < Math.max.apply(null, xs); x += step) {
          var jx = x + (Math.sin(x * 9000 + y * 7000) * 0.0003), jy = y + (Math.cos(x * 8000 - y * 6000) * 0.0003); if (!pointInRing(jx, jy, ring)) continue; if (n > 3000) return;
          if (flat) { ds.entities.add({ position: C.Cartesian3.fromDegrees(jx, jy), billboard: { image: treeImg, width: 18, height: 24, heightReference: C.HeightReference.CLAMP_TO_GROUND, verticalOrigin: C.VerticalOrigin.BOTTOM, distanceDisplayCondition: ddc, scaleByDistance: new C.NearFarScalar(500, 1.2, 12000, 0.4) } }); }
          else { ds.entities.add({ position: C.Cartesian3.fromDegrees(jx, jy), cylinder: { length: 4, topRadius: 0.6, bottomRadius: 0.6, material: trunk, heightReference: C.HeightReference.CLAMP_TO_GROUND, distanceDisplayCondition: ddc } });
            ds.entities.add({ position: C.Cartesian3.fromDegrees(jx, jy, 5.5), cylinder: { length: 9, topRadius: 0.2, bottomRadius: 5, material: canopy, heightReference: C.HeightReference.RELATIVE_TO_GROUND, distanceDisplayCondition: ddc } }); }
          n++; } }); });
    FARM_DS = ds; viewer.dataSources.add(ds); settleOnGround(ds.entities.values); COUNTS.farm_symbols = n; renderLayers();
  }
  // ------------------------------------------------------------ legible roads: cased lines and names
  var ROAD_STYLE = { motorway: ["#F5A623", 9], trunk: ["#F5A623", 8], primary: ["#F7C948", 6.5], secondary: ["#FFFFFF", 5], tertiary: ["#E9E9E9", 3.6], motorway_link: ["#F5A623", 4], trunk_link: ["#F5A623", 4], primary_link: ["#F7C948", 3.5], secondary_link: ["#FFFFFF", 3], tertiary_link: ["#E9E9E9", 2.6] };
  var ROAD_PRINT = { motorway: ["#2B2B2B", 4.5], trunk: ["#2B2B2B", 4], primary: ["#3A3A3A", 3.4], secondary: ["#5A5A5A", 2.6], tertiary: ["#7A7A7A", 1.8] };
  var ROADS_CACHE = null; var ROAD_PRIMS = [];
  // ------------------------------------------------------------ the ground picture: roads and footprints as imagery
  // Measured 27 Sep 2026 (tests/twin-map/perfonly.mjs, 2.5 km tilted): with no ground draped primitive the page draws a
  // frame in 37 ms; the footprints as one ground primitive add 40 ms, the road kerbs 64 ms and the asphalt 77 ms, and
  // together they take a frame to 303 ms, three frames a second. A ground primitive is drawn as a volume through the
  // terrain, twice, for every frustum. The same picture painted into imagery tiles costs the GPU one more texture: the
  // roads and the footprints are painted per tile on a canvas, from the same rows, widths and colours, and the globe
  // drapes them like any other imagery. Nothing is fetched. The solids stand on it.
  //   roads       the embedded network (1,658 main, 15,202 neighbourhood centrelines): kerb strip, asphalt at a width by
  //               class, dashed centre line on the main network. Widths are typical for the class, not surveyed.
  //   footprints  every building the filter leaves, in the colour the look gives its roof
  var TWIN_DATA = null, ROAD_LIST = null, ROAD_INDEX = null, ROAD_LAYER = null, FOOT_COLOUR = null, TILE_BLANK = null, PLAT_HIDDEN = [];
  var ROAD_WIDTH = [28, 22, 16, 12, 9, 7, 7, 7, 9, 7, 5, 4, 5];   // metres: 0 motorway, 1 trunk, 2 primary, 3 secondary, 4 tertiary, 5 to 7 links, 8 tertiary, 9 residential, 10 service, 11 and 12 the rest
  function decodeRoad(r, MM) { var f = r.p, x = 0, y = 0, arr = []; for (var i = 0; i < f.length; i += 2) { x += f[i]; y += f[i + 1]; arr.push(MM.origin[0] + x / MM.mlon, MM.origin[1] + y / MM.mlat); } return arr; }
  // decoded once and indexed on a 0.02 degree grid, so a tile asks a few cells for its roads
  function buildRoadIndex() {
    if (ROAD_LIST || !TWIN_DATA || !TWIN_DATA.meta) return;
    var MM = TWIN_DATA.meta, all = (TWIN_DATA.roads || []).concat(TWIN_DATA.roads_local || []); ROAD_LIST = []; ROAD_INDEX = {};
    all.forEach(function (r) {
      var arr = decodeRoad(r, MM); if (arr.length < 4) return;
      var w = 180, e = -180, so = 90, n = -90; for (var i = 0; i < arr.length; i += 2) { if (arr[i] < w) w = arr[i]; if (arr[i] > e) e = arr[i]; if (arr[i + 1] < so) so = arr[i + 1]; if (arr[i + 1] > n) n = arr[i + 1]; }
      var id = ROAD_LIST.length; ROAD_LIST.push({ p: arr, c: r.c, w: ROAD_WIDTH[r.c] || 6, main: r.c <= 4 || r.c === 8, box: [w, so, e, n] });
      for (var gx = Math.floor(w * 50); gx <= Math.floor(e * 50); gx++) for (var gy = Math.floor(so * 50); gy <= Math.floor(n * 50); gy++) { var k = gx + "|" + gy; (ROAD_INDEX[k] || (ROAD_INDEX[k] = [])).push(id); }
    });
    COUNTS.road_surfaces = ROAD_LIST.length;
  }
  function paintRoads(ctx, w, s, e, n, size) {
    if (!ROAD_LIST || !REAL) return false;
    var sx = size / (e - w), sy = size / (n - s), mpp = (e - w) * 111320 * Math.cos((s + n) / 2 * Math.PI / 180) / size;   // metres to a pixel
    if (mpp > 60) return false;
    var pad = 0.0004, seen = {}, by = {}, any = false;
    for (var gx = Math.floor((w - pad) * 50); gx <= Math.floor((e + pad) * 50); gx++) for (var gy = Math.floor((s - pad) * 50); gy <= Math.floor((n + pad) * 50); gy++) {
      var ids = ROAD_INDEX[gx + "|" + gy]; if (!ids) continue;
      for (var j = 0; j < ids.length; j++) { var id = ids[j]; if (seen[id]) continue; seen[id] = 1; var rd = ROAD_LIST[id];
        if (rd.box[2] < w - pad || rd.box[0] > e + pad || rd.box[3] < s - pad || rd.box[1] > n + pad) continue;
        if (mpp > 20 && !rd.main) continue;   // from far off only the main network is worth a line
        var key = rd.w + (rd.main ? "m" : "l"); (by[key] || (by[key] = [])).push(rd); any = true; } }
    if (!any) return false;
    var stroke = function (list, px, css, dash) { ctx.lineWidth = px; ctx.strokeStyle = css; ctx.lineJoin = "round"; ctx.lineCap = dash ? "butt" : "round"; ctx.setLineDash(dash || []); ctx.beginPath();
      for (var a = 0; a < list.length; a++) { var q = list[a].p; ctx.moveTo((q[0] - w) * sx, (n - q[1]) * sy); for (var b = 2; b < q.length; b += 2) ctx.lineTo((q[b] - w) * sx, (n - q[b + 1]) * sy); } ctx.stroke(); };
    var keys = Object.keys(by);
    if (mpp < 4) keys.forEach(function (k) { stroke(by[k], (by[k][0].w + 2.4) / mpp, "rgba(156,151,139,0.85)"); });   // the kerb strip, 1.2 m either side
    keys.forEach(function (k) { stroke(by[k], Math.max(0.8, by[k][0].w / mpp), by[k][0].main ? "rgba(58,61,65,0.92)" : "rgba(75,78,82,0.90)"); });
    if (mpp < 1.5) keys.forEach(function (k) { var l = by[k].filter(function (r) { return r.c <= 4; }); if (l.length) stroke(l, Math.max(1, 0.35 / mpp), "rgba(242,239,230,0.85)", [3 / mpp, 6 / mpp]); });
    ctx.setLineDash([]);
    return true;
  }
  function paintFootprints(ctx, w, s, e, n, size) {
    if (!HOUSES || !HOUSE_GRID || !FOOT_COLOUR) return false;
    var sx = size / (e - w), sy = size / (n - s), mpp = (e - w) * 111320 * Math.cos((s + n) / 2 * Math.PI / 180) / size;
    if (mpp > 90) return false;   // a tile wider than 20 km: a building is a fraction of a pixel
    var pad = 0.0015, groups = {}, any = false;   // a building reaches about 150 m from its first vertex at most
    // from about 4 km of height a building is a few pixels on its tile: it is painted lighter, opaque and with the shadow a
    // solid would cast, so a town reads as built ground where no solid stands (29 Sep 2026: from 12 km it read as empty)
    var far = mpp > 4;
    for (var gx = Math.floor((w - pad) * 100); gx <= Math.floor((e + pad) * 100); gx++) for (var gy = Math.floor((s - pad) * 100); gy <= Math.floor((n + pad) * 100); gy++) {
      var cell = HOUSE_GRID[gx + "|" + gy]; if (!cell) continue;
      for (var j = 0; j < cell.length; j++) { var i = cell[j], r = HOUSES.houses[i], ring = r[2];
        if (ring[0] < w - pad || ring[0] > e + pad || ring[1] < s - pad || ring[1] > n + pad || !houseVisible(r)) continue;
        var css = FOOT_COLOUR(i, r, far); (groups[css] || (groups[css] = [])).push(ring); any = true; } }
    if (!any) return false;
    // the extent of a ring on the tile, in pixels
    var ext = function (g) { var x0 = 1e9, x1 = -1e9, y0 = 1e9, y1 = -1e9; for (var q = 0; q < g.length; q += 2) { var px = (g[q] - w) * sx, py = (n - g[q + 1]) * sy; if (px < x0) x0 = px; if (px > x1) x1 = px; if (py < y0) y0 = py; if (py > y1) y1 = py; } return [x0, y0, x1 - x0, y1 - y0]; };
    if (far) {   // the shadow first, under every roof: north east of the building, the way the map's sun casts it, 6 m for a two storey house
      var off = Math.max(0.8, 6 / mpp); ctx.fillStyle = "rgba(22,26,30,0.40)";
      for (var cssS in groups) { var rs = groups[cssS]; for (var ks = 0; ks < rs.length; ks++) { var b0 = ext(rs[ks]); ctx.fillRect(b0[0] + off, b0[1] - off, Math.max(1.6, b0[2]), Math.max(1.6, b0[3])); } }
    }
    for (var css2 in groups) { var rings = groups[css2]; ctx.fillStyle = css2;
      if (mpp > 5) {   // under three pixels across: a block of its own extent, never less than a pixel and a half, so the far field keeps its buildings
        for (var k = 0; k < rings.length; k++) { var b1 = ext(rings[k]); ctx.fillRect(b1[0], b1[1], Math.max(1.6, b1[2]), Math.max(1.6, b1[3])); }
      } else { ctx.beginPath(); for (var k2 = 0; k2 < rings.length; k2++) { var g2 = rings[k2]; ctx.moveTo((g2[0] - w) * sx, (n - g2[1]) * sy); for (var q2 = 2; q2 < g2.length; q2 += 2) ctx.lineTo((g2[q2] - w) * sx, (n - g2[q2 + 1]) * sy); ctx.closePath(); } ctx.fill(); } }
    return true;
  }
  // an imagery provider that paints its tiles: the shape Cesium asks of any provider, nothing fetched
  function PaintedTiles(paint) { this._paint = paint; this._tiling = new C.GeographicTilingScheme(); this._rect = C.Rectangle.fromDegrees(55.30, 24.80, 56.60, 26.30); this._err = new C.Event(); }
  function definePainted() {
    if (PaintedTiles.defined) return; PaintedTiles.defined = true;
    var ro = function (v) { return { get: v }; };
    Object.defineProperties(PaintedTiles.prototype, { tileWidth: ro(function () { return 256; }), tileHeight: ro(function () { return 256; }), maximumLevel: ro(function () { return 20; }), minimumLevel: ro(function () { return 0; }),
      tilingScheme: ro(function () { return this._tiling; }), rectangle: ro(function () { return this._rect; }), tileDiscardPolicy: ro(function () { return undefined; }), errorEvent: ro(function () { return this._err; }),
      credit: ro(function () { return undefined; }), proxy: ro(function () { return undefined; }), hasAlphaChannel: ro(function () { return true; }) });
    PaintedTiles.prototype.getTileCredits = function () { return undefined; };
    PaintedTiles.prototype.pickFeatures = function () { return undefined; };
    PaintedTiles.prototype.requestImage = function (x, y, level) {
      if (!TILE_BLANK) { TILE_BLANK = document.createElement("canvas"); TILE_BLANK.width = TILE_BLANK.height = 2; }
      if (level < 9) return Promise.resolve(TILE_BLANK);
      var r = this._tiling.tileXYToRectangle(x, y, level), cv = document.createElement("canvas"), painted = false; cv.width = cv.height = 256;
      try { painted = this._paint(cv.getContext("2d"), C.Math.toDegrees(r.west), C.Math.toDegrees(r.south), C.Math.toDegrees(r.east), C.Math.toDegrees(r.north), 256); } catch (e) {}
      return Promise.resolve(painted ? cv : TILE_BLANK);
    };
  }
  function paintedLayer(paint) { definePainted(); return viewer.imageryLayers.addImageryProvider(new PaintedTiles(paint)); }
  // the roads are painted once the twin's data is in, and again when the look changes
  function roadPicture() {
    if (ROAD_LAYER) { viewer.imageryLayers.remove(ROAD_LAYER, true); ROAD_LAYER = null; }
    if (!viewer || !REAL) return;
    buildRoadIndex(); if (!ROAD_LIST) return;
    ROAD_LAYER = paintedLayer(paintRoads);
    if (HOUSE_FLAT) viewer.imageryLayers.raiseToTop(HOUSE_FLAT);   // buildings over roads
    applyLod(); viewer.scene.requestRender();
  }
  // the platform's own road lines (wm001-twin's regional and neighbourhood sets) step back while the painted roads show
  function platformRoads(show) {
    var R = window.__wmaTwinRegional && window.__wmaTwinRegional.state; if (!R) return;
    if (!show) { (R.localPrims || []).forEach(function (q) { if (q.prim && q.prim.show) { q.prim.show = false; PLAT_HIDDEN.push(q.prim); } }); [R.ds, R.localDs].forEach(function (d) { if (d && d.show) { d.show = false; PLAT_HIDDEN.push(d); } }); }
    else if (PLAT_HIDDEN.length) { PLAT_HIDDEN.forEach(function (x) { try { x.show = true; } catch (e) {} }); PLAT_HIDDEN = []; }
  }
  function segLength(coords) { var m = 0; for (var i = 1; i < coords.length; i++) { m += Math.hypot((coords[i][0] - coords[i - 1][0]) * 111320 * Math.cos(coords[i][1] * Math.PI / 180), (coords[i][1] - coords[i - 1][1]) * 110540); } return m; }
  function midpoint(coords) { var total = segLength(coords), acc = 0; for (var i = 1; i < coords.length; i++) { var d = Math.hypot((coords[i][0] - coords[i - 1][0]) * 111320 * Math.cos(coords[i][1] * Math.PI / 180), (coords[i][1] - coords[i - 1][1]) * 110540); if (acc + d >= total / 2) { var t = d ? (total / 2 - acc) / d : 0; return [coords[i - 1][0] + (coords[i][0] - coords[i - 1][0]) * t, coords[i - 1][1] + (coords[i][1] - coords[i - 1][1]) * t, Math.atan2((coords[i][1] - coords[i - 1][1]) * 110540, (coords[i][0] - coords[i - 1][0]) * 111320 * Math.cos(coords[i][1] * Math.PI / 180))]; } acc += d; } var c = coords[Math.floor(coords.length / 2)]; return [c[0], c[1], 0]; }
  function twinRoads(show) { var T = window.__wmaTwin; if (!T || !T.layers) return; Object.keys(T.layers).forEach(function (k) { if (/road/i.test(k)) (T.layers[k].items || []).forEach(function (e) { try { e.show = show; } catch (x) {} }); }); }
  async function roadsLabelled(style) {
    if (!viewer) return null; var k = "roads_labelled";
    if (!ROADS_CACHE) { try { ROADS_CACHE = await (await fetch(SWMP + "/v1/layers/roads", { headers: { Accept: "application/json" } })).json(); } catch (e) { COUNTS[k] = "?"; renderLayers(); return null; } }
    if (DS[k]) { viewer.dataSources.remove(DS[k], true); DS[k] = null; }
    if (ROAD_PRIMS.length) { ROAD_PRIMS.forEach(function (p) { viewer.scene.groundPrimitives.remove(p); }); ROAD_PRIMS = []; }
    var ds = new C.CustomDataSource("roads-labelled"); var print = style === "print"; var casing = C.Color.fromCssColorString(print ? "#FFFFFF" : "#141A1F").withAlpha(print ? 0.9 : 0.95);
    var placed = {}; var labels = 0; var caseInst = [], fillInst = [];
    ROADS_CACHE.features.forEach(function (f, i) {
      var p = f.properties || {}; var hw = p.highway || "tertiary"; var st = (print ? ROAD_PRINT : ROAD_STYLE)[hw] || (print ? ["#7A7A7A", 1.6] : ["#E9E9E9", 2.6]); var coords = f.geometry.coordinates; if (!coords || coords.length < 2) return;
      var flat = []; coords.forEach(function (c) { flat.push(c[0], c[1]); }); var pos = C.Cartesian3.fromDegreesArray(flat);
      caseInst.push(new C.GeometryInstance({ geometry: new C.GroundPolylineGeometry({ positions: pos, width: st[1] + (print ? 2.4 : 4.4) }), attributes: { color: C.ColorGeometryInstanceAttribute.fromColor(casing) }, id: "road-case|" + i }));
      fillInst.push(new C.GeometryInstance({ geometry: new C.GroundPolylineGeometry({ positions: pos, width: st[1] }), attributes: { color: C.ColorGeometryInstanceAttribute.fromColor(C.Color.fromCssColorString(st[0])) }, id: "road|" + i }));
      var name = p.name_en || p.ref; var major = /^(motorway|trunk|primary|secondary)$/.test(hw);
      if (name && major && segLength(coords) > 350) {
        var m = midpoint(coords); var key = name; var near = (placed[key] || []).some(function (q) { return Math.hypot((q[0] - m[0]) * 111320 * Math.cos(m[1] * Math.PI / 180), (q[1] - m[1]) * 110540) < 1500; });
        if (!near) { (placed[key] = placed[key] || []).push(m); labels++;
          ds.entities.add({ position: C.Cartesian3.fromDegrees(m[0], m[1]), label: { text: String(name), font: (hw === "motorway" || hw === "trunk" ? "bold 14px" : "bold 12.5px") + " Calibri, sans-serif", fillColor: C.Color.fromCssColorString(print ? "#111111" : "#FFFFFF"), outlineColor: C.Color.fromCssColorString(print ? "#FFFFFF" : "#0C1319"), outlineWidth: 5, style: C.LabelStyle.FILL_AND_OUTLINE, heightReference: C.HeightReference.NONE, disableDepthTestDistance: Number.POSITIVE_INFINITY, distanceDisplayCondition: new C.DistanceDisplayCondition(0, hw === "motorway" || hw === "trunk" ? 60000 : 22000), scaleByDistance: new C.NearFarScalar(2000, 1.05, 40000, 0.75), pixelOffset: new C.Cartesian2(0, -10) } }); }
      }
    });
    // casing first, then the fill on top: two batched draws for the whole network, no picking
    [caseInst, fillInst].forEach(function (inst) { if (inst.length) ROAD_PRIMS.push(viewer.scene.groundPrimitives.add(new C.GroundPolylinePrimitive({ geometryInstances: inst, appearance: new C.PolylineColorAppearance(), asynchronous: true, allowPicking: false }))); });
    DS[k] = ds; await viewer.dataSources.add(ds); COUNTS[k] = ROADS_CACHE.count + " roads, " + labels + " names"; viewer.scene.requestRender(); return ds;
  }
  var HOUSES = null; var HOUSE_PRIMS = []; var HOUSE_COLOUR = null; var HOUSES_VERSION = "20260926g"; // bump when omcc-source/houses_all.json is rebuilt
  function dropFlat() { if (HOUSE_FLAT) { viewer.imageryLayers.remove(HOUSE_FLAT, true); HOUSE_FLAT = null; HOUSE_FLAT_N = 0; } }
  async function rebuildHouses(colourFor) { HOUSE_COLOUR = colourFor || null; FLAT_GEN++; dropFlat(); var l = LAYERS.find(function (x) { return x.kind === "houses"; }); if (l) await loadHouses(l); }
  var HOUSES_BUSY = false, HOUSES_AGAIN = false, FLAT_GEN = 0, HOUSE_FLAT_GEN = 0;   // the footprints carry the generation of the rebuild request they answer
  async function loadHouses(layer) {
    if (!viewer) return;
    // never two builds at once: a reload that arrives mid build (the platform probe finishing, a preference change) runs after this one
    if (HOUSES_BUSY) { HOUSES_AGAIN = true; return; }
    HOUSES_BUSY = true;
    try { await buildHouses(layer); } finally { HOUSES_BUSY = false; }
    // a queued run answers the latest request: footprints built under an older one are dropped so they are drawn again
    if (HOUSES_AGAIN) { HOUSES_AGAIN = false; if (HOUSE_FLAT && HOUSE_FLAT_GEN !== FLAT_GEN) dropFlat(); await loadHouses(layer); }
  }
  // a 0.01 degree grid over the rows, about 1 km, built once: it turns "what is in view" into a few cell lookups
  var HOUSE_GRID = null, HOUSE_BUILT = null, HOUSE_CAP = (+localStorage.getItem("twinmap-house-cap") || 40000);   // 20,000 until 27 Sep 2026: the ground draped layers, not the solids, were what the GPU could not carry
  var HOUSE_FLAT_CAP = (+localStorage.getItem("twinmap-house-flat-cap") || 70000);  // a footprint costs a fraction of a solid
  var HOUSE_FLAT = null, HOUSE_FLAT_N = 0;
  function gridKey(lon, lat) { return Math.floor(lon * 100) + "|" + Math.floor(lat * 100); }
  // the ground a building stands on, metres above the ellipsoid, sampled offline from the flood model's 50 m grid and
  // carried at index 7 of its row. The city is within a few metres of the ellipsoid; Digdaga is 30 m above it, and
  // buildings extruded from 0 there were buried. A lookup in the browser costs 10 ms a building, so it is never done here.
  function groundOf(r) { return typeof r[7] === "number" ? r[7] : 0; }
  function buildGrid() {
    if (HOUSE_GRID) return;
    HOUSE_GRID = {};
    for (var i = 0; i < HOUSES.houses.length; i++) {
      var r = HOUSES.houses[i][2], k = gridKey(r[0], r[1]);
      (HOUSE_GRID[k] || (HOUSE_GRID[k] = [])).push(i);
    }
  }
  // how many buildings the view holds, with its margin: the solid draw is worth its cost only when they all fit under
  // the cap, and that is a question of count, not of height. Over the city that is about 2 to 3 km; over the farms it
  // is well over 10 km. Summing grid cells costs nothing, so it can run on every camera move.
  // the point on the ground the camera is looking at: the screen centre, or the ground under the camera when the centre is sky
  function focusLonLat() {
    var cv = viewer.canvas, c = viewer.camera.positionCartographic, out = [C.Math.toDegrees(c.longitude), C.Math.toDegrees(c.latitude)];
    try {
      var ray = viewer.camera.getPickRay(new C.Cartesian2(cv.clientWidth / 2, cv.clientHeight / 2));
      var hit = ray && viewer.scene.globe.pick(ray, viewer.scene);
      if (hit) { var g = C.Cartographic.fromCartesian(hit); out = [C.Math.toDegrees(g.longitude), C.Math.toDegrees(g.latitude)]; }
    } catch (e) {}
    return out;
  }
  // the box the solids are chosen from: the view with its margin, clipped to a square of 2.5 times the camera height
  // each way around the focus. A tilted view reaches the horizon, and buildings that far off are specks: the
  // footprints under everything carry them, and the solids go where they can be seen as solids.
  function gridCount(bb) {
    var n = 0;
    for (var gx = Math.floor(bb[0] * 100); gx <= Math.floor(bb[2] * 100); gx++)
      for (var gy = Math.floor(bb[1] * 100); gy <= Math.floor(bb[3] * 100); gy++) { var c = HOUSE_GRID[gx + "|" + gy]; if (c) n += c.length; }
    return n;
  }
  // the square shrinks, 4 times the height each way down to 0.8, until what it holds fits under the cap: the solids then
  // cover the nearest ground that can be drawn solid, and the footprints under everything carry the rest
  function nearBox(pad) {
    var bb = viewBbox(); if (!bb || !HOUSES) return null;
    buildGrid();   // the index is built on first use, and this can be the first use
    var mx = Math.max((bb[2] - bb[0]) * pad, 0.004), my = Math.max((bb[3] - bb[1]) * pad, 0.004);
    var f = focusLonLat(), h = Math.max(300, cameraHeight()), cosLat = Math.max(0.2, Math.cos(f[1] * Math.PI / 180));
    var factors = [4.0, 3.0, 2.5, 1.8, 1.2, 0.8], best = null;
    for (var k = 0; k < factors.length; k++) {
      var rLat = h * factors[k] / 111000, rLon = rLat / cosLat;
      var w = Math.max(bb[0] - mx, f[0] - rLon), e = Math.min(bb[2] + mx, f[0] + rLon), so = Math.max(bb[1] - my, f[1] - rLat), n = Math.min(bb[3] + my, f[1] + rLat);
      if (!(w < e && so < n)) { w = f[0] - rLon; e = f[0] + rLon; so = f[1] - rLat; n = f[1] + rLat; }
      var box = [w, so, e, n], count = gridCount(box);
      best = { box: box, focus: f, count: count, fits: count <= HOUSE_CAP, factor: factors[k] };
      if (best.fits) break;
    }
    return best;
  }
  function countInView(pad) { var nb = nearBox(pad); return nb ? nb.count : 0; }
  // solids are wanted wherever there is a building near and the camera is below solidFar. Until 29 Sep 2026 they were wanted
  // only when every building of the smallest box fitted under the cap, so over the city from 9 km up none was drawn at all;
  // now the cap takes the nearest and the painted roofs carry the rest
  function solidWanted() { if (cameraHeight() >= LOD.solidFar) return false; var nb = nearBox(0.15); return !!(nb && nb.count > 0); }
  // the rows inside the view with a margin, nearest the centre first so the cap drops the far ones
  function rowsInView(cap, pad) {
    cap = cap || HOUSE_CAP;
    pad = pad == null ? 0.35 : pad;   // margin beyond the view, so a small pan does not force a rebuild
    buildGrid();
    var nb = nearBox(pad);
    if (!nb) return { rows: [], bbox: null };
    var bb = nb.box, w = bb[0], s = bb[1], e = bb[2], n = bb[3];
    var cx = nb.focus[0], cy = nb.focus[1], out = [], lost = 0;
    for (var gx = Math.floor(w * 100); gx <= Math.floor(e * 100); gx++) {
      for (var gy = Math.floor(s * 100); gy <= Math.floor(n * 100); gy++) {
        var cell = HOUSE_GRID[gx + "|" + gy];
        if (cell) for (var j = 0; j < cell.length; j++) out.push(cell[j]);
      }
    }
    var found = out.length, reach = 0;
    if (out.length > cap) {
      out.sort(function (a, b) {
        var ra = HOUSES.houses[a][2], rb = HOUSES.houses[b][2];
        return ((ra[0] - cx) * (ra[0] - cx) + (ra[1] - cy) * (ra[1] - cy)) - ((rb[0] - cx) * (rb[0] - cx) + (rb[1] - cy) * (rb[1] - cy));
      });
      var far = HOUSES.houses[out[cap - 1]][2]; reach = Math.hypot(far[0] - cx, far[1] - cy);   // degrees from the focus to the last solid kept
      // the drop is from the padded edge inward, so count what it cost inside the view itself rather than assume it cost nothing
      for (var d = cap; d < out.length; d++) {
        var rr = HOUSES.houses[out[d]][2];
        if (rr[0] >= bb[0] && rr[0] <= bb[2] && rr[1] >= bb[1] && rr[1] <= bb[3]) lost++;
      }
      out = out.slice(0, cap);
    }
    return { rows: out, bbox: [w, s, e, n], found: found, capped: found > cap, lostInView: lost, focus: nb.focus, reach: reach };
  }
  // rebuild when the camera has moved off what was built, or the zoom has changed enough to matter
  var HOUSE_PENDING = null, HOUSE_CAM = null;
  // the camera as a few numbers, to tell a real move from the moveEnd the scene raises about once a second while the
  // camera stands still (its own per frame bookkeeping trips the comparison): those must never cost a rebuild. At a
  // close zoom they once did, every second, so the solids were replaced before they could render (CR-054).
  function cameraStill() {
    var c = viewer.camera, k = [c.positionWC.x, c.positionWC.y, c.positionWC.z, c.direction.x * 1000, c.direction.y * 1000, c.direction.z * 1000];
    var still = !!HOUSE_CAM && k.every(function (v, i) { return Math.abs(v - HOUSE_CAM[i]) < 0.5; });
    HOUSE_CAM = k; return still;
  }
  function housesFollowCamera(force) {
    var l = LAYERS.find(function (x) { return x.kind === "houses"; });
    if (!l || !l.on || !HOUSES || !HOUSE_BUILT) return;
    if (!force && cameraStill()) return;
    if (!force && HOUSE_BUILT && !HOUSE_BUILT.solid && HOUSE_FLAT && !solidWanted()) return;   // footprints hold everything and no solids fit here: nothing to follow
    var bb = viewBbox();
    if (!bb) return;
    if (force) { if (HOUSE_PENDING) clearTimeout(HOUSE_PENDING); HOUSE_PENDING = setTimeout(function () { HOUSE_PENDING = null; loadHouses(l); }, 200); return; }
    var b = HOUSE_BUILT.bbox;
    var inside = bb[0] >= b[0] && bb[1] >= b[1] && bb[2] <= b[2] && bb[3] <= b[3];
    var span = (bb[2] - bb[0]), builtSpan = (b[2] - b[0]);
    // a capped build holds the nearest buildings around the point it was built for: once the view looks a third of that
    // reach away, the solids are wanted around the new point
    if (inside && HOUSE_BUILT.capped && HOUSE_BUILT.focus && HOUSE_BUILT.reach) { var fNow = focusLonLat(); if (Math.hypot(fNow[0] - HOUSE_BUILT.focus[0], fNow[1] - HOUSE_BUILT.focus[1]) > HOUSE_BUILT.reach * 0.35) inside = false; }
    // still covered: nothing to follow. Zooming in never needs more solids than a box that was not capped already holds;
    // only a capped build is worth redoing for a closer view, where the cap can then drop the far ones instead of the near
    if (inside && (!HOUSE_BUILT.capped || span > builtSpan * 0.4)) return;
    if (HOUSE_PENDING) clearTimeout(HOUSE_PENDING);
    HOUSE_PENDING = setTimeout(function () { HOUSE_PENDING = null; loadHouses(l); }, 450);
  }

  // ---------------------------------------------------------------- the real look: sand walls, recessed windows, flat roofs
  // Saif's reference is a Gulf villa render: off white walls (sand at first, changed the same day), dark recessed windows in rows, flat roofs with a
  // parapet and a unit or two on top, true heights, warm light. Each building keeps its footprint and its one instance; the
  // look is drawn by the fragment shader in the building's own frame (metres from its base centre, passed per instance in
  // the high and low halves Cesium uses for precision), so nothing is loaded and no pattern swims with the camera. The map
  // colour moves to the roof at its tint, so H1 to H4, the categories and an impact colouring still read from above and
  // in the card. The diagram look stays as a switch in the Filters panel.
  var REAL = PREF("twinmap-style", "real") !== "map", WALLS = null;
  function wallColour(i, kind, homesOnly) {
    // homes in off whites, warm and cool, the way the new villas are finished (Saif, 26 Sep 2026: off white, "so it would look modern")
    if (!WALLS) WALLS = { sand: ["#F4F2ED", "#EEECE6", "#F7F5F0", "#E9E7E1", "#F2EFE8", "#ECEAE3", "#F6F4EE", "#E6E4DE"].map(function (h) { return C.Color.fromCssColorString(h); }),
      civic: ["#EFEBE3", "#E8E5DD", "#F3F0E9"].map(function (h) { return C.Color.fromCssColorString(h); }),
      grey: C.Color.fromCssColorString("#B8B3A8"), concrete: C.Color.fromCssColorString("#DCD5C6") };
    if (kind === 1 || (homesOnly && kind !== 0)) return WALLS.grey;   // a shed or a store under 40 m2; everything but a home when homes only
    if (kind === 2) return WALLS.concrete.brighten((jitter(i * 7 + 3) - 0.5) * 0.12, new C.Color());   // blocks and halls: pale concrete
    if (kind >= 3) return WALLS.civic[Math.floor(jitter(i * 3 + 11) * WALLS.civic.length)];   // schools, colleges, government, hospitals, mosques, commerce, industry: off white, the category colour on the roof and the bands
    return WALLS.sand[Math.floor(jitter(i * 3 + 11) * WALLS.sand.length)].brighten((jitter(i * 13 + 5) - 0.5) * 0.10, new C.Color());
  }
  function realAttributes(attrs, lon, lat, groundM, h, kind, i, roofCol, tint) {
    var e = C.EncodedCartesian3.fromCartesian(C.Cartesian3.fromDegrees(lon, lat, groundM)), F = C.ComponentDatatype.FLOAT;
    attrs.cHigh = new C.GeometryInstanceAttribute({ componentDatatype: F, componentsPerAttribute: 3, value: [e.high.x, e.high.y, e.high.z] });
    attrs.cLow = new C.GeometryInstanceAttribute({ componentDatatype: F, componentsPerAttribute: 3, value: [e.low.x, e.low.y, e.low.z] });
    attrs.roofc = new C.GeometryInstanceAttribute({ componentDatatype: F, componentsPerAttribute: 3, value: [roofCol.red, roofCol.green, roofCol.blue] });
    attrs.info = new C.GeometryInstanceAttribute({ componentDatatype: F, componentsPerAttribute: 4, value: [h, kind, Math.floor(jitter(i * 5 + 2) * 97), tint] });
  }
  // walls: windows in rows from 1 m up, one every 3.2 m along the wall, 1.2 m wide and 1.7 m tall, never on a shed; floor
  // slabs on the taller blocks; a darker plinth at the foot; a lighter parapet band at the top. Roofs: flat, lighter than
  // the walls, the map colour at its tint, a light unit on one cell in twelve; a shed gets a grey sheet.
  var REAL_VS = [
    "in vec3 position3DHigh;",
    "in vec3 position3DLow;",
    "in vec3 normal;",
    "in vec4 color;",
    "in float batchId;",
    "out vec3 v_positionEC;",
    "out vec3 v_normalEC;",
    "out vec4 v_color;",
    "out vec3 v_roofc;",
    "out vec3 v_local;",
    "out vec3 v_up;",
    "out vec3 v_normalWC;",
    "out vec4 v_info;",
    "void main()",
    "{",
    "    vec4 p = czm_computePosition();",
    "    vec3 cHigh = czm_batchTable_cHigh(batchId);",
    "    vec3 cLow = czm_batchTable_cLow(batchId);",
    "    v_local = (position3DHigh - cHigh) + (position3DLow - cLow);",
    "    v_up = normalize(cHigh + cLow);",
    "    v_normalWC = normal;",
    "    v_roofc = czm_batchTable_roofc(batchId);",
    "    v_info = czm_batchTable_info(batchId);",
    "    v_positionEC = (czm_modelViewRelativeToEye * p).xyz;",
    "    v_normalEC = czm_normal * normal;",
    "    v_color = color;",
    "    gl_Position = czm_modelViewProjectionRelativeToEye * p;",
    "}"].join("\n");
  var REAL_FS = [
    "in vec3 v_positionEC;",
    "in vec3 v_normalEC;",
    "in vec4 v_color;",
    "in vec3 v_roofc;",
    "in vec3 v_local;",
    "in vec3 v_up;",
    "in vec3 v_normalWC;",
    "in vec4 v_info;",
    "float wmHash(vec2 p) { vec3 q = fract(vec3(p.xyx) * 0.1031); q += dot(q, q.yzx + 33.33); return fract((q.x + q.y) * q.z); }",
    "void main()",
    "{",
    "    vec3 positionToEyeEC = -v_positionEC;",
    "    vec3 normalEC = normalize(v_normalEC);",
    "    vec3 up = normalize(v_up);",
    "    vec3 n = normalize(v_normalWC);",
    "    float h = v_info.x, kind = v_info.y, seed = v_info.z, tint = v_info.w;",
    "    float shed = step(0.5, kind) * (1.0 - step(1.5, kind));",
    "    float z = dot(v_local, up);",
    "    vec3 wall = czm_gammaCorrect(v_color).rgb;",
    "    vec3 col = wall;",
    "    float spec = 0.0;",
    "    if (dot(n, up) < 0.5) {",
    "        vec3 t = cross(up, n); float tl = max(length(t), 1e-4); t /= tl;",
    "        float x = dot(v_local, t) + seed * 0.37;",
    "        float fz = fract((z - 1.0) / 3.1) * 3.1;",
    "        float fx = fract(x / 3.2) * 3.2;",
    "        float storey = step(1.0, z) * step(z, h - 0.8);",
    "        float civic = step(0.8, tint);",
    "        float home = step(kind, 0.25) * (1.0 - civic);",
    "        float kf = floor((z - 1.0) / 3.4); float z0 = 1.0 + kf * 3.4;",
    "        float fit = step(z0 + 1.9, h - 0.9) * step(0.0, kf);",
    "        float fxh = fract((x + 1.3) / 4.2) * 4.2;",
    "        float winH = step(1.45, fxh) * step(fxh, 2.75) * step(z0, z) * step(z, z0 + 1.9) * fit;",
    "        float glsH = step(1.57, fxh) * step(fxh, 2.63) * step(z0 + 0.12, z) * step(z, z0 + 1.78) * fit;",
    "        float door = step(fract((x + seed * 0.11) / 13.0), 0.1) * step(0.0, z) * step(z, 2.3);",
    "        float lo = 1.0 - 0.4 * civic, hi = 2.2 + 0.4 * civic;",
    "        float winB = step(lo, fx) * step(fx, hi) * step(fz, 1.7) * storey * (1.0 - shed);",
    "        float glsB = step(lo + 0.1, fx) * step(fx, hi - 0.1) * step(0.1, fz) * step(fz, 1.6) * storey * (1.0 - shed);",
    "        float pw = max(fwidth(x), fwidth(z));",
    "        float seen = clamp(1.8 - pw / 0.7, 0.0, 1.0);",
    "        float win = mix(winB, max(winH, door), home) * seen;",
    "        float gls = mix(glsB, max(glsH, door), home) * seen;",
    "        float slab = step(8.0, h) * step(fract(z / 3.1) * 3.1, 0.22) * step(2.0, z) * (1.0 - home);",
    "        float fine = (wmHash(floor(vec2(x, z) * 3.0) + seed) - 0.5) * 0.035 + (wmHash(floor(vec2(x, z) * 0.5) + seed * 3.0) - 0.5) * 0.05;",
    "        vec3 stucco = clamp(wall + fine * clamp(1.0 - pw / 0.3, 0.0, 1.0), 0.0, 1.0) * (1.0 - 0.10 * (1.0 - smoothstep(0.0, 1.4, z)));",
    "        float facing = 1.0 - abs(dot(normalEC, vec3(0.0, 0.0, 1.0)));",
    "        vec3 glass = mix(vec3(0.14, 0.18, 0.23), vec3(0.50, 0.60, 0.70), 0.35 * facing);",
    "        vec3 frame = mix(vec3(0.93, 0.93, 0.91), wall * 0.55, civic);",
    "        col = mix(stucco, frame, win);",
    "        col = mix(col, mix(glass, vec3(0.30, 0.22, 0.16), door * home), gls);",
    "        col = mix(col, min(wall * 1.08 + 0.03, 1.0), slab);",
    "        vec3 band = czm_gammaCorrect(vec4(v_roofc, 1.0)).rgb;",
    "        col = mix(col, mix(wall * 0.74, band * 0.8, civic), step(z, 0.35));",
    "        col = mix(col, mix(min(wall * 1.06 + 0.02, 1.0), band, civic), step(h - 0.45, z));",
    "        spec = gls * 0.6;",
    "    } else {",
    "        vec3 east = normalize(cross(vec3(0.0, 0.0, 1.0), up)); vec3 north = cross(up, east);",
    "        vec2 rp = vec2(dot(v_local, east), dot(v_local, north));",
    "        vec2 cell = floor(rp / 1.7); vec2 ic = fract(rp / 1.7);",
    "        float unit = step(0.95, wmHash(cell + seed)) * step(0.2, ic.x) * step(ic.x, 0.8) * step(0.2, ic.y) * step(ic.y, 0.8) * clamp(1.6 - max(fwidth(rp.x), fwidth(rp.y)) / 0.9, 0.0, 1.0);",
    "        float pr = max(fwidth(rp.x), fwidth(rp.y));",
    "        float grain = (wmHash(floor(rp * 2.0) + seed) - 0.5) * 0.05 * clamp(1.0 - pr / 0.3, 0.0, 1.0);",
    "        vec3 slabc = clamp(wall * 0.86 + grain, 0.0, 1.0);",
    "        col = mix(slabc, czm_gammaCorrect(vec4(v_roofc, 1.0)).rgb, tint);",
    "        col = mix(col, vec3(0.80, 0.80, 0.78), unit);",
    "        col = mix(col, vec3(0.55, 0.57, 0.59) + grain + step(0.5, fract(rp.x / 0.6)) * 0.05 * clamp(1.0 - pr / 0.3, 0.0, 1.0), shed);",
    "    }",
    "    vec3 upEC = normalize(czm_normal * up);",
    "    vec3 toEye = normalize(positionToEyeEC);",
    "    float sun = max(dot(normalEC, czm_lightDirectionEC), 0.0);",
    "    float sky = 0.5 + 0.5 * dot(normalEC, upEC);",
    "    vec3 lit = col * czm_lightColor * (0.42 + 0.50 * sun + 0.14 * sky);",
    "    vec3 refl = reflect(-czm_lightDirectionEC, normalEC);",
    "    lit += czm_lightColor * spec * pow(max(dot(refl, toEye), 0.0), 40.0) * 0.7;",
    "    out_FragColor = vec4(lit, 1.0);",
    "}"].join("\n");
  function solidAppearance() {
    return REAL ? new C.PerInstanceColorAppearance({ translucent: false, closed: true, vertexShaderSource: REAL_VS, fragmentShaderSource: REAL_FS })
                : new C.PerInstanceColorAppearance({ translucent: false, closed: true });
  }
  // the houses file, loaded once for the layer and for route design; the claude.ai build replaces this one line
  async function fetchHouses() {
    if (!HOUSES) HOUSES = await (async function () {   // artifact copy: the file is split to fit what an artifact file may hold
        var parts = await Promise.all([0, 1, 2].map(function (i) {
          return fetch("omcc-source/houses_all." + i + ".json?v=" + HOUSES_VERSION).then(function (r) { return r.json(); });
        }));
        var all = parts[0]; all.houses = parts[0].houses.concat(parts[1].houses, parts[2].houses); return all;
      })();
    return HOUSES;
  }
  async function buildHouses(layer) {
    if (!HOUSES) { try { await fetchHouses(); } catch (e) { COUNTS.houses = "?"; renderLayers(); return; } }
    // the solids standing now stay until the new ones are ready: removed first, every pan left the view on its
    // footprints for the seconds a build takes, which read as buildings missing
    var OLD_PRIMS = HOUSE_PRIMS, FRESH = [];
    var flatGen = FLAT_GEN;   // the request this build answers, stamped on the footprints it makes
    var dens = {}; HOUSES.areas.forEach(function (a) { dens[a.i] = a.density; });
    var red = C.Color.fromCssColorString("#D9342B"), grey = C.Color.fromCssColorString("#C9D1D9"), flatH = HOUSES.meta.heightM || 6;
    // heights: the row carries one (source height, floors, or the builder's estimate from footprint and class); an old file without one
    // falls back to the area bands; or the flat planning height
    // emphasised (the default) draws 1.5 times the height so the massing reads at planning distances; the Selection tab always shows the true figure
    var hPref = PREF("twinmap-heights", REAL ? "estimated" : "emphasised"), hK = hPref === "emphasised" ? 1.5 : 1;
    // a real street is not one height: vary each building within a tenth of its own, keyed on its row so it never moves
    var vary = PREF("twinmap-detail", "max") !== "plain";
    var hBase = hPref === "flat" ? function () { return flatH; } : function (r) { if (r[3] > 0) return r[3] * hK; var a = +r[1] || 0; return (a < 60 ? 3.5 : a < 400 ? 7 : a < 1200 ? 12 : 15) * hK; };
    // a home is one or two storeys (Saif, 26 Sep 2026): under 120 m2 one storey with its parapet, 4.6 m; otherwise at most
    // 7.8 m, two storeys with the parapet. A kind 0 building the file puts at 15 m or more is a block in a dense zone and keeps it.
    var homeCap = function (r, h) { if ((r[4] || 0) !== 0 || (+r[1] || 0) >= 800 || h >= 15 * hK) return h; return Math.min(h, ((+r[1] || 0) < 120 ? 4.6 : 7.8) * hK); };
    var hFor = function (r, i) { var h = homeCap(r, hBase(r)); return vary && i != null ? h * (0.94 + 0.12 * jitter(i)) : h; };
    var isBlock = function (r, h) { return (r[4] || 0) === 0 && h >= 15 * hK; };   // a home row that is really a block: drawn as one
    var shades = [grey, grey.brighten(0.07, new C.Color()), grey.darken(0.07, new C.Color())];
    // kind 0 house, 1 structure under 40 m2, 2 another building (commercial, industrial, public, or over 1,500 m2)
    var slate = C.Color.fromCssColorString("#8FA3B8"), slates = [slate, slate.brighten(0.08, new C.Color()), slate.darken(0.08, new C.Color())], dark = grey.darken(0.3, new C.Color());
    // kinds 3 to 8 (schools, hospitals, government, mosques, commercial, industrial) take the colour the file declares for them
    var catCol = {}, catCode = {}; ((HOUSES.meta && HOUSES.meta.categories) || []).forEach(function (c) { if (c.kind >= 3) { catCol[c.kind] = C.Color.fromCssColorString(c.colour); catCode[c.kind] = c.code || ""; } });
    var tags = []; // one label per public building: the code, and the name where a source has one
    var typeCol = HOUSE_TYPES.map(function (t) { return C.Color.fromCssColorString(t.colour); });
    var homesOnly = PREF("twinmap-homes", "all") === "homes";
    var faded = C.Color.fromCssColorString("#4A5560");
    var colourAt = HOUSE_COLOUR ? function (i, heavy) { return HOUSE_COLOUR(i, heavy) || (heavy ? red : grey); } : function (i, heavy, kind, areaM2) {
      if (kind === 0) { var tc = typeCol[HOUSE_TYPES.indexOf(houseType(areaM2))]; if (heavy) tc = C.Color.lerp(tc, red, 0.38, new C.Color());
        return vary ? tc.brighten((jitter(i * 7 + 3) - 0.5) * 0.22, new C.Color()) : tc; }
      if (homesOnly) return faded;
      return catCol[kind] ? catCol[kind] : kind === 2 ? slates[i % 3] : kind === 1 ? dark : shades[i % 3];
    };
    var pub = [];
    // only what the camera can see: the whole layer at once is more geometry than an integrated GPU holds, and Chrome
    // ends the renderer process rather than draw it
    // and only the draw the camera is going to show: assembling 20,000 extrusions that are hidden the moment they are
    // finished leaves the high view empty for the seconds it takes, which is what reads as missing buildings
    var wantSolid = solidWanted();
    var sel = wantSolid ? rowsInView(HOUSE_CAP, 0.15) : { rows: [], bbox: viewBbox(), found: 0, capped: false, lostInView: 0 };
    var idx = sel.rows; HOUSE_BUILT = { bbox: sel.bbox, n: idx.length, capped: sel.capped, found: sel.found, lost: sel.lostInView || 0, solid: wantSolid, focus: sel.focus || null, reach: sel.reach || 0 };
    // the rows nearest the focus, for the villa massing, the codes, the compound walls and the palms: within the code
    // range only; the selection is only sorted when it exceeds its cap because grid order starts in the west
    var nearRows = null, nearSet = null, codeRows = null;
    if (wantSolid && cameraHeight() < LOD.villa) {
      var fc = sel.focus || [0, 0], d2 = function (gi) { var q = HOUSES.houses[gi][2]; return (q[0] - fc[0]) * (q[0] - fc[0]) + (q[1] - fc[1]) * (q[1] - fc[1]); };
      var sorted = idx.length > ID_CAP ? idx.slice().sort(function (a, b) { return d2(a) - d2(b); }) : idx;
      nearRows = sorted.slice(0, VILLA_CAP);
      if (REAL) nearSet = new Set(nearRows);
      if (cameraHeight() < LOD.ids) codeRows = sorted.slice(0, ID_CAP);
    }
    HOUSE_BUILT.villa = !!(nearRows && REAL);
    for (var b = 0; b < idx.length; b += 2500) {
      var inst = [];
      idx.slice(b, b + 2500).forEach(function (gi_i) {
        var r = HOUSES.houses[gi_i], i = gi_i;
        if (!houseVisible(r)) return;
        try { var k4 = r[4] || 0, hh = hFor(r, i), mapCol = colourAt(i, dens[r[0]] === "heavy", k4, r[1] || 0);
          var ring = r[2], lon = 0, lat = 0, np = ring.length / 2; for (var q = 0; q < ring.length; q += 2) { lon += ring[q]; lat += ring[q + 1]; }
          var kindT = k4 === 0 ? (isBlock(r, hh) ? 0.3 : HOUSE_TYPES.indexOf(houseType(r[1] || 0)) * 0.1) : k4, tint = HOUSE_COLOUR ? 0.85 : k4 >= 3 ? 0.9 : 0.30, wc = REAL ? wallColour(i, k4, homesOnly) : mapCol;
          var mkAttrs = function (h) { var a = { color: C.ColorGeometryInstanceAttribute.fromColor(wc) }; if (REAL) realAttributes(a, lon / np, lat / np, groundOf(r), h, kindT, i, mapCol, tint); return a; };
          var mkGeom = function (flat, h0, h1) { return new C.PolygonGeometry({ polygonHierarchy: new C.PolygonHierarchy(C.Cartesian3.fromDegreesArray(flat)), height: h0, extrudedHeight: h1, vertexFormat: C.PerInstanceColorAppearance.VERTEX_FORMAT }); };
          var tagged = k4 >= 3 && (k4 <= 6 || k4 === 9 || r[6]);
          // a villa near the camera is two masses: the ground floor on the whole footprint with a parapet at 4 m, and a set
          // back upper floor on 78 per cent of it, shifted a little off centre, so it reads as a villa with a roof terrace
          if (REAL && nearSet && nearSet.has(i) && k4 === 0 && (r[1] || 0) < 800 && hh > 6.2 && !isBlock(r, hh)) {
            var cx = lon / np, cy = lat / np, rmx = 0; for (var q4 = 0; q4 < ring.length; q4 += 2) rmx += Math.hypot(ring[q4] - cx, ring[q4 + 1] - cy); rmx /= np;
            var ang = jitter(i * 17 + 1) * 6.283, ux = cx + Math.cos(ang) * 0.10 * rmx, uy = cy + Math.sin(ang) * 0.10 * rmx, upr = [];
            for (var q5 = 0; q5 < ring.length; q5 += 2) upr.push(ux + (ring[q5] - cx) * 0.78, uy + (ring[q5 + 1] - cy) * 0.78);
            inst.push(new C.GeometryInstance({ geometry: mkGeom(r[2], groundOf(r) - 3, groundOf(r) + 4.0), attributes: mkAttrs(4.0), id: "house|" + i }));
            inst.push(new C.GeometryInstance({ geometry: mkGeom(upr, groundOf(r) + 3.4, groundOf(r) + hh), attributes: mkAttrs(hh), id: "house|" + i }));
          } else {
            var gi = new C.GeometryInstance({ geometry: mkGeom(r[2], groundOf(r) - 3, groundOf(r) + hh), attributes: mkAttrs(hh), id: (k4 >= 3 ? "pub|" : "house|") + i });
            if (tagged) { pub.push(gi);
              tags.push({ position: C.Cartesian3.fromDegrees(lon / np, lat / np, groundOf(r) + hh + 1.5), code: catCode[k4] || "", name: r[6] ? String(r[6]).slice(0, 28) : "", colour: catCol[k4] || grey }); } else inst.push(gi);
          } } catch (e) {}
      });
      if (inst.length) { var p = viewer.scene.primitives.add(new C.Primitive({ geometryInstances: inst, appearance: solidAppearance(), asynchronous: true, allowPicking: false, shadows: C.ShadowMode.ENABLED, show: false })); FRESH.push(p); }
      COUNTS.houses = Math.min(b + 2500, idx.length) + " of " + idx.length + " nearby"; if (b % 25000 === 0) renderLayers();
      await new Promise(function (r) { setTimeout(r, 16); }); // yield a frame between batches: 116,677 houses must never freeze the tab
    }
    // the same buildings as flat footprints, for the heights where a solid is not worth its cost and a gap is not
    // acceptable either. Drawn on the ground, so they read as a plan rather than pretending to be a model.
    // every building, once. A footprint costs little enough that choosing between them buys nothing and can only lose one.
    // the footprints beyond the solids: in the real look they are roofs seen from above, light grey with the map colour
    // at the roof's tint, so the far field reads as built ground rather than a plan; in the diagram look the type colour
    var flatColour = function (i, r) { var k = r[4] || 0, mc = colourAt(i, dens[r[0]] === "heavy", k, r[1] || 0); if (!REAL) return mc.withAlpha(0.9);
      var base = wallColour(i, k, homesOnly).darken(0.14, new C.Color()); return C.Color.lerp(base, mc, HOUSE_COLOUR ? 0.85 : k >= 3 ? 0.9 : 0.30, new C.Color()).withAlpha(0.92); };
    // the footprints are part of the ground picture: painted into imagery tiles, not drawn as a ground primitive. What
    // is counted here is what the filter leaves to paint; the layer is made again only when a rebuild dropped it.
    FOOT_COLOUR = function (i, r, far) { var c = flatColour(i, r); if (far) c = C.Color.lerp(c, C.Color.WHITE, 0.24, new C.Color()).withAlpha(1);
      return "rgba(" + Math.round(c.red * 31) * 8 + "," + Math.round(c.green * 31) * 8 + "," + Math.round(c.blue * 31) * 8 + "," + c.alpha.toFixed(2) + ")"; };
    if (!HOUSE_FLAT) {
      var flatN = 0; for (var fq = 0; fq < HOUSES.houses.length; fq++) if (houseVisible(HOUSES.houses[fq])) flatN++;
      HOUSE_FLAT_N = flatN; HOUSE_FLAT_GEN = flatGen;
      HOUSE_FLAT = paintedLayer(paintFootprints);
    }
    HOUSE_FLAT.show = layer.on && cameraHeight() < LOD.housesFlat;
    HOUSE_BUILT.flatFound = HOUSE_FLAT_N; HOUSE_BUILT.flatCapped = false; HOUSE_BUILT.flatN = HOUSE_FLAT_N; HOUSE_BUILT.flatLost = 0;
    // the public buildings are few, so they stay pickable: a click opens the school or hospital on the Selection tab
    if (pub.length) { var pp = viewer.scene.primitives.add(new C.Primitive({ geometryInstances: pub, appearance: solidAppearance(), asynchronous: true, allowPicking: true, shadows: C.ShadowMode.ENABLED, show: false })); FRESH.push(pp); }
    // the code sits on the roof in the category colour, readable to about 4 km, and disappears with the layer
    // the code on every roof, the way a sales map numbers every villa: a dark pill, built below 700 m of height and
    // shown to 1.5 times that from the camera, because a tilted view at 500 m looks further than 700 m along the
    // ground; only on what was built solid, nearest the focus first, never clamped (see the label freeze of CR-047)
    HOUSE_BUILT.ids = false;
    if (codeRows && ID_ON) {
      var ic = viewer.scene.primitives.add(new C.LabelCollection({ show: false }));
      var pillBg = C.Color.fromCssColorString("#0C1319").withAlpha(0.82), pillFar = new C.DistanceDisplayCondition(0, LOD.ids * 1.5), pillScale = new C.NearFarScalar(250, 0.8, LOD.ids * 1.5, 0.5), pillPad = new C.Cartesian2(3, 2);
      codeRows.forEach(function (gi_i) {
        var r = HOUSES.houses[gi_i]; if (!r[8] || !houseVisible(r)) return;
        var ring = r[2], lon = 0, lat = 0, np = ring.length / 2; for (var q = 0; q < ring.length; q += 2) { lon += ring[q]; lat += ring[q + 1]; }
        ic.add({ position: C.Cartesian3.fromDegrees(lon / np, lat / np, groundOf(r) + hFor(r, gi_i) + 1.2), text: r[8], font: "9px Calibri, sans-serif", fillColor: C.Color.WHITE,
          showBackground: true, backgroundColor: pillBg, backgroundPadding: pillPad, horizontalOrigin: C.HorizontalOrigin.CENTER, verticalOrigin: C.VerticalOrigin.BOTTOM,
          disableDepthTestDistance: Number.POSITIVE_INFINITY, distanceDisplayCondition: pillFar, scaleByDistance: pillScale });
      });
      FRESH.push(ic); HOUSE_BUILT.ids = true;
    }
    // the compound wall of a home: a villa in Ras Al Khaimah stands inside its boundary wall, so within the code range the
    // nearest homes get one, 2 m high and 0.3 m thick, 3 m out from the house scaled about its centre. No plot lines exist:
    // the wall marks the setback, not the boundary. An apartment block gets none.
    if (REAL && nearRows) {
      var wallInst = [], wallCol = C.Color.fromCssColorString("#E6E3DC");
      nearRows.forEach(function (gi_i) {
        var r = HOUSES.houses[gi_i]; if ((r[4] || 0) !== 0 || (r[1] || 0) >= 800 || !houseVisible(r) || isBlock(r, hFor(r, gi_i))) return;
        var ring = r[2], np = ring.length / 2, lon0 = 0, lat0 = 0; for (var q = 0; q < ring.length; q += 2) { lon0 += ring[q]; lat0 += ring[q + 1]; } lon0 /= np; lat0 /= np;
        var mx = 111320 * Math.cos(lat0 * Math.PI / 180), my = 110540, rm = 0;
        for (var q2 = 0; q2 < ring.length; q2 += 2) rm += Math.hypot((ring[q2] - lon0) * mx, (ring[q2 + 1] - lat0) * my); rm /= np;
        if (rm < 3) return;
        var so = 1 + 3.0 / rm, si = 1 + 2.7 / rm, outer = [], inner = [];
        for (var q3 = 0; q3 < ring.length; q3 += 2) { outer.push(lon0 + (ring[q3] - lon0) * so, lat0 + (ring[q3 + 1] - lat0) * so); inner.push(lon0 + (ring[q3] - lon0) * si, lat0 + (ring[q3 + 1] - lat0) * si); }
        try { wallInst.push(new C.GeometryInstance({ geometry: new C.PolygonGeometry({ polygonHierarchy: new C.PolygonHierarchy(C.Cartesian3.fromDegreesArray(outer), [new C.PolygonHierarchy(C.Cartesian3.fromDegreesArray(inner))]), height: groundOf(r) - 1, extrudedHeight: groundOf(r) + 2.0, vertexFormat: C.PerInstanceColorAppearance.VERTEX_FORMAT }), attributes: { color: C.ColorGeometryInstanceAttribute.fromColor(wallCol) } })); } catch (e) {}
      });
      if (wallInst.length) { var wp = viewer.scene.primitives.add(new C.Primitive({ geometryInstances: wallInst, appearance: new C.PerInstanceColorAppearance({ translucent: false, closed: true }), asynchronous: true, allowPicking: false, shadows: C.ShadowMode.ENABLED, show: false })); FRESH.push(wp); }
    }
    if (tags.length) {
      var lc = viewer.scene.primitives.add(new C.LabelCollection({ show: false }));
      var dark = C.Color.fromCssColorString("#0C1319"), ddc = new C.DistanceDisplayCondition(0, 4000);
      // a small code up to 4 km; the name smaller still and only within 1.2 km, so the map stays readable at planning distance
      var near = new C.DistanceDisplayCondition(0, 1200);
      tags.forEach(function (g) {
        var fill = g.colour.brighten(0.35, new C.Color());
        if (g.code) lc.add({ position: g.position, text: g.code, font: "bold 9px Calibri, sans-serif", fillColor: fill, outlineColor: dark, outlineWidth: 3, style: C.LabelStyle.FILL_AND_OUTLINE,
          horizontalOrigin: C.HorizontalOrigin.CENTER, verticalOrigin: C.VerticalOrigin.BOTTOM, distanceDisplayCondition: ddc, disableDepthTestDistance: Number.POSITIVE_INFINITY });
        if (g.name) lc.add({ position: g.position, text: g.name, font: "8px Calibri, sans-serif", fillColor: C.Color.fromCssColorString("#E6EDF3"), outlineColor: dark, outlineWidth: 3, style: C.LabelStyle.FILL_AND_OUTLINE,
          horizontalOrigin: C.HorizontalOrigin.CENTER, verticalOrigin: C.VerticalOrigin.TOP, pixelOffset: new C.Cartesian2(0, 2), distanceDisplayCondition: near, disableDepthTestDistance: Number.POSITIVE_INFINITY });
      });
      FRESH.push(lc);
    }
    // the swap: the new set was built hidden, and takes the place of the old one when every primitive in it is ready
    var shown = layer.on && wantSolid, tSwap = Date.now();
    while (FRESH.some(function (q) { return q.ready === false; }) && Date.now() - tSwap < 8000) { viewer.scene.requestRender(); await new Promise(function (r) { setTimeout(r, 60); }); }
    OLD_PRIMS.forEach(function (q) { viewer.scene.primitives.remove(q); });
    FRESH.forEach(function (q) { q.show = shown; });
    HOUSE_PRIMS = FRESH; viewer.scene.requestRender();
    // the count column shows the buildings drawn; the breakdown goes into the note so the row keeps its width
    var nh = houseCount(); var mc = HOUSES.meta && HOUSES.meta.counts;
    COUNTS.houses = houseCountLabel();
    if (mc) {
      var f = function (n) { return (n || 0).toLocaleString("en-GB"); };
      layer.baseNote = layer.baseNote || layer.note;
      layer.note = "drawn for the view you are on, of " + f(HOUSES.houses.length) + " in the file. " + f(nh) + " houses with a bin" + (mc.digitised ? " (" + f(mc.digitised) + " digitised by PSD from imagery, unverified)" : "") + ", " + f(mc.ancillary) + " structures under 40 m2, " + f(mc.other) + " other buildings, " +
        f(mc.education) + " education, " + f(mc.medical) + " medical, " + f(mc.government) + " government and civic, " + f(mc.religious) + " mosques, " + f(mc.commercial) + " commercial, " + f(mc.industrial) + " industrial, each tagged on its roof SCH, HOS, GOV, MSQ, COM or IND (" + f(mc.named) + " named: click one); " +
        f(mc.heightFromSource + mc.heightFromFloors) + " heights recorded at source, the rest estimated. " + layer.baseNote;
      layer.legend = HOUSE_TYPES.map(function (t) { return { colour: t.colour, label: t.code + " " + t.label }; })
        .concat(((HOUSES.meta && HOUSES.meta.categories) || []).filter(function (c) { return c.kind !== 0; }).map(function (c) { return { colour: c.colour, label: (c.code ? c.code + " " : "") + c.label }; }));
    }
    renderLayers(); renderPlan(); applyLod(); viewer.scene.requestRender();
  }
  // a home reads by its footprint: the department collects from all of them, but a supervisor needs to see the difference
  // between a single storey house and an apartment block when planning a round
  var HOUSE_TYPES = [
    { max: 150, code: "H1", label: "Small house, single storey", colour: "#F2F5F7" },
    { max: 350, code: "H2", label: "Villa, ground and first floor", colour: "#7DD3A8" },
    { max: 800, code: "H3", label: "Large villa or compound", colour: "#4A9FD8" },
    { max: 1e9, code: "H4", label: "Apartment block", colour: "#7C5CC4" },
  ];
  function houseType(areaM2) { for (var i = 0; i < HOUSE_TYPES.length; i++) if (areaM2 <= HOUSE_TYPES[i].max) return HOUSE_TYPES[i]; return HOUSE_TYPES[3]; }
  // what a building is, for the filter and the card: a house is its type H1 to H4, anything else its category kind
  function houseKey(r) { var k = r[4] || 0; return k === 0 ? houseType(r[1] || 0).code : "K" + k; }
  var HOUSE_FILTER = null;   // null draws everything; otherwise the set of keys that stay on the map
  function houseVisible(r) { return !HOUSE_FILTER || HOUSE_FILTER.has(houseKey(r)); }
  var ID_ON = PREF("twinmap-ids", "on") !== "off", ID_CAP = 1500, VILLA_CAP = 4000;   // the code on every roof within LOD.ids; the villa detail on the nearest homes within LOD.villa
  var KIND_LABEL = { 3: "Education (school, college, university)", 4: "Medical (hospital, clinic)", 5: "Government and civic", 6: "Mosque", 7: "Commercial", 8: "Industrial" };
  function publicBuildingSelect(i) {
    var r = HOUSES && HOUSES.houses[i]; if (!r) return;
    var a = HOUSES.areas[r[0]] || {}; var kind = r[4] || 0;
    var code = ""; ((HOUSES.meta && HOUSES.meta.categories) || []).forEach(function (c) { if (c.kind === kind && c.code) code = c.code + " · "; });
    var src = { 1: "digitised from imagery, reviewed", 2: "detected by the imagery sweep, not reviewed one by one" }[r[5]] || "open data footprint";
    var p = planInputs();
    var attrs = { footprintM2: (r[1] || 0).toLocaleString("en-GB") + " m2", heightM: r[3] + " m", storeys: Math.max(1, Math.round((r[3] - 1) / 3.6)), area: a.title, density: a.density, sector: a.sector || "" };
    var name, cls;
    if (kind === 0) {
      var t = houseType(r[1] || 0);
      name = t.code + " " + t.label; cls = "Home, one 1,100 L bin";
      var freq = a.density === "heavy" ? p.freqHeavy : p.freqNormal;
      attrs = Object.assign({ bin: "1 x 1,100 L (proposed, confirm on survey)", collections: freq + " a week (" + a.density + " density)", binsAtThisHouse: 1 }, attrs);
    } else if (kind === 1) { name = "Structure under 40 m2"; cls = "Ancillary, no bin"; }
    else { name = r[6] || (KIND_LABEL[kind] || "Building") + " (unnamed)"; cls = code + (KIND_LABEL[kind] || "Building") + ", no household bin"; }
    showSelection({ name: name, class: cls, origin: "reference", verificationStatus: "not verified",
      accuracyClass: src, source: (HOUSES.meta && HOUSES.meta.source) || "", id: "building " + i, attributes: attrs });
  }
  // a click on the ground: the building under the point, if the layer is on and we are close enough to be pointing at one
  function houseAt(lon, lat) {
    if (!HOUSES || !HOUSE_PRIMS.length || !(HOUSE_BUILT && HOUSE_BUILT.solid)) return false;
    var hl = LAYERS.find(function (x) { return x.kind === "houses"; }); if (!hl || !hl.on) return false;
    var best = -1, bestD = 1e9, tol = 0.00045; // about 50 m
    for (var i = 0; i < HOUSES.houses.length; i++) {
      var r = HOUSES.houses[i][2];
      if (Math.abs(r[0] - lon) > tol || Math.abs(r[1] - lat) > tol) continue;
      var cx = 0, cy = 0, n = r.length / 2;
      for (var j = 0; j < r.length; j += 2) { cx += r[j]; cy += r[j + 1]; }
      cx /= n; cy /= n;
      var d = (cx - lon) * (cx - lon) + (cy - lat) * (cy - lat);
      if (d < bestD) { bestD = d; best = i; }
    }
    if (best < 0 || bestD > tol * tol) return false;
    publicBuildingSelect(best); return true;
  }
  // what the count column says depends on which of the two draws is showing
  function houseCountLabel() {
    if (!HOUSES || !HOUSE_BUILT) return "…";
    var h = cameraHeight(), flat = !(HOUSE_BUILT && HOUSE_BUILT.solid);
    if (h >= LOD.housesFlat) return "not drawn above " + Math.round(LOD.housesFlat / 1000) + " km";
    var n = flat ? (HOUSE_BUILT.flatN || 0) : HOUSE_BUILT.n;
    var found = flat ? (HOUSE_BUILT.flatFound || 0) : HOUSE_BUILT.found;
    var capped = flat ? HOUSE_BUILT.flatCapped : HOUSE_BUILT.capped;
    if (n >= HOUSES.houses.length) return HOUSES.houses.length;
    var lost = flat ? (HOUSE_BUILT.flatLost || 0) : (HOUSE_BUILT.lost || 0);
    if (!flat && HOUSE_FLAT_N) return n.toLocaleString("en-GB") + " solid near you" + (capped ? " (the nearest)" : "") + "; all " + HOUSE_FLAT_N.toLocaleString("en-GB") + " as footprints";
    var s = capped ? n.toLocaleString("en-GB") + " of " + (found || 0).toLocaleString("en-GB") + " nearby (limit)"
                   : n.toLocaleString("en-GB") + " nearby";
    if (lost) s += ", " + lost.toLocaleString("en-GB") + " in view not drawn";
    return flat ? s + ", flat" : s;
  }
  function houseCount() { if (!HOUSES) return 0; if (HOUSES.meta && HOUSES.meta.counts && HOUSES.meta.counts.houses != null) return HOUSES.meta.counts.houses; var n = 0; for (var i = 0; i < HOUSES.houses.length; i++) if (!HOUSES.houses[i][4]) n++; return n;
  }
  function housesInView(cap) {
    var bb = viewBbox(); if (!bb || !HOUSES) return { stops: [], total: 0 };
    var out = [];
    for (var i = 0; i < HOUSES.houses.length; i++) { if (HOUSES.houses[i][4]) continue; var r = HOUSES.houses[i][2]; var lon = 0, lat = 0, n = r.length / 2; for (var j = 0; j < r.length; j += 2) { lon += r[j]; lat += r[j + 1]; } lon /= n; lat /= n;
      if (lon >= bb[0] && lon <= bb[2] && lat >= bb[1] && lat <= bb[3]) out.push({ id: "house-" + i, lon: +lon.toFixed(6), lat: +lat.toFixed(6), demand: 1, name: "House " + i + " (1,100 L bin)" }); }
    return { stops: cap ? out.slice(0, cap) : out, total: out.length };
  }
  // MSW sub-sectors: a local GeoJSON file rather than a platform layer, so it draws without signing in to the O&M platform
  var SUBSECTOR_COLOUR = { "1": "#57C4DD", "2": "#06D6A0", "3": "#FFD166", "4": "#EF476F", "5": "#A786FF", "6": "#F78C6B", "7": "#8ECAE6", "8": "#90BE6D", "9": "#E060D8" };
  var LOCAL_FILES = {};
  // Cesium makes one entity per polygon, so a MultiPolygon feature yields more entities than features: read each entity's own
  // properties instead of pairing by index, or every feature after a MultiPolygon takes the wrong name and colour
  function localProps(e) {
    var b = e.properties, o = {};
    if (!b) return o;
    ["name", "sectorNo", "subSector", "kind", "areaKm2", "layer", "source", "fill", "fillOpacity", "stroke", "strokeWidth", "highway", "surface", "osmId"].forEach(function (k) { if (b[k]) { try { o[k] = b[k].getValue(); } catch (x) {} } });
    return o;
  }
  function polygonCentre(e) {
    try {
      var pos = e.polygon.hierarchy.getValue(C.JulianDate.now()).positions;
      var g = C.Cartographic.fromCartesian(C.BoundingSphere.fromPoints(pos).center);
      return C.Cartesian3.fromRadians(g.longitude, g.latitude);
    } catch (x) { return null; }
  }
  // the RPT-13 priority ranking on the named areas
  var PRIORITY = null;
  async function loadPriority(layer) {
    var k = layer.dsKey || layer.key;
    if (!PRIORITY) { try { PRIORITY = await (await fetch("omcc-source/area_priority.json?v=" + HOUSES_VERSION)).json(); } catch (e) { COUNTS[k] = "?"; renderLayers(); return; } }
    if (!LOCAL_FILES["omcc-source/rak_subsectors.geojson"]) {
      try { LOCAL_FILES["omcc-source/rak_subsectors.geojson"] = await (await fetch("omcc-source/rak_subsectors.geojson")).json(); } catch (e) { COUNTS[k] = "?"; renderLayers(); return; }
    }
    // joined on name AND size: the map holds three areas called Commercial and the ranking two, and a join on the name
    // alone drew all three as the last one read, a High area shown as rank 79. Each ranked area takes the same named
    // polygon whose own area is closest to the km2 the ranking recorded, and each polygon is used once.
    var geoAreas = LOCAL_FILES["omcc-source/rak_subsectors.geojson"].features.filter(function (f) { return f.properties.kind === "area"; });
    var byGeoName = {}; geoAreas.forEach(function (f, gi) { (byGeoName[f.properties.name] = byGeoName[f.properties.name] || []).push(gi); });
    var taken = {}, pairs = [];
    PRIORITY.areas.forEach(function (a) {
      var best = -1, bestD = Infinity;
      (byGeoName[a.name] || []).forEach(function (gi) {
        if (taken[gi]) return;
        var d = Math.abs((geoAreas[gi].properties.areaKm2 || 0) - (a.km2 || 0));
        if (d < bestD) { bestD = d; best = gi; }
      });
      if (best >= 0) { taken[best] = true; pairs.push({ f: geoAreas[best], a: a }); }
    });
    var src = pairs.map(function (p) { return p.f; });
    if (DS[k]) { viewer.dataSources.remove(DS[k], true); DS[k] = null; }
    FEATS[k] = src;
    var ds = await C.GeoJsonDataSource.load({ type: "FeatureCollection", features: pairs.map(function (p, pi) { return { type: "Feature", geometry: p.f.geometry, properties: { name: p.f.properties.name, pk: pi } }; }) }, { clampToGround: true });
    var cols = PRIORITY.meta.colours || {}, edges = [];
    ds.entities.values.forEach(function (e) {
      var pk = null; try { pk = e.properties.pk.getValue(); } catch (x) {}
      var a = pk != null && pairs[pk] ? pairs[pk].a : null; if (!a) return;
      var col = C.Color.fromCssColorString(cols[a.priority] || cols["Not scored"] || "#5C6672");
      if (e.polygon) {
        // the band gives the hue, the score within the band gives the strength, so the worst area in a band is the darkest
        // light enough that the buildings and bins read through it; a fifth lighter again on 26 Sep 2026 at Saif's request
        e.polygon.material = col.withAlpha((0.07 + 0.21 * Math.min(1, (a.score || 0) / 100)) * 0.8);
        e.polygon.outline = false;   // Cesium draws no outline on a polygon clamped to the terrain: the edge is a ground line below
        var hier = e.polygon.hierarchy && e.polygon.hierarchy.getValue(C.JulianDate.now());
        if (hier && hier.positions && hier.positions.length > 2) edges.push({ ring: hier.positions.concat([hier.positions[0]]), col: col, a: a, owner: e });
        e.polygon.classificationType = C.ClassificationType.TERRAIN; e.polygon.height = undefined; e.polygon.perPositionHeight = false;
      }
      var c = polygonCentre(e); if (c) e.position = new C.ConstantPositionProperty(c);
      e.label = new C.LabelGraphics({ text: "#" + a.rank + "  " + a.priority, font: (a.priority === "Critical" ? "bold " : "") + "12px Calibri, sans-serif",
        fillColor: col.brighten(0.55, new C.Color()), outlineColor: C.Color.fromCssColorString("#0C1319"), outlineWidth: 4,
        style: C.LabelStyle.FILL_AND_OUTLINE, heightReference: C.HeightReference.NONE,
        disableDepthTestDistance: Number.POSITIVE_INFINITY, scaleByDistance: new C.NearFarScalar(6000, 1.0, 160000, 0.0) });
      e.twinLayer = k;
      e.twinProps = { name: a.name, class: "Priority " + a.priority + ", rank " + a.rank + " of " + PRIORITY.meta.scored,
        origin: "derived", verificationStatus: "computed from measured indicators, see WA-PSD-RPT-13",
        accuracyClass: "percentile ranking", source: PRIORITY.meta.source, id: a.name,
        attributes: { score: a.score, leadDriver: a.driver,
          overflowRank: a.pO + " percentile (" + a.full_per_1000 + " full container reports per 1,000 houses)",
          densityRank: a.pD + " percentile (" + Math.round(a.dens) + " houses per km2)",
          smallDwellingRank: a.pS + " percentile (" + Math.round(a.small_pct) + " per cent under 350 m2)",
          houses: (a.houses || 0).toLocaleString("en-GB"), areaKm2: a.km2,
          containersItsAnalysisRequires: (a.bins_final || 0).toLocaleString("en-GB") + " (" + (a.binding_final || "") + " binds)",
          sector: a.sector, subSector: a.sub } };
    });
    edges.forEach(function (g) {
      var owner = g.owner;
      var ln = ds.entities.add({ polyline: { positions: g.ring, width: g.a.priority === "Critical" ? 4 : 2.5,
        material: g.col.withAlpha(0.95), clampToGround: true } });
      if (owner) { ln.twinLayer = owner.twinLayer; ln.twinProps = owner.twinProps; }
    });
    ds.show = layer.on; DS[k] = ds; await viewer.dataSources.add(ds);
    var m = PRIORITY.meta;
    COUNTS[k] = m.scored + " ranked";
    layer.legend = ["Critical", "High", "Medium", "Low"].map(function (b) {
      return { colour: m.colours[b], label: b + " (" + m.bands[b] + ", " + m.counts[b] + " areas)" };
    });
    layer.baseNote = layer.baseNote || layer.note;
    layer.note = m.scored + " of " + m.of + " areas ranked; the rest hold fewer than 200 houses and are served but are not capacity decisions at this stage. The band gives the colour and the score gives its strength, so the worst area in a band is the darkest. " + layer.baseNote;
    renderLayers(); viewer.scene.requestRender();
  }

  // the asset register. Points for the far view, boxes for the near one: 15,741 boxes are sub pixel above a few km and
  // cost the card for nothing, and dots stop reading as objects once you are down among them.
  // width, depth, height in metres, the real container: a 1,100 L bin is 1.37 x 1.07 x 1.45
  var BIN_DIM = { "120L": [0.48, 0.55, 0.93], "240L": [0.58, 0.73, 1.07], "360L": [0.62, 0.85, 1.10],
    "1.1  CbM": [1.37, 1.07, 1.45], "2.5 CbM": [1.80, 1.50, 1.50], "3.5 CbM": [2.00, 1.60, 1.70],
    "4.5 CbM": [2.20, 1.70, 1.80], "5    CbM": [2.40, 1.70, 1.85], "7    CbM": [3.40, 2.00, 1.60],
    "17  CbM": [6.00, 2.40, 1.70], "20  CbM": [6.50, 2.45, 1.90] };
  var BIN_EXAG = 2;   // twice size, so a bin reads at the height a supervisor works at without pretending to be a building
  var BINREG = null, BINREG_PRIM = null, BINREG_BOX = null;
  async function loadBinRegister(layer) {
    var k = layer.dsKey || layer.key;
    if (!BINREG) { try { BINREG = await (await fetch("omcc-source/bin_register.json?v=" + HOUSES_VERSION)).json(); } catch (e) { COUNTS[k] = "?"; renderLayers(); return; } }
    if (BINREG_PRIM) { viewer.scene.primitives.remove(BINREG_PRIM); BINREG_PRIM = null; }
    var cols = BINREG.meta.sizeColours || {}, m3 = BINREG.meta.sizeM3 || {};
    var pc = viewer.scene.primitives.add(new C.PointPrimitiveCollection({ blendOption: C.BlendOption.OPAQUE }));
    var far = new C.NearFarScalar(800, 1.35, 90000, 0.5);  // a multiplier on pixelSize, not a size: 8 px near, 3 px across the emirate
    BINREG.bins.forEach(function (b, i) {
      if (b[10]) return;   // withdrawn since the 2024 record: 3.5 m3 outside sectors 1, 7 and 8, per the T&R Manager
      var col = C.Color.fromCssColorString(cols[b[2]] || "#7C8794");
      pc.add({ position: C.Cartesian3.fromDegrees(b[0], b[1]), color: col, outlineColor: C.Color.fromCssColorString("#0C1319"),
        outlineWidth: 1, pixelSize: 6, scaleByDistance: far,   // never faded: a bin that cannot be seen from the opening view is a bin missing from it
        disableDepthTestDistance: Number.POSITIVE_INFINITY, id: "bin|" + i });
    });
    pc.show = layer.on && cameraHeight() >= LOD.bins; BINREG_PRIM = pc;
    // the boxes: one primitive, black, sized by what the container actually is
    var body = C.Color.fromCssColorString("#FF17C8"), inst = [];   // magenta: nothing in the imagery is this colour
    BINREG.bins.forEach(function (b, i) {
      if (b[10]) return;
      var d = BIN_DIM[b[2]] || [1.2, 1.0, 1.2];
      var h = d[2] * BIN_EXAG, gb = typeof b[7] === "number" ? b[7] : 0;   // the ground under the bin, from the file
      try {
        inst.push(new C.GeometryInstance({
          geometry: C.BoxGeometry.fromDimensions({ vertexFormat: C.PerInstanceColorAppearance.VERTEX_FORMAT,
            dimensions: new C.Cartesian3(d[0] * BIN_EXAG, d[1] * BIN_EXAG, h) }),
          modelMatrix: C.Matrix4.multiplyByTranslation(C.Transforms.eastNorthUpToFixedFrame(C.Cartesian3.fromDegrees(b[0], b[1], gb)),
            new C.Cartesian3(0, 0, h / 2), new C.Matrix4()),
          attributes: { color: C.ColorGeometryInstanceAttribute.fromColor(body) }, id: "bin|" + i }));
      } catch (e) {}
    });
    if (inst.length) {
      BINREG_BOX = viewer.scene.primitives.add(new C.Primitive({ geometryInstances: inst,
        appearance: new C.PerInstanceColorAppearance({ translucent: false, closed: true }), asynchronous: true,
        allowPicking: true, shadows: C.ShadowMode.ENABLED, show: layer.on && cameraHeight() < LOD.bins }));
    }
    var c = BINREG.meta.counts, W = BINREG.meta.withdrawal || null, bySize = BINREG.meta.bySizeStanding || BINREG.meta.bySize;
    COUNTS[k] = (W ? W.standing : c.placed).toLocaleString("en-GB");
    // the colour is the container size: a legend under the row, largest count first, so red and yellow are never a guess
    layer.legend = Object.keys(bySize).map(function (sz) {
      return { colour: cols[sz] || "#7C8794", label: sz.replace(/\s+/g, " ").trim().replace("CbM", "m3") + " (" + bySize[sz].toLocaleString("en-GB") + ")" };
    });
    layer.baseNote = layer.baseNote || layer.note;
    var topSizes = Object.keys(bySize).slice(0, 5).map(function (s) { return bySize[s].toLocaleString("en-GB") + " x " + s.replace(/\s+/g, " ").trim(); }).join(", ");
    layer.note = (W ? W.standing.toLocaleString("en-GB") + " drawn as standing today: " : "") + c.placed.toLocaleString("en-GB") + " of " + c.records.toLocaleString("en-GB") + " records carry a position ("
      + c.noPosition.toLocaleString("en-GB") + " do not). " + topSizes + ". "
      + (W ? W.withdrawn.toLocaleString("en-GB") + " further 3.5 m3 records outside sectors " + (W.sectorsStillWith35.length > 1 ? W.sectorsStillWith35.slice(0, -1).join(", ") + " and " + W.sectorsStillWith35.slice(-1) : W.sectorsStillWith35.join("")) + " are not drawn: the 1.1 m3 programme replaced them since the register was made (" + W.source + "). " : "")
      + layer.baseNote;
    renderLayers(); viewer.scene.requestRender();
  }
  function binRegisterSelect(i) {
    var b = BINREG && BINREG.bins[i]; if (!b) return;
    var m3 = (BINREG.meta.sizeM3 || {})[b[2]];
    showSelection({ name: (b[2] || "container").replace(/\s+/g, " ").trim() + (b[6] ? " " + b[6] : ""), class: "Bin on the ground, " + (b[3] || "body type not stated"),
      origin: "reference", verificationStatus: "reported, position not survey verified", accuracyClass: "asset register position",
      source: BINREG.meta.source, id: b[6] || ("record " + i),
      attributes: { size: (b[2] || "not stated").replace(/\s+/g, " ").trim(), capacityM3: m3 == null ? "not stated" : m3,
        sector: b[8] != null ? "Sector " + b[8] + (b[9] ? ", " + b[9] : "") : "not stated", standing: b[10] ? "withdrawn since the 2024 record" : "standing, per the 2024 record",
        bodyType: b[3] || "not stated", serves: b[4] || "not stated", service: b[5] || "not stated",
        position: b[1].toFixed(5) + ", " + b[0].toFixed(5) } });
  }

  // ------------------------------------------------------------ the smart bin system: RFID tagged bins and the route register
  // omcc-source/smart_bins.json, built by smart-waste-platform/database/seeds/build_smart_bins.py from the two exports Saif put
  // in WM002 MAP APP (1 Oct 2026). A bin stands at the position registered at deployment, coloured by the days from its last
  // collection read to the export: the file is a snapshot, so the age is never counted to today. Dots far, boxes at the real
  // size near, in the same colour; the 2024 asset register keeps its magenta boxes, so the two sources are never mistaken.
  var SB = null, SB_PRIM = null, SB_BOX = null, SB_VERSION = "20261001a", SB_RR = false;
  async function fetchSmartBins() { if (!SB) { try { SB = await (await fetch("omcc-source/smart_bins.json?v=" + SB_VERSION)).json(); } catch (e) { SB = null; } } return SB; }
  // both strings carry no offset, so both are read as local time and the difference is the true one
  function sbAgeDays(b) { if (!SB || !b[7]) return null; return (Date.parse(SB.meta.exportedAt) - Date.parse(b[7])) / 86400000; }
  function sbBand(b) { var a = sbAgeDays(b); if (a == null) return null; var bands = SB.meta.bands; for (var i = 0; i < bands.length; i++) if (a <= bands[i].maxDays + 0.999) return bands[i]; return bands[bands.length - 1]; }
  function sbExportDay() { return SB ? new Date(Date.parse(SB.meta.exportedAt)).toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" }) : ""; }
  async function loadSmartBins(layer) {
    var k = layer.dsKey || layer.key, f = function (n) { return (+n || 0).toLocaleString("en-GB"); };
    if (!(await fetchSmartBins())) { COUNTS[k] = "?"; renderLayers(); return; }
    if (SB_PRIM) { viewer.scene.primitives.remove(SB_PRIM); SB_PRIM = null; }
    if (SB_BOX) { viewer.scene.primitives.remove(SB_BOX); SB_BOX = null; }
    var cols = {}, grey = C.Color.fromCssColorString("#7C8794"), outline = C.Color.fromCssColorString("#0C1319");
    SB.meta.bands.forEach(function (bd) { cols[bd.key] = C.Color.fromCssColorString(bd.colour); });
    var pc = viewer.scene.primitives.add(new C.PointPrimitiveCollection({ blendOption: C.BlendOption.OPAQUE }));
    var far = new C.NearFarScalar(800, 1.4, 60000, 0.55), inst = [];
    SB.bins.forEach(function (b, i) {
      var bd = sbBand(b), col = bd ? cols[bd.key] : grey, gb = typeof b[14] === "number" ? b[14] : 0;
      pc.add({ position: C.Cartesian3.fromDegrees(b[0], b[1], gb + 1), color: col, outlineColor: outline, outlineWidth: 1.5, pixelSize: 7, scaleByDistance: far,
        disableDepthTestDistance: Number.POSITIVE_INFINITY, id: "rfid|" + i });
      var d = /120/.test(b[5] || "") ? BIN_DIM["120L"] : BIN_DIM["1.1  CbM"], h = d[2] * BIN_EXAG;
      try {
        inst.push(new C.GeometryInstance({
          geometry: C.BoxGeometry.fromDimensions({ vertexFormat: C.PerInstanceColorAppearance.VERTEX_FORMAT, dimensions: new C.Cartesian3(d[0] * BIN_EXAG, d[1] * BIN_EXAG, h) }),
          modelMatrix: C.Matrix4.multiplyByTranslation(C.Transforms.eastNorthUpToFixedFrame(C.Cartesian3.fromDegrees(b[0], b[1], gb)), new C.Cartesian3(0, 0, h / 2), new C.Matrix4()),
          attributes: { color: C.ColorGeometryInstanceAttribute.fromColor(col) }, id: "rfid|" + i }));
      } catch (e) {}
    });
    pc.show = layer.on && cameraHeight() >= LOD.bins; SB_PRIM = pc;
    if (inst.length) SB_BOX = viewer.scene.primitives.add(new C.Primitive({ geometryInstances: inst, appearance: new C.PerInstanceColorAppearance({ translucent: false, closed: true }),
      asynchronous: true, allowPicking: true, shadows: C.ShadowMode.ENABLED, show: layer.on && cameraHeight() < LOD.bins }));
    var M = SB.meta, cnt = M.counts;
    COUNTS[k] = f(SB.bins.length);
    layer.legend = M.bands.map(function (bd) { return { colour: bd.colour, label: bd.label + " (" + f(bd.count) + ")" }; });
    layer.baseNote = layer.baseNote || layer.note;
    var cov = M.coverage.map(function (c) { return c.name.replace(/^\w+ - /, "").replace(/\b\w+/g, function (w) { return w.charAt(0) + w.slice(1).toLowerCase(); }) + " " + f(c.rfidBins) + (c.programmePlaced ? " of the " + f(c.programmePlaced) + " the programme counts as placed" : ""); }).join(", ");
    layer.note = f(SB.bins.length) + " bins with an RFID record, exported " + sbExportDay() + ": " + cov + "; the rest carry no record and no position. "
      + f(cnt.dubaiDefault) + " carry the system's Dubai default as their last read position. " + M.ageRule.charAt(0).toUpperCase() + M.ageRule.slice(1) + ". " + layer.baseNote;
    renderLayers(); applyLod(); viewer.scene.requestRender();
  }
  function smartBinSelect(i) {
    var b = SB && SB.bins[i]; if (!b) return;
    var age = sbAgeDays(b), bd = sbBand(b), exp = sbExportDay();
    var cv = SB.meta.coverage.find(function (c) { return c.namedArea === b[13]; });
    showSelection({ name: b[2] || ("RFID bin " + i), class: "Smart bin, RFID tagged, " + (b[5] || "capacity not stated"),
      origin: "reference", verificationStatus: "reported by the smart bin system, position not survey verified", accuracyClass: "position registered at deployment",
      source: SB.meta.source, captureDate: SB.meta.exportedAt.slice(0, 10), id: b[2] || ("row " + i),
      attributes: { serial: b[3] || "not stated", rfidEpc: b[4] || "not stated", capacity: b[5] || "not stated", deployed: b[6] || "not stated",
        lastCollectionRead: b[7] ? b[7].replace("T", " ") + (age != null ? ", " + (age < 1 ? Math.max(1, Math.round(age * 24)) + " hours" : Math.floor(age) + (Math.floor(age) === 1 ? " day" : " days")) + " before the export of " + exp : "") : "no read in the export",
        readStatus: bd ? bd.label : "no read", readOnExportDay: b[10] ? "yes" : "no",
        lastReadPosition: b[8] != null ? (b[15] != null ? b[15] + " m from the registered position" : "recorded") : "the system's Dubai default: no real position",
        lastWashingRead: b[11] || "none in the export", area: cv ? cv.name : (b[12] || "not stated"), position: b[1].toFixed(5) + ", " + b[0].toFixed(5) } });
  }
  // what the smart bin system holds for the area chosen in the design panel: bins with a position, and the routes it registers
  function dzSmart(sel) {
    if (!SB || !sel || !sel.sub) return "";
    var cov = SB.meta.coverage.filter(function (c) { return sel.value === "area:" + c.namedArea || (sel.value === "sub:" + sel.sub && c.sub === sel.sub); });
    var regs = SB.routes.filter(function (r) { return !r.test && r.group.replace(/^P-/, "") === "MSW" && r.subSectors.indexOf(sel.sub) >= 0; });
    var out = "", f = function (n) { return (+n || 0).toLocaleString("en-GB"); };
    if (cov.length) { var nb = 0, np = 0; cov.forEach(function (c) { nb += c.rfidBins; np += c.programmePlaced || 0; });
      out += '<b>Smart bins</b> ' + f(nb) + ' RFID tagged bins have a position here' + (np ? ', of the ' + f(np) + ' the programme counts as placed (' + Math.round(100 * nb / np) + ' per cent)' : '') + '; the design keeps the houses as its stops until every container has a position.<br>'; }
    if (regs.length) out += '<b>Registered</b> ' + regs.length + ' MSW route' + (regs.length > 1 ? 's' : '') + ' for ' + esc(sel.sub) + ' in the smart bin system: ' + esc(regs.map(function (r) { return r.code; }).join(", ")) + '.<br>';
    return out;
  }
  // the route register of the smart bin system, listed in the Routes tab: it carries no geometry, so it is a list, not lines
  function rrSetup() {
    if (!SB || !$("rr-list")) return;
    if (!SB_RR) {
      var subs = {}, groups = {};
      SB.routes.forEach(function (r) { r.subSectors.forEach(function (s) { subs[s] = (subs[s] || 0) + 1; }); groups[r.group] = (groups[r.group] || 0) + 1; });
      $("rr-sub").innerHTML = '<option value="">All sub sectors (' + SB.routes.length + ')</option>' + Object.keys(subs).sort(function (a, b) { return a.localeCompare(b, "en", { numeric: true }); })
        .map(function (s) { return '<option value="' + esc(s) + '">' + esc(s) + ' (' + subs[s] + ')</option>'; }).join("");
      $("rr-group").innerHTML = '<option value="">All groups</option>' + Object.keys(groups).sort().map(function (g) { return '<option value="' + esc(g) + '">' + esc(g) + ' (' + groups[g] + ')</option>'; }).join("");
      $("rr-sub").onchange = rrRender; $("rr-group").onchange = rrRender;
      var R = SB.meta.routes;
      $("rr-note").innerHTML = esc(R.count + " routes: " + Object.keys(R.groups).map(function (g) { return R.groups[g] + " " + g; }).join(", ") + ". " + R.note.charAt(0).toUpperCase() + R.note.slice(1) + "." + (R.test ? " " + R.test + " are test entries, marked." : ""))
        + '<br>Source: ' + esc(SB.meta.source);
      SB_RR = true;
    }
    rrRender();
  }
  function rrRender() {
    if (!SB || !$("rr-list")) return;
    var sub = $("rr-sub").value, grp = $("rr-group").value;
    var list = SB.routes.filter(function (r) { return (!sub || r.subSectors.indexOf(sub) >= 0) && (!grp || r.group === grp); });
    $("rr-list").innerHTML = list.length ? '<table class="dz-table"><thead><tr><th>Route</th><th>Group</th><th>From and to</th><th>km</th><th>min</th></tr></thead><tbody>' + list.map(function (r) {
      return '<tr><td>' + esc(r.code) + (r.test ? ' <span class="pill warn">test</span>' : '') + (r.suspended ? ' <span class="pill bad">suspended</span>' : '') + '</td><td>' + esc(r.group) + '</td><td>'
        + esc(r.start === r.end ? r.start : r.start + " to " + r.end) + '</td><td>' + (r.distanceKm == null ? "" : r.distanceKm) + '</td><td>' + (r.durationMin == null ? "" : r.durationMin) + '</td></tr>';
    }).join("") + '</tbody></table>' : '<div style="color:var(--ink-faint)">No registered route for this choice.</div>';
  }

  // the mobilisation framework joined to the named areas: see database/seeds/build_bin_programme.py for the join
  var BINPROG = null;
  // what stands behind the programme's count, and what is not on order, so the remainder is not read as stock on the way
  function procurementNote(p) {
    if (!p) return "";
    var f = function (v) { return (+v || 0).toLocaleString("en-GB"); };
    return "Bought: " + f(p.bought) + " x 1.1 m3 on " + p.orders + " orders since 2021, AED " + (p.valueInclVat / 1e6).toFixed(2) + "m incl VAT"
      + (p.noReceipt ? ", " + f(p.noReceipt) + " of them on PO " + p.noReceiptPo.join(", ") + " with no receipt recorded" : "")
      + ". On order: " + f(p.onOrder)
      + (p.cancelled ? "; PO " + p.cancelledPo.join(", ") + " for " + f(p.cancelled) + " was cancelled with no delivery" : "")
      + (p.requested ? ", and request " + p.requestNo.join(", ") + " for " + f(p.requested) + " has no order yet" : "")
      + " (purchase record reviewed 21 Sep 2026). ";
  }
  async function loadBinProgramme(layer) {
    var k = layer.dsKey || layer.key;
    if (!BINPROG) { try { BINPROG = await (await fetch("omcc-source/bin_programme.json?v=" + HOUSES_VERSION)).json(); } catch (e) { COUNTS[k] = "?"; renderLayers(); return; } }
    if (!LOCAL_FILES["omcc-source/rak_subsectors.geojson"]) {
      try { LOCAL_FILES["omcc-source/rak_subsectors.geojson"] = await (await fetch("omcc-source/rak_subsectors.geojson")).json(); } catch (e) { COUNTS[k] = "?"; renderLayers(); return; }
    }
    var by = {}; BINPROG.areas.forEach(function (a) { by[a.name] = a; });
    var src = LOCAL_FILES["omcc-source/rak_subsectors.geojson"].features.filter(function (f) { return f.properties.kind === "area" && by[f.properties.name]; });
    var fc = { type: "FeatureCollection", features: src.map(function (f) { return { type: "Feature", geometry: f.geometry, properties: { name: f.properties.name } }; }) };
    if (DS[k]) { viewer.dataSources.remove(DS[k], true); DS[k] = null; }
    FEATS[k] = src;
    var ds = await C.GeoJsonDataSource.load(fc, { clampToGround: true });
    var cols = (BINPROG.meta && BINPROG.meta.statusColours) || {};
    var maxBins = 1; BINPROG.areas.forEach(function (a) { if (a.bins > maxBins) maxBins = a.bins; });
    ds.entities.values.forEach(function (e) {
      var nm = null; try { nm = e.properties.name.getValue(); } catch (x) {}
      var a = nm && by[nm]; if (!a) return;
      var col = C.Color.fromCssColorString(cols[a.status] || cols[""] || "#7C8794");
      if (e.polygon) {
        // the fill carries how many bins are actually standing there, the outline carries the programme status
        var weight = Math.min(0.55, 0.10 + 0.45 * Math.sqrt(a.bins / maxBins)) * 0.8;   // a fifth lighter, 26 Sep 2026
        e.polygon.material = col.withAlpha(a.bins ? weight : 0.048);
        e.polygon.outline = true; e.polygon.outlineColor = col.withAlpha(0.95); e.polygon.outlineWidth = 3;
        e.polygon.classificationType = C.ClassificationType.TERRAIN; e.polygon.height = undefined; e.polygon.perPositionHeight = false;
      }
      var c = polygonCentre(e); if (c) e.position = new C.ConstantPositionProperty(c);
      e.label = new C.LabelGraphics({ text: a.bins ? a.bins.toLocaleString("en-GB") : "0", font: "bold 12px Calibri, sans-serif",
        fillColor: col.brighten(0.5, new C.Color()), outlineColor: C.Color.fromCssColorString("#0C1319"), outlineWidth: 4,
        style: C.LabelStyle.FILL_AND_OUTLINE, heightReference: C.HeightReference.NONE,
        disableDepthTestDistance: Number.POSITIVE_INFINITY, scaleByDistance: new C.NearFarScalar(6000, 1.0, 140000, 0.0) });
      e.twinLayer = k;
      e.twinProps = { name: a.name, class: "Bin programme, " + (a.status || "status not stated"), origin: "reference",
        verificationStatus: "reported by the section, positions not surveyed", accuracyClass: "count by area",
        source: BINPROG.meta.source, id: "area " + (a.code == null ? a.name : a.code),
        attributes: { binsPlaced: a.bins.toLocaleString("en-GB"), onASlab: a.withSlab.toLocaleString("en-GB"),
          withoutASlab: a.woSlab.toLocaleString("en-GB"), subSector: a.subSector, sector: a.sector,
          phase: a.phase || "not stated", priority: a.priority || "not stated",
          subSectorProposed: a.subProposed11.toLocaleString("en-GB") + " x 1.1 m3" + (a.subProposed25 ? ", " + a.subProposed25 + " x 2.5 m3" : ""),
          subSectorDeployed: a.subDeployed11.toLocaleString("en-GB") + " x 1.1 m3" + (a.subDeployed25 ? ", " + a.subDeployed25 + " x 2.5 m3" : ""),
          subSectorRemaining: a.subRemaining11.toLocaleString("en-GB") + " x 1.1 m3",
          subSectorExistingBefore: a.subExisting.toLocaleString("en-GB") } };
    });
    ds.show = layer.on; DS[k] = ds; await viewer.dataSources.add(ds);
    var m = BINPROG.meta.counts;
    COUNTS[k] = m.binsPlaced.toLocaleString("en-GB");
    layer.baseNote = layer.baseNote || layer.note;
    layer.note = m.binsPlaced.toLocaleString("en-GB") + " bins standing in " + m.matched + " areas (" + m.withSlab.toLocaleString("en-GB") + " on a slab); the programme is "
      + m.programmeProposed11.toLocaleString("en-GB") + " x 1.1 m3 and " + m.programmeProposed25.toLocaleString("en-GB") + " x 2.5 m3 proposed, "
      + m.programmeDeployed11.toLocaleString("en-GB") + " and " + m.programmeDeployed25.toLocaleString("en-GB") + " deployed, "
      + m.programmeRemaining11.toLocaleString("en-GB") + " x 1.1 m3 still owed"
      + (m.binsUnplaced ? "; " + m.binsUnplaced + " bins are in an area the map cannot place" : "") + ". "
      + procurementNote(BINPROG.meta.procurement) + layer.baseNote.charAt(0).toUpperCase() + layer.baseNote.slice(1);
    renderLayers(); viewer.scene.requestRender();
  }

  async function loadLocal(layer) {
    var k = layer.dsKey || layer.key;
    if (!LOCAL_FILES[layer.file]) {
      try { LOCAL_FILES[layer.file] = await (await fetch(layer.file)).json(); } catch (e) { COUNTS[k] = "?"; renderLayers(); return; }
    }
    var all = LOCAL_FILES[layer.file].features.filter(function (f) { return f.properties.kind === layer.pick; });
    var fc = { type: "FeatureCollection", features: all };
    if (DS[k]) { viewer.dataSources.remove(DS[k], true); DS[k] = null; }
    FEATS[k] = all;
    var ds = await C.GeoJsonDataSource.load(fc, { clampToGround: true });
    var isArea = layer.pick === "area";
    ds.entities.values.forEach(function (e) {
      var p = localProps(e); if (!p.name) return;
      if (e.polyline) {
        // a track is dotted: there is no built road, only the sand line a truck follows
        e.polyline.material = new C.PolylineDashMaterialProperty({ color: C.Color.fromCssColorString(layer.color).withAlpha(0.95), gapColor: C.Color.TRANSPARENT, dashLength: 14 });
        e.polyline.width = 3; e.polyline.clampToGround = true;
        e.twinLayer = k; e.twinProps = { name: p.name, class: "Sand track or unpaved road", origin: "reference", verificationStatus: "not verified", accuracyClass: "OpenStreetMap line", source: p.source, id: "osm way " + p.osmId,
          attributes: { highway: p.highway, surface: p.surface, osmId: p.osmId } };
        return;
      }
      // the department map styles every feature itself; the sector palette is only a fallback for a feature without a style
      var col = C.Color.fromCssColorString(p.fill || (isArea ? (SUBSECTOR_COLOUR[p.sectorNo] || layer.color) : layer.color));
      var edge = C.Color.fromCssColorString(p.stroke || p.fill || layer.color);
      if (e.polygon) {
        // the department map colours at their own opacity, uncapped: an area styled solid hides the imagery beneath it, as it does on the Google map
        var alpha = (p.fillOpacity == null ? (isArea ? 0.16 : 0.05) : p.fillOpacity) * 0.8;   // a fifth lighter than the department map, 26 Sep 2026
        e.polygon.material = col.withAlpha(alpha);
        e.polygon.outline = true; e.polygon.outlineColor = edge.withAlpha(0.95); e.polygon.outlineWidth = Math.max(2, Math.round((p.strokeWidth || 1) * 2));
        e.polygon.classificationType = C.ClassificationType.TERRAIN; e.polygon.height = undefined; e.polygon.perPositionHeight = false;
      }
      var c = polygonCentre(e);
      if (c) e.position = new C.ConstantPositionProperty(c);
      e.label = new C.LabelGraphics({ text: p.name, font: (isArea ? "bold 12px" : "11px") + " Calibri, sans-serif", fillColor: col.brighten(0.5, new C.Color()),
        outlineColor: C.Color.fromCssColorString("#0C1319"), outlineWidth: 4, style: C.LabelStyle.FILL_AND_OUTLINE,
        heightReference: C.HeightReference.CLAMP_TO_GROUND, disableDepthTestDistance: Number.POSITIVE_INFINITY,
        scaleByDistance: new C.NearFarScalar(6000, 1.0, isArea ? 120000 : 60000, 0.0) });
      e.twinLayer = k; e.twinProps = { name: p.name, class: isArea ? "MSW sub-sector area" : "MSW collection route area", origin: "reference",
        verificationStatus: "not verified", accuracyClass: "drawn on a Google map", source: p.source, id: p.subSector + " " + p.name,
        attributes: { subSector: p.subSector, sector: p.sectorNo, areaKm2: p.areaKm2, layer: p.layer } };
    });
    ds.show = layer.on; DS[k] = ds; await viewer.dataSources.add(ds); settleOnGround(ds.entities.values); COUNTS[k] = all.length; renderLayers(); viewer.scene.requestRender();
  }
  async function loadLayer(layer, scenario) {
    if (!viewer) return;
    if (layer.kind === "houses") { if (!layer.on && !HOUSE_PRIMS.length) { COUNTS.houses = "tick to load"; renderLayers(); return; } return loadHouses(layer); }
    if (layer.kind === "roads") { var rds = await roadsLabelled(); if (rds) { rds.show = layer.on; ROAD_PRIMS.forEach(function (p) { p.show = layer.on; }); twinRoads(!layer.on); } renderLayers(); return; }
    if (layer.kind === "custom") { collectionOnly(); return sectorBorders(layer.on); }
    if (layer.kind === "priority") { if (!layer.on && !DS[layer.dsKey || layer.key]) { COUNTS[layer.key] = "tick to load"; renderLayers(); return; } return loadPriority(layer); }
    if (layer.kind === "smartbins") { if (!layer.on && !SB_PRIM) { COUNTS[layer.key] = "tick to load"; renderLayers(); return; } if (SB_PRIM) { applyLod(); viewer.scene.requestRender(); return; } return loadSmartBins(layer); }
    if (layer.kind === "binreg") { if (!layer.on && !BINREG_PRIM) { COUNTS[layer.key] = "tick to load"; renderLayers(); return; } if (BINREG_PRIM) { applyLod(); viewer.scene.requestRender(); return; } return loadBinRegister(layer); }
    if (layer.kind === "binprog") { if (!layer.on && !DS[layer.dsKey || layer.key]) { COUNTS[layer.key] = "tick to load"; renderLayers(); return; } return loadBinProgramme(layer); }
    if (layer.kind === "local") return loadLocal(layer);
    var fc; var k = layer.dsKey || layer.key; scenario = scenario || layer.scenario;
    try { fc = await api.features(layer.key, null, scenario); } catch (e) { COUNTS[k] = "?"; renderLayers(); return; }
    if (DS[k]) { viewer.dataSources.remove(DS[k], true); DS[k] = null; }
    FEATS[k] = fc.features;
    var ds = await C.GeoJsonDataSource.load(fc, { clampToGround: layer.kind !== "polygon3d" });
    var byId = {}; fc.features.forEach(function (x) { byId[x.id] = x; });
    ds.entities.values.forEach(function (e) { var f = byId[e.id]; styleEntity(e, layer, f ? f.properties : {}); });
    ds.show = layer.on; DS[k] = ds; await viewer.dataSources.add(ds); settleOnGround(ds.entities.values); COUNTS[k] = fc.count; renderLayers();
    if (layer.key === "container") renderBins();
    if (layer.key === "landuse_agriculture") farmSymbols(fc);
    if (layer.key === "generator_medical") renderList("med-list", fc.features);
    if (layer.key === "generator_commercial") renderList("com-list", fc.features);
  }
  function renderLayers() {
    $("layer-list").innerHTML = LAYERS.map(function (l) { var k = l.dsKey || l.key; return '<div class="layer"><input type="checkbox" data-layer="' + k + '"' + (l.on ? " checked" : "") + '><span class="sw" style="background:' + l.color + '"></span><span>' + esc(l.title) + '<small>' + esc(l.note) + '</small>' + (l.legend ? '<small style="display:block;margin-top:3px">' + l.legend.map(function (g) { return '<span style="display:inline-block;width:9px;height:9px;border-radius:2px;background:' + esc(g.colour) + ';margin:0 4px 0 0;vertical-align:-1px"></span>' + esc(g.label) + ' '; }).join('') + '</small>' : '') + '</span><span class="n">' + (COUNTS[k] == null ? "…" : COUNTS[k]) + '</span></div>'; }).join("") + floodSectionHtml();
    $("layer-list").querySelectorAll("select[data-pref]").forEach(function (sel) { sel.onchange = function () { setPref(sel.dataset.pref, sel.value); if (sel.dataset.pref === "twinmap-render" && viewer) { viewer.scene.requestRenderMode = sel.value === "change"; viewer.scene.requestRender(); } if (sel.dataset.pref === "twinmap-trees") { var lu = LAYERS.find(function (x) { return x.key === "landuse_agriculture"; }); if (lu && FEATS.landuse_agriculture) farmSymbols({ features: FEATS.landuse_agriculture }); } if (sel.dataset.pref === "twinmap-detail") { applyDetail(); rebuildHouses(HOUSE_COLOUR); } if (sel.dataset.pref === "twinmap-heights" || sel.dataset.pref === "twinmap-homes") rebuildHouses(HOUSE_COLOUR); }; });
    $("layer-list").querySelectorAll("input[data-layer]").forEach(function (cb) { cb.onchange = function () { var l = LAYERS.find(function (x) { return (x.dsKey || x.key) === cb.dataset.layer; }) || FLOOD_LAYERS.find(function (x) { return x.key === cb.dataset.layer; }); l.on = cb.checked; if (l.flood === "cells" && l.on && !CELL_PRIMS.length) { loadFloodLayer(l); return; } if (l.on && (((l.kind === "local" || l.kind === "binprog" || l.kind === "priority") && !DS[cb.dataset.layer]) || (l.kind === "binreg" && !BINREG_PRIM) || (l.kind === "smartbins" && !SB_PRIM) || (l.kind === "houses" && !HOUSE_PRIMS.length))) { loadLayer(l); return; } if (DS[cb.dataset.layer]) DS[cb.dataset.layer].show = l.on; if (l.kind === "houses" || l.kind === "binreg" || l.kind === "smartbins") applyLod(); /* solids and painted roofs together, and the bins at once: until 29 Sep 2026 the roofs stayed on the ground after the layer was unticked */ if (l.flood === "cells") CELL_PRIMS.forEach(function (p) { p.show = l.on; }); if (l.kind === "roads") { twinRoads(!l.on); ROAD_PRIMS.forEach(function (p) { p.show = l.on; }); } viewer.scene.requestRender(); if (l.key === "landuse_agriculture" && FARM_DS) FARM_DS.show = l.on; if (l.kind === "custom") sectorBorders(l.on); }; });
  }
  function fly(f) { var g = f.geometry; if (!viewer) return; if (g.type === "Point") viewer.camera.flyTo({ destination: C.Cartesian3.fromDegrees(g.coordinates[0], g.coordinates[1], 900), duration: 1.2 }); else { var ring = g.type === "Polygon" ? g.coordinates[0] : g.coordinates; var lons = ring.map(function (c) { return c[0]; }), lats = ring.map(function (c) { return c[1]; }); viewer.camera.flyTo({ destination: C.Rectangle.fromDegrees(Math.min.apply(null, lons) - 0.002, Math.min.apply(null, lats) - 0.002, Math.max.apply(null, lons) + 0.002, Math.max.apply(null, lats) + 0.002), duration: 1.2 }); } showSelection(f.properties); }
  function renderList(id, feats) { var el = $(id); el.innerHTML = feats.length ? feats.slice(0, 300).map(function (f, i) { return '<div data-i="' + i + '"><b>' + esc(f.properties.name) + '</b> <small style="color:var(--ink-faint)">' + esc((f.properties.attributes && f.properties.attributes.osm && (f.properties.attributes.osm.amenity || f.properties.attributes.osm.shop || f.properties.attributes.osm.healthcare)) || f.properties.origin) + '</small></div>'; }).join("") : '<div style="color:var(--ink-faint)">none on the platform yet</div>'; el.querySelectorAll("div[data-i]").forEach(function (d) { d.onclick = function () { fly(feats[+d.dataset.i]); }; }); }
  function renderBins() { var feats = FEATS.container || []; $("bin-count").textContent = feats.length; var el = $("bin-list"); el.innerHTML = feats.length ? feats.map(function (f, i) { var a = f.properties.attributes || {}; return '<div data-i="' + i + '"><b>' + esc(f.properties.name) + '</b> · ' + esc(a.type || "") + ' ' + esc(a.stream || "") + ' × ' + esc(a.units || 1) + ' <small style="color:var(--ink-faint)">' + esc(f.properties.origin) + (f.properties.hAccuracyM ? " ±" + f.properties.hAccuracyM + " m" : "") + '</small></div>'; }).join("") : '<div style="color:var(--ink-faint)">no bins yet: place the first one</div>'; el.querySelectorAll("div[data-i]").forEach(function (d) { d.onclick = function () { fly(feats[+d.dataset.i]); }; }); }
  function showSelection(p) {
    if (!p) { $("sel").innerHTML = ""; return; }
    if (p.flood) { $("sel").innerHTML = floodSelectionHtml(p); document.querySelector('nav button[data-tab="select"]').click(); return; }
    var a = p.attributes || {};
    var reg = a.register; var regHtml = reg ? '<h2>RAKEZ register, ' + esc(reg.asOf) + '</h2><dl class="kv"><dt>Companies</dt><dd>' + esc(reg.companies) + '</dd><dt>Service locations</dt><dd>' + esc(reg.serviceLocations) + '</dd><dt>Contract value</dt><dd>AED ' + Number(reg.contractValueAED || 0).toLocaleString("en-GB") + '</dd><dt>Volume to date</dt><dd>' + Number(reg.volumeToDateCbm || 0).toLocaleString("en-GB") + ' cbm</dd><dt>Lifts to date</dt><dd>' + Number(reg.liftsToDate || 0).toLocaleString("en-GB") + '</dd><dt>By zone</dt><dd>' + Object.keys(reg.byZone || {}).map(function (k) { return esc(k) + " " + reg.byZone[k]; }).join(", ") + '</dd><dt>By stream</dt><dd>' + Object.keys(reg.byStream || {}).map(function (k) { return esc(k) + " " + reg.byStream[k]; }).join(", ") + '</dd><dt>Top activities</dt><dd>' + Object.keys(reg.byActivity || {}).slice(0, 6).map(function (k) { return esc(k) + " " + reg.byActivity[k]; }).join(", ") + '</dd><dt>Expiry</dt><dd>' + esc(reg.expiring) + '</dd><dt>Source</dt><dd>' + esc(reg.source) + '</dd></dl>' : "";
    $("sel").innerHTML = regHtml + '<dl class="kv"><dt>Name</dt><dd><b>' + esc(p.name) + '</b></dd><dt>Class</dt><dd>' + esc(p.class) + '</dd><dt>Origin</dt><dd><span class="pill ' + (p.origin === "surveyed" ? "ok" : /estimated|user_submitted|derived|reference/.test(p.origin) ? "warn" : "") + '">' + esc(p.origin) + '</span> ' + esc(p.verificationStatus) + '</dd><dt>Accuracy</dt><dd>' + esc(p.accuracyClass || "") + (p.hAccuracyM ? " · ±" + p.hAccuracyM + " m" : "") + '</dd><dt>Source</dt><dd>' + esc(p.source) + (p.sourceRef ? " · " + esc(p.sourceRef) : "") + '</dd><dt>Captured</dt><dd>' + esc(p.captureDate || "not stated") + '</dd><dt>Id</dt><dd style="font-family:var(--mono)">' + esc(p.id) + '</dd>' + Object.keys(a).filter(function (k) { return k !== "osm" && k !== "stops" && k !== "register"; }).map(function (k) { return '<dt>' + esc(k) + '</dt><dd>' + esc(typeof a[k] === "object" ? JSON.stringify(a[k]) : a[k]) + '</dd>'; }).join("") + (a.osm ? Object.keys(a.osm).map(function (k) { return '<dt>osm ' + esc(k) + '</dt><dd>' + esc(a.osm[k]) + '</dd>'; }).join("") : "") + (a.stops ? '<dt>stops</dt><dd>' + a.stops.length + ' bins, ' + esc(a.distanceM) + ' m, load ' + esc(a.load) + '/' + esc(a.capacity) + '</dd>' : "") + '</dl>';
    document.querySelector('nav button[data-tab="select"]').click();
  }

  // ------------------------------------------------------------ capture
  var capture = false; var handler = null;
  function setCapture(on) { capture = on; $("capture-hint").style.display = on ? "block" : "none"; $("bin-capture").textContent = on ? "Cancel placing" : "Place a bin"; }
  function groundPoint(pos) { var scene = viewer.scene; var cart = scene.pickPosition && scene.pickPositionSupported ? scene.pickPosition(pos) : undefined; if (!C.defined(cart)) { var ray = viewer.camera.getPickRay(pos); cart = ray ? scene.globe.pick(ray, scene) : undefined; } if (!C.defined(cart)) return null; var g = C.Cartographic.fromCartesian(cart); return { lon: C.Math.toDegrees(g.longitude), lat: C.Math.toDegrees(g.latitude), h: g.height }; }
  async function placeBin(pt) {
    var units = Math.max(1, +$("bin-units").value || 1); var type = $("bin-type").value, stream = $("bin-stream").value, note = $("bin-note").value.trim();
    var name = type + " " + stream.toLowerCase() + (note ? " · " + note : "") + " (" + pt.lat.toFixed(5) + ", " + pt.lon.toFixed(5) + ")";
    try {
      var f = await api.addFeature({ "class": "container", name: name.slice(0, 200), geometry: { type: "Point", coordinates: [pt.lon, pt.lat, pt.h || 0] }, origin: "user_submitted", source: "placed on the WM Twin Map by " + (USER ? USER.name : "unknown"), hAccuracyM: 5, captureDate: new Date().toISOString().slice(0, 10), surveyMethod: "on screen placement over imagery", attributes: { type: type, stream: stream, units: units, note: note, kind: "bin" } });
      status("Bin " + f.id + " saved on the platform (user submitted, ±5 m)");
      await loadLayer(LAYERS[0]);
    } catch (e) { var f2 = e.error && e.error.fields ? e.error.fields.map(function (x) { return x.message; }).join("; ") : ""; status("Refused: " + e.message + (f2 ? " · " + f2 : "")); }
  }
  var draw = { on: false, pts: [], ent: null };
  function setDraw(on) { draw.on = on; draw.pts = []; if (draw.ent) { viewer.entities.remove(draw.ent); draw.ent = null; } $("area-draw").textContent = on ? "Cancel drawing" : "Draw an area"; $("area-finish").disabled = !on; $("area-hint").textContent = on ? "Click the corners on the map, then press Finish (3 corners or more). Esc cancels." : ""; if (on) { $("capture-hint").style.display = "block"; $("capture-hint").textContent = "Click the corners of the area, then Finish. Esc cancels"; } else { $("capture-hint").style.display = "none"; $("capture-hint").textContent = "Click on the map to place a bin. Esc cancels"; } }
  function drawPreview() { if (!draw.ent) draw.ent = viewer.entities.add({ polyline: { positions: new C.CallbackProperty(function () { return draw.pts.map(function (p) { return C.Cartesian3.fromDegrees(p.lon, p.lat); }); }, false), width: 3, material: C.Color.fromCssColorString("#C77DFF"), clampToGround: true } }); }
  async function finishArea() {
    if (draw.pts.length < 3) { $("area-hint").textContent = "An area needs at least three corners."; return; }
    var cls = $("area-class").value, name = $("area-name").value.trim(), contract = $("area-contract").value.trim(), note = $("area-note").value.trim();
    if (!name) { $("area-hint").textContent = "Give the area a name first."; return; }
    var ring = draw.pts.map(function (p) { return [p.lon, p.lat]; }); ring.push(ring[0]);
    try {
      var f = await api.addFeature({ "class": cls, name: name.slice(0, 200), geometry: { type: "Polygon", coordinates: [ring] }, origin: "user_submitted", source: "drawn on the WM Twin Map by " + (USER ? USER.name : "unknown"), hAccuracyM: 5, captureDate: new Date().toISOString().slice(0, 10), surveyMethod: "on screen digitising over imagery", attributes: { kind: cls.replace(/^landuse_/, ""), contract: contract || undefined, note: note || undefined } });
      status("Area " + f.id + " saved (" + cls + ", user submitted, about 5 m)"); setDraw(false); var l = LAYERS.find(function (x) { return x.key === cls; }); if (l) await loadLayer(l); showSelection(f);
    } catch (e) { var f2 = e.error && e.error.fields ? e.error.fields.map(function (x) { return x.message; }).join("; ") : ""; $("area-hint").textContent = "Refused: " + e.message + (f2 ? " · " + f2 : ""); }
  }
  // ---------------------------------------------------------------- the map controls: card, filters, zoom, corner views
  var CARD = null, CARD_T = null, PRIO_FOR_CARD = null;
  function cardSoon() { if (!CARD) return; if (CARD_T) clearTimeout(CARD_T); CARD_T = setTimeout(function () { CARD_T = null; updateCard(); }, 350); }
  function esc2(s) { return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]; }); }
  // what stands inside the view: the named area most of it belongs to, the buildings by type, the bins, the priority
  function updateCard() {
    if (!CARD || !viewer) return;
    var f = function (n) { return (+n || 0).toLocaleString("en-GB"); };
    if (!HOUSES) { CARD.innerHTML = '<h4>Ras Al Khaimah</h4><div class="tm-hint">Tick Houses and buildings for the count under the view.</div>'; return; }
    var h = cameraHeight(), bb = viewBbox();
    if (!bb || h >= LOD.housesFlat) {
      var cs = HOUSES.meta && HOUSES.meta.counts || {};
      CARD.innerHTML = '<h4>Ras Al Khaimah, nine sectors</h4>' + row("Buildings", f(HOUSES.houses.length), true) + row("Houses with a bin", f(cs.houses)) + row("Bins standing today", BINREG ? f((BINREG.meta.withdrawal || {}).standing || BINREG.meta.counts.placed) : "tick the register")
        + '<div class="tm-hint">Zoom in for the area under the view.</div>';
      return;
    }
    buildGrid();
    var by = {}, area = {}, total = 0, seen = 0;
    for (var gx = Math.floor(bb[0] * 100); gx <= Math.floor(bb[2] * 100); gx++) for (var gy = Math.floor(bb[1] * 100); gy <= Math.floor(bb[3] * 100); gy++) {
      var cell = HOUSE_GRID[gx + "|" + gy]; if (!cell) continue;
      for (var j = 0; j < cell.length; j++) { var r = HOUSES.houses[cell[j]], p = r[2]; if (p[0] < bb[0] || p[0] > bb[2] || p[1] < bb[1] || p[1] > bb[3]) continue;
        if (++seen > 80000) break; if (!houseVisible(r)) continue; total++; var k = houseKey(r); by[k] = (by[k] || 0) + 1; if (r[9] != null) area[r[9]] = (area[r[9]] || 0) + 1; }
    }
    var ids = Object.keys(area).sort(function (a, b) { return area[b] - area[a]; }), named = (HOUSES.meta && HOUSES.meta.namedAreas) || [];
    var top = ids.length && named[+ids[0]] ? named[+ids[0]] : null;
    var title = top ? esc2(top.name) + (top.sub ? ' <span class="tm-sub">' + esc2(top.sub) + '</span>' : "") : "Outside the named areas";
    var html = '<h4>' + title + '</h4>' + (ids.length > 1 ? '<div class="tm-hint">' + ids.length + ' named areas in view; the largest is named</div>' : "");
    html += row("Buildings in view", f(total), true);
    HOUSE_TYPES.forEach(function (t) { if (by[t.code]) html += row('<i style="background:' + t.colour + '"></i>' + t.code + " " + esc2(t.label.split(",")[0]), f(by[t.code])); });
    ((HOUSES.meta && HOUSES.meta.categories) || []).forEach(function (c) { if (c.kind >= 1 && by["K" + c.kind]) html += row('<i style="background:' + (c.colour || "#7C8794") + '"></i>' + esc2(c.code || c.label || ("kind " + c.kind)), f(by["K" + c.kind])); });
    if (BINREG) { var nb = 0; BINREG.bins.forEach(function (b) { if (!b[10] && b[0] >= bb[0] && b[0] <= bb[2] && b[1] >= bb[1] && b[1] <= bb[3]) nb++; }); html += row("Bins standing in view", f(nb)); }
    if (SB && SB_PRIM) { var ns = 0, late = 0; SB.bins.forEach(function (b) { if (b[0] >= bb[0] && b[0] <= bb[2] && b[1] >= bb[1] && b[1] <= bb[3]) { ns++; var a = sbAgeDays(b); if (a != null && a >= 7) late++; } });
      if (ns) html += row("Smart bins (RFID) in view", f(ns)) + row("Unread 7 days or more, " + sbExportDay(), f(late)); }
    if (top && PRIO_FOR_CARD) { var pa = PRIO_FOR_CARD[top.name]; if (pa) html += row("Priority (RPT-13)", esc2(pa.priority) + ", rank " + pa.rank); }
    else if (top && PRIO_FOR_CARD === null) { PRIO_FOR_CARD = false; fetch("omcc-source/area_priority.json?v=" + HOUSES_VERSION).then(function (r) { return r.json(); }).then(function (d) { PRIO_FOR_CARD = {}; d.areas.forEach(function (a) { PRIO_FOR_CARD[a.name] = a; }); cardSoon(); }).catch(function () {}); }
    CARD.innerHTML = html;
    function row(k, v, strong) { return '<div class="tm-row' + (strong ? ' tm-strong' : '') + '"><span>' + k + '</span><b>' + v + '</b></div>'; }
  }
  // a corner view: the camera flies to that side of the point it is looking at, and looks back at it from 42 degrees
  function flyCorner(dx, dy) {
    if (!viewer) return; TOUCHED = true;
    var f = focusLonLat(), h = Math.min(20000, Math.max(400, cameraHeight())), d = h;
    var lat = f[1] + dy * d / 111000, lon = f[0] + dx * d / (111000 * Math.cos(f[1] * Math.PI / 180));
    var heading = Math.atan2(-dx, -dy);   // from the corner back towards the focus
    viewer.camera.cancelFlight();
    viewer.camera.flyTo({ destination: C.Cartesian3.fromDegrees(lon, lat, h * 0.9), orientation: { heading: heading, pitch: C.Math.toRadians(-42), roll: 0 }, duration: 1.2 });
  }
  function installMapControls() {
    var scene = document.getElementById("twin-scene"); if (!scene || document.getElementById("tm-card")) return;
    var wrap = document.createElement("div"); wrap.id = "tm-controls";
    wrap.innerHTML = '<div id="tm-card" class="tm-panel"></div>'
      + '<div class="tm-panel tm-filters"><button type="button" id="tm-filter-btn">Filters <span>&#9662;</span></button><div id="tm-filter-body" hidden></div></div>'
      + '<div class="tm-zoom"><button type="button" data-z="in" title="Zoom in">+</button><span>Zoom</span><button type="button" data-z="out" title="Zoom out">&minus;</button></div>'
      + '<button type="button" class="tm-view tm-nw" data-dx="-1" data-dy="1"><i></i>North-West view</button>'
      + '<button type="button" class="tm-view tm-ne" data-dx="1" data-dy="1"><i></i>North-East view</button>'
      + '<button type="button" class="tm-view tm-sw" data-dx="-1" data-dy="-1"><i></i>South-West view</button>'
      + '<button type="button" class="tm-view tm-se" data-dx="1" data-dy="-1"><i></i>South-East view</button>';
    scene.appendChild(wrap);
    CARD = document.getElementById("tm-card");
    wrap.querySelectorAll(".tm-zoom button").forEach(function (b) { b.onclick = function () { TOUCHED = true; var h = cameraHeight(); if (b.dataset.z === "in") viewer.camera.zoomIn(h * 0.45); else viewer.camera.zoomOut(h * 0.8); viewer.scene.requestRender(); }; });
    wrap.querySelectorAll(".tm-view").forEach(function (b) { b.onclick = function () { flyCorner(+b.dataset.dx, +b.dataset.dy); }; });
    var btn = document.getElementById("tm-filter-btn"), body = document.getElementById("tm-filter-body");
    btn.onclick = function () { body.hidden = !body.hidden; if (!body.hidden) renderFilters(); };
    function renderFilters() {
      var keys = HOUSE_TYPES.map(function (t) { return { key: t.code, label: t.code + " " + t.label, colour: t.colour }; });
      ((HOUSES && HOUSES.meta && HOUSES.meta.categories) || []).forEach(function (c) { if (c.kind >= 1) keys.push({ key: "K" + c.kind, label: (c.code ? c.code + " " : "") + (c.label || "kind " + c.kind), colour: c.colour || "#7C8794" }); });
      body.innerHTML = keys.map(function (k) { var on = !HOUSE_FILTER || HOUSE_FILTER.has(k.key); return '<label><input type="checkbox" data-key="' + k.key + '"' + (on ? ' checked' : '') + '><i style="background:' + k.colour + '"></i>' + esc2(k.label) + '</label>'; }).join("")
        + '<label class="tm-sep"><input type="checkbox" id="tm-ids"' + (ID_ON ? ' checked' : '') + '>Codes on the roofs within 700 m</label>'
        + '<label><input type="checkbox" id="tm-real"' + (REAL ? ' checked' : '') + '>Real look: white walls, windows, flat roofs, true heights, asphalt roads</label>'
        + '<div class="tm-hint">' + (HOUSES ? "Unticked types leave the map; the card counts what is drawn." : "Tick Houses and buildings first.") + '</div>';
      body.querySelectorAll("input[data-key]").forEach(function (cb) { cb.onchange = function () {
        var all = keys.map(function (k) { return k.key; }), on = []; body.querySelectorAll("input[data-key]").forEach(function (x) { if (x.checked) on.push(x.dataset.key); });
        HOUSE_FILTER = on.length === all.length ? null : new Set(on); if (HOUSES) rebuildHouses(HOUSE_COLOUR); cardSoon();
      }; });
      var idc = document.getElementById("tm-ids"); idc.onchange = function () { ID_ON = idc.checked; setPref("twinmap-ids", ID_ON ? "on" : "off"); if (HOUSES) rebuildHouses(HOUSE_COLOUR); };
      // the look redraws the solids and the footprints: the footprints carry the roof colours in the real look and the type colours in the diagram
      var rc = document.getElementById("tm-real"); rc.onchange = function () { REAL = rc.checked; setPref("twinmap-style", REAL ? "real" : "map"); applyDetail(); roadPicture(); if (HOUSES) rebuildHouses(HOUSE_COLOUR); };
    }
    viewer.camera.moveEnd.addEventListener(cardSoon);
    updateCard();
  }
  function installPicking() {
    handler = new C.ScreenSpaceEventHandler(viewer.scene.canvas);
    handler.setInputAction(function (m) {
      if (draw.on) { if (!can("edit")) { status("Sign in with an editing role to draw areas"); return; } var pt2 = groundPoint(m.position); if (pt2) { draw.pts.push(pt2); drawPreview(); $("area-hint").textContent = draw.pts.length + " corner" + (draw.pts.length > 1 ? "s" : "") + " placed; press Finish when the area is closed."; } return; }
      if (capture) { if (!can("edit")) { status("Sign in with an editing role to place bins"); return; } var pt = groundPoint(m.position); if (pt) placeBin(pt); setCapture(false); return; }
      var picked = viewer.scene.pick(m.position); var e = picked && picked.id; if (e && e.twinProps) { showSelection(e.twinProps); } else if (typeof e === "string" && e.indexOf("cell|") === 0) { floodCellSelect(+e.split("|")[1]); } else if (typeof e === "string" && e.indexOf("pub|") === 0) { publicBuildingSelect(+e.split("|")[1]); } else if (typeof e === "string" && e.indexOf("bin|") === 0) { binRegisterSelect(+e.split("|")[1]); } else if (typeof e === "string" && e.indexOf("rfid|") === 0) { smartBinSelect(+e.split("|")[1]); } else { var gp = groundPoint(m.position); if (gp && houseAt(gp.lon, gp.lat)) return; if (gp && CELLS && CELLS.length) floodCellAt(gp.lon, gp.lat); }
    }, C.ScreenSpaceEventType.LEFT_CLICK);
    document.addEventListener("keydown", function (ev) { if (ev.key === "Escape") { setCapture(false); if (draw.on) setDraw(false); } });
  }

  // ------------------------------------------------------------ land use, routes
  function sectorsBbox() { var g = window.__wmaTwin && window.__wmaTwin.toGeoJSON ? window.__wmaTwin.toGeoJSON() : null; var w = 180, s = 90, e = -180, n = -90; if (g && g.features) g.features.forEach(function (f) { var rings = f.geometry.type === "Polygon" ? f.geometry.coordinates[0] : []; rings.forEach(function (c) { w = Math.min(w, c[0]); e = Math.max(e, c[0]); s = Math.min(s, c[1]); n = Math.max(n, c[1]); }); }); if (w > e) return [55.70, 25.40, 56.30, 26.10]; return [w - 0.01, s - 0.01, e + 0.01, n + 0.01]; }
  async function importLanduse() {
    if (!can("configure")) { $("lu-result").innerHTML = '<div class="err">Importing needs the Administrator role.</div>'; return; }
    $("lu-result").innerHTML = '<div class="note">Asking OpenStreetMap for the nine sectors… this takes up to a minute; the raw answer is kept as a source file.</div>';
    try { var r = await api.importLanduse(sectorsBbox()); $("lu-result").innerHTML = '<div class="note">Batch ' + esc(r.batch) + ': ' + r.counts.new + ' new, ' + r.counts.skipped + ' already known, ' + r.counts.unclassified + ' unclassified, ' + r.counts.nogeom + ' without usable geometry.<br>' + Object.keys(r.byClass).map(function (k) { return esc(k) + " " + r.byClass[k]; }).join(" · ") + '<br><small>' + esc(r.attribution) + '</small></div>'; await reloadAll(); }
    catch (e) { $("lu-result").innerHTML = '<div class="err">' + esc(e.message) + '</div>'; }
  }
  function viewBbox() { var rect = viewer.camera.computeViewRectangle(viewer.scene.globe.ellipsoid); if (!rect) return null; return [C.Math.toDegrees(rect.west), C.Math.toDegrees(rect.south), C.Math.toDegrees(rect.east), C.Math.toDegrees(rect.north)]; }
  function preferAlMullah() { var sel = $("rt-depot"); if (!sel) return; for (var i = 0; i < sel.options.length; i++) { if (/mullah|mulla/i.test(sel.options[i].textContent)) { sel.selectedIndex = i; return; } } }
  function fillDepots() {
    var geo = window.__wmaTwin && window.__wmaTwin.toGeoJSON ? null : null; var sel = $("rt-depot"); sel.innerHTML = "";
    var fac = (window.__twinFacilities || []); fac.forEach(function (f) { var o = document.createElement("option"); o.value = f.lon + "," + f.lat; o.textContent = f.name + " (" + f.layer + ")"; sel.appendChild(o); });
    var o2 = document.createElement("option"); o2.value = "view"; o2.textContent = "Centre of the current view"; sel.appendChild(o2);
  }
  async function optimise() {
    if (!can("edit")) { $("rt-result").innerHTML = '<div class="err">Optimising needs an editing role.</div>'; return; }
    var dv = $("rt-depot").value; var depot; if (dv === "view") { var b = viewBbox(); if (!b) return; depot = { lon: (b[0] + b[2]) / 2, lat: (b[1] + b[3]) / 2, name: "view centre" }; } else { var p = dv.split(","); depot = { lon: +p[0], lat: +p[1], name: $("rt-depot").selectedOptions[0].textContent }; }
    var dep = ($("rt-start") && $("rt-start").value) || "07:00"; var hour = parseInt(dep.split(":")[0], 10);
    var body = { name: $("rt-name").value.trim() || undefined, depot: depot, stopClass: "container", vehicles: +$("rt-veh").value || 2, capacity: +$("rt-cap").value || 40, demandAttribute: "units", timeLimitS: 15, hour: isNaN(hour) ? 7 : hour, start: dep, serviceS: +($("rt-service") && $("rt-service").value) || 60, unloadS: (+($("rt-unload-min") && $("rt-unload-min").value) || 15) * 60 };
    if ($("rt-source") && $("rt-source").value === "proposed") body.scenario = "SC-BINS-HOUSES-20260914";
    if ($("rt-source") && $("rt-source").value === "houses") { var hv = housesInView(400); if (!hv.total) { $("rt-result").innerHTML = '<div class="err">No houses in the current view; zoom to a public collection area.</div>'; return; } if (hv.total > 400) { $("rt-result").innerHTML = '<div class="err">' + hv.total + ' houses in view; a plan takes at most 400 stops. Zoom in to a neighbourhood and run one plan per neighbourhood.</div>'; return; } body.stops = hv.stops; body.bbox = undefined; }
    if ($("rt-max").value) body.maxRouteKm = +$("rt-max").value;
    if ($("rt-stops").value === "view") { body.bbox = viewBbox(); }
    $("rt-result").innerHTML = '<div class="note">Snapping bins to the road graph and solving the vehicle routing problem (about 10 s)…</div>';
    try { var plan = await api.optimise(body); currentPlan = plan; LAST_PLAN = plan; renderPlanResult(plan); await loadLayer(LAYERS.find(function (l) { return l.key === "collection_route"; }), plan.id); await loadPlans(); }
    catch (e) { $("rt-result").innerHTML = '<div class="err">' + esc(e.message) + '</div>'; }
  }
  var LAST_PLAN = null; var SIM = { ds: null, running: false };
  function hms(s) { s = Math.round(s); return Math.floor(s / 3600) + " h " + ("0" + Math.floor((s % 3600) / 60)).slice(-2) + " min"; }
  function renderPlanResult(plan) {
    var traffic = plan.peak ? "peak hour traffic on main roads" : "free flow traffic";
    $("rt-result").innerHTML = '<div class="note"><b>' + esc(plan.name) + '</b> · ' + plan.stopsServed + ' stops, ' + plan.routes.length + ' routes, ' + (plan.totalDistanceM / 1000).toFixed(1) + ' km, ' + hms(plan.totalDurationS || 0) + ' vehicle time · start ' + esc(plan.start || "07:00") + ', ' + traffic + (plan.stopsDropped.length ? ' · <span class="pill bad">' + plan.stopsDropped.length + ' dropped</span>' : '') + '<br>' +
      plan.routes.map(function (r) { return '<span style="color:' + ROUTE_COLORS[(r.vehicle - 1) % ROUTE_COLORS.length] + '">■</span> vehicle ' + r.vehicle + ': ' + r.stops.length + ' stops, ' + (r.distanceM / 1000).toFixed(1) + ' km, ' + esc(r.start || "") + ' to ' + esc(r.end || "") + ' (' + hms(r.durationS || 0) + '), load ' + r.load + '/' + r.capacity + (r.overLengthCap ? ' <span class="pill bad">over length cap</span>' : ''); }).join("<br>") +
      '<div class="row" style="margin-top:6px"><button class="btn" id="sim-play">Simulate the drive</button><button class="btn ghost" id="sim-stop">Stop</button><label style="margin:0 6px">speed <select id="sim-speed"><option value="30">30 x</option><option value="60" selected>60 x</option><option value="180">180 x</option></select></label><span id="sim-clock" style="color:var(--ink-soft)"></span></div>' +
      '<small>' + esc(plan.method) + '</small></div>';
    $("sim-play").onclick = function () { simulate(plan); }; $("sim-stop").onclick = stopSim;
  }
  function stopSim() { if (SIM.ds) { viewer.dataSources.remove(SIM.ds, true); SIM.ds = null; } viewer.clock.shouldAnimate = false; SIM.running = false; if (SIM.tick) { viewer.clock.onTick.removeEventListener(SIM.tick); SIM.tick = null; } if ($("sim-clock")) $("sim-clock").textContent = ""; }
  function simulate(plan) {
    if (!viewer) return; stopSim();
    var ds = new C.CustomDataSource("simulation"); var startParts = (plan.start || "07:00").split(":"); var today = new Date(); today.setHours(+startParts[0] || 7, +startParts[1] || 0, 0, 0);
    var t0 = C.JulianDate.fromDate(today); var maxS = 0;
    plan.routes.forEach(function (r) {
      var coords = r.geometry.coordinates; if (!coords || coords.length < 2) return;
      var cum = [0]; for (var i = 1; i < coords.length; i++) { var dx = (coords[i][0] - coords[i - 1][0]) * 111320 * Math.cos(coords[i][1] * Math.PI / 180), dy = (coords[i][1] - coords[i - 1][1]) * 110540; cum.push(cum[i - 1] + Math.hypot(dx, dy)); }
      var total = cum[cum.length - 1] || 1; var travel = r.travelS || (total / 6), serviceEach = (r.serviceS || 0) / Math.max(1, r.stops.length), stopsN = r.stops.length;
      var pos = new C.SampledPositionProperty(); var stopEvery = total / Math.max(1, stopsN); var nextStop = stopEvery; var clock = 0;
      for (var i = 0; i < coords.length; i++) {
        if (i > 0) { clock += (cum[i] - cum[i - 1]) / total * travel; while (cum[i] >= nextStop && nextStop <= total) { pos.addSample(C.JulianDate.addSeconds(t0, clock, new C.JulianDate()), C.Cartesian3.fromDegrees(coords[i][0], coords[i][1])); clock += serviceEach; nextStop += stopEvery; } }
        pos.addSample(C.JulianDate.addSeconds(t0, clock, new C.JulianDate()), C.Cartesian3.fromDegrees(coords[i][0], coords[i][1]));
      }
      clock += (r.unloadS || 0); if (clock > maxS) maxS = clock;
      pos.setInterpolationOptions({ interpolationDegree: 1, interpolationAlgorithm: C.LinearApproximation });
      var col = C.Color.fromCssColorString(ROUTE_COLORS[(r.vehicle - 1) % ROUTE_COLORS.length]);
      ds.entities.add({ position: pos, orientation: new C.VelocityOrientationProperty(pos), box: { dimensions: new C.Cartesian3(7, 2.6, 3.2), material: col, outline: true, outlineColor: C.Color.BLACK, heightReference: C.HeightReference.CLAMP_TO_GROUND },
        label: { text: "V" + r.vehicle, font: "bold 13px Calibri, sans-serif", fillColor: C.Color.WHITE, outlineColor: C.Color.BLACK, outlineWidth: 3, style: C.LabelStyle.FILL_AND_OUTLINE, pixelOffset: new C.Cartesian2(0, -22), disableDepthTestDistance: Number.POSITIVE_INFINITY },
        path: { resolution: 5, material: new C.PolylineGlowMaterialProperty({ glowPower: 0.2, color: col }), width: 8, leadTime: 0, trailTime: 1800 } });
    });
    SIM.ds = ds; viewer.dataSources.add(ds);
    viewer.clock.startTime = t0.clone(); viewer.clock.currentTime = t0.clone(); viewer.clock.stopTime = C.JulianDate.addSeconds(t0, maxS + 60, new C.JulianDate()); viewer.clock.clockRange = C.ClockRange.CLAMPED; viewer.clock.multiplier = +($("sim-speed") && $("sim-speed").value) || 60; viewer.clock.shouldAnimate = true; SIM.running = true;
    SIM.tick = function (clk) { var el = $("sim-clock"); if (el) { var d = C.JulianDate.toDate(clk.currentTime); el.textContent = "clock " + ("0" + d.getHours()).slice(-2) + ":" + ("0" + d.getMinutes()).slice(-2) + " · " + (viewer.clock.multiplier) + " x"; } };
    viewer.clock.onTick.addEventListener(SIM.tick);
    if ($("sim-speed")) $("sim-speed").onchange = function () { viewer.clock.multiplier = +$("sim-speed").value; };
  }
  async function loadPlans() { try { var plans = await api.plans(); $("rt-plans").innerHTML = plans.length ? plans.map(function (p) { var T = p.summary.totals; return '<div data-id="' + esc(p.id) + '"><b>' + esc(p.name) + '</b> · ' + (p.summary.kind === "area design" && T ? T.vehicles + ' vehicles, ' + T.trips + ' trips, ' + (T.houses || 0).toLocaleString("en-GB") + ' houses' : p.routes + ' routes') + ' · ' + ((p.summary.totalDistanceM || 0) / 1000).toFixed(1) + ' km · <span class="pill">' + esc(p.status) + '</span><small style="color:var(--ink-faint)"> ' + esc(p.createdAt.slice(0, 16).replace("T", " ")) + '</small></div>'; }).join("") : '<div style="color:var(--ink-faint)">no plans yet</div>'; $("rt-plans").querySelectorAll("div[data-id]").forEach(function (d) { d.onclick = async function () { currentPlan = d.dataset.id; loadLayer(LAYERS.find(function (l) { return l.key === "collection_route"; }), currentPlan); try { var p = await api.plan(d.dataset.id); if (p && p.routes) { p.routes.forEach(function (r) { var a = r.attributes || {}; r.geometry = r.geometry || a.geometry; r.travelS = r.travelS || a.travelS; r.serviceS = r.serviceS || 0; r.durationS = r.durationS || a.durationS; r.start = r.start || a.start; r.end = r.end || a.end; }); LAST_PLAN = p; renderPlanResult(p); } } catch (e) {} }; }); } catch (e) {} }
  // ------------------------------------------------------------ route design: the routes of a whole collection area
  // Every house of a named area, or of a whole sub sector, goes to the platform's designer (POST /v1/om/twin/routes/design).
  // Houses on one stretch of road become one street stop; a trip carries at most a truck's load and ends at the tipping
  // site; trips fill vehicles to the length of a shift. The inputs come prefilled from omcc-source/collection_baseline.json
  // (database/seeds/build_collection_baseline.py: the weighbridge tickets of January to June 2026 and the fleet register),
  // each with its source, and every one can be changed. A house the roads do not reach is listed and marked, never routed.
  var BASELINE = null, BASELINE_VERSION = "20260927a", DZ = { plan: null, marks: null, filled: false, fac: [], rows: null };
  var EMIRATE_KG = 6.65;   // kg a house a day: RPT-13, the 2024 MSW tonnage over 111,614 houses, reproduced by the population route
  var VEH_LABEL = { "BIG REAR LOADER": "Big rear loader", "FRONT LOADER": "Front loader", "SMALL REAR LOADER": "Small rear loader" };
  var FREQ_LABEL = { 7: "daily", 6: "six a week", 3: "three a week", 2: "twice a week", 1: "once a week" };
  var DAY_GROUPS = { 1: ["Saturday", "Sunday", "Monday", "Tuesday", "Wednesday", "Thursday"], 2: ["Saturday and Tuesday", "Sunday and Wednesday", "Monday and Thursday"], 3: ["Saturday, Monday and Wednesday", "Sunday, Tuesday and Thursday"] };
  function dzGroups(freq) { return freq >= 6 ? [freq === 6 ? "Saturday to Thursday" : "every day"] : (DAY_GROUPS[freq] || ["every collection day"]); }
  function dzN(n) { return (+n || 0).toLocaleString("en-GB"); }
  async function fetchBaseline() { if (!BASELINE) { try { BASELINE = await (await fetch("omcc-source/collection_baseline.json?v=" + BASELINE_VERSION)).json(); } catch (e) { BASELINE = null; } } return BASELINE; }
  function dzSub(code) { return (BASELINE && BASELINE.subSectors.find(function (s) { return s.code === code; })) || null; }
  function dzVehicle(type) { return (BASELINE && BASELINE.vehicleTypes.find(function (v) { return v.type === type; })) || null; }
  function dzAssigned(type, code) {   // units of this type the register assigns to the sub sector ("8A & 9A" counts for both)
    var n = 0; ((BASELINE && BASELINE.fleet) || []).forEach(function (f) { if (f.ticketType !== type) return; Object.keys(f.bySector || {}).forEach(function (k) { if (k.toUpperCase().split(/[^0-9A-Z]+/).indexOf(code) >= 0) n += f.bySector[k]; }); });
    return n;
  }
  function dzFleet(type) { var u = 0, w = 0; ((BASELINE && BASELINE.fleet) || []).forEach(function (f) { if (f.ticketType === type) { u += f.units; w += f.working; } }); return { units: u, working: w }; }
  function dzIndex() {
    if (DZ.rows) return;
    var named = (HOUSES.meta && HOUSES.meta.namedAreas) || [], byArea = {};
    HOUSES.houses.forEach(function (r, i) { if ((r[4] || 0) !== 0 || r[9] == null) return; (byArea[r[9]] || (byArea[r[9]] = [])).push(i); });
    var bySub = {};
    named.forEach(function (a) { if (!byArea[a.id]) return; var s = a.sub || ("S" + (a.sector || "?")); (bySub[s] || (bySub[s] = [])).push(a); });
    DZ.named = named; DZ.rows = byArea; DZ.bySub = bySub;
  }
  function dzUnderView() {   // the named area with the most houses under the view, as the card finds it
    var bb = viewBbox(); if (!bb || cameraHeight() > 30000) return null;
    buildGrid(); var count = {}, seen = 0;
    for (var gx = Math.floor(bb[0] * 100); gx <= Math.floor(bb[2] * 100); gx++) for (var gy = Math.floor(bb[1] * 100); gy <= Math.floor(bb[3] * 100); gy++) {
      var cell = HOUSE_GRID[gx + "|" + gy]; if (!cell) continue;
      for (var k = 0; k < cell.length && seen < 80000; k++) { var r = HOUSES.houses[cell[k]], p = r[2]; if ((r[4] || 0) !== 0 || r[9] == null || p[0] < bb[0] || p[0] > bb[2] || p[1] < bb[1] || p[1] > bb[3]) continue; seen++; count[r[9]] = (count[r[9]] || 0) + 1; }
    }
    var ids = Object.keys(count).sort(function (a, b) { return count[b] - count[a]; });
    return ids.length ? +ids[0] : null;
  }
  function dzSelection() {
    var v = $("dz-area").value || "", out = { value: v, label: "", sub: "", rows: [] };
    if (v.indexOf("area:") === 0) { var a = DZ.named[+v.slice(5)]; out.label = a.name; out.sub = a.sub || ("S" + (a.sector || "?")); out.rows = DZ.rows[a.id] || []; }
    else if (v.indexOf("sub:") === 0) { out.sub = v.slice(4); out.label = "Sub sector " + out.sub; (DZ.bySub[out.sub] || []).forEach(function (a) { out.rows = out.rows.concat(DZ.rows[a.id] || []); }); }
    return out;
  }
  function dzFacility(name) { var n = String(name || "").toLowerCase(); for (var i = 0; i < DZ.fac.length; i++) { var f = DZ.fac[i].name.toLowerCase(); if (f === n || f.indexOf(n) === 0 || n.indexOf(f) === 0) return i; } return -1; }
  function dzPrefillArea() {   // the sub sector's own figures: waste a house a day and where its compactors tip
    var sel = dzSelection(), s = dzSub(sel.sub);
    var kg = s && s.kgPerHouseDay, ok = kg && kg >= 2 && kg <= 12;
    $("dz-kg").value = ok ? kg.toFixed(2) : EMIRATE_KG.toFixed(2);
    DZ.kgSource = ok ? "measured for " + sel.sub : (s ? "the emirate figure: " + sel.sub + " reads " + (kg == null ? "no figure" : kg.toFixed(2) + " kg") + ", outside 2 to 12 kg" : "the emirate figure: " + sel.sub + " has no weighbridge figure");
    var t = s && s.tip && s.tip[0] ? dzFacility(s.tip[0].name) : -1; if (t >= 0) $("dz-tip").value = String(t);
    DZ.tipSource = s && s.tip && s.tip[0] ? Math.round(s.tip[0].share * 100) + " per cent of " + sel.sub + "'s compactor loads tipped at " + s.tip[0].name : "no weighbridge record for " + sel.sub;
    dzBasis();
  }
  function dzPrefillVehicle() { var v = dzVehicle($("dz-veh").value); if (v) $("dz-load").value = v.loadT.median.toFixed(2); dzBasis(); }
  function dzBasis() {
    var el = $("dz-basis"); if (!el || !DZ.rows) return;
    var sel = dzSelection(), n = sel.rows.length, kg = +$("dz-kg").value || 0, freq = +$("dz-freq").value || 2, gap = 7 / freq, loadT = +$("dz-load").value || 0;
    var s = dzSub(sel.sub), vt = $("dz-veh").value, v = dzVehicle(vt), fl = dzFleet(vt), asg = sel.sub ? dzAssigned(vt, sel.sub) : 0;
    var grp = dzGroups(freq), nd = Math.round(n / grp.length), t = nd * kg * gap / 1000, trips = loadT ? Math.ceil(t / loadT) : 0, mins = Math.max(1, Math.round((20 + n * 0.014) / 60));
    var T = BASELINE && BASELINE.meta && BASELINE.meta.tickets;
    el.innerHTML = '<b>' + esc(sel.label || "Choose an area") + '</b>: ' + dzN(n) + ' houses.<br>' +
      '<b>Waste</b> ' + kg.toFixed(2) + ' kg a house a day, ' + esc(DZ.kgSource || "") + (s ? ' (' + dzN(Math.round(s.compactorTonnesPerDay * 10) / 10) + ' t a day by compactors over ' + dzN(s.houses) + ' houses, weighbridge ' + esc(T ? T.from + " to " + T.to : "") + ')' : '') + '; emirate figure ' + EMIRATE_KG + ' kg (RPT-13).<br>' +
      '<b>Collection days</b> ' + (grp.length > 1 ? grp.length + ' groups, ' + esc(grp.join("; ")) + ': about ' : '') + dzN(nd) + ' houses a day × ' + kg.toFixed(2) + ' kg × ' + gap.toFixed(1) + ' days = ' + t.toFixed(1) + ' t, about ' + trips + ' trips of ' + loadT.toFixed(2) + ' t.<br>' +
      (v ? '<b>' + esc(VEH_LABEL[vt] || vt) + '</b> median load ' + v.loadT.median.toFixed(2) + ' t over ' + dzN(v.trips) + ' public MSW trips, ' + v.tripsPerVehicleDay.mean + ' trips a vehicle day (weighbridge). Register: ' + fl.working + ' of ' + fl.units + ' working' + (sel.sub ? ', ' + asg + ' assigned to ' + esc(sel.sub) : '') + ' (WM001 Master Reference, 07 Equipment Register, ' + esc((BASELINE.meta.fleet || {}).asOf || "") + ').<br>' : '<span class="pill bad">collection_baseline.json not loaded</span><br>') +
      dzSmart(sel) + '<b>Containers</b> one 1,100 L container for ' + (+$("dz-hpc").value || 2) + ' houses (GDL-01, Director 18 Sep 2026). <b>Shift</b> ' + (+$("dz-shift").value || 8) + ' h (PSD rate basis, 26 days of 8 h). <b>Tip</b> ' + esc(DZ.tipSource || "") + '.<br>' +
      '<span style="color:var(--ink-faint)">Takes about ' + mins + ' minute' + (mins > 1 ? 's' : '') + '. Needs a sign in with an editing role.</span>';
  }
  async function designPanel() {
    if (!$("dz-area") || !viewer) return;
    await fetchBaseline(); await fetchSmartBins(); rrSetup();
    if (!HOUSES) { $("dz-basis").innerHTML = "Loading the houses of the emirate…"; try { await fetchHouses(); } catch (e) { $("dz-basis").innerHTML = '<span class="pill bad">omcc-source/houses_all.json could not be read</span>'; return; } }
    dzIndex();
    if (!DZ.filled) {
      var subs = Object.keys(DZ.bySub).sort(function (a, b) { return a.localeCompare(b, "en", { numeric: true }); });
      $("dz-area").innerHTML = subs.map(function (s) {
        var list = DZ.bySub[s].slice().sort(function (a, b) { return a.name.localeCompare(b.name); }), tot = 0; list.forEach(function (a) { tot += DZ.rows[a.id].length; });
        return '<optgroup label="' + esc(s) + '"><option value="sub:' + esc(s) + '">Whole sub sector ' + esc(s) + ' (' + dzN(tot) + ' houses)</option>' + list.map(function (a) { return '<option value="area:' + a.id + '">' + esc(a.name) + ' (' + dzN(DZ.rows[a.id].length) + ')</option>'; }).join("") + '</optgroup>';
      }).join("");
      var vts = BASELINE ? BASELINE.vehicleTypes.filter(function (v) { return v.liftsContainers; }) : [];
      $("dz-veh").innerHTML = vts.length ? vts.map(function (v) { return '<option value="' + esc(v.type) + '">' + esc(VEH_LABEL[v.type] || v.type) + '</option>'; }).join("") : '<option value="">no baseline</option>';
      DZ.fac = (window.__twinFacilities || []).filter(function (f) { return f.layer !== "projects"; });
      var opts = DZ.fac.map(function (f, i) { return '<option value="' + i + '">' + esc(f.name) + '</option>'; }).join("");
      $("dz-depot").innerHTML = opts; $("dz-tip").innerHTML = opts;
      var dm = dzFacility("Al Mullah Transit Station"); if (dm >= 0) $("dz-depot").value = String(dm);   // all equipment starts at Al Mullah (the section, 14 Sep 2026)
      ["dz-freq", "dz-kg", "dz-load", "dz-hpc", "dz-svc", "dz-shift", "dz-depot", "dz-tip", "dz-start", "dz-unload"].forEach(function (id) { $(id).addEventListener("input", dzBasis); $(id).addEventListener("change", dzBasis); });
      $("dz-area").onchange = dzPrefillArea; $("dz-veh").onchange = dzPrefillVehicle;
      DZ.filled = true;
      dzPrefillVehicle();
    }
    var here = dzUnderView(); if (here != null && DZ.rows[here]) $("dz-area").value = "area:" + here;
    dzPrefillArea();
  }
  function dzMarks(plan) {
    if (DZ.marks) { viewer.scene.primitives.remove(DZ.marks); DZ.marks = null; }
    var pc = new C.PointPrimitiveCollection(), red = C.Color.fromCssColorString("#E8646A"), amber = C.Color.fromCssColorString("#E5934B"), n = 0;
    (plan.unreachable || []).forEach(function (u) { var r = DZ.byCode && DZ.byCode[u.id]; pc.add({ position: C.Cartesian3.fromDegrees(u.lon, u.lat, (r ? groundOf(r) : 0) + 3), pixelSize: 8, color: red, outlineColor: C.Color.WHITE, outlineWidth: 1.5, disableDepthTestDistance: Number.POSITIVE_INFINITY }); n++; });
    (plan.unserved || []).forEach(function (u) { if (!u.ll) return; pc.add({ position: C.Cartesian3.fromDegrees(u.ll[0], u.ll[1], 3), pixelSize: 8, color: amber, outlineColor: C.Color.WHITE, outlineWidth: 1.5, disableDepthTestDistance: Number.POSITIVE_INFINITY }); n++; });
    if (n) DZ.marks = viewer.scene.primitives.add(pc);
    viewer.scene.requestRender();
  }
  function dzRender(plan) {
    var t = plan.totals, h = function (s) { return (s / 3600).toFixed(1) + " h"; }, veh = plan.vehicle || {};
    var groups = plan.days || [{ group: 1, days: "", fleet: plan.fleet }];
    var html = '<div class="note"><b>' + esc(plan.name) + '</b><br>' +
      dzN(t.vehicles) + ' vehicles on the busiest collection day' + (veh.available ? ' · the register assigns ' + veh.available + ' of this type to the sub sector' : '') + '<br>' +
      (groups.length > 1 ? groups.length + ' collection day groups, ' + dzN(t.trips) + ' trips in all · ' : dzN(t.trips) + ' trips · ') + dzN(t.houses) + ' of ' + dzN(t.housesAsked) + ' houses in ' + dzN(t.streetStops) + ' street stops · ' + dzN(t.containers) + ' containers<br>' +
      dzN(Math.round(t.distanceM / 1000)) + ' km a round, ' + dzN(Math.round((t.weekDistanceM || t.distanceM) / 1000)) + ' km a week · longest day ' + h(t.longestDayS) + ' · solved in ' + t.solvedInS + ' s</div>';
    (plan.warnings || []).forEach(function (w) { html += '<div class="note warn">' + esc(w) + '</div>'; });
    var colourOf = function (grp, v) { return ROUTE_COLORS[((groups.length > 1 ? grp : v) - 1) % ROUTE_COLORS.length]; };
    html += '<table class="dz-table"><thead><tr><th>Vehicle</th><th>Day</th><th>Trips</th><th>t</th><th>km</th></tr></thead><tbody>' + groups.map(function (d) {
      return (groups.length > 1 ? '<tr><td colspan="5" style="padding-top:7px"><span style="color:' + colourOf(d.group, 1) + '">■</span> <b>' + esc(d.days) + '</b> · ' + d.vehicles + ' vehicles, ' + d.trips + ' trips, ' + dzN(d.houses) + ' houses, ' + (d.kg / 1000).toFixed(1) + ' t</td></tr>' : '') +
        d.fleet.map(function (v) {
          return '<tr><td><span style="color:' + colourOf(d.group, v.vehicle) + '">■</span> ' + v.vehicle + '</td><td>' + esc(v.start) + ' to ' + esc(v.end) + (v.overShift ? ' <span class="pill bad">over shift</span>' : '') + '</td><td>' +
            v.trips.map(function (x) { return x.stops.length + ' stops, tips ' + esc(x.tips); }).join('<br>') + '</td><td>' + (v.kg / 1000).toFixed(1) + '</td><td>' + Math.round(v.distanceM / 1000) + '</td></tr>';
        }).join("");
    }).join("") + '</tbody></table>' +
      '<div class="dz-src" style="color:var(--ink-faint)">On the map ' + (groups.length > 1 ? 'each collection day group' : 'each vehicle') + ' is drawn in its colour. Red dots: houses with no street a truck can stop on within 150 m. Amber dots: streets left unserved. Click a route for its stops and times.</div>';
    $("dz-result").innerHTML = html;
  }
  async function designArea() {
    if (!USER) { $("dz-result").innerHTML = '<div class="err">Sign in on the Account tab: a design is written to the platform as a proposed plan.</div>'; return; }
    if (!can("edit")) { $("dz-result").innerHTML = '<div class="err">Designing routes needs an editing role.</div>'; return; }
    if (!DZ.rows) await designPanel();
    var sel = dzSelection(); if (!sel.rows.length) { $("dz-result").innerHTML = '<div class="err">Choose an area with houses.</div>'; return; }
    var kg = +$("dz-kg").value, loadT = +$("dz-load").value, freq = +$("dz-freq").value || 2, gap = 7 / freq;
    if (!(kg > 0) || !(loadT > 0)) { $("dz-result").innerHTML = '<div class="err">Waste a house a day and load a trip are both needed.</div>'; return; }
    var depot = DZ.fac[+$("dz-depot").value], tip = DZ.fac[+$("dz-tip").value], vt = $("dz-veh").value, start = $("dz-start").value || "07:00";
    DZ.byCode = {};
    var stops = sel.rows.map(function (i) { var r = HOUSES.houses[i], ring = r[2], lon = 0, lat = 0, np = ring.length / 2; for (var q = 0; q < ring.length; q += 2) { lon += ring[q]; lat += ring[q + 1]; } var id = r[8] || ("H-" + i); DZ.byCode[id] = r; return { id: id, lon: lon / np, lat: lat / np, houses: 1, kg: Math.round(kg * gap * 100) / 100 }; });
    var s = dzSub(sel.sub), v = dzVehicle(vt), asg = dzAssigned(vt, sel.sub);
    var body = { name: sel.label + ", " + (FREQ_LABEL[freq] || freq + " a week") + ", " + (VEH_LABEL[vt] || vt), area: { code: sel.value, name: sel.label, subSector: sel.sub },
      depot: { lon: depot.lon, lat: depot.lat, name: depot.name }, tip: { lon: tip.lon, lat: tip.lat, name: tip.name }, stops: stops, loadKg: Math.round(loadT * 1000), vehicleType: vt,
      available: sel.value.indexOf("sub:") === 0 && asg ? asg : null, collectionsPerWeek: freq,   // the register assigns trucks to a sub sector, so only a whole sub sector compares
      shiftS: Math.round((+$("dz-shift").value || 8) * 3600), serviceS: +$("dz-svc").value || 60, unloadS: Math.round((+$("dz-unload").value || 15) * 60), housesPerContainer: +$("dz-hpc").value || 2,
      start: start, hour: parseInt(start, 10), timeLimitS: 8, save: true,
      inputs: { kgPerHouseDay: kg, kgSource: DZ.kgSource, collectionsAWeek: freq, loadT: loadT, loadSource: v ? "weighbridge median of " + v.trips + " public MSW trips, " + BASELINE.meta.tickets.from + " to " + BASELINE.meta.tickets.to : "typed", tipSource: DZ.tipSource,
        housesPerContainer: +$("dz-hpc").value || 2, containerSource: "GDL-01, Director 18 Sep 2026", baseline: "collection_baseline.json, " + (BASELINE ? BASELINE.meta.builtAt : "not loaded"), subSectorMeasured: s ? { tonnesPerDay: s.compactorTonnesPerDay, houses: s.houses } : null } };
    var t0 = Date.now(), est = Math.max(20, Math.round(20 + stops.length * 0.014));
    var tick = function () { var el = $("dz-result"); if (el) el.innerHTML = '<div class="note">Designing ' + dzN(stops.length) + ' houses of ' + esc(sel.label) + ': ' + Math.round((Date.now() - t0) / 1000) + ' s of about ' + est + ' s. The map stays usable meanwhile.</div>'; };
    tick(); var timer = setInterval(tick, 1000); $("dz-go").disabled = true;
    try {
      var plan = await api.design(body);
      clearInterval(timer);   // before the result is drawn: a tick after this point would write the progress line over it
      DZ.plan = plan; currentPlan = plan.id; dzRender(plan); $("dz-csv").disabled = false;
      await loadLayer(LAYERS.find(function (l) { return l.key === "collection_route"; }), plan.id);
      dzMarks(plan); loadPlans();
      var W = 180, S = 90, E = -180, N = -90; stops.forEach(function (p) { if (p.lon < W) W = p.lon; if (p.lon > E) E = p.lon; if (p.lat < S) S = p.lat; if (p.lat > N) N = p.lat; });
      TOUCHED = true; viewer.camera.flyTo({ destination: C.Rectangle.fromDegrees(W - 0.004, S - 0.004, E + 0.004, N + 0.004), duration: 1.4 });
    } catch (e) { $("dz-result").innerHTML = '<div class="err">' + esc(e.message) + '</div>'; }
    finally { clearInterval(timer); $("dz-go").disabled = false; }
  }
  function dzCsv() {
    var plan = DZ.plan; if (!plan) return;
    var q = function (v) { var s = String(v == null ? "" : v); return /[",\r\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s; };
    var rows = [["Days", "Vehicle", "Trip", "Order", "Arrive", "Stop", "Road", "First house", "Last house", "Houses", "Containers", "Kg", "Longitude", "Latitude"]];
    plan.fleet.forEach(function (v) { v.trips.forEach(function (t) { t.stops.forEach(function (s, i) { var c = s.codes || []; rows.push([v.days || "", v.vehicle, t.trip, i + 1, s.arrive, s.street, s.road, c[0], c[c.length - 1], s.houses, s.containers, s.kg, s.lon, s.lat]); }); }); });
    (plan.unreachable || []).forEach(function (u) { rows.push(["", "", "", "", "not routed", u.reason, "", u.id, u.id, 1, "", "", u.lon, u.lat]); });
    (plan.unserved || []).forEach(function (u) { rows.push([u.days || "", "", "", "", "not served", u.reason, u.street, "", "", u.houses, "", u.kg, u.ll ? u.ll[0] : "", u.ll ? u.ll[1] : ""]); });
    var a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob(["\ufeff" + rows.map(function (r) { return r.map(q).join(","); }).join("\r\n")], { type: "text/csv;charset=utf-8" }));
    a.download = ("Route sheets " + plan.name + " " + plan.id.slice(-20)).replace(/[\\/:*?"<>|]+/g, " ") + ".csv";
    document.body.appendChild(a); a.click(); setTimeout(function () { URL.revokeObjectURL(a.href); a.remove(); }, 1500);
  }
  var PLAN_DEFAULTS = { freqHeavy: 7, freqNormal: 3, binsPerLoad: 85, minPerBin: 1.5, haulMin: 40, shiftMin: 420, days: 6, crew: 3 };
  var FLEET = { rearLoaders: 53, hookLifts: 30, skipLoaders: 7, source: "WM001 Master Reference v3.1, WM fleet (43 large and 10 small rear loaders, 25 heavy and 5 light hook loaders, 7 skip loaders); all equipment starts at Al Mullah Transit Station" };
  var K_ALLOC = "wma_twin_fleet_alloc_v1";
  function allocRead() { try { var v = localStorage.getItem(K_ALLOC); return v ? JSON.parse(v) : null; } catch (e) { return null; } }
  function allocWrite(a) { try { var clean = {}; Object.keys(a).forEach(function (k) { if (a[k] > 0) clean[k] = a[k]; }); if (Object.keys(clean).length) localStorage.setItem(K_ALLOC, JSON.stringify(clean)); else localStorage.removeItem(K_ALLOC); } catch (e) {} }
  function sectorNeeds(p, binsPerShift) {
    var by = {}; HOUSES.areas.forEach(function (a) { var s = a.sector || (a.bbox ? sectorOfBbox(a.bbox) : null) || "unplaced"; var freq = a.density === "heavy" ? p.freqHeavy : p.freqNormal; var daily = a.houses * freq / p.days; (by[s] = by[s] || { sector: s, houses: 0, daily: 0 }); by[s].houses += a.houses; by[s].daily += daily; });
    Object.keys(by).forEach(function (s) { by[s].shifts = Math.ceil(by[s].daily / binsPerShift); });
    return by;
  }
  function sectorOfBbox(bb) { // a drawn area is credited to the sector holding its centre (the twin's sector polygons)
    var T = window.__wmaTwin; if (!T || !T.toGeoJSON) return null; var cx = (bb[0] + bb[2]) / 2, cy = (bb[1] + bb[3]) / 2; var fc = T.toGeoJSON();
    for (var i = 0; i < fc.features.length; i++) { var g = fc.features[i].geometry; var rings = g.type === "Polygon" ? [g.coordinates[0]] : g.coordinates.map(function (q) { return q[0]; }); for (var r = 0; r < rings.length; r++) if (pointInRing(cx, cy, rings[r])) return "S" + fc.features[i].properties.n; }
    return null;
  }
  function renderFleet(p, binsPerShift) {
    var needs = sectorNeeds(p, binsPerShift); var keys = Object.keys(needs).sort(); var totalShifts = keys.reduce(function (s, k) { return s + needs[k].shifts; }, 0) || 1;
    var saved = allocRead() || {}; var alloc = {}; var used = 0;
    keys.forEach(function (k) { alloc[k] = saved[k] != null ? +saved[k] : Math.round(FLEET.rearLoaders * needs[k].shifts / totalShifts); used += alloc[k]; });
    var rows = keys.map(function (k) { var n = needs[k]; var shiftsPerVehicle = 1; var cover = alloc[k] * shiftsPerVehicle; var gap = n.shifts - cover; return '<tr><td>' + esc(k) + '</td><td class="num">' + n.houses.toLocaleString("en-GB") + '</td><td class="num">' + Math.round(n.daily).toLocaleString("en-GB") + '</td><td class="num">' + n.shifts + '</td><td class="num"><input data-alloc="' + esc(k) + '" type="number" min="0" max="80" value="' + alloc[k] + '" style="width:64px"></td><td class="num">' + (gap > 0 ? '<b style="color:#E8646A">' + gap + ' short</b>' : '<span style="color:#4FBF8C">covered</span>') + '</td></tr>'; }).join("");
    return '<h2 style="margin-top:14px">Starting equipment by sector</h2><div class="note">' + esc(FLEET.source) + '. Rear loaders are split by each sector\'s share of the daily services as a start; edit the counts to reassign, the gap column follows. One shift per vehicle a day; a second shift doubles the cover.</div>' +
      '<div class="kpis"><span>Rear loaders <b>' + FLEET.rearLoaders + '</b></span><span>Allocated <b id="alloc-used">' + used + '</b></span><span>Hook lifts <b>' + FLEET.hookLifts + '</b></span><span>Skip loaders <b>' + FLEET.skipLoaders + '</b></span></div>' +
      '<div class="tw"><table class="plan"><thead><tr><th>Sector</th><th>Houses</th><th>Services a day</th><th>Vehicle shifts needed</th><th>Rear loaders assigned</th><th>Cover</th></tr></thead><tbody>' + rows + '</tbody></table></div>';
  }
  function planInputs() { var o = {}; Object.keys(PLAN_DEFAULTS).forEach(function (k) { var el = $("pl-" + k); o[k] = el && +el.value > 0 ? +el.value : PLAN_DEFAULTS[k]; }); return o; }
  function renderPlan() {
    var el = $("plan-body"); if (!el) return;
    if (!HOUSES) { el.innerHTML = '<div class="note">Turn on the Houses layer to load the house counts.</div>'; return; }
    var p = planInputs();
    var loadsPerShift = Math.max(1, Math.floor(p.shiftMin / (p.binsPerLoad * p.minPerBin + p.haulMin)));
    var binsPerShift = Math.min(loadsPerShift * p.binsPerLoad, Math.floor(p.shiftMin / p.minPerBin));
    var tot = { houses: 0, weekly: 0, daily: 0, shifts: 0 };
    var rows = HOUSES.areas.slice().sort(function (a, b) { return b.houses - a.houses; }).map(function (a) {
      var freq = a.density === "heavy" ? p.freqHeavy : p.freqNormal; var weekly = a.houses * freq; var daily = weekly / p.days; var shifts = Math.ceil(daily / binsPerShift);
      tot.houses += a.houses; tot.weekly += weekly; tot.daily += daily; tot.shifts += shifts;
      return '<tr><td>' + esc(a.title) + '</td><td>' + (a.density === "heavy" ? '<b style="color:#E8646A">heavy</b>' : 'normal') + '</td><td class="num">' + a.houses.toLocaleString("en-GB") + '</td><td class="num">' + freq + '</td><td class="num">' + Math.round(daily).toLocaleString("en-GB") + '</td><td class="num">' + shifts + '</td><td class="num">' + (shifts * p.crew) + '</td></tr>';
    }).join("");
    el.innerHTML = '<div class="note">Planning estimate from ' + houseCount().toLocaleString("en-GB") + ' house footprints (' + esc(HOUSES.meta.source.split(",")[0]) + ') and one 1,100 L bin per house, in every sector. A house takes the density of the drawn area it sits in, otherwise its sector (Sector 3 heavy, others normal). Frequencies and productivity figures are assumptions to confirm with PSD; the bin survey replaces the counts.</div>' +
      '<div class="kpis"><span>Bins per vehicle shift <b>' + binsPerShift + '</b></span><span>Loads per shift <b>' + loadsPerShift + '</b></span><span>Houses <b>' + tot.houses.toLocaleString("en-GB") + '</b></span><span>Services a day <b>' + Math.round(tot.daily).toLocaleString("en-GB") + '</b></span><span>Vehicle shifts a day <b>' + tot.shifts + '</b></span><span>Crew a day <b>' + (tot.shifts * p.crew) + '</b></span></div>' +
      '<div class="tw"><table class="plan"><thead><tr><th>Area</th><th>Density</th><th>Houses</th><th>Services a week per house</th><th>Services a day</th><th>Vehicle shifts a day</th><th>Crew</th></tr></thead><tbody>' + rows + '</tbody></table></div>' +
      renderFleet(p, binsPerShift) +
      '<div class="note">Doubling: with the same fleet the lever is bins per shift. Shorter haul (transfer station nearer), better route order (fewer minutes per bin) and a second shift raise it; change the figures above to see the effect. Route drawings: on the Routes tab choose Stop source: houses in view and run one plan per neighbourhood (400 stops each).</div>';
    el.querySelectorAll("input[data-alloc]").forEach(function (inp) { inp.onchange = function () { var a = allocRead() || {}; a[inp.dataset.alloc] = +inp.value || 0; allocWrite(a); renderPlan(); }; });
  }
  async function reloadAll() { if (window.__twinMap2d) { if (window.__twin2dReload) await window.__twin2dReload(); return; } LAYERS.forEach(function (l) { if ((l.kind === "local" || l.kind === "binprog" || l.kind === "binreg" || l.kind === "smartbins" || l.kind === "priority") && COUNTS[l.key] == null && !DS[l.dsKey || l.key] && !(l.kind === "binreg" && BINREG_PRIM) && !(l.kind === "smartbins" && SB_PRIM)) COUNTS[l.key] = "tick to load"; }); await loadFloodLayers(); var rl = LAYERS.find(function (x) { return x.kind === "roads"; }); if (rl) await loadLayer(rl); if (!USER) { LAYERS.forEach(function (l) { if (l.kind === "houses" && HOUSE_PRIMS.length) return; if (l.kind === "binreg" && BINREG_PRIM) return; if (l.kind === "smartbins" && SB_PRIM) return; if (DS[l.dsKey || l.key]) return; COUNTS[l.key] = (l.kind === "local" || l.kind === "houses" || l.kind === "binprog" || l.kind === "binreg" || l.kind === "smartbins" || l.kind === "priority") ? "tick to load" : "sign in"; }); renderLayers(); var hl = LAYERS.find(function (x) { return x.kind === "houses"; }); if (hl && hl.on && !HOUSE_PRIMS.length) loadLayer(hl); status("Twin ready · tick Houses or the MSW sub-sector layers to load them; sign in on the Account tab for the platform layers"); return; } for (var i = 0; i < LAYERS.length; i++) { var l = LAYERS[i]; if (l.key === "collection_route") { if (currentPlan) await loadLayer(l, currentPlan); else COUNTS[l.key] = 0; } else await loadLayer(l); } renderLayers(); }

  // ------------------------------------------------------------ account
  function renderAccount() {
    var el = $("acct");
    if (USER) { el.innerHTML = '<p>Signed in as <b>' + esc(USER.name) + '</b> (' + esc(USER.role) + '). Platform ' + esc(PLAT) + '.</p><button class="btn ghost" id="acct-out">Sign out</button>'; $("acct-out").onclick = async function () { await api.logout(); renderAccount(); updatePlatPill(); }; return; }
    el.innerHTML = '<p>Sign in to the O&M platform to place bins, import land use and optimise routes. Viewing needs no sign in beyond a platform account.</p><label>Platform address</label><input id="acct-url" value="' + esc(PLAT) + '"><label>Email</label><input id="acct-email" type="email" autocomplete="username"><label>Password</label><input id="acct-pw" type="text" data-masked="1" style="-webkit-text-security:disc" autocomplete="current-password"><button class="btn" id="acct-in">Sign in</button><div id="acct-err"></div>';
    $("acct-in").onclick = async function () { PLAT = $("acct-url").value.replace(/\/+$/, ""); try { localStorage.setItem("omcc-platform-url", PLAT); } catch (e) {} try { await api.login($("acct-email").value.trim(), $("acct-pw").value); renderAccount(); updatePlatPill(); await reloadAll(); await loadPlans(); document.querySelector('nav button[data-tab="layers"]').click(); } catch (e) { $("acct-err").innerHTML = '<div class="err">' + esc(e.status === 401 ? "Email or password is wrong." : e.message) + '</div>'; } };
    $("acct-pw").onkeydown = function (ev) { if (ev.key === "Enter") $("acct-in").click(); };
  }
  function updatePlatPill() { var p = $("ws-plat"); if (USER) { p.textContent = USER.name + " · " + USER.role; p.className = "pill ok"; } else { p.textContent = "sign in"; p.className = "pill warn"; } }

  // ------------------------------------------------------------ boot
  document.querySelectorAll("nav button[data-tab]").forEach(function (b) { b.onclick = function () { document.querySelectorAll("nav button[data-tab]").forEach(function (x) { x.classList.toggle("on", x === b); }); document.querySelectorAll(".tab").forEach(function (t) { t.classList.toggle("on", t.id === "tab-" + b.dataset.tab); }); }; });
  $("bin-capture").onclick = function () { setCapture(!capture); }; $("bin-reload").onclick = function () { loadLayer(LAYERS[0]); };
  $("area-draw").onclick = function () { if (!viewer) return; setDraw(!draw.on); }; $("area-finish").onclick = finishArea;
  Object.keys(PLAN_DEFAULTS).forEach(function (k) { var el = $("pl-" + k); if (el) { el.value = PLAN_DEFAULTS[k]; el.oninput = renderPlan; } });
  $("lu-import").onclick = importLanduse; $("rt-go").onclick = optimise; $("rt-clear").onclick = function () { var l = LAYERS.find(function (x) { return x.key === "collection_route"; }); if (DS[l.key]) { viewer.dataSources.remove(DS[l.key], true); DS[l.key] = null; } COUNTS[l.key] = 0; currentPlan = null; renderLayers(); $("rt-result").innerHTML = ""; if (DZ.marks) { viewer.scene.primitives.remove(DZ.marks); DZ.marks = null; viewer.scene.requestRender(); } };
  $("dz-go").onclick = designArea; $("dz-csv").onclick = dzCsv;
  (function () { var b = document.querySelector('nav button[data-tab="routes"]'); if (b) b.addEventListener("click", function () { designPanel(); }); })();
  renderLayers(); renderAccount();
  window.__twinMap = { api: api, reloadAll: reloadAll, flyHome: flyHome, layers: LAYERS, counts: COUNTS, feats: FEATS, optimise: optimise, loadPlans: loadPlans, loadLayer: loadLayer, renderPlanResult: renderPlanResult, simulate: simulate, stopSim: stopSim, sectorBorders: sectorBorders, floodLayers: FLOOD_LAYERS, loadFloodLayers: loadFloodLayers, swmp: SWMP, houses: function () { return HOUSES; }, rebuildHouses: rebuildHouses, roadsLabelled: roadsLabelled, twinRoads: twinRoads, ds: DS, cellPrims: function () { return CELL_PRIMS; }, housePrims: function () { return HOUSE_PRIMS; }, roadSurfaces: function () { return ROAD_LAYER ? [ROAD_LAYER] : []; }, groundLayers: function () { return { roads: ROAD_LAYER, footprints: HOUSE_FLAT }; }, houseFlat: function () { return HOUSE_FLAT; }, houseBuilt: function () { return HOUSE_BUILT; }, binBox: function () { return BINREG_BOX; }, want2d: WANT2D, token: function () { return TOKEN; }, defs: { CLASS_COLOUR: CLASS_COLOUR, FLOOD_FIELDS: FLOOD_FIELDS, floodSelectionHtml: floodSelectionHtml, floodIcon: floodIcon, floodSymbolKind: floodSymbolKind, floodProvenance: floodProvenance, ROAD_STYLE: ROAD_STYLE, plat: function () { return PLAT; } }, showSelection: showSelection, publicBuildingSelect: publicBuildingSelect, binRegisterSelect: binRegisterSelect, viewer: function () { return viewer; }, user: function () { return USER; }, can: can };

  function xrayDefault() {
    var host = $("twin-host"); var b = host && host.querySelector('button[data-act="xray"]'); if (!b) return;
    var pref = "off"; try { pref = localStorage.getItem("twinmap-xray") || "off"; } catch (e) {}
    var isOn = b.classList.contains("on") || b.getAttribute("aria-pressed") === "true";
    if (pref === "on" && !isOn) b.click(); else if (pref === "off" && isOn) b.click();
    b.addEventListener("click", function () { try { localStorage.setItem("twinmap-xray", b.classList.contains("on") || b.getAttribute("aria-pressed") === "true" ? "on" : "off"); } catch (e) {} });
  }
  // ?at=lon,lat,height[,heading,pitch] sets the opening view and ?layers=key,key ticks those layers, so a link can open
  // on the picture someone wants to show instead of instructions for finding it. Nothing is stored.
  var URLQ = new URLSearchParams(location.search), TOUCHED = false;
  function watchTouch() {
    // any real click, key, scroll or touch anywhere on the page hands the camera to the person: the link stops steering
    ["pointerdown", "keydown", "wheel", "touchstart"].forEach(function (ev) {
      document.addEventListener(ev, function (e) { if (e.isTrusted) TOUCHED = true; }, { capture: true, passive: true });
    });
    // the twin flies the camera on its own several times while it loads (home, the demo project, restores). Until the
    // person takes over, any of those that carries the camera away from the linked view is undone when it ends.
    if (URLQ.get("at")) viewer.camera.moveEnd.addEventListener(function () { if (!TOUCHED && awayFromLink()) urlView(); });
    // the twin clicks its own demo fly button when the demonstration project arrives from the platform, which lands
    // the camera at 1,900 m over the demo roads whenever that happens to be, including while a person is working.
    // That automatic click is always stopped on this page; a person's own click on the button is trusted and flies.
    document.addEventListener("click", function (e) {
      if (!e.isTrusted && e.target && e.target.id === "twin-demo-fly") { e.stopPropagation(); e.preventDefault(); }
    }, true);
  }
  function linkAt() {
    var at = (URLQ.get("at") || "").split(",").filter(function (s) { return s !== ""; }).map(Number);
    return at.length >= 3 && at.every(isFinite) ? at : null;
  }
  function awayFromLink() {
    var at = linkAt(); if (!at) return false;
    var c = viewer.camera.positionCartographic;
    var dLon = (C.Math.toDegrees(c.longitude) - at[0]) * 100000, dLat = (C.Math.toDegrees(c.latitude) - at[1]) * 111000;
    return Math.abs(c.height - at[2]) > at[2] * 0.2 || Math.sqrt(dLon * dLon + dLat * dLat) > Math.max(1000, at[2]);
  }
  function urlView() {
    var at = linkAt();
    if (TOUCHED || !at) return;   // the person has taken over, or there is no view to set
    viewer.camera.cancelFlight();   // a flight in progress would overwrite the view on its next frame
    viewer.camera.lookAtTransform(C.Matrix4.IDENTITY);
    viewer.camera.setView({ destination: C.Cartesian3.fromDegrees(at[0], at[1], at[2]),
      orientation: { heading: C.Math.toRadians(at.length > 3 ? at[3] : 0), pitch: C.Math.toRadians(at.length > 4 ? at[4] : -45), roll: 0 } });
    viewer.scene.requestRender();
  }
  function urlLayers() {
    (URLQ.get("layers") || "").split(",").map(function (s) { return s.trim(); }).filter(Boolean).forEach(function (k) {
      var cb = document.querySelector('input[data-layer="' + k.replace(/[^a-z0-9_]/gi, "") + '"]');
      if (cb && !cb.checked) cb.click();
    });
  }
  async function boot() {
    try { var h = await api.health(); if (!h || !h.ok) throw new Error("no"); } catch (e) { $("ws-plat").textContent = "offline"; $("ws-plat").className = "pill bad"; status("Published copy: the department platform is not reachable from here, so the platform layers are off. Everything drawn from the files in this page works: buildings, bins, areas, roads and the registers."); }
    if (TOKEN) { try { USER = await api.me(); } catch (e) { TOKEN = ""; USER = null; } }
    renderAccount(); updatePlatPill();
    var geo, twin;
    try { geo = await (await fetch("omcc-source/wm001_twin_data.json")).json(); twin = await (await fetch("omcc-source/twin_data.json")).json(); } catch (e) { status("Twin data files missing (omcc-source/wm001_twin_data.json, twin_data.json)"); return; }
    TWIN_DATA = twin;   // the ground picture paints its roads from it
    window.__twinFacilities = (twin.facilities || []).map(function (f) { return { name: f.name, layer: f.layer, lon: twin.meta.origin[0] + f.x / twin.meta.mlon, lat: twin.meta.origin[1] + f.y / twin.meta.mlat }; });
    fillDepots(); preferAlMullah();
    if (WANT2D) { window.__twinMap2d = { reason: WANT2D, geo: geo, twin: twin }; status(WANT2D === "requested" ? "2D map (requested with ?view=2d)" : "WebGL is not available in this browser: the 2D map opens instead of the twin"); if (window.__twin2dMount) window.__twin2dMount(WANT2D); return; }
    if (!window.__wm001TwinMount) { status("wm001-twin.js did not load"); return; }
    if (!document.getElementById("wm001-twin-css")) { var st = document.createElement("style"); st.id = "wm001-twin-css"; st.textContent = window.__wm001TwinCss; document.head.appendChild(st); }
    var host = $("twin-host"); host.innerHTML = window.__wm001TwinMarkup;
    // artifact copy: no outside host is reachable, so the basemap and terrain that ship with the twin are used
    var EMB = null; try { EMB = await (await fetch("omcc-source/twin_basemap.json")).json(); } catch (e) {}
    window.__wm001TwinMount(host, { cesiumBase: "cesium/", embeddedBase: EMB, embedRoads: true, geo: geo.geo, rq: geo.rq, aj: geo.aj, extra: { roads: twin.roads, roadsLocal: twin.roads_local, meta: twin.meta, proposed: twin.proposed } });
    var tries = 0; (function waitViewer() { viewer = window.__wmaTwin && window.__wmaTwin.viewer; if (!viewer) { if (++tries < 200) return setTimeout(waitViewer, 250); status("The twin did not start"); return; } status("Twin ready · layers loading"); installPicking(); installMapControls(); fastScene(); easeReadout(); [2500, 6000, 12000, 25000].forEach(function (ms) { setTimeout(settleTwin, ms); }); setInterval(settleTwin, 20000); roadPicture(); applyDetail(); renderGuard(); xrayDefault(); watchTouch(); urlView(); [1500, 4000, 9000].forEach(function (ms) { setTimeout(collectionOnly, ms); setTimeout(urlView, ms); }); reloadAll().then(function () { urlView(); urlLayers(); if (USER) status("Twin ready · " + LAYERS.filter(function (l) { return COUNTS[l.key]; }).map(function (l) { return COUNTS[l.key] + " " + l.title.toLowerCase(); }).join(" · ")); }); if (USER) loadPlans(); })();
  }
  boot();
})();
