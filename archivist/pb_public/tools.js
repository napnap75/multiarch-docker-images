"use strict";

// Tools page (US-40, US-41): one card per tool with its description and a
// run button, live progress while the job runs, final summary afterwards.
// The job panel is restored from GET /api/archivist/tools on reload.

var pollTimer = null;
var currentJobId = null;

function $(id) { return document.getElementById(id); }

// refile-all (and find-lost fallback): the counts are documents of the base.
var SUMMARY_LABELS = {
  scanned: "Documents analysés",
  ok: "OK (à leur place)",
  unchanged: "Déjà bien classés",
  corrected: "Corrigés (déplacés)",
  moved: "Déplacés",
  duplicates: "Copies parasites",
  skipped: "Laissés en place",
  missing: "Introuvables",
  errors: "Erreurs"
};

// find-lost-files: the counts are bucket objects, and each category states
// its consequence explicitly.
var FIND_LOST_LABELS = {
  scanned: "Objets PDF analysés dans le bucket",
  ok: "À leur place (rien à faire)",
  corrected: "Retrouvés ailleurs (file_key corrigé)",
  moved: "Inconnus de la base (renvoyés vers inbox)",
  duplicates: "Copies parasites (laissées en place)",
  missing: "Disparus du bucket (signalés, sans action)",
  errors: "Erreurs"
};

function summaryLabels(tool) {
  return tool === "find-lost-files" ? FIND_LOST_LABELS : SUMMARY_LABELS;
}

var SUMMARY_ORDER = ["scanned", "ok", "unchanged", "corrected", "moved", "duplicates", "skipped", "missing", "errors"];

function esc(v) {
  return String(v == null ? "" : v)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

async function init() {
  initTheme("themeBtn");
  var res = await api("GET", "/api/archivist/tools");
  if (res.status !== 200) return;
  renderTools(res.data.tools || []);
  var job = res.data && res.data.job;
  if (job && job.id) {
    renderJob(job);
    if (job.status === "running") poll(job.id);
  } else {
    hideJob();
  }
}

// hideJob clears and hides the progress/summary block: nothing to show
// when no job is running and no result is worth restoring.
function hideJob() {
  stopPoll();
  currentJobId = null;
  var panel = $("jobpanel");
  panel.hidden = true;
  panel.innerHTML = "";
}

function renderTools(tools) {
  var host = $("tools");
  host.innerHTML = "";
  tools.forEach(function (tool) {
    var card = document.createElement("div");
    card.className = "card toolcard";
    card.innerHTML =
      '<div class="toolhead"><h3>' + esc(tool.name) + "</h3>" +
      '<div class="toolactions">' +
      '<button type="button" class="btn secondary" data-dryrun="1" data-tool="' + esc(tool.id) + '">Simulation</button>' +
      '<button type="button" class="btn primary" data-tool="' + esc(tool.id) + '">Lancer</button>' +
      "</div></div>" +
      '<p class="tooldesc">' + esc(tool.description) + "</p>";
    card.querySelectorAll("button").forEach(function (btn) {
      btn.addEventListener("click", function () { run(tool.id, btn.dataset.dryrun === "1"); });
    });
    host.appendChild(card);
  });
}

async function run(toolId, dryRun) {
  var res = await api("POST", "/api/archivist/tools/" + encodeURIComponent(toolId) + "/run",
    dryRun ? { dry_run: true } : undefined);
  if (res.status !== 200) {
    $("jobpanel").hidden = false;
    $("jobpanel").innerHTML = '<div class="card"><p class="errbox" style="display:block">' +
      esc(errMsg(res)) + "</p></div>";
    return;
  }
  renderJob(res.data);
  poll(res.data.id);
}

function poll(jobId) {
  currentJobId = jobId;
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(async function () {
    var res = await api("GET", "/api/archivist/tools/jobs/" + encodeURIComponent(jobId));
    if (res.status === 404) {
      hideJob();
      return;
    }
    if (res.status !== 200) {
      stopPoll();
      return;
    }
    renderJob(res.data);
    if (res.data.status !== "running") stopPoll();
  }, 1000);
}

function stopPoll() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = null;
}

