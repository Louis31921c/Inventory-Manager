"use strict";

// ---- stock control: sites, reservations, corrections, cycle counts --------

let siteRows = [];

function drawStockControl(data) {
  const summary = data.summary ?? {};
  siteRows = data.sites ?? [];
  $("site-stats").replaceChildren(
    statTile("lines", String(summary.lines ?? 0)),
    statTile("held back", showNum(summary.reserved ?? 0)),
    statTile("below zero", String(summary.negative ?? 0)));
  renderSiteStock();

  $("stock-articles").replaceChildren(...[...new Set(siteRows.map((row) => row.article))]
    .map((name) => el("option", { value: name })));
  $("stock-sites").replaceChildren(...[...new Set(siteRows.map((row) => row.site).filter(Boolean))]
    .map((name) => el("option", { value: name })));

  renderTable($("table-reservations"), [
    { label: "Article", cell: (row) => document.createTextNode(row.article) },
    { label: "Site", cell: (row) => document.createTextNode(row.site || "—") },
    { label: "Work item", cell: (row) => document.createTextNode(row.work_item || "—") },
    { label: "Held", numeric: true, cell: (row) => document.createTextNode(
        `${showNum(row.left)} of ${showNum(row.quantity)}`) },
    { label: "Needed by", cell: (row) => document.createTextNode(row.needed_by ?? "—") },
    { label: "", cell: (row) => {
      const free = el("button", { className: "mini", textContent: "release" });
      free.addEventListener("click", () => stockControlAsk(
        `/api/stockctl/reservations/${row.reservation_id}`, { method: "DELETE" }));
      return free;
    } },
  ], data.reservations ?? [], "Nothing is held back.");

  renderTable($("table-moves"), [
    { label: "Day", cell: (row) => document.createTextNode(row.moved_on) },
    { label: "What", cell: (row) => document.createTextNode(
        { transfer_out: "transfer out", transfer_in: "transfer in", adjust: "correction",
          count: "count gap" }[row.kind] ?? row.kind) },
    { label: "Article", cell: (row) => document.createTextNode(row.article) },
    { label: "Site", cell: (row) => document.createTextNode(row.site || "—") },
    { label: "Qty", numeric: true, cell: (row) => document.createTextNode(showNum(row.quantity)) },
    { label: "Why", cell: (row) => document.createTextNode(row.reason || row.ref || "—") },
    { label: "By", cell: (row) => document.createTextNode(row.by ?? "—") },
  ], data.moves ?? [], "No movement yet.");

  const cycle = data.cycle ?? {};
  $("cycle-summary").textContent =
    `${cycle.total_due ?? 0} article(s) are due. The busiest quarter is counted every 30 days, `
    + "the next every 60, the rest every 90. Count first, the paperwork's figure comes after.";
  renderTable($("table-cycle"), [
    { label: "Article", cell: (row) => document.createTextNode(row.article) },
    { label: "Class", cell: (row) => document.createTextNode(row.class) },
    { label: "Last counted", cell: (row) => document.createTextNode(
        row.last_counted ? `${row.last_counted} (${row.days_since} days)` : "never") },
    { label: "Counted", cell: (row) => {
      const box = el("input", { className: "cell", inputMode: "decimal", size: 6 });
      const go = el("button", { className: "mini", textContent: "count" });
      go.addEventListener("click", async () => {
        if (box.value.trim() === "") return;
        try {
          const done = await api("/api/stockctl/count",
                                 postJson({ article: row.article, quantity: Number(box.value) }));
          drawStockControl(done.control);
          notice($("cycle-note"), done.variance ? "warn" : "ok",
                 `${row.article}: counted ${showNum(done.counted)}, the paperwork said `
                 + `${showNum(done.expected)} (${done.variance > 0 ? "+" : ""}${showNum(done.variance)}).`);
          loadStock();
        } catch (e) {
          notice($("cycle-note"), "bad", e.message);
        }
      });
      return el("span", {}, [box, go]);
    } },
  ], cycle.due ?? [], "Nothing is due for a count.");
}

