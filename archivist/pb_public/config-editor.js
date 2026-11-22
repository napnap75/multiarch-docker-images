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
  configVersion: 0,
  whenGroup: -1,
  whenChainKey: null,
  whenSequence: 0
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
    remove.innerHTML = TRASH_SVG;
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
    remove.innerHTML = TRASH_SVG;
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
    var title = document.createElement("span");
    title.textContent = groupTitle(group.header, index);
    summary.appendChild(title);

    var rename = document.createElement("button");
    rename.type = "button";
    rename.className = "icon-control group-rename";
    rename.setAttribute("aria-label", "Renommer la section " + groupTitle(group.header, index));
    rename.title = "Renommer la section";
    rename.innerHTML = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" aria-hidden="true"><path d="M362.7 19.3L314.3 67.7 444.3 197.7l48.4-48.4c25-25 25-65.5 0-90.5L453.3 19.3c-25-25-65.5-25-90.5 0zm-71 71L58.6 323.5c-10.4 10.4-18 23.3-22.2 37.4L1 437.3c-2.9 9.6-.3 20 6.9 27.2s17.5 9.8 27.2 6.9l76.3-25.3c14.1-4.2 27-11.8 37.4-22.2L384 190.7 291.7 98.3z"/></svg>';
    // The rename rewrites the section's separator comment, the pair a
    // click on the icon must not toggle the collapsible (US-50).
    rename.addEventListener("click", function (event) {
      event.preventDefault();
      var name = prompt("Nom de la section :", groupTitle(group.header, index));
      if (name === null) return;
      name = name.trim();
      if (!name) { alert("Le nom d'une section ne peut pas être vide."); return; }
      syncDraftState();
      state.groups[index].header = "// --- " + name;
      renderGroups();
      scheduleDryRun();
    });
    summary.appendChild(rename);
    details.appendChild(summary);

    var textarea = document.createElement("textarea");
    textarea.className = "rule-source mono";
    textarea.rows = 10;
    textarea.spellcheck = false;
    textarea.value = group.rules || "[]";
    textarea.setAttribute("aria-label", "Règles de " + groupTitle(group.header, index));
    textarea.addEventListener("input", scheduleDryRun);
    details.appendChild(textarea);

    var remove = document.createElement("button");
    remove.type = "button";
    remove.className = "icon-control group-rename group-delete";
    remove.setAttribute("aria-label", "Supprimer la section " + groupTitle(group.header, index));
    var test = document.createElement("button");
    test.type = "button";
    test.className = "icon-control group-rename group-test";
    test.setAttribute("aria-label", "Voir les documents concernés par la section " + groupTitle(group.header, index));
    test.title = "Voir les documents concernés";
    test.innerHTML = GLASS_SVG;
    test.addEventListener("click", function (event) {
      event.preventDefault();
      openWhenDialog(index);
    });
    summary.appendChild(test);
    remove.title = "Supprimer la section";
    remove.innerHTML = TRASH_SVG;
    remove.addEventListener("click", function (event) {
      event.preventDefault();
      syncDraftState();
      state.groups.splice(index, 1);
      renderGroups();
      scheduleDryRun();
    });
    summary.appendChild(remove);
    container.appendChild(details);
  });
}

// GLASS_SVG is the "impacted files" icon of the rule sections.
var GLASS_SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" aria-hidden="true"><path d="' +
  'M416 208c0 45.9-14.9 88.3-40 122.7L502.6 457.4c12.5 12.5 12.5 32.8 0 45.3s-32.8 12.5-45.3 0L330.7 376c-34.4 25.2-76.8 40-122.7 40C93.1 416 0 322.9 0 208S93.1 0 208 0S416 93.1 416 208zM208 352a144 144 0 1 0 0-288 144 144 0 1 0 0 288z"/></svg>';

// TRASH_SVG is the shared delete icon of the config editor (field and
// template rows, rule sections).
var TRASH_SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 448 512" aria-hidden="true"><path d="' +
  'M135.2 17.7L128 32H32C14.3 32 0 46.3 0 64S14.3 96 32 96H416c17.7 0 32-14.3 32-32s-14.3-32-32-32H320l-7.2-14.3C307.4 6.8 296.3 0 284.5 0H163.5c-11.8 0-22.9 6.8-28.3 17.7zM416 128H32L53.2 467c1.6 25.3 22.6 45 47.9 45H346.9c25.3 0 46.3-19.7 47.9-45L416 128z"/></svg>';

// The section popup lists the when chain of every rule of the section
// (hierarchy flattened, sub-rules indented under their parents) as radio
// buttons, and shows the documents matching the selected chain: parents
// evaluated before the chosen rule, against the stored pages and fields
// of every document, under the current draft configuration.