function renderJob(job) {
  job = job || {};
  var panel = $("jobpanel");
  panel.hidden = false;
  var running = job.status === "running";
  var processed = job.processed || 0;
  var total = job.total || 0;
  var pct = total > 0 ? Math.round((processed / total) * 100) : 0;

  var html = '<div class="card jobcard">';
  var tag = job.dry_run ? ' <span class="jobdrytag">Simulation — aucune modification</span>' : "";
  if (running) {
    html += '<div class="jobhead"><h3>Tâche en cours…' + tag + '</h3>' +
      '<button type="button" id="cancelBtn" class="btn secondary">Annuler</button></div>';
  } else {
    var label = job.status === "done" ? "Tâche terminée"
      : job.status === "cancelled" ? "Tâche annulée"
      : job.status === "failed" ? "Tâche échouée"
      : job.status ? "Tâche " + job.status : "Tâche";
    html += '<div class="jobhead"><h3>' + esc(label) + tag + "</h3></div>";
  }

  var isFind = job.tool === "find-lost-files";
  var countWord = isFind ? "objets analysés" : "documents analysés";
  html += '<div class="progress" role="progressbar" aria-valuenow="' + pct + '" aria-valuemin="0" aria-valuemax="100">' +
    '<div class="progressbar" style="width:' + pct + '%"></div></div>' +
    '<div class="progressinfo">' + processed + " / " + total + " " + countWord +
    (running && job.current ? " — " + '<span class="mono">' + esc(job.current) + "</span>" : "") + "</div>";

  var summary = job.summary || {};
  html += '<div class="summarygrid">';
  var labels = summaryLabels(job.tool);
  SUMMARY_ORDER.forEach(function (key) {
    if (summary[key] === undefined) return;
    html += '<div class="summaryitem"><span class="summarynum">' + summary[key] + "</span>" +
      '<span class="summarylabel">' + esc(labels[key] || key) + "</span></div>";
  });
  html += "</div>";
  if (isFind) {
    html += '<p class="tooldesc">Les effectifs portent sur les objets PDF du bucket (hors inbox) : ils peuvent différer du nombre de documents en base. ' +
      "La somme des catégories ci-dessus est égale au nombre d'objets analysés.</p>";
  }

  if ((job.planned || []).length) {
    html += '<details class="jobdetails"><summary>Déplacements prévus (' + job.planned.length + ')</summary><ul class="mono-list">';
    job.planned.forEach(function (k) { html += '<li class="mono">' + esc(k) + "</li>"; });
    html += "</ul></details>";
  }
  if ((job.corrected_files || []).length) {
    html += '<details class="jobdetails"><summary>Fichiers retrouvés ailleurs — file_key corrigé (' + job.corrected_files.length + ')</summary><ul class="mono-list">';
    job.corrected_files.forEach(function (k) { html += '<li class="mono">' + esc(k) + "</li>"; });
    html += "</ul></details>";
  }
  if ((job.moved_files || []).length) {
    html += '<details class="jobdetails"><summary>Fichiers inconnus de la base — renvoyés vers inbox (' + job.moved_files.length + ')</summary><ul class="mono-list">';
    job.moved_files.forEach(function (k) { html += '<li class="mono">' + esc(k) + "</li>"; });
    html += "</ul></details>";
  }
  if ((job.duplicate_files || []).length) {
    html += '<details class="jobdetails"><summary>Copies parasites laissées en place (' + job.duplicate_files.length + ')</summary><ul class="mono-list">';
    job.duplicate_files.forEach(function (k) { html += '<li class="mono">' + esc(k) + "</li>"; });
    html += "</ul></details>";
  }
  if ((job.skipped || []).length) {
    html += '<details class="jobdetails"><summary>Documents laissés en place (' + job.skipped.length + ')</summary><ul class="mono-list">';
    job.skipped.forEach(function (k) { html += '<li class="mono">' + esc(k) + "</li>"; });
    html += "</ul></details>";
  }
  if ((job.missing || []).length) {
    html += '<details class="jobdetails"><summary>Fichiers disparus du bucket — signalés sans action (' + job.missing.length + ')</summary><ul class="mono-list">';
    job.missing.forEach(function (k) { html += '<li class="mono">' + esc(k) + "</li>"; });
    html += "</ul></details>";
  }
  if ((job.errors || []).length) {
    html += '<details class="jobdetails"><summary>Erreurs (' + job.errors.length + ')</summary><ul class="mono-list">';
    job.errors.forEach(function (e) { html += '<li class="mono">' + esc(e) + "</li>"; });
    html += "</ul></details>";
  }
  html += "</div>";
  panel.innerHTML = html;

  var cancelBtn = $("cancelBtn");
  if (cancelBtn && job.status === "running") {
    cancelBtn.addEventListener("click", async function () {
      cancelBtn.disabled = true;
      await api("POST", "/api/archivist/tools/jobs/" + encodeURIComponent(job.id) + "/cancel");
    });
  }
}

init();
