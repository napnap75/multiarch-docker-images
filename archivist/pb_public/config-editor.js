"use strict";

var state = {
  fields: [],
  templates: {},
  groups: [],
  record: null,
  documentIds: [],
  sequence: 0,
  version: 0,
  timer: null,
  draftValid: false,
  dirty: false,
  configVersion: 0
};

function $(id) { return document.getElementById(id); }

function appendTextCell(row, value) {
  var cell = document.createElement("td");
  var input = document.createElement("input");
  input.type = "text";
  input.value = value || "";
  input.addEventListener("input", scheduleDryRun);
  cell.appendChild(input);
  row.appendChild(cell);
  return input;
}

function renderFields() {
  var body = $("fieldsBody");
  body.innerHTML = "";
  state.fields.forEach(function (field, index) {
    var row = document.createElement("tr");
    appendTextCell(row, field.name);
    appendTextCell(row, field.french);

    var typeCell = document.createElement("td");
    var typeSelect = document.createElement("select");
    ["text", "number", "date", "boolean", "enum"].forEach(function (type) {
      addOption(typeSelect, type, type);
    });
    typeSelect.value = field.type;
    typeSelect.addEventListener("change", scheduleDryRun);
    typeCell.appendChild(typeSelect);
    row.appendChild(typeCell);

    var mandatoryCell = document.createElement("td");
    mandatoryCell.className = "config-check-cell";
    var mandatory = document.createElement("input");
    mandatory.type = "checkbox";
    mandatory.checked = !!field.mandatory;
    mandatory.addEventListener("change", scheduleDryRun);
    mandatoryCell.appendChild(mandatory);
    row.appendChild(mandatoryCell);

    var actionCell = document.createElement("td");
    var remove = document.createElement("button");
    remove.type = "button";
    remove.className = "icon-control config-remove";
    remove.setAttribute("aria-label", "Supprimer le champ " + (field.name || index + 1));
    remove.title = "Supprimer le champ";
    remove.textContent = "×";
    remove.addEventListener("click", function () {
      syncDraftState();
      state.fields.splice(index, 1);
      renderFields();
      scheduleDryRun();
    });
    actionCell.appendChild(remove);
    row.appendChild(actionCell);
    body.appendChild(row);
  });
}

function renderTemplates() {
  var body = $("templatesBody");
  body.innerHTML = "";
  Object.keys(state.templates).forEach(function (name, index) {
    var row = document.createElement("tr");
    appendTextCell(row, name);
    var valueCell = document.createElement("td");
    var value = document.createElement("textarea");
    value.rows = 2;
    value.value = state.templates[name] || "";
    value.addEventListener("input", scheduleDryRun);
    valueCell.appendChild(value);
    row.appendChild(valueCell);

    var actionCell = document.createElement("td");
    var remove = document.createElement("button");
    remove.type = "button";
    remove.className = "icon-control config-remove";
    remove.setAttribute("aria-label", "Supprimer le modèle " + name);
    remove.title = "Supprimer le modèle";
    remove.textContent = "×";
    remove.addEventListener("click", function () {
      syncDraftState();
      delete state.templates[row.querySelector("input").value.trim()];
      renderTemplates();
      scheduleDryRun();
    });
    actionCell.appendChild(remove);
    row.appendChild(actionCell);
    body.appendChild(row);
  });
}

function renderGroups() {
  var container = $("ruleGroups");
  container.innerHTML = "";
  state.groups.forEach(function (group, index) {
    var details = document.createElement("details");
    details.className = "rule-group";
    details.open = index === 0;
    var summary = document.createElement("summary");
    summary.textContent = groupTitle(group.header, index);
    details.appendChild(summary);

    var textarea = document.createElement("textarea");
    textarea.className = "rule-source mono";
    textarea.rows = 10;
    textarea.spellcheck = false;
    textarea.value = group.rules || "[]";
    textarea.setAttribute("aria-label", "Règles de " + groupTitle(group.header, index));
    textarea.addEventListener("input", scheduleDryRun);
    details.appendChild(textarea);

    var actions = document.createElement("div");
    actions.className = "rule-group-actions";
    var remove = document.createElement("button");
    remove.type = "button";
    remove.className = "btn secondary";
    remove.textContent = "Supprimer la section";
    remove.addEventListener("click", function () {
      syncDraftState();
      state.groups.splice(index, 1);
      renderGroups();
      scheduleDryRun();
    });
    actions.appendChild(remove);
    details.appendChild(actions);
    container.appendChild(details);
  });
}

