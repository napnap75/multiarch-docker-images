"use strict";

// Document detail page (US-34 to US-36): PDF preview, typed form,
// pipeline proposals, live rendered-name dry-run, event history, the
// rule trace panel (US-42), and the three distinct exits - draft save,
// full correction, validation.

var docId = new URLSearchParams(window.location.search).get("id");

var cfg = null;
var record = null;
var controls = {};   // field name -> control element
var originals = {};  // field name -> control value at load
var dirty = {};      // field name -> boolean
var dryErrors = [];
var dryTimer = null;
var dryRunSequence = 0;
var traceData = null;   // US-42: rule evaluation trace from the classification dry-run
var traceErrors = [];   // US-42: dry-run errors shown at the top of the trace panel

function $(id) { return document.getElementById(id); }

async function init() {
  if (!docId) { window.location.href = "/"; return; }
  var rc = await api("GET", "/api/archivist/config");
  if (rc.status !== 200) return;
  var rr = await api("GET", "/api/collections/documents/records/" + docId);
  if (rr.status !== 200) { alert("Document introuvable."); window.location.href = "/"; return; }
  cfg = rc.data;
  record = rr.data;

  initTheme("themeBtn");
  renderHead();
  renderDocumentPages();
  renderDocumentNavigation();
  loadPdf();
  var evs = await loadEvents();
  renderImportErrors(evs);
  renderHistory(evs);
  renderForm();
  bindActions();
  if (record.status === "processed") $("validateBtn").hidden = false;
  loadCurrentTarget();
  loadProposals();
}

function renderDocumentNavigation() {
  var ids = [];
  try {
    var stored = JSON.parse(sessionStorage.getItem("archivist.documentIds") || "[]");
    if (Array.isArray(stored)) ids = stored;
  } catch (e) {}
  var index = ids.indexOf(docId);
  setDocumentLink("prevDoc", index > 0 ? ids[index - 1] : null);
  setDocumentLink("nextDoc", index >= 0 && index < ids.length - 1 ? ids[index + 1] : null);
}

function setDocumentLink(id, targetId) {
  var link = $(id);
  if (targetId) {
    link.href = "/document.html?id=" + encodeURIComponent(targetId);
    link.removeAttribute("aria-disabled");
    link.removeAttribute("tabindex");
  } else {
    link.removeAttribute("href");
    link.setAttribute("aria-disabled", "true");
    link.setAttribute("tabindex", "-1");
  }
}

async function loadEvents() {
  var q = "/api/collections/document_events/records?perPage=200&sort=-timestamp&filter=" +
    encodeURIComponent("(document='" + docId + "')");
  var r = await api("GET", q);
  return (r.status === 200 && r.data.items) ? r.data.items : [];
}

function renderHead() {
  var fileKey = record.file_key || ("Document " + docId);
  var fileName = fileKey.substring(fileKey.lastIndexOf("/") + 1);
  document.title = "Archivist — " + fileName;
  $("docname").textContent = fileName;
  var st = $("docstatus");
  st.textContent = record.status || "";
  st.className = "badge st-" + record.status;
}

function renderDocumentPages() {
  var pages = record.pages;
  if (typeof pages === "string") {
    try { pages = JSON.parse(pages); } catch (e) { pages = [pages]; }
  }
  if (!Array.isArray(pages)) pages = [];
  var text = pages.map(function (page, index) {
    return "Page " + (index + 1) + "\n\n" + String(page || "");
  }).join("\n\n");
  $("pagesText").textContent = text || "Aucun texte extrait.";
}

async function loadPdf() {
  var iframe = $("pdf");
  var error = $("pdferror");
  try {
    var response = await apiBlob("/api/archivist/documents/" + docId + "/pdf");
    if (!response.ok) {
      var data = null;
      try { data = await response.json(); } catch (e) {}
      error.textContent = "Aperçu indisponible : " + (data && data.message || "HTTP " + response.status);
      error.hidden = false;
      iframe.hidden = true;
      return;
    }
    var objectUrl = URL.createObjectURL(await response.blob());
    iframe.src = objectUrl;
    window.addEventListener("pagehide", function () { URL.revokeObjectURL(objectUrl); }, { once: true });
  } catch (e) {
    var reason = e && e.message ? e.message : String(e);
    console.error("PDF preview failed:", e);
    error.textContent = "Impossible de charger l'aperçu PDF : " + reason;
    error.hidden = false;
    iframe.hidden = true;
  }
}

