"use strict";

// Shared helpers of the review-dashboard pages (US-30 to US-36).
// The pages carry no data; everything goes through authenticated API
// calls, the admin session cookie riding along.

function redirectLogin() {
  window.location.href = "/_/";
}

function storedToken() {
  try {
    return localStorage.getItem("archivist.token") || "";
  } catch (e) {
    return "";
  }
}

// api performs one authenticated call. A stale stored token is dropped
// and the call retried once; an unauthenticated visitor is redirected to
// the admin UI (US-30).
async function api(method, path, body) {
  var res = await rawFetch(method, path, body, storedToken());
  if (res.status === 401 && storedToken()) {
    try { localStorage.removeItem("archivist.token"); } catch (e) {}
    res = await rawFetch(method, path, body, "");
  }
  if (res.status === 401 || (res.status === 403 && path.indexOf("/api/archivist/") === 0)) {
    redirectLogin();
  }
  return res;
}

async function rawFetch(method, path, body, token) {
  var opts = { method: method, headers: {} };
  if (token) {
    opts.headers["Authorization"] = token;
  }
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  var res = await fetch(path, opts);
  var data = null;
  try { data = await res.json(); } catch (e) { data = null; }
  return { status: res.status, data: data };
}

function errMsg(res) {
  if (res && res.data && res.data.message) return res.data.message;
  if (res && res.data) {
    var s = JSON.stringify(res.data);
    if (s && s !== "{}") return s.slice(0, 300);
  }
  return "status " + (res ? res.status : "?");
}

// initTheme wires the light/dark toggle: the class on <html> is set by
// the inline head script before first paint, the button persists the
// manual choice (arch.md 7.5).
function initTheme(btnId) {
  var btn = document.getElementById(btnId);
  if (!btn) return;
  btn.addEventListener("click", function () {
    var dark = document.documentElement.classList.toggle("dark");
    document.documentElement.classList.toggle("light", !dark);
    try {
      localStorage.setItem("archivist.theme", dark ? "dark" : "light");
    } catch (e) {}
  });
}

function fmtVal(v) {
  if (v === null || v === undefined || v === "") return "—";
  if (typeof v === "boolean") return v ? "true" : "false";
  if (typeof v === "string") return v;
  return String(v);
}

function fmtDate(v) {
  if (!v) return "—";
  return String(v).replace("T", " ").slice(0, 16);
}

function pbDateInput(v) {
  if (!v) return "";
  return String(v).slice(0, 10);
}

function addOption(sel, value, label) {
  var o = document.createElement("option");
  o.value = value;
  o.textContent = label;
  sel.appendChild(o);
}

// buildValueControl builds the typed control of one field (US-33 bulk
// edit, US-34 form, US-32 filters).
function buildValueControl(meta, vocab, value) {
  var el;
  var s = (value === null || value === undefined) ? "" : String(value);
  switch (meta.type) {
  case "number":
    el = document.createElement("input");
    el.type = "number";
    el.step = "any";
    el.value = s;
    break;
  case "date":
    el = document.createElement("input");
    el.type = "date";
    el.value = pbDateInput(s);
    break;
  case "boolean":
    el = document.createElement("select");
    addOption(el, "", "—");
    addOption(el, "true", "oui");
    addOption(el, "false", "non");
    el.value = value === true ? "true" : (value === false ? "false" : "");
    break;
  case "enum":
    el = document.createElement("select");
    addOption(el, "", "—");
    (vocab || []).forEach(function (v) { addOption(el, v, v); });
    el.value = s;
    break;
  default:
    el = document.createElement("input");
    el.type = "text";
    el.value = s;
  }
  el.dataset.field = meta.name;
  return el;
}

// readValueControl reads the control back: {clear:true} when empty,
// {value:v} otherwise. Throws on a malformed value.
function readValueControl(meta, el) {
  var v = el.value;
  switch (meta.type) {
  case "number":
    if (v === "") return { clear: true };
    if (isNaN(Number(v))) throw new Error(meta.name + " : nombre invalide");
    return { value: Number(v) };
  case "date":
    if (v === "") return { clear: true };
    return { value: v };
  case "boolean":
    if (v === "") return { clear: true };
    return { value: v === "true" };
  default:
    if (v === "") return { clear: true };
    return { value: v };
  }
}