function groupTitle(header, index) {
  var title = String(header || "").replace(/^\/\/\s*---\s*/, "").replace(/\s*-+\s*$/, "").trim();
  return title || "Règles " + (index + 1);
}

function readDraft() {
  var fields = [];
  Array.from($("fieldsBody").rows).forEach(function (row) {
    var inputs = row.querySelectorAll("input");
    fields.push({
      name: inputs[0].value.trim(),
      french: inputs[1].value.trim(),
      type: row.querySelector("select").value,
      mandatory: inputs[2].checked
    });
  });

  var templates = {};
  Array.from($("templatesBody").rows).forEach(function (row) {
    var inputs = row.querySelectorAll("input");
    var text = row.querySelector("textarea");
    templates[inputs[0].value.trim()] = text.value;
  });

  var groups = [];
  Array.from($("ruleGroups").querySelectorAll(".rule-group")).forEach(function (details, index) {
    groups.push({
      header: state.groups[index].header || "",
      rules: details.querySelector("textarea").value
    });
  });
  return { fields: fields, templates: templates, groups: groups };
}

function syncDraftState() {
  var draft = readDraft();
  state.fields = draft.fields;
  state.templates = draft.templates;
  state.groups = draft.groups;
}

function indentJSON(value, continuationIndent) {
  var lines = value.split("\n");
  return lines[0] + lines.slice(1).map(function (line) {
    return "\n" + continuationIndent + line;
  }).join("");
}

function composeSource(draft) {
  var rules = [];
  var hasRule = false;
  var separatorWritten = false;
  draft.groups.forEach(function (group, index) {
    var header = String(group.header || "").trim();
    if (header && (header.indexOf("// --- ") !== 0 || /[\r\n]/.test(header))) {
      throw new Error("Le titre d'une section de règles est invalide.");
    }
    if (header) {
      if (hasRule && !separatorWritten) {
        rules.push(",");
        separatorWritten = true;
      }
      rules.push("    " + header);
    }
    var groupRules;
    try {
      groupRules = JSON.parse(group.rules);
    } catch (error) {
      throw new Error("JSON invalide dans « " + groupTitle(header, index) + " » : " + error.message);
    }
    if (!Array.isArray(groupRules)) {
      throw new Error("La section « " + groupTitle(header, index) + " » doit être un tableau JSON.");
    }
    groupRules.forEach(function (rule) {
      if (!rule || Array.isArray(rule) || typeof rule !== "object") {
        throw new Error("Chaque règle doit être un objet JSON.");
      }
      if (hasRule && !separatorWritten) rules.push(",");
      rules.push(indentJSON(JSON.stringify(rule, null, 2), "    "));
      hasRule = true;
      separatorWritten = false;
    });
  });

  var fieldsJSON = JSON.stringify(draft.fields, null, 2);
  var templatesJSON = JSON.stringify(draft.templates, null, 2);
  return "{\n  \"fields\": " + indentJSON(fieldsJSON, "  ") +
    ",\n  \"rules\": [\n" + rules.join("\n") +
    "\n  ],\n  \"templates\": " + indentJSON(templatesJSON, "  ") + "\n}\n";
}

function scheduleDryRun(configurationChanged) {
  state.sequence++;
  if (configurationChanged !== false) {
    state.configVersion++;
    state.dirty = true;
    state.draftValid = false;
    $("saveConfig").disabled = true;
    $("saveStatus").textContent = "Modifications non activées";
  }
  if (state.timer) clearTimeout(state.timer);
  state.timer = setTimeout(runDraft, 400);
}

function pagesValue() {
  var text = $("pagesInput").value.trim();
  if (!text) return [];
  var pages;
  try {
    pages = JSON.parse(text);
  } catch (error) {
    throw new Error("Le texte des pages doit être un tableau JSON : " + error.message);
  }
  if (!Array.isArray(pages) || pages.some(function (page) { return typeof page !== "string"; })) {
    throw new Error("Le texte doit être un tableau JSON de chaînes, une chaîne par page.");
  }
  return pages;
}

