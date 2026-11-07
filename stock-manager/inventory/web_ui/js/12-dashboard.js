"use strict";

// ---- landing dashboard and autonomy --------------------------------------

const td = (value) => document.createTextNode(value == null || value === "" ? "—" : String(value));

function drawDashboard(data) {
  const k = data.kpis ?? {};
  $("dash-kpis").replaceChildren(
    statTile("line down", String(k.line_down ?? 0)), statTile("critical", String(k.critical ?? 0)),
    statTile("at risk", String(k.at_risk ?? 0)),
    statTile("short, units", showNum(k.shortage_units ?? 0)),
    statTile("open actions", String(k.open_actions ?? 0)));
  renderTable($("table-shortages"), [
    { label: "Status", cell: (row) => el("span", {
        className: row.status === "line down" ? "status-down" : (row.status === "critical" ? "status-critical" : ""),
        textContent: row.status }) },
    { label: "SKU", cell: (row) => td(row.article) },
    { label: "Short", numeric: true, cell: (row) => td(showNum(row.shortage)) },
    { label: "Needed", cell: (row) => td(`${row.need_date.slice(5)} (${row.days_left}d)`) },
    { label: "Blocks", cell: (row) => td(row.blocks.join(", ") || "stock cover") },
    { label: "Root cause", cell: (row) => td(`${row.cause}${row.cause_detail ? `: ${row.cause_detail}` : ""}`) },
    { label: "Supplier (lead, days)", cell: (row) => td(
        `${row.supplier ?? "—"}${row.lead_days == null ? "" : ` (${showNum(row.lead_days)})`}`) },
  ], data.shortages ?? [], "Nothing is short for the next two weeks.");
  renderTable($("table-actions"), [
    { label: "", cell: (row) => el("span", { className: row.severity === "critical" || row.severity === "high"
        ? "status-down" : "muted", textContent: row.severity }) },
    { label: "What", cell: (row) => td(row.title) },
    { label: "Detail", cell: (row) => td(row.detail) },
    { label: "Since", cell: (row) => td(row.since.replace("T", " ")) },
  ], data.actions ?? [], "Nothing needs you.");
}

async function loadDashboard() {
  try {
    drawDashboard(await api("/api/dashboard"));
  } catch {
  }
}

function drawAutonomy(data) {
  $("autonomy-score").textContent = Number(data.score).toFixed(2);
}

async function loadAutonomy() {
  try {
    drawAutonomy(await api("/api/autonomy"));
  } catch {
  }
}

// The dashboard is the landing page's, folded away until the button on the bar is pressed.
var dashOpen = false;

function showDashboardButton() {
  const home = !$("panel-home").hidden;
  const shown = home && dashOpen;
  // On any other screen the button stays, and a press brings the dashboard up on the landing page.
  $("dash-toggle").hidden = false;
  $("dash-toggle").setAttribute("aria-expanded", String(shown));
  $("dash-toggle").textContent = shown ? "Fold dashboard" : "Bugs dashboard";
  $("dash-card").hidden = !shown;
}

function setDashboard(open) {
  dashOpen = open;
  showDashboardButton();
  try {
    localStorage.setItem("dash-open", open ? "1" : "0");
  } catch {
  }
}

$("dash-toggle").addEventListener("click", () => {
  if ($("panel-home").hidden) {
    treePick = null;
    openTab("home");
    setDashboard(true);
    return;
  }
  setDashboard(!dashOpen);
});
try {
  dashOpen = localStorage.getItem("dash-open") === "1";
} catch {
}
setDashboard(dashOpen);

loadAutonomy();
setInterval(() => { if (!document.hidden) loadAutonomy(); }, 5000);
setInterval(() => { if (!document.hidden) loadDashboard(); }, 60000);