function payloadOf(ev) {
  var p = ev.payload;
  if (typeof p === "string") {
    try { p = JSON.parse(p); } catch (e) { p = {}; }
  }
  return p || {};
}

function renderImportErrors(evs) {
  var box = $("importerrors");
  for (var i = 0; i < evs.length; i++) {
    var p = payloadOf(evs[i]);
    if (evs[i].event_type === "imported" && p.errors && p.errors.length) {
      box.hidden = false;
      box.innerHTML = "";
      var head = document.createElement("div");
      head.textContent = "Échec de la classification à l'import :";
      box.appendChild(head);
      var ul = document.createElement("ul");
      p.errors.forEach(function (e) {
        var li = document.createElement("li");
        li.textContent = e;
        ul.appendChild(li);
      });
      box.appendChild(ul);
      return;
    }
  }
  box.hidden = true;
}

function renderForm() {
  var form = $("docform");
  form.innerHTML = "";
  cfg.fields.forEach(function (f) {
    var row = document.createElement("div");
    row.className = "fieldrow";
    var label = document.createElement("label");
    label.textContent = fieldTitle(f.name) + (f.mandatory ? " *" : "");
    var ctl = buildValueControl(f, (cfg.enums || {})[f.name] || [], record[f.name]);
    controls[f.name] = ctl;
    originals[f.name] = ctl.value;
    dirty[f.name] = false;
    ctl.addEventListener("input", function () { fieldChanged(f); });
    ctl.addEventListener("change", function () { fieldChanged(f); });
    var prop = document.createElement("div");
    prop.className = "proposal";
    prop.hidden = true;
    prop.id = "prop-" + f.name;
    row.appendChild(label);
    row.appendChild(ctl);
    row.appendChild(prop);
    form.appendChild(row);
  });
}

function fieldTitle(name) {
  for (var i = 0; i < cfg.fields.length; i++) {
    if (cfg.fields[i].name === name) return cfg.fields[i].french || name;
  }
  return name;
}

function fieldChanged(f) {
  dirty[f.name] = controls[f.name].value !== originals[f.name];
  dryRunSequence++;
  renderTargetName("");
  if (dryTimer) clearTimeout(dryTimer);
  dryTimer = setTimeout(runDryRun, 400);
}

// dirtyBody collects the modified fields for a dry-run, a draft save or
// a full correction; empty or null clears the field.
function dirtyBody() {
  var body = {};
  var any = false;
  for (var i = 0; i < cfg.fields.length; i++) {
    var f = cfg.fields[i];
    if (!dirty[f.name]) continue;
    var r;
    try {
      r = readValueControl(f, controls[f.name]);
    } catch (e) {
      return { error: e.message };
    }
    body[f.name] = r.clear ? null : r.value;
    any = true;
  }
  return { body: body, any: any };
}

async function runDryRun() {
  var sequence = ++dryRunSequence;
  var d = dirtyBody();
  if (d.error) { renderTargetName(""); renderDryErrors([d.error]); return; }
  var res = await api("POST", "/api/archivist/documents/" + docId + "/dry-run", d.body);
  if (sequence !== dryRunSequence) return;
  if (res.status !== 200) {
    renderTargetName("");
    renderDryErrors(["dry-run : " + errMsg(res)]);
    return;
  }
  dryErrors = res.data.errors || [];
  renderDryErrors(dryErrors);
  renderTargetName(dryErrors.length ? "" : res.data.rendered);
}

async function loadCurrentTarget() {
  var sequence = ++dryRunSequence;
  var res = await api("POST", "/api/archivist/documents/" + docId + "/dry-run", {});
  if (sequence !== dryRunSequence || res.status !== 200) return;
  var errors = res.data.errors || [];
  renderTargetName(errors.length ? "" : res.data.rendered);
}

