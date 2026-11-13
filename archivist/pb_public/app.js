"use strict";

// Document list page (US-32, US-33): server-side sort, per-column typed
// filters, status tabs, pagination, column chooser persisted in the
// browser, bulk single-field edit through one standard PATCH per
// document.

var state = {
  config: null,
  meta: [],
  page: 1,
  perPage: 50,
  sort: null,
  columns: [],
  statusTab: "",
  filters: {},
  selected: {},
  docs: [],
  total: 0,
  totalPages: 1,
  counts: {}
};
var docsLoadSequence = 0;

function $(id) { return document.getElementById(id); }

function esc(v) { return String(v).replace(/'/g, "\\'"); }

function store(key, val) {
  if (val === undefined) {
    try {
      var raw = localStorage.getItem(key);
      return raw ? JSON.parse(raw) : null;
    } catch (e) { return null; }
  }
  try { localStorage.setItem(key, JSON.stringify(val)); } catch (e) {}
}

function fixedMeta() {
  return [
    { name: "status", type: "status", fixed: true },
    { name: "file_key", type: "text", fixed: true },
    { name: "created", type: "date", fixed: true },
    { name: "updated", type: "date", fixed: true }
  ];
}

function fieldMeta(name) {
  for (var i = 0; i < state.meta.length; i++) {
    if (state.meta[i].name === name) return state.meta[i];
  }
  return null;
}

async function init() {
  var r = await api("GET", "/api/archivist/config");
  if (r.status !== 200) return;
  state.config = r.data;
  state.meta = state.config.fields.map(function (f) {
    return { name: f.name, type: f.type, mandatory: !!f.mandatory };
  }).concat(fixedMeta());

  var defaults = state.config.fields.filter(function (f) { return f.mandatory; })
    .map(function (f) { return f.name; }).concat(["status", "file_key"]);
  var savedCols = store("archivist.columns");
  state.columns = Array.isArray(savedCols) ? savedCols : defaults;
  var savedSort = store("archivist.sort");
  state.sort = savedSort && savedSort.field ? savedSort : null;

  initTheme("themeBtn");
  buildChooser();
  bindBulk();
  renderTabs();
  await loadCounts();
  await loadDocs();
}

function buildChooser() {
  var grid = $("colgrid");
  grid.innerHTML = "";
  state.meta.forEach(function (m) {
    var label = document.createElement("label");
    var cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = state.columns.indexOf(m.name) >= 0;
    cb.addEventListener("change", function () {
      var i = state.columns.indexOf(m.name);
      if (cb.checked && i < 0) state.columns.push(m.name);
      if (!cb.checked && i >= 0) state.columns.splice(i, 1);
      store("archivist.columns", state.columns);
      state.page = 1;
      loadDocs();
    });
    label.appendChild(cb);
    label.appendChild(document.createTextNode(" " + m.name));
    grid.appendChild(label);
  });
}

async function loadCounts() {
  var statuses = state.config.statuses || [];
  var results = await Promise.all(statuses.map(function (st) {
    return api("GET", "/api/collections/documents/records?perPage=1&fields=status&filter=" +
      encodeURIComponent("(status='" + esc(st) + "')"));
  }));
  statuses.forEach(function (st, i) {
    state.counts[st] = (results[i].data && results[i].data.totalItems) || 0;
  });
  renderTabs();
}

function renderTabs() {
  var tabs = $("tabs");
  tabs.innerHTML = "";
  var all = document.createElement("button");
  all.type = "button";
  all.className = "tab" + (state.statusTab === "" ? " active" : "");
  all.textContent = "Tous";
  all.addEventListener("click", function () {
    state.statusTab = "";
    state.page = 1;
    loadDocs();
  });
  tabs.appendChild(all);
  (state.config.statuses || []).forEach(function (st) {
    var b = document.createElement("button");
    b.type = "button";
    b.className = "tab" + (state.statusTab === st ? " active" : "");
    b.appendChild(document.createTextNode(st));
    var c = document.createElement("span");
    c.className = "count";
    c.textContent = state.counts[st] !== undefined ? String(state.counts[st]) : "";
    b.appendChild(c);
    b.addEventListener("click", function () {
      state.statusTab = st;
      state.page = 1;
      loadDocs();
    });
    tabs.appendChild(b);
  });
}

function buildFilterQuery() {
  var parts = [];
  if (state.statusTab) parts.push("(status='" + esc(state.statusTab) + "')");
  state.columns.forEach(function (name) {
    if (name === "status") return; // owned by the tabs
    var m = fieldMeta(name);
    var f = state.filters[name];
    if (!m || !f || m.type === "number") return;
    if (m.type === "date") {
      if (f.from) parts.push("(" + name + ">='" + esc(f.from) + "')");
      if (f.to) parts.push("(" + name + "<='" + esc(f.to) + "')");
    } else if (m.type === "boolean") {
      if (f.value === "true" || f.value === "false") parts.push("(" + name + "=" + f.value + ")");
    } else if (m.type === "enum") {
      if (f.value) parts.push("(" + name + "='" + esc(f.value) + "')");
    } else {
      if (f.value) parts.push("(" + name + "~'" + esc(f.value) + "')");
    }
  });
  if (!parts.length) return "";
  return "&filter=" + encodeURIComponent(parts.join(" && "));
}

async function loadDocs() {
  var sequence = ++docsLoadSequence;
  var sort = state.sort
    ? ("&sort=" + (state.sort.dir === "desc" ? "-" : "") + encodeURIComponent(state.sort.field))
    : "";
  var listQuery = sort + buildFilterQuery();
  var q = "/api/collections/documents/records?page=" + state.page +
    "&perPage=" + state.perPage + listQuery;
  var r = await api("GET", q);
  if (r.status !== 200 || sequence !== docsLoadSequence) return;
  state.docs = r.data.items || [];
  state.total = r.data.totalItems || 0;
  state.totalPages = r.data.totalPages || 1;
  try {
    sessionStorage.setItem("archivist.documentIds", JSON.stringify(state.docs.map(function (doc) { return doc.id; })));
  } catch (e) {}
  renderHead();
  renderRows();
  renderPager();
  renderBulkbar();
}

function renderHead() {
  var thead = $("doctable").tHead;
  thead.innerHTML = "";
  var trh = document.createElement("tr");
  var thAll = document.createElement("th");
  var cbAll = document.createElement("input");
  cbAll.type = "checkbox";
  cbAll.addEventListener("change", function () {
    state.docs.forEach(function (d) {
      if (cbAll.checked) state.selected[d.id] = true;
      else delete state.selected[d.id];
    });
    renderRows();
    renderBulkbar();
  });
  thAll.appendChild(cbAll);
  trh.appendChild(thAll);
  state.columns.forEach(function (name) {
    var th = document.createElement("th");
    th.textContent = name + (state.sort && state.sort.field === name
      ? (state.sort.dir === "desc" ? " ↓" : " ↑") : "");
    th.addEventListener("click", function () {
      if (!state.sort || state.sort.field !== name) {
        state.sort = { field: name, dir: "asc" };
      } else if (state.sort.dir === "asc") {
        state.sort.dir = "desc";
      } else {
        state.sort = null;
      }
      store("archivist.sort", state.sort);
      state.page = 1;
      loadDocs();
    });
    trh.appendChild(th);
  });
  thead.appendChild(trh);

  var trf = document.createElement("tr");
  trf.appendChild(document.createElement("td"));
  state.columns.forEach(function (name) {
    var td = document.createElement("td");
    if (name !== "status") {
      var m = fieldMeta(name);
      if (m && m.type !== "number") {
        td.appendChild(buildFilterControl(m));
      }
    }
    trf.appendChild(td);
  });
  thead.appendChild(trf);
}

function buildFilterControl(m) {
  var f = state.filters[m.name] || (state.filters[m.name] = {});
  var wrap = document.createElement("span");
  if (m.type === "date") {
    var from = document.createElement("input");
    from.type = "date";
    from.value = f.from || "";
    var to = document.createElement("input");
    to.type = "date";
    to.value = f.to || "";
    from.addEventListener("change", function () { f.from = from.value; state.page = 1; loadDocs(); });
    to.addEventListener("change", function () { f.to = to.value; state.page = 1; loadDocs(); });
    wrap.appendChild(from);
    wrap.appendChild(to);
  } else if (m.type === "boolean") {
    var sel = document.createElement("select");
    addOption(sel, "", "—");
    addOption(sel, "true", "oui");
    addOption(sel, "false", "non");
    sel.value = f.value || "";
    sel.addEventListener("change", function () { f.value = sel.value; state.page = 1; loadDocs(); });
    wrap.appendChild(sel);
  } else if (m.type === "enum") {
    var es = document.createElement("select");
    addOption(es, "", "—");
    ((state.config.enums || {})[m.name] || []).forEach(function (v) { addOption(es, v, v); });
    es.value = f.value || "";
    es.addEventListener("change", function () { f.value = es.value; state.page = 1; loadDocs(); });
    wrap.appendChild(es);
  } else {
    var inp = document.createElement("input");
    inp.type = "text";
    inp.placeholder = "contient…";
    inp.value = f.value || "";
    var timer = null;
    inp.addEventListener("input", function () {
      if (timer) clearTimeout(timer);
      timer = setTimeout(function () { f.value = inp.value; state.page = 1; loadDocs(); }, 300);
    });
    wrap.appendChild(inp);
  }
  return wrap;
}

function renderRows() {
  var tbody = $("doctable").tBodies[0];
  tbody.innerHTML = "";
  state.docs.forEach(function (doc) {
    var tr = document.createElement("tr");
    tr.className = "clickable";
    var tdcb = document.createElement("td");
    var cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = !!state.selected[doc.id];
    cb.addEventListener("click", function (ev) { ev.stopPropagation(); });
    cb.addEventListener("change", function () {
      if (cb.checked) state.selected[doc.id] = true;
      else delete state.selected[doc.id];
      renderBulkbar();
    });
    tdcb.appendChild(cb);
    tr.appendChild(tdcb);
    state.columns.forEach(function (name) {
      var m = fieldMeta(name);
      var td = document.createElement("td");
      if (name === "status") {
        var b = document.createElement("span");
        b.className = "badge st-" + doc.status;
        b.textContent = doc.status;
        td.appendChild(b);
      } else if (m && m.type === "date") {
        td.textContent = fmtDate(doc[name]);
      } else {
        td.textContent = fmtVal(doc[name]);
      }
      tr.appendChild(td);
    });
    tr.addEventListener("click", function (ev) {
      if (ev.target.tagName === "INPUT") return;
      window.location.href = "/document.html?id=" + doc.id;
    });
    tbody.appendChild(tr);
  });
}

function renderPager() {
  var pager = $("pager");
  pager.innerHTML = "";
  var prev = document.createElement("button");
  prev.type = "button";
  prev.className = "btn secondary";
  prev.textContent = "‹ Précédent";
  prev.disabled = state.page <= 1;
  prev.addEventListener("click", function () { state.page--; loadDocs(); });
  var info = document.createElement("span");
  info.textContent = state.total + " document(s) — page " + state.page + " / " + Math.max(1, state.totalPages);
  var next = document.createElement("button");
  next.type = "button";
  next.className = "btn secondary";
  next.textContent = "Suivant ›";
  next.disabled = state.page >= state.totalPages;
  next.addEventListener("click", function () { state.page++; loadDocs(); });
  pager.appendChild(prev);
  pager.appendChild(info);
  pager.appendChild(next);
}

function bindBulk() {
  var sel = $("bulkfield");
  sel.innerHTML = "";
  state.config.fields.forEach(function (f) { addOption(sel, f.name, f.name); });
  sel.addEventListener("change", rebuildBulkValue);
  $("bulkapply").addEventListener("click", applyBulk);
  $("bulkclear").addEventListener("click", function () {
    state.selected = {};
    renderRows();
    renderBulkbar();
  });
  $("bulkall").addEventListener("click", selectAllFiltered);
  rebuildBulkValue();
}

function bulkField() {
  var name = $("bulkfield").value;
  for (var i = 0; i < state.config.fields.length; i++) {
    if (state.config.fields[i].name === name) return state.config.fields[i];
  }
  return null;
}

function rebuildBulkValue() {
  var holder = $("bulkvalue");
  holder.innerHTML = "";
  var f = bulkField();
  if (!f) return;
  holder.appendChild(buildValueControl(f, (state.config.enums || {})[f.name] || [], null));
}

async function selectAllFiltered() {
  var page = 1;
  for (;;) {
    var q = "/api/collections/documents/records?page=" + page + "&perPage=200&fields=id" + buildFilterQuery();
    var r = await api("GET", q);
    if (r.status !== 200) break;
    (r.data.items || []).forEach(function (d) { state.selected[d.id] = true; });
    if (page >= (r.data.totalPages || 1)) break;
    page++;
  }
  renderRows();
  renderBulkbar();
}

async function applyBulk() {
  var f = bulkField();
  if (!f) { alert("Choisissez un champ."); return; }
  var el = $("bulkvalue").firstChild;
  var value;
  try {
    var r = readValueControl(f, el);
    if (r.clear) {
      if (f.mandatory) { alert("Ce champ est obligatoire : une valeur est requise."); return; }
      value = null;
    } else {
      value = r.value;
    }
  } catch (e) { alert(e.message); return; }
  var ids = Object.keys(state.selected);
  if (!ids.length) { alert("Aucun document sélectionné."); return; }
  $("bulkresult").textContent = "…";
  var ok = 0;
  var failed = [];
  for (var i = 0; i < ids.length; i++) {
    var body = {};
    body[f.name] = value;
    var res = await api("PATCH", "/api/collections/documents/records/" + ids[i], body);
    if (res.status === 200) ok++;
    else failed.push(ids[i] + " : " + errMsg(res));
  }
  $("bulkresult").textContent = ok + " document(s) modifié(s)" +
    (failed.length ? ", " + failed.length + " échec(s) — " + failed[0] : "");
  state.selected = {};
  await loadCounts();
  await loadDocs();
}

function renderBulkbar() {
  var n = Object.keys(state.selected).length;
  $("bulkbar").hidden = n === 0;
  $("bulkcount").textContent = n + " document(s) sélectionné(s)";
}

init();
