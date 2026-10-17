/* WM001 shared copy: the map frame's data files are encrypted on the host (14 Oct 2026). ES5.
 * The map asks for "omcc-source/<name>"; this shim answers from "<name>.<hash>.enc", decrypted with the key the
 * application window holds after the access code was accepted (window.parent.__WMA_KEY, a non extractable
 * AES-GCM key) and unzipped. Without the code there is no key, and the files are unreadable on the host.
 * The manifest of names and hashes (window.parent.__WMA_ENC) lives inside the encrypted application page.
 * The frame's own code files (wm001-twin.js and the others) are sealed the same way and started by window.__wmaCode(name, next);
 * its page markup is sealed too and inserted by window.__wmaBody(next). Sealed files are named by a hash only (<hash>.enc).
 */
(function () {
  "use strict";
  var W = window, of = W.fetch.bind(W), cache = {};
  function ctype(n) { return /\.geojson$/i.test(n) ? "application/geo+json" : "application/json"; }
  function gunzip(buf) {
    var ds = new DecompressionStream("gzip"), w = ds.writable.getWriter();
    w.write(buf); w.close();
    return new Response(ds.readable).arrayBuffer();
  }
  function unseal(url, key) {
    return of(url, { cache: "force-cache" }).then(function (r) {
      if (!r.ok) { throw new TypeError("sealed file " + url + " " + r.status); }
      return r.arrayBuffer();
    }).then(function (b) {
      var a = new Uint8Array(b);
      return W.crypto.subtle.decrypt({ name: "AES-GCM", iv: a.subarray(0, 12) }, key, a.subarray(12));
    }).then(gunzip);
  }
  W.fetch = function (input, init) {
    var s = "";
    try { s = String(input && input.url ? input.url : input); } catch (e) { s = ""; }
    var m = /^(?:\.\/)?omcc-source\/([^?#]+)/.exec(s);
    if (!m) { return of(input, init); }
    var name = decodeURIComponent(m[1]), P = W.parent, enc = P && P.__WMA_ENC, key = P && P.__WMA_KEY;
    if (!enc || !key || !enc[name]) { return Promise.reject(new TypeError("This data file is locked: " + name)); }
    var url = "omcc-source/" + enc[name] + ".enc";
    if (!cache[url]) {
      cache[url] = of(url, { cache: "force-cache" }).then(function (r) {
        if (!r.ok) { throw new TypeError("data file " + name + " " + r.status); }
        return r.arrayBuffer();
      }).then(function (b) {
        var a = new Uint8Array(b);
        return W.crypto.subtle.decrypt({ name: "AES-GCM", iv: a.subarray(0, 12) }, key, a.subarray(12));
      }).then(gunzip);
    }
    return cache[url].then(function (buf) { return new Response(buf.slice(0), { status: 200, headers: { "Content-Type": ctype(name) } }); });
  };
  /* a sealed code file: fetched, decrypted, unzipped and run as an inline script, then next() */
  W.__wmaCode = function (name, next) {
    var P = W.parent, enc = P && P.__WMA_ENC, key = P && P.__WMA_KEY;
    if (!enc || !key || !enc[name]) { if (W.console) { W.console.error("This code file is locked: " + name); } return; }
    unseal(enc[name] + ".enc", key).then(function (buf) {
      var s = document.createElement("script");
      s.textContent = new TextDecoder().decode(buf);
      document.body.appendChild(s);
      if (next) { next(); }
    }).catch(function (e) { if (W.console) { W.console.error(e); } });
  };
  /* the frame's own markup, sealed like the code: put in at the start of the body, then next() */
  W.__wmaBody = function (next) {
    var P = W.parent, enc = P && P.__WMA_ENC, key = P && P.__WMA_KEY;
    if (!enc || !key || !enc["frame-body"]) { if (W.console) { W.console.error("The map frame is locked"); } return; }
    unseal(enc["frame-body"] + ".enc", key).then(function (buf) {
      document.body.insertAdjacentHTML("afterbegin", new TextDecoder().decode(buf));
      if (next) { next(); }
    }).catch(function (e) { if (W.console) { W.console.error(e); } });
  };
})();
