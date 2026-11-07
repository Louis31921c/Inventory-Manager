"use strict";

// ---- what a delivery note still owes --------------------------------------

function renderWaiting(data) {
  const lines = data.lines ?? [];
  $("waiting-stats").replaceChildren(el("span", { className: "stat" }, [
    el("span", { className: "stat-name", textContent: "lines missing" }),
    el("strong", { textContent: showNum(lines.length) }),
  ]));

  renderTable($("waiting-table"), [
    { label: "Supplier", cell: (line) => document.createTextNode(line.supplier ?? "—") },
    { label: "Article", cell: (line) => {
      const box = el("span", {}, [el("strong", { textContent: line.article })]);
      if (line.designation) {
        box.append(el("div", { className: "desig", textContent: line.designation }));
      }
      return box;
    } },
    { label: "Site / work item", cell: (line) => document.createTextNode(
      [line.site, line.work_item].filter(Boolean).join(" / ") || "—") },
  ], lines, "Every delivery note is complete.");
}

async function loadWaiting() {
  try {
    renderWaiting(await api("/api/backorders"));
  } catch (e) {
    notice($("waiting-note"), "bad", e.message);
  }
}

// ---- Trucks -------------------------------------------------------------

let vans = [];

// One picture per kind of vehicle: the van is the Renault Partner, the truck the Iveco Daily.
const VAN_PICTURES = { van: "van-renault", truck: "van-iveco" };

function vanDrawing(shape) {
  const picture = VAN_PICTURES[shape] ?? VAN_PICTURES.van;
  return el("img", { className: "van-shape", src: `/vehicles/${picture}.png`, alt: "" });
}

const vanBoxes = new Map();

function renderVans(data) {
  vans = data.vehicles ?? [];
  const stops = data.stops ?? [];
  vanBoxes.clear();

  // The lorry is picked in one place only: the box beside FIND THE SHORTEST RUN.
  const select = $("route-van");
  const chosen = select.value;
  select.replaceChildren(el("option", { value: "", textContent: "every van" }),
    ...vans.map((van) => el("option", { value: String(van.vehicle_id), textContent: van.name })));
  select.value = vans.some((van) => String(van.vehicle_id) === chosen) ? chosen : "";

  $("vans").replaceChildren(...vans.map((van) => {
    const run = el("div", { className: "van-route" });
    vanBoxes.set(van.vehicle_id, run);
    return el("div", { className: "van" }, [
      vanDrawing(van.shape),
      el("div", { className: "row tight" }, [el("strong", { textContent: van.name })]),
      run,
    ]);
  }));
}

function showRun(run, plan) {
  run.replaceChildren(el("ol", { className: "van-run" }, plan.stops.map((stop) => el("li", {}, [
    el("strong", { textContent: stop.label ?? stop.address }),
    el("div", { className: "stop-where", textContent: stop.address ?? "" }),
  ]))));
}

// ---- route wrangler -------------------------------------------------------

let stopRows = [];

function renderStops() {
  renderTable($("table-stops"), [
    { label: "Stop", cell: (row) => document.createTextNode(row.label ?? "—") },
    { label: "Address", cell: (row) => document.createTextNode(short(row.address, 46)) },
    { label: "Done", cell: (row) => {
      const box = el("input", { type: "checkbox", checked: row.done });
      box.addEventListener("change", async () => {
        await api(`/api/route/places/${row.place_id}`, patchJson({ done: box.checked }));
        await loadRoute();
      });
      return box;
    } },
    { label: "", cell: (row) => {
      const drop = el("button", { className: "mini", type: "button", textContent: "delete" });
      drop.addEventListener("click", async () => {
        await api(`/api/route/places/${row.place_id}`, { method: "DELETE" });
        await loadRoute();
      });
      return drop;
    } },
  ], stopRows, "No delivery address yet.");
}

async function loadRoute() {
  try {
    const data = await api("/api/route");
    stopRows = data.stops ?? [];
    renderVans(data);
    $("tree-route").textContent = `(${stopRows.filter((row) => !row.done).length})`;
    $("depot-now").textContent = data.depot
      ? `Leaving from ${data.depot.label} — ${data.depot.address}`
      : "No yard saved yet.";
    renderStops();
  } catch {
  }
}

async function addPlace(kind) {
  const address = $(kind === "depot" ? "depot-address" : "stop-address");
  const label = $(kind === "depot" ? "depot-label" : "stop-label");
  if (!address.value.trim()) {
    notice($("route-note"), "warn", "Type an address first.");
    return;
  }
  notice($("route-note"), "", "Looking the address up on the map...");
  try {
    const extra = {};
    await api("/api/route/places", postJson({
      address: address.value.trim(), label: label.value.trim() || null, kind, ...extra }));
    address.value = "";
    label.value = "";
    notice($("route-note"), "ok", "Address placed on the map.");
    await loadRoute();
  } catch (e) {
    notice($("route-note"), "bad", e.message);
  }
}

$("depot-save").addEventListener("click", () => addPlace("depot"));
$("stop-add").addEventListener("click", () => addPlace("stop"));
$("stops-clear").addEventListener("click", async () => {
  if (!confirm("Clear every delivery address?")) return;
  await api("/api/route/stops", { method: "DELETE" });
  await loadRoute();
});

async function findRoute() {
  const van = $("route-van").value;
  const run = vanBoxes.get(Number(van));
  if (!run) {
    notice($("route-note"), "warn", "Pick the lorry the round is for first.");
    return;
  }
  $("route-go").disabled = true;
  notice($("route-note"), "", "Measuring the roads...");
  run.replaceChildren(el("p", { className: "muted", textContent: "Measuring the roads..." }));
  try {
    const plan = await api(`/api/route/plan?round_trip=${$("route-loop").checked}`, postJson({}));
    showRun(run, plan);
    notice($("route-note"), "ok", "Route worked out.");
  } catch (e) {
    run.replaceChildren();
    notice($("route-note"), "bad", e.message);
  } finally {
    $("route-go").disabled = false;
  }
}

$("route-go").addEventListener("click", findRoute);
