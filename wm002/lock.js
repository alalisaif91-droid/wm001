/* WM001 shared copy: turns off the ways the application hands its data out (14 Oct 2026, revised after an independent attack test).
 * Loaded first in the shared (public) build: in the application window, in the map frame and in the embedded 3D model frames. ES5.
 * What it does: no export, download, print, email or copy of the page's data; no right click or drag outside the entry fields;
 * no text selection outside the entry fields; the usual save, print, view source and developer tool shortcuts are cancelled
 * (by the physical key, so it holds on a non Latin keyboard, and including the Mac forms); the export buttons are greyed.
 * What it cannot do: stop a screenshot, a photograph of the screen, the browser's own menu, its developer tools, an extension or a
 * script run from the console. The data stays encrypted on the host; whoever holds the access code sees it.
 */
(function () {
  "use strict";
  if (window.__wmaLock) { return; }
  window.__wmaLock = true;
  var D = document, W = window;
  var MSG = "Export, copy and email are turned off in the shared copy of this application.";
  /* a control is an export control when its short visible words begin with one of these verbs, or name a data file type in brackets */
  var EXPORT_RX = /^\W*(export|download|print|save snapshot|save copy|copy|issue by email|send by email|email|e-mail|send to .{1,40} for comment|share)\b|\((csv|json|pdf|xlsx|png|glb|obj)\)/i;

  /* ---------- a short notice ---------- */
  var tt = null;
  function toast() {
    try {
      var host = D.body || D.documentElement; if (!host) { return; }
      var t = D.getElementById("wma-lock-toast");
      if (!t) {
        t = D.createElement("div"); t.id = "wma-lock-toast"; t.setAttribute("role", "status");
        t.style.cssText = "position:fixed;left:50%;bottom:22px;transform:translateX(-50%);z-index:2147483600;max-width:90vw;padding:9px 16px;pointer-events:none;" +
          "border-radius:8px;border:1px solid #2FA3C0;background:#121C24;color:#E7EEF3;font:600 13px Calibri,Carlito,'Segoe UI',sans-serif;box-shadow:0 6px 24px rgba(0,0,0,.4)";
        host.appendChild(t);
      }
      t.textContent = MSG; t.style.display = "block";
      if (tt) { clearTimeout(tt); }
      tt = setTimeout(function () { t.style.display = "none"; }, 2600);
    } catch (e) { }
  }
  function editable(t) {
    return !!(t && t.closest && t.closest("input,textarea,select,[contenteditable='true'],[contenteditable='']"));
  }

  /* ---------- style: no selecting, nothing on paper ---------- */
  try {
    var st = D.createElement("style"); st.id = "wma-lock-style";
    st.textContent = "html,body{-webkit-user-select:none;user-select:none;-webkit-touch-callout:none}" +
      "input,textarea,[contenteditable='true'],[contenteditable='']{-webkit-user-select:text;user-select:text}" +
      "img{-webkit-user-drag:none;user-drag:none}" +
      "@media print{html{display:none!important}}" +
      "[data-wma-locked]{opacity:.45;cursor:not-allowed!important}";
    (D.head || D.documentElement).appendChild(st);
  } catch (e) { }

  /* ---------- events ---------- */
  ["contextmenu", "dragstart", "selectstart"].forEach(function (ev) {
    D.addEventListener(ev, function (e) { if (editable(e.target)) { return; } e.preventDefault(); }, true);
  });
  ["copy", "cut"].forEach(function (ev) {
    D.addEventListener(ev, function (e) { if (editable(e.target)) { return; } e.preventDefault(); if (e.clipboardData) { try { e.clipboardData.setData("text/plain", ""); } catch (x) { } } toast(); }, true);
  });
  function letter(e) {   /* the physical key, so a non Latin layout cannot slip past */
    var c = String(e.code || ""), k = String(e.key || "").toLowerCase();
    return c.indexOf("Key") === 0 ? c.slice(3).toLowerCase() : k;
  }
  D.addEventListener("keydown", function (e) {
    var k = letter(e), c = e.ctrlKey || e.metaKey, block = false;
    if (e.key === "F12") { block = true; }
    else if (c && e.shiftKey && "ijckem".indexOf(k) >= 0 && k.length === 1) { block = true; }
    else if (c && e.altKey && "ijcu".indexOf(k) >= 0 && k.length === 1) { block = true; }
    else if (c && "spu".indexOf(k) >= 0 && k.length === 1) { block = true; }
    else if (c && k === "a" && !editable(e.target)) { block = true; }
    else if (c && (k === "c" || k === "x") && !editable(e.target)) { block = true; }
    if (block) { e.preventDefault(); e.stopPropagation(); toast(); }
  }, true);
  W.addEventListener("beforeprint", function () { try { D.documentElement.style.display = "none"; } catch (e) { } });
  W.addEventListener("afterprint", function () { try { D.documentElement.style.display = ""; } catch (e) { } });

  /* ---------- downloads, printing, mail, clipboard, prompts ---------- */
  try { W.print = function () { toast(); }; } catch (e) { }
  function isFileLink(a) {
    if (!a || a.tagName !== "A") { return false; }
    var h = String(a.getAttribute("href") || "");
    return a.hasAttribute("download") || /^(blob|data|mailto):/i.test(h);
  }
  try {
    var ac = HTMLAnchorElement.prototype.click;
    HTMLAnchorElement.prototype.click = function () { if (isFileLink(this)) { toast(); return; } return ac.apply(this, arguments); };
  } catch (e) { }
  try {
    var de = EventTarget.prototype.dispatchEvent;
    EventTarget.prototype.dispatchEvent = function (ev) { if (ev && ev.type === "click" && isFileLink(this)) { toast(); return false; } return de.apply(this, arguments); };
  } catch (e) { }
  try {
    var wo = W.open;
    W.open = function (u) { if (/^(blob|data|mailto):/i.test(String(u || ""))) { toast(); return null; } return wo.apply(W, arguments); };
  } catch (e) { }
  try { W.prompt = function () { toast(); return null; }; } catch (e) { }
  try {
    var ex = D.execCommand;
    D.execCommand = function (c) { if (/^(copy|cut)$/i.test(String(c || ""))) { toast(); return false; } return ex.apply(D, arguments); };
  } catch (e) { }
  try {
    if (navigator.clipboard) {
      var deny = function () { toast(); return Promise.reject(new Error(MSG)); };
      try { navigator.clipboard.writeText = deny; } catch (e1) { }
      try { navigator.clipboard.write = deny; } catch (e2) { }
    }
  } catch (e) { }
  try { if (W.showSaveFilePicker) { W.showSaveFilePicker = function () { toast(); return Promise.reject(new Error(MSG)); }; } } catch (e) { }

  /* ---------- the buttons and links that export (open shadow roots included) ---------- */
  function label(el) {
    /* the visible words of a short control; a long one (a tile that opens a page) is never an export button */
    var t = (el.children.length > 4 ? "" : (el.textContent || "")).replace(/\s+/g, " ").trim();
    if (!t) { t = String(el.getAttribute("aria-label") || el.getAttribute("title") || "").replace(/\s+/g, " ").trim(); }
    return t.length > 60 ? "" : t;
  }
  function collect(root, out, deep) {
    var l = root.querySelectorAll("button, a, [role='button'], summary"), i;
    for (i = 0; i < l.length; i++) { out.push(l[i]); }
    if (deep) {   /* open shadow roots (the 3D viewers draw their buttons there): looked for every fourth pass only */
      var all = root.querySelectorAll("*");
      for (i = 0; i < all.length; i++) { if (all[i].shadowRoot) { collect(all[i].shadowRoot, out, true); } }
    }
    return out;
  }
  var pass = 0;
  function sweep() {
    if (D.hidden) { return; }
    var l = [], i, el, hit, lb;
    try { collect(D, l, (pass++ % 4) === 0); } catch (e) { return; }
    for (i = 0; i < l.length; i++) {
      el = l[i]; if (el.hasAttribute("data-wma-locked")) { continue; }
      if (el.closest && el.closest("#wma-gate")) { continue; }
      lb = label(el); hit = lb ? EXPORT_RX.exec(lb) : null;
      if (hit) { el.setAttribute("data-wma-locked", (hit[1] || hit[2] || "export").toLowerCase()); el.setAttribute("aria-disabled", "true"); el.setAttribute("title", MSG); }
    }
  }
  function onClick(e) {
    var p = e.composedPath ? e.composedPath() : [e.target], i, t = null;
    for (i = 0; i < p.length; i++) {
      if (p[i] && p[i].nodeType === 1 && (p[i].hasAttribute("data-wma-locked") || (p[i].tagName === "A" && isFileLink(p[i])))) { t = p[i]; break; }
    }
    if (!t) { return; }
    e.preventDefault(); e.stopImmediatePropagation(); toast();
  }
  D.addEventListener("click", onClick, true);
  var timer = null;
  function later() { if (timer) { return; } timer = setTimeout(function () { timer = null; sweep(); }, 350); }
  D.addEventListener("click", later, false);
  setInterval(sweep, 4000);
  if (D.readyState === "loading") { D.addEventListener("DOMContentLoaded", sweep); } else { sweep(); }
})();
