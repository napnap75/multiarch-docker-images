"use strict";

// Tools page (US-40, US-41): one card per tool with its description and a
// run button, live progress while the job runs, final summary afterwards.
// The job panel is restored from GET /api/archivist/tools on reload.

var pollTimer = null;
var currentJobId = null;

function $(id) { return document.getElementById(id); }

var SUMMARY_LABELS = {
  scanned: "Fichiers analysés",
  ok: "OK (à leur place)",
  corrected: "Corrigés (déplacés)",
  moved: "Déplacés vers inbox/",
  duplicates: "Copies parasites",
  missing: "Introuvables",
  errors: "Erreurs"
};

var SUMMARY_ORDER = ["scanned", "ok", "corrected", "moved", "duplicates", "missing", "errors"];

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
  if (res.data.job) {
    renderJob(res.data.job);
    if (res.data.job.status === "running") poll(res.data.job.id);
  }
}

function renderTools(tools) {
  var host = $("tools");
  host.innerHTML = "";
  tools.forEach(function (tool) {
    var card = document.createElement("div");
    card.className = "card toolcard";
    card.innerHTML =
      '<div class="toolhead"><h3>' + esc(tool.name) + "</h3>" +
      '<button type="button" class="btn primary" data-tool="' + esc(tool.id) + '">Lancer</button></div>' +
      '<p class="tooldesc">' + esc(tool.description) + "</p>";
    card.querySelector("button").addEventListener("click", function () { run(tool.id); });
    host.appendChild(card);
  });
}

async function run(toolId) {
  var res = await api("POST", "/api/archivist/tools/" + encodeURIComponent(toolId) + "/run");
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
  var panel = $("jobpanel");
  panel.hidden = false;
  var running = job.status === "running";
  var pct = job.total > 0 ? Math.round((job.processed / job.total) * 100) : 0;

  var html = '<div class="card jobcard">';
  if (running) {
    html += '<div class="jobhead"><h3>Tâche en cours…</h3>' +
      '<button type="button" id="cancelBtn" class="btn secondary">Annuler</button></div>';
  } else {
    var label = job.status === "done" ? "Tâche terminée"
      : job.status === "cancelled" ? "Tâche annulée"
      : job.status === "failed" ? "Tâche échouée" : "Tâche " + job.status;
    html += '<div class="jobhead"><h3>' + esc(label) + "</h3></div>";
  }

  html += '<div class="progress" role="progressbar" aria-valuenow="' + pct + '" aria-valuemin="0" aria-valuemax="100">' +
    '<div class="progressbar" style="width:' + pct + '%"></div></div>' +
    '<div class="progressinfo">' + job.processed + " / " + job.total + " fichiers analysés" +
    (running && job.current ? " — " + '<span class="mono">' + esc(job.current) + "</span>" : "") + "</div>";

  var summary = job.summary || {};
  html += '<div class="summarygrid">';
  SUMMARY_ORDER.forEach(function (key) {
    if (summary[key] === undefined) return;
    html += '<div class="summaryitem"><span class="summarynum">' + summary[key] + "</span>" +
      '<span class="summarylabel">' + esc(SUMMARY_LABELS[key] || key) + "</span></div>";
  });
  html += "</div>";

  if ((job.missing || []).length) {
    html += '<details class="jobdetails"><summary>Fichiers introuvables dans le bucket (' + job.missing.length + ')</summary><ul class="mono-list">';
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