async function runDraft() {
  var sequence = ++state.sequence;
  var source;
  try {
    source = composeSource(readDraft());
  } catch (error) {
    state.draftValid = false;
    showErrors([error.message]);
    renderOutput(null);
    renderTrace([]);
    return;
  }
  var pages = [];
  var pageError = "";
  try { pages = pagesValue(); }
  catch (error) { pageError = error.message; }

  $("saveStatus").textContent = "Test en cours…";
  var result = await api("POST", "/api/archivist/config/dry-run", { source: source, pages: pages });
  if (sequence !== state.sequence) return;
  if (result.status !== 200) {
    state.draftValid = false;
    showErrors((result.data && result.data.errors) || [errMsg(result)]);
    renderOutput(null);
    renderTrace([]);
    $("saveStatus").textContent = "Configuration à corriger";
    return;
  }

  state.draftValid = true;
  showErrors((pageError ? [pageError] : []).concat(result.data.errors || []));
  renderOutput(result.data);
  renderTrace(result.data.trace || []);
  $("saveConfig").disabled = !state.dirty;
  $("saveStatus").textContent = state.dirty ? "Brouillon valide, non activé" : "Configuration active";
}

function showErrors(errors) {
  var box = $("testErrors");
  box.innerHTML = "";
  if (!errors || !errors.length) {
    box.hidden = true;
    $("noErrors").hidden = false;
    return;
  }
  $("noErrors").hidden = true;
  box.hidden = false;
  errors.forEach(function (message) {
    var line = document.createElement("div");
    line.textContent = message;
    box.appendChild(line);
  });
}

function renderOutput(result) {
  var body = $("outputFields");
  body.innerHTML = "";
  // The draft dry-run route returns the full target key, files prefix included.
  $("targetName").textContent = result && result.rendered ? "Clé cible : " + result.rendered : "";
  var proposed = result && result.fields || {};
  var fields = readDraft().fields;
  fields.forEach(function (field) {
    var row = document.createElement("tr");
    var label = document.createElement("td");
    label.textContent = field.french || field.name || "Champ sans nom";
    row.appendChild(label);

    var current = document.createElement("td");
    var currentValue = state.record && state.record[field.name];
    current.textContent = state.record ? fmtVal(currentValue) : "—";
    row.appendChild(current);

    var value = document.createElement("td");
    var proposedValue = proposed[field.name];
    value.textContent = result && Object.prototype.hasOwnProperty.call(proposed, field.name)
      ? fmtVal(proposedValue) : "—";
    if (state.record && !sameValue(currentValue, proposedValue)) value.className = "config-proposal";
    row.appendChild(value);
    body.appendChild(row);
  });
}

function sameValue(left, right) {
  if (left === null || left === undefined || right === null || right === undefined) {
    return left === right;
  }
  if (typeof left === "object" || typeof right === "object") {
    return JSON.stringify(left) === JSON.stringify(right);
  }
  return String(left) === String(right);
}

function renderTrace(trace) {
  var list = $("traceList");
  list.innerHTML = "";
  if (!trace.length) {
    var empty = document.createElement("div");
    empty.className = "muted";
    empty.textContent = "Aucune règle n'a modifié de valeur ni produit d'erreur.";
    list.appendChild(empty);
    return;
  }
  trace.forEach(function (entry) {
    var line = document.createElement("div");
    line.className = "trace-line" + (entry.op === "error" ? " trace-error" : "");
    var rule = document.createElement("div");
    rule.className = "trace-rule mono";
    rule.textContent = "when " + entry.rule;
    line.appendChild(rule);

    var body = document.createElement("div");
    body.className = "trace-body";
    var field = document.createElement("span");
    field.className = "k";
    field.textContent = (entry.field ? fieldTitle(entry.field) : "Règle") + " : ";
    body.appendChild(field);
    var value = document.createElement("span");
    if (entry.op === "delete" || entry.deleted) {
      value.className = "trace-deleted";
      value.textContent = "valeur supprimée";
    } else if (entry.op === "error") {
      value.textContent = entry.error || "erreur";
    } else {
      value.textContent = fmtVal(entry.value);
    }
    body.appendChild(value);
    if (entry.expr) {
      var expression = document.createElement("div");
      expression.className = "trace-expr mono";
      expression.textContent = "eval " + entry.expr;
      body.appendChild(expression);
    }
    line.appendChild(body);
    list.appendChild(line);
  });
}

function fieldTitle(name) {
  for (var index = 0; index < state.fields.length; index++) {
    if (state.fields[index].name === name) return state.fields[index].french || name;
  }
  return name;
}