function renderTargetName(rendered) {
  var targetKey = rendered ? "files/" + rendered : "";
  $("targetname").textContent = targetKey && targetKey !== record.file_key
    ? "Clé cible : " + targetKey : "";
}

function renderDryErrors(errs) {
  var box = $("dryerrors");
  if (!errs || !errs.length) {
    box.hidden = true;
    box.innerHTML = "";
    $("correctBtn").disabled = false;
    return;
  }
  box.hidden = false;
  box.innerHTML = "";
  errs.forEach(function (e) {
    var div = document.createElement("div");
    div.textContent = e;
    box.appendChild(div);
  });
  $("correctBtn").disabled = true; // US-35: no full correction while errors remain
}

// loadProposals runs the classification dry-run (no body) and offers,
// under each field whose proposed value differs from the current one,
// the pipeline value with one-click adoption (US-35).
async function loadProposals() {
  var res = await api("POST", "/api/archivist/documents/" + docId + "/dry-run");
  if (res.status !== 200) { traceData = null; renderTracePanel(); return; }
  traceData = res.data.trace || [];
  traceErrors = res.data.errors || [];
  renderTracePanel();
  var props = res.data.fields || {};
  cfg.fields.forEach(function (f) {
    var p = props[f.name];
    if (p === null || p === undefined) return;
    var raw;
    switch (f.type) {
    case "date": raw = pbDateInput(p); break;
    case "boolean": raw = p ? "true" : "false"; break;
    default: raw = String(p);
    }
    if (raw === originals[f.name]) return;
    var slot = document.getElementById("prop-" + f.name);
    slot.hidden = false;
    slot.innerHTML = "";
    var span = document.createElement("span");
    span.textContent = "Pipeline : " + fmtVal(p);
    slot.appendChild(span);
    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "btn secondary";
    btn.textContent = "Adopter";
    btn.addEventListener("click", function () {
      controls[f.name].value = raw;
      slot.hidden = true;
      fieldChanged(f);
    });
    slot.appendChild(btn);
  });
}

function bindActions() {
  $("draftBtn").addEventListener("click", draftSave);
  $("correctBtn").addEventListener("click", fullCorrect);
  $("validateBtn").addEventListener("click", validate);
  $("pagesToggle").addEventListener("click", function () { togglePanel("pagesPanel", "pagesToggle"); });
  $("historyToggle").addEventListener("click", function () { togglePanel("historyPanel", "historyToggle"); });
  $("traceToggle").addEventListener("click", function () { togglePanel("tracePanel", "traceToggle"); });
}

function togglePanel(panelId, buttonId) {
  var panel = $(panelId);
  panel.hidden = !panel.hidden;
  $(buttonId).textContent = panel.hidden ? "Afficher" : "Masquer";
  $(buttonId).setAttribute("aria-expanded", panel.hidden ? "false" : "true");
}

// renderTracePanel fills the collapsed-by-default rule trace panel
// (US-42): the dry-run errors first, then one line per traced rule.
function renderTracePanel() {
  var badge = $("traceBadge");
  if (traceErrors.length) {
    badge.textContent = String(traceErrors.length) + " erreur" + (traceErrors.length > 1 ? "s" : "");
    badge.hidden = false;
  } else {
    badge.hidden = true;
  }
  var errsBox = $("traceErrors");
  if (traceErrors.length) {
    errsBox.hidden = false;
    errsBox.innerHTML = "";
    var head = document.createElement("div");
    head.textContent = "Erreurs du dry-run :";
    errsBox.appendChild(head);
    var ul = document.createElement("ul");
    traceErrors.forEach(function (e) {
      var li = document.createElement("li");
      li.textContent = e;
      ul.appendChild(li);
    });
    errsBox.appendChild(ul);
  } else {
    errsBox.hidden = true;
    errsBox.innerHTML = "";
  }
  var list = $("traceList");
  list.innerHTML = "";
  if (traceData === null) {
    var na = document.createElement("div");
    na.className = "muted";
    na.textContent = "Trace indisponible.";
    list.appendChild(na);
    return;
  }
  if (!traceData.length) {
    var empty = document.createElement("div");
    empty.className = "muted";
    empty.textContent = "Aucune règle n'a modifié de valeur ni produit d'erreur.";
    list.appendChild(empty);
    return;
  }
  traceData.forEach(function (t) {
    var line = document.createElement("div");
    line.className = "trace-line" + (t.op === "error" ? " trace-error" : "");
    var rule = document.createElement("div");
    rule.className = "trace-rule mono";
    rule.textContent = "when " + t.rule;
    line.appendChild(rule);
    var body = document.createElement("div");
    body.className = "trace-body";
    var field = document.createElement("span");
    field.className = "k";
    field.textContent = fieldTitle(t.field) + " : ";
    body.appendChild(field);
    if (t.op === "delete" || t.deleted) {
      var del = document.createElement("span");
      del.className = "trace-deleted";
      del.textContent = "valeur supprimée";
      body.appendChild(del);
    } else if (t.op === "error") {
      var err = document.createElement("span");
      err.textContent = t.error || "erreur";
      body.appendChild(err);
    } else {
      var val = document.createElement("span");
      val.textContent = fmtVal(t.value);
      body.appendChild(val);
      if (t.expr) {
        var expr = document.createElement("div");
        expr.className = "trace-expr mono";
        expr.textContent = "eval " + t.expr;
        body.appendChild(expr);
      }
    }
    line.appendChild(body);
    list.appendChild(line);
  });
}

