"use strict";

// Shared helpers of the review-dashboard pages (US-30 to US-36, US-37).
// The pages carry no data; everything goes through authenticated API calls.

var memoryAuth = null;
var loginPromise = null;

function storedAuth() {
  try {
    var shared = JSON.parse(localStorage.getItem("pocketbase_auth") || "null");
    if (shared && shared.token) return { token: shared.token, record: shared.record || null };
    var legacyToken = localStorage.getItem("archivist.token") || "";
    return legacyToken ? { token: legacyToken, record: null, legacy: true } : null;
  } catch (e) {
    return memoryAuth;
  }
}

function storedToken() {
  var auth = storedAuth();
  return auth ? auth.token : "";
}

function saveAuth(auth) {
  memoryAuth = { token: auth.token, record: auth.record || null };
  try {
    localStorage.setItem("pocketbase_auth", JSON.stringify({
      token: auth.token,
      record: auth.record || null
    }));
    localStorage.removeItem("archivist.token");
  } catch (e) {}
}

function clearToken() {
  memoryAuth = null;
  try {
    localStorage.removeItem("pocketbase_auth");
    localStorage.removeItem("archivist.token");
  } catch (e) {}
}

function tokenExpiresSoon(token) {
  try {
    var payload = JSON.parse(atob(token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/")));
    return !payload.exp || payload.exp * 1000 < Date.now() + 24 * 60 * 60 * 1000;
  } catch (e) {
    return true;
  }
}

async function refreshStoredToken() {
  var auth = storedAuth();
  if (!auth) return "";
  if (!auth.legacy && !tokenExpiresSoon(auth.token)) return auth.token;

  try {
    var result = await rawFetch("POST", "/api/collections/_superusers/auth-refresh", undefined, auth.token);
    if (result.status === 200 && result.data && result.data.token) {
      saveAuth(result.data);
      return result.data.token;
    }
    if (result.status === 401) clearToken();
  } catch (e) {}
  return storedToken();
}

function requestLogin() {
  if (loginPromise) return loginPromise;

  loginPromise = new Promise(function (resolve) {
    var dialog = document.createElement("dialog");
    dialog.className = "auth-dialog";
    dialog.setAttribute("aria-labelledby", "auth-title");
    dialog.innerHTML = '<div class="auth-card">' +
      '<h2 id="auth-title">Connexion requise</h2>' +
      '<p>Connectez-vous avec un compte superutilisateur.</p>' +
      '<form class="auth-form">' +
      '<label>Adresse e-mail<input name="identity" type="email" autocomplete="username" required autofocus></label>' +
      '<label>Mot de passe<input name="password" type="password" autocomplete="current-password" required></label>' +
      '<p class="auth-error" role="alert" aria-live="polite"></p>' +
      '<div class="auth-actions"><a href="/_/">Administration PocketBase</a>' +
      '<button class="btn primary" type="submit">Se connecter</button></div>' +
      '</form></div>';

    var form = dialog.querySelector("form");
    var error = dialog.querySelector(".auth-error");
    var submit = form.querySelector("button[type=submit]");
    form.addEventListener("submit", async function (event) {
      event.preventDefault();
      error.textContent = "";
      submit.disabled = true;
      try {
        var result = await rawFetch("POST", "/api/collections/_superusers/auth-with-password", {
          identity: form.elements.identity.value,
          password: form.elements.password.value
        }, "");
        var token = result.data && result.data.token;
        if (result.status !== 200 || !token) {
          error.textContent = result.data && result.data.message || "Identifiants invalides.";
          submit.disabled = false;
          return;
        }
        saveAuth(result.data);
        dialog.close();
        dialog.remove();
        resolve(token);
      } catch (err) {
        error.textContent = "Connexion impossible. Vérifiez le réseau puis réessayez.";
        submit.disabled = false;
      }
    });
    dialog.addEventListener("cancel", function (event) {
      event.preventDefault();
    });
    document.body.appendChild(dialog);
    dialog.showModal();
  });

  var pending = loginPromise;
  return pending.finally(function () {
    loginPromise = null;
  });
}

// api retries the blocked request after sign-in. A stale token is cleared
// first; the server still enforces superuser access on every protected route.
async function api(method, path, body) {
  var token = await refreshStoredToken();
  var res = await rawFetch(method, path, body, token);
  if (res.status === 401 && token) {
    clearToken();
    token = "";
    res = await rawFetch(method, path, body, token);
  }
  if (res.status === 401 || (res.status === 403 && path.indexOf("/api/archivist/") === 0)) {
    token = await requestLogin();
    res = await rawFetch(method, path, body, token);
    if (res.status === 401) clearToken();
  }
  return res;
}

// An iframe cannot attach the dashboard's Authorization header, so protected
// binary resources use fetch and are assigned to the frame as object URLs.
async function apiBlob(path) {
  var token = await refreshStoredToken();
  var res = await rawBlob(path, token);
  if (res.status === 401 && token) {
    clearToken();
    token = "";
    res = await rawBlob(path, token);
  }
  if (res.status === 401 || (res.status === 403 && path.indexOf("/api/archivist/") === 0)) {
    token = await requestLogin();
    res = await rawBlob(path, token);
    if (res.status === 401) clearToken();
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

async function rawBlob(path, token) {
  var opts = { method: "GET", headers: {} };
  if (token) opts.headers["Authorization"] = token;
  return fetch(path, opts);
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
  if (typeof v === "string") {
    if (/^\d{4}-\d{2}-\d{2}T/.test(v)) return fmtDate(v);
    return v;
  }
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

// buildEnumControl builds the enum dropdown (US-32, US-33, US-34, US-37):
// only the official values of the field's select list, plus the
// "Ajouter une valeur…" entry that opens the registration modal. A value
// the record carries but the list does not know (e.g. a pipeline
// proposal) appears as a temporary option; it is registered by the
// correction or the import path.
function buildEnumControl(meta, vocab, value) {
  var current = value === null || value === undefined ? "" : String(value);
  var wrapper = document.createElement("div");
  wrapper.className = "enum-control";
  wrapper.dataset.field = meta.name;

  var select = document.createElement("select");
  addOption(select, "", "—");
  (vocab || []).forEach(function (item) { addOption(select, item, item); });
  if (current && !(vocab || []).includes(current)) insertEnumOption(select, current);
  addOption(select, "__archivist_add_value__", "Ajouter une valeur…");

  select.value = current;
  select.addEventListener("change", function () {
    if (select.value === "__archivist_add_value__") {
      select.value = "";
      promptNewEnumValue(meta, select);
    }
  });

  Object.defineProperty(wrapper, "value", {
    get: function () { return select.value; },
    set: function (next) {
      var nextValue = next === null || next === undefined ? "" : String(next);
      var optionExists = Array.from(select.options).some(function (option) {
        return option.value === nextValue;
      });
      if (!optionExists && nextValue !== "") insertEnumOption(select, nextValue);
      select.value = nextValue;
    }
  });

  wrapper.appendChild(select);
  return wrapper;
}

// insertEnumOption adds an option before the "Ajouter une valeur…" entry.
function insertEnumOption(select, value) {
  var addEntry = null;
  Array.from(select.options).forEach(function (option) {
    if (option.value === "__archivist_add_value__") addEntry = option;
  });
  var o = document.createElement("option");
  o.value = value;
  o.textContent = value;
  if (addEntry) select.insertBefore(o, addEntry); else select.appendChild(o);
}

// promptNewEnumValue is the modal behind "Ajouter une valeur…" (US-37):
// the value is registered on the field's official select list through the
// dedicated route, then selected in the dropdown.
function promptNewEnumValue(meta, select) {
  var dialog = document.createElement("dialog");
  dialog.className = "auth-dialog";
  dialog.setAttribute("aria-labelledby", "enum-value-title");
  dialog.innerHTML = '<div class="auth-card">' +
    '<h2 id="enum-value-title">Nouvelle valeur</h2>' +
    '<p>Ajout au champ ' + meta.name + '.</p>' +
    '<form class="auth-form">' +
    '<label>Valeur<input name="value" type="text" required autofocus></label>' +
    '<p class="auth-error" role="alert" aria-live="polite"></p>' +
    '<div class="auth-actions"><button class="btn secondary" type="button">Annuler</button>' +
    '<button class="btn primary" type="submit">Ajouter</button></div>' +
    '</form></div>';

  var form = dialog.querySelector("form");
  var error = dialog.querySelector(".auth-error");
  dialog.querySelector("button[type=button]").addEventListener("click", function () {
    dialog.close();
  });
  dialog.addEventListener("close", function () {
    if (select.value === "__archivist_add_value__") select.value = "";
    dialog.remove();
  });
  form.addEventListener("submit", async function (event) {
    event.preventDefault();
    error.textContent = "";
    var value = form.elements.value.value.trim();
    if (!value) {
      error.textContent = "La valeur ne peut pas être vide.";
      return;
    }
    var res = await api("POST", "/api/archivist/fields/" + encodeURIComponent(meta.name) + "/values", { value: value });
    if (res.status !== 200) {
      error.textContent = errMsg(res);
      return;
    }
    insertEnumOption(select, value);
    select.value = value;
    dialog.close();
    select.dispatchEvent(new Event("change", { bubbles: true }));
  });
  document.body.appendChild(dialog);
  dialog.showModal();
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
    return buildEnumControl(meta, vocab, value);
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