function renderSiteStock() {
  const asked = $("site-search").value.trim().toUpperCase();
  const rows = siteRows.filter((row) => !asked
    || row.article.includes(asked) || row.site.includes(asked));
  renderTable($("table-site-stock"), [
    { label: "Article", cell: (row) => document.createTextNode(row.article) },
    { label: "Site", cell: (row) => document.createTextNode(row.site || "—") },
    { label: "On hand", numeric: true, cell: (row) => document.createTextNode(showNum(row.on_hand)) },
    { label: "Held back", numeric: true, cell: (row) => document.createTextNode(
        row.reserved ? showNum(row.reserved) : "—") },
    { label: "Free", numeric: true, cell: (row) => el("span", {
        className: row.available < 0 ? "bad" : "", textContent: showNum(row.available) }) },
  ], rows.slice(0, 300), "Nothing is standing anywhere yet.");
}

async function stockControlAsk(path, options) {
  try {
    const done = await api(path, options);
    drawStockControl(done.control);
    notice($("stockctl-note"), "ok", "Done.");
    loadStock();
    return true;
  } catch (e) {
    notice($("stockctl-note"), "bad", e.message);
    return false;
  }
}

async function loadLeadTimes() {
  let data;
  try {
    data = await api("/api/leadtimes");
  } catch {
    return;
  }
  $("lead-summary").textContent =
    "Learned from the ordered and delivered days on the delivery notes. 'Plan around' is the "
    + "delay one delivery in five exceeds. An article with "
    + `${data.min_samples}+ deliveries is planned with its own, anything else with the setting.`;
  renderTable($("table-leads"), [
    { label: "Supplier", cell: (row) => document.createTextNode(row.supplier) },
    { label: "Deliveries", numeric: true, cell: (row) => document.createTextNode(String(row.n)) },
    { label: "Usually (days)", numeric: true, cell: (row) => document.createTextNode(showNum(row.days)) },
    { label: "Plan around", numeric: true, cell: (row) => document.createTextNode(showNum(row.plan_days)) },
    { label: "Fastest", numeric: true, cell: (row) => document.createTextNode(String(row.fastest)) },
    { label: "Slowest", numeric: true, cell: (row) => document.createTextNode(String(row.slowest)) },
  ], data.suppliers ?? [], "No delivery note carries an order date yet.");
}

async function loadStockControl() {
  try {
    drawStockControl(await api("/api/stockctl"));
  } catch {
  }
}

$("site-search").addEventListener("input", renderSiteStock);
$("mv-go").addEventListener("click", () => stockControlAsk("/api/stockctl/transfer", postJson({
  article: $("mv-article").value, from_site: $("mv-from").value, to_site: $("mv-to").value,
  quantity: Number($("mv-qty").value) })));
$("rs-go").addEventListener("click", () => stockControlAsk("/api/stockctl/reserve", postJson({
  article: $("rs-article").value, site: $("rs-site").value, work_item: $("rs-work").value,
  quantity: Number($("rs-qty").value), needed_by: $("rs-by").value || null })));
$("aj-go").addEventListener("click", () => stockControlAsk("/api/stockctl/adjust", postJson({
  article: $("aj-article").value, site: $("aj-site").value,
  quantity: Number($("aj-qty").value), reason: $("aj-reason").value })));

// ---- the tables a question can be asked about -----------------------------

async function loadTables() {
  if (!$("tables-list")) return;
  try {
    const data = await api("/api/tables");
    $("tables-list").replaceChildren(...(data.tables ?? []).map((table) => {
      const chip = el("button", {
        type: "button", className: "mini",
        title: table.columns.join(", "),
        textContent: `${table.table} (${showNum(table.rows)})`,
      });
      chip.addEventListener("click", () => {
        $("question").value = `SELECT * FROM ${table.table} LIMIT 100`;
        $("question").focus();
      });
      return chip;
    }));
  } catch {
  }
}
