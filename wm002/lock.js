/* WM001 shared copy: turns off the ways the application hands its data out (14 Oct 2026).
 * Loaded first in the shared (public) build, in the application window and in the map frame. ES5.
 * What it does: no export, download, print or copy of the page's data; no right click, drag or text selection
 * outside the entry fields; the usual save, print, view source and developer tool shortcuts are cancelled.
 * What it cannot do: stop a screenshot, a photograph of the screen, or someone who opens the browser's own
 * developer tools from its menu. The data stays encrypted on the host; whoever holds the access code sees it.
 */
(function () {
  "use strict";
  if (window.__wmaLock) { return; }
  window.__wmaLock = true;
  var D = document, W = window;
  var MSG = "Export and copy are turned off in the shared copy of this application.";
  var EXPORT_RX = /(^|[^a-z])(export|download|print|pdf|csv|save snapshot|save copy|copy link|copy table|copy to clipboard)([^a-z]|$)/i;

  /* ---------- a short notice ---------- */
  var tt = null;
  function toast() {
    try {
      var host = D.body || D.documentElement; if (!host) { return; }
      var t = D.getElementById("wma-lock-toast");
      if (!t) {
        t = D.createElement("div"); t.id = "wma-lock-toast"; t.setAttribute("role", "status");
        t.style.cssText = "position:fixed;left:50%;bottom:22px;transform:translateX(-50%);z-index:2147483600;max-width:90vw;padding:9px 16px;" +
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
    D.addEventListener(ev, function (e) { if (ev === "selectstart" && editable(e.target)) { return; } e.preventDefault(); }, true);
  });
  ["copy", "cut"].forEach(function (ev) {
    D.addEventListener(ev, function (e) { if (editable(e.target)) { return; } e.preventDefault(); if (e.clipboardData) { try { e.clipboardData.setData("text/plain", ""); } catch (x) { } } toast(); }, true);
  });
  D.addEventListener("keydown", function (e) {
    var k = String(e.key || "").toLowerCase(), c = e.ctrlKey || e.metaKey, block = false;
    if (e.key === "F12") { block = true; }
    else if (c && e.shiftKey && (k === "i" || k === "j" || k === "c" || k === "k")) { block = true; }
    else if (c && (k === "s" || k === "p" || k === "u")) { block = true; }
    else if (c && k === "a" && !editable(e.target)) { block = true; }
    else if (c && (k === "c" || k === "x") && !editable(e.target)) { block = true; }
    if (block) { e.preventDefault(); e.stopPropagation(); toast(); }
  }, true);
  W.addEventListener("beforeprint", function () { try { D.documentElement.style.display = "none"; } catch (e) { } });
  W.addEventListener("afterprint", function () { try { D.documentElement.style.display = ""; } catch (e) { } });

  /* ---------- downloads and printing ---------- */
  try { W.print = function () { toast(); }; } catch (e) { }
  function isFileLink(a) {
    if (!a || a.tagName !== "A") { return false; }
    var h = String(a.getAttribute("href") || "");
    return a.hasAttribute("download") || /^(blob|data):/i.test(h);
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
    W.open = function (u) { if (/^(blob|data):/i.test(String(u || ""))) { toast(); return null; } return wo.apply(W, arguments); };
  } catch (e) { }
  try {
    if (navigator.clipboard) {
      var deny = function () { toast(); return Promise.reject(new Error(MSG)); };
      try { navigator.clipboard.writeText = deny; } catch (e1) { }
      try { navigator.clipboard.write = deny; } catch (e2) { }
    }
  } catch (e) { }
  try { if (W.showSaveFilePicker) { W.showSaveFilePicker = function () { toast(); return Promise.reject(new Error(MSG)); }; } } catch (e) { }

  /* ---------- the buttons and links that export ---------- */
  function label(el) {
    /* the visible words of a short control; a long one (a tile that opens a page) is never an export button */
    var t = (el.children.length > 4 ? "" : (el.textContent || "")).replace(/\s+/g, " ").trim();
    if (!t) { t = String(el.getAttribute("aria-label") || el.getAttribute("title") || "").replace(/\s+/g, " ").trim(); }
    return t.length > 60 ? "" : t;
  }
  function sweep() {
    var l = D.querySelectorAll("button, a, [role='button'], summary"), i, el, hit;
    for (i = 0; i < l.length; i++) {
      el = l[i]; if (el.hasAttribute("data-wma-locked")) { continue; }
      if (el.closest && el.closest("#wma-gate")) { continue; }
      var lb = label(el); hit = lb ? EXPORT_RX.exec(lb) : null;
      if (hit) { el.setAttribute("data-wma-locked", hit[2].toLowerCase()); el.setAttribute("aria-disabled", "true"); el.setAttribute("title", MSG); }
    }
  }
  D.addEventListener("click", function (e) {
    var t = e.target && e.target.closest ? e.target.closest("[data-wma-locked], a[download]") : null;
    if (!t) { return; }
    e.preventDefault(); e.stopImmediatePropagation(); toast();
  }, true);
  var timer = null;
  function later() { if (timer) { return; } timer = setTimeout(function () { timer = null; sweep(); }, 350); }
  D.addEventListener("click", later, false);
  setInterval(sweep, 2000);
  if (D.readyState === "loading") { D.addEventListener("DOMContentLoaded", sweep); } else { sweep(); }
})();