function openWhenDialog(groupIndex) {
  syncDraftState();
  state.whenGroup = groupIndex;
  renderWhenRules();
  renderWhenResults(null);
  $("whenResultsHead").textContent = "";
  $("whenDialog").showModal();
}

function renderWhenRules() {
  var box = $("whenRules");
  box.innerHTML = "";
  var rules = state.groups[state.whenGroup] ? state.groups[state.whenGroup].rules : "[]";
  var entries = [];
  try {
    var parsed = JSON.parse(rules || "[]");
    if (!Array.isArray(parsed)) throw new Error("tableau attendu");
    (function walk(list, chain, depth) {
      list.forEach(function (rule) {
        if (!rule || typeof rule.when !== "string" || !rule.when) return;
        var next = chain.concat([rule.when]);
        entries.push({ when: rule.when, chain: next, depth: depth });
        if (Array.isArray(rule.rules)) walk(rule.rules, next, depth + 1);
      });
    })(parsed, [], 0);
  } catch (error) {
    showWhenErrors(["Règles de la section invalides : " + error.message]);
    return;
  }
  if (!entries.length) {
    showWhenErrors(["La section ne contient aucune règle."]);
    return;
  }
  showWhenErrors([]);
  var selected = null;
  entries.forEach(function (entry, index) {
    var label = document.createElement("label");
    label.className = "when-rule";
    label.style.paddingLeft = (entry.depth * 1.25) + "rem";
    var radio = document.createElement("input");
    radio.type = "radio";
    radio.name = "whenSelect";
    radio.value = String(index);
    var chainKey = JSON.stringify(entry.chain);
    if (chainKey === state.whenChainKey) {
      radio.checked = true;
      selected = entry;
    }
    radio.addEventListener("change", function () { runWhenTest(entry.chain); });
    label.appendChild(radio);
    var text = document.createElement("span");
    text.className = "mono when-rule-text";
    text.textContent = entry.when;
    label.appendChild(text);
    box.appendChild(label);
  });
  if (selected) runWhenTest(selected.chain);
}

async function runWhenTest(chain) {
  state.whenChainKey = JSON.stringify(chain);
  var sequence = ++state.whenSequence;
  var source;
  try { source = composeSource(readDraft()); }
  catch (error) {
    showWhenErrors([error.message]);
    renderWhenResults(null);
    $("whenResultsHead").textContent = "";
    return;
  }
  $("whenResults").innerHTML = "";
  $("whenResultsHead").textContent = "Recherche…";
  var result = await api("POST", "/api/archivist/config/when-match", { source: source, when: chain });
  if (sequence !== state.whenSequence) return;
  if (result.status !== 200) {
    showWhenErrors((result.data && result.data.errors) || [errMsg(result)]);
    renderWhenResults(null);
    $("whenResultsHead").textContent = "";
    return;
  }
  showWhenErrors([]);
  var docs = result.data.documents || [];
  $("whenResultsHead").textContent = docs.length + " document(s) concerné(s) :";
  renderWhenResults(docs);
}

function renderWhenResults(documents) {
  var box = $("whenResults");
  box.innerHTML = "";
  if (!documents) {
    box.appendChild(document.createTextNode("Sélectionnez une condition ci-dessus."));
    return;
  }
  if (!documents.length) {
    box.className = "when-results muted";
    box.appendChild(document.createTextNode("Aucun document concerné."));
    return;
  }
  box.className = "when-results";
  documents.forEach(function (doc) {
    var line = document.createElement("div");
    line.className = "when-result";
    var key = document.createElement("span");
    key.className = "mono";
    key.textContent = doc.file_key || doc.id;
    var badge = document.createElement("span");
    badge.className = "when-status";
    badge.textContent = doc.status;
    line.appendChild(key);
    line.appendChild(badge);
    box.appendChild(line);
  });
}

function showWhenErrors(errors) {
  var box = $("whenErrors");
  box.innerHTML = "";
  if (!errors || !errors.length) {
    box.hidden = true;
    return;
  }
  box.hidden = false;
  errors.forEach(function (message) {
    var line = document.createElement("div");
    line.textContent = message;
    box.appendChild(line);
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
  // A stored date and its dry-run proposal can spell the same instant
  // differently (PocketBase record vs RFC 3339): compare at the display
  // precision so an equal date never shows the proposal highlight.
  if (isDateString(left) && isDateString(right)) {
    return fmtDate(left) === fmtDate(right);
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
  $("whenClose").addEventListener("click", function () { $("whenDialog").close(); });
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