// Draft save: a standard PATCH, no re-filing, no status change (US-22).
async function draftSave() {
  var d = dirtyBody();
  if (d.error) { renderDryErrors([d.error]); return; }
  if (!d.any) return;
  var res = await api("PATCH", "/api/collections/documents/records/" + docId, d.body);
  if (res.status !== 200) { renderDryErrors([errMsg(res)]); return; }
  window.location.reload();
}

// Full correction: re-filing and validation in one step (US-36).
async function fullCorrect() {
  if (dryErrors.length) return;
  var d = dirtyBody();
  if (d.error) { renderDryErrors([d.error]); return; }
  var res = await api("POST", "/api/archivist/documents/" + docId + "/correct", d.body);
  if (res.status === 200) { window.location.reload(); return; }
  renderDryErrors((res.data && res.data.errors && res.data.errors.length)
    ? res.data.errors : [errMsg(res)]);
}

// Simple validation of a processed document (US-23).
async function validate() {
  var res = await api("PATCH", "/api/collections/documents/records/" + docId, { status: "validated" });
  if (res.status === 200) { window.location.reload(); return; }
  renderDryErrors([errMsg(res)]);
}

function renderHistory(evs) {
  var hist = $("history");
  hist.innerHTML = "";
  evs.forEach(function (ev) {
    var p = payloadOf(ev);
    var item = document.createElement("div");
    item.className = "hist";
    var head = document.createElement("div");
    head.className = "head";
    var b = document.createElement("span");
    b.className = "badge";
    b.textContent = ev.event_type;
    head.appendChild(b);
    var time = document.createElement("time");
    time.textContent = fmtDate(ev.timestamp);
    head.appendChild(time);
    item.appendChild(head);
    if (p.errors && p.errors.length) {
      var ul = document.createElement("ul");
      p.errors.forEach(function (e) {
        var li = document.createElement("li");
        li.textContent = e;
        ul.appendChild(li);
      });
      item.appendChild(ul);
    }
    var before = p.before || {};
    var after = p.after || {};
    var keys = {};
    Object.keys(before).forEach(function (k) { keys[k] = true; });
    Object.keys(after).forEach(function (k) { keys[k] = true; });
    Object.keys(keys).forEach(function (k) {
      var line = document.createElement("div");
      line.className = "diff";
      var ks = document.createElement("span");
      ks.className = "k";
      ks.textContent = fieldTitle(k) + " : ";
      var bv = document.createElement("span");
      bv.textContent = fmtVal(before[k]);
      var ar = document.createElement("span");
      ar.className = "arrow";
      ar.textContent = " → ";
      var av = document.createElement("span");
      av.textContent = fmtVal(after[k]);
      line.appendChild(ks);
      line.appendChild(bv);
      line.appendChild(ar);
      line.appendChild(av);
      item.appendChild(line);
    });
    hist.appendChild(item);
  });
}

init();