async function loadDocument(id, initial) {
  state.record = null;
  $("documentName").textContent = "";
  $("documentKey").textContent = "";
  if (!id) {
    $("documentNav").hidden = true;
    $("pagesInput").value = "";
    renderOutput(null);
    return;
  }
  var response = await api("GET", "/api/collections/documents/records/" + encodeURIComponent(id));
  if (response.status !== 200) {
    showErrors(["Document introuvable : " + errMsg(response)]);
    return;
  }
  state.record = response.data;
  var pages = state.record.pages;
  if (typeof pages === "string") {
    try { pages = JSON.parse(pages); } catch (error) { pages = [pages]; }
  }
  if (!Array.isArray(pages)) pages = [];
  $("pagesInput").value = JSON.stringify(pages, null, 2);
  var key = state.record.file_key || "";
  $("documentName").textContent = "Nom actuel : " + (key ? key.substring(key.lastIndexOf("/") + 1) : "Document " + id);
  $("documentKey").textContent = key ? "Clé actuelle : " + key : "";
  updateDocumentNavigation(id);
  if (!initial) history.pushState({}, "", "/config.html?id=" + encodeURIComponent(id));
}

function updateDocumentNavigation(id) {
  try {
    var ids = JSON.parse(sessionStorage.getItem("archivist.documentIds") || "[]");
    if (Array.isArray(ids)) state.documentIds = ids;
  } catch (error) { state.documentIds = []; }
  var index = state.documentIds.indexOf(id);
  var previous = index > 0 ? state.documentIds[index - 1] : null;
  var next = index >= 0 && index < state.documentIds.length - 1 ? state.documentIds[index + 1] : null;
  setDocumentLink("prevDoc", previous);
  setDocumentLink("nextDoc", next);
  $("documentNav").hidden = false;
}

function setDocumentLink(id, targetId) {
  var link = $(id);
  if (!targetId) {
    link.removeAttribute("href");
    link.setAttribute("aria-disabled", "true");
    link.setAttribute("tabindex", "-1");
    return;
  }
  link.href = "/config.html?id=" + encodeURIComponent(targetId);
  link.removeAttribute("aria-disabled");
  link.removeAttribute("tabindex");
  link.onclick = async function (event) {
    event.preventDefault();
    await loadDocument(targetId, false);
    scheduleDryRun(false);
  };
}

async function saveConfig() {
  if (!state.draftValid || !state.dirty) return;
  var source;
  try { source = composeSource(readDraft()); }
  catch (error) { showErrors([error.message]); return; }
  var savedVersion = state.configVersion;
  $("saveConfig").disabled = true;
  $("saveStatus").textContent = "Enregistrement…";
  var result = await api("PUT", "/api/archivist/config", { source: source });
  if (result.status !== 200) {
    showErrors((result.data && result.data.errors) || [errMsg(result)]);
    $("saveStatus").textContent = "Activation refusée";
    $("saveConfig").disabled = false;
    return;
  }
  state.draftValid = true;
  showErrors([]);
  if (savedVersion === state.configVersion) {
    state.dirty = false;
    $("saveStatus").textContent = "Configuration activée";
    $("saveConfig").disabled = true;
  } else {
    state.draftValid = false;
    $("saveStatus").textContent = "Configuration activée; nouveau brouillon à tester";
    $("saveConfig").disabled = true;
    scheduleDryRun(false);
  }
}

function bindActions() {
  $("pagesInput").addEventListener("input", function () { scheduleDryRun(false); });
  $("addField").addEventListener("click", function () {
    syncDraftState();
    state.fields.push({ name: "", french: "", type: "text", mandatory: false });
    renderFields();
    scheduleDryRun();
  });
  $("addTemplate").addEventListener("click", function () {
    syncDraftState();
    var name = "nouveau_modele";
    var suffix = 2;
    while (Object.prototype.hasOwnProperty.call(state.templates, name)) name = "nouveau_modele_" + suffix++;
    state.templates[name] = "";
    renderTemplates();
    scheduleDryRun();
  });
  $("addRuleGroup").addEventListener("click", function () {
    syncDraftState();
    state.groups.push({ header: "// --- Nouvelle section", rules: "[]" });
    renderGroups();
    scheduleDryRun();
  });
  $("saveConfig").addEventListener("click", saveConfig);
  window.addEventListener("popstate", function () {
    var id = new URLSearchParams(window.location.search).get("id");
    loadDocument(id, true).then(function () { scheduleDryRun(false); });
  });
}

async function init() {
  initTheme("themeBtn");
  bindActions();
  var response = await api("GET", "/api/archivist/config/source");
  if (response.status !== 200) {
    showErrors([errMsg(response)]);
    $("loadError").hidden = false;
    $("loadError").textContent = errMsg(response);
    return;
  }
  state.fields = response.data.fields || [];
  state.templates = response.data.templates || {};
  state.groups = response.data.groups || [];
  renderFields();
  renderTemplates();
  renderGroups();
  var documentId = new URLSearchParams(window.location.search).get("id");
  await loadDocument(documentId, true);
  state.dirty = false;
  runDraft();
}

init();
