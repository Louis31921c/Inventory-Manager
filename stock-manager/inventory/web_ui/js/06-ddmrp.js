"use strict";

const FKEYS = [
  ["F1", "SQL", () => {
    openTab("query");
    $("question").focus();
  }],
  ["F2", "Run", () => {
    openTab("query");
    $("ask-btn").click();
  }],
  ["F3", "Inventory", () => openTab("inventory")],
  ["F4", "Orders", () => openTab("orders")],
  ["F5", "Export CSV", () => { window.location.href = "/export/inventory.csv"; }],
  ["F6", "Export SQL", () => { window.location.href = "/export/inventory.sql"; }],
  ["F7", "Exit", () => $("quit-form").submit()],
  ["F8", "Remember me", () => $("lock-form").submit()],
  ["F9", "DDMRP", openPredictions],
];

$("fkeys").replaceChildren(
  ...FKEYS.map(([key, label, action]) => {
    const button = el("button", { className: "fkey", type: "button", title: `${key} = ${label}` }, [
      el("b", { textContent: key }),
      el("span", { textContent: `=${label}` }),
    ]);
    button.addEventListener("click", action);
    return button;
  }),
  el("a", {
    className: "license", href: "/license", target: "_blank", rel: "noopener",
    textContent: "licence", title: "MIT licence. No warranty of any kind.",
  }),
);

document.addEventListener("keydown", (event) => {
  if (event.ctrlKey || event.altKey || event.metaKey) return;
  const entry = FKEYS.find(([key]) => key === event.key);
  if (!entry) return;
  event.preventDefault();
  entry[2]();
});

function countCell(row) {
  const box = el("input", {
    className: "cell", type: "number", step: "any", min: "0",
    value: row.on_hand == null ? "" : row.on_hand,
    title: row.counted_on ? `counted ${row.counted_on}` : "never counted",
    "aria-label": `On hand for ${row.article}`,
  });
  box.addEventListener("change", async () => {
    const quantity = box.value.trim() === "" ? null : Number(box.value);
    if (quantity != null && (isNaN(quantity) || quantity < 0)) {
      notice($("buffer-note"), "warn", "A count is a number, zero or more.");
      return;
    }
    box.disabled = true;
    try {
      await api("/api/stock-count", putJson({ article: row.article, quantity }));
      notice($("buffer-note"), "", "");
      say(`${row.article}: ${quantity == null ? "count cleared" : quantity + " on hand"}`);
      await renderBuffers();
    } catch (e) {
      notice($("buffer-note"), "bad", e.message);
    } finally {
      box.disabled = false;
    }
  });
  return box;
}

let plan = null;
let moves = null;
let orderDraft = null;
let period = "week";

function columnChart(points, label, value) {
  const width = 640;
  const height = 120;
  const pad = { left: 34, right: 8, top: 8, bottom: 20 };
  const top = Math.max(...points.map(value), 1);
  const plotWidth = width - pad.left - pad.right;
  const plotHeight = height - pad.top - pad.bottom;
  const step = plotWidth / Math.max(points.length, 1);
  // A name over every column is a smear at a year's worth of weeks, and the quantity
  // over every column is worse: both are on the bar itself, for whoever points at it.
  const every = Math.ceil(points.length / 13);
  const parts = [];

  points.forEach((point, index) => {
    const tall = (value(point) / top) * plotHeight;
    const x = pad.left + index * step + step * 0.2;
    const barWidth = Math.max(step * 0.6, 2);
    parts.push(tip(svg("rect", {
      class: "bar", x, width: barWidth,
      y: pad.top + plotHeight - tall, height: Math.max(tall, 1),
    }), `${label(point)} · ${showNum(Math.round(value(point)))}`));
    if (index % every === 0) {
      parts.push(svg("text", {
        x: x + barWidth / 2, y: height - 8, "text-anchor": "middle",
      }, [label(point)]));
    }
  });

  return svg("svg", { viewBox: `0 0 ${width} ${height}`, role: "img" }, [
    svg("line", {
      class: "axis", x1: pad.left, y1: pad.top + plotHeight, x2: width - pad.right,
      y2: pad.top + plotHeight,
    }),
    svg("text", { x: 0, y: pad.top + 8 }, [showNum(Math.round(top))]),
    ...parts,
  ]);
}

function barList(points, label, value) {
  const width = 640;
  const rowHeight = 15;
  const pad = { left: 150, right: 10, top: 4 };
  const height = pad.top + points.length * rowHeight + 4;
  const top = Math.max(...points.map(value), 1);
  const plotWidth = width - pad.left - pad.right;
  const parts = [];

  points.forEach((point, index) => {
    const y = pad.top + index * rowHeight;
    const long = Math.max((value(point) / top) * plotWidth, 1);
    parts.push(svg("text", { x: 0, y: y + 9 }, [short(label(point), 24)]));
    parts.push(svg("rect", { class: "track", x: pad.left, y: y + 1, width: plotWidth, height: 9 }));
    parts.push(tip(svg("rect", { class: "bar", x: pad.left, y: y + 1, width: long, height: 9 }),
                   `${label(point)} · ${showNum(value(point))}`));
  });

  return svg("svg", { viewBox: `0 0 ${width} ${height}`, role: "img" }, parts);
}

function renderCharts() {
  const weeks = plan.weeks ?? [];
  $("weeks-chart").replaceChildren(weeks.length
    ? columnChart(weeks, (week) => week.week.slice(5), (week) => week.quantity)
    : el("p", { className: "muted", textContent: "No delivery note yet." }));

  const sites = plan.sites ?? [];
  $("sites-chart").replaceChildren(sites.length
    ? barList(sites, (site) => site.site, (site) => site.lines)
    : el("p", { className: "muted", textContent: "No site named yet." }));
}

function renderMovements() {
  const head = ["Period", "Quantity in", "Lines in", "Quantity out", "Lines out", "Net"];
  const rows = moves.rows ?? [];
  $("table-movements").replaceChildren(
    el("thead", {}, [el("tr", {}, head.map((name) => el("th", { textContent: name })))]),
    el("tbody", {}, rows.length
      ? rows.map((row) => el("tr", {}, [
          el("td", {}, [el("strong", { textContent: row.label })]),
          el("td", { className: "num", textContent: showNum(row.in_quantity) }),
          el("td", { className: "num", textContent: showNum(row.in_lines) }),
          el("td", { className: "num", textContent: showNum(row.out_quantity) }),
          el("td", { className: "num", textContent: showNum(row.out_lines) }),
          el("td", { className: "num", textContent: showNum(row.net) }),
        ]))
      : [el("tr", {}, [el("td", { colSpan: head.length, className: "muted",
                                  textContent: "Nothing booked in yet." })])]),
    el("tfoot", {}, [el("tr", {}, [
      el("th", { textContent: "total" }),
      el("th", { className: "num", textContent: showNum(moves.totals.in_quantity) }),
      el("th", {}),
      el("th", { className: "num", textContent: showNum(moves.totals.out_quantity) }),
      el("th", {}),
      el("th", { className: "num", textContent: showNum(moves.totals.net) }),
    ])]),
  );

  for (const name of ["week", "month", "half"]) {
    $(`period-${name}`).setAttribute("aria-pressed", String(name === period));
  }
}

let buffers = null;
let bufferAll = false;

function bufferRules() {
  return {
    cycle: Number($("buffer-cycle").value) || 7,
    horizon: Number($("buffer-horizon").value) || 4,
  };
}

function zonePill(zone) {
  const words = { red: "red", yellow: "yellow", green: "green", over: "over the top",
                  uncounted: "not counted" };
  return el("span", { className: `pill ${zone}`, textContent: words[zone] ?? zone });
}

const BUFFER_SHOWN = 10;

function tip(node, text) {
  node.append(svg("title", {}, [text]));
  return node;
}

const ZONE_WORDS = { red: "red", yellow: "yellow", green: "green",
                     over: "over the top", uncounted: "not counted" };

function zoneStrip(counts) {
  // One line for the whole workshop: how the articles sit across the zones.
  const width = 640;
  const height = 16;
  const zones = ["red", "yellow", "green", "over", "uncounted"]
    .map((zone) => [zone, counts[zone] ?? 0]).filter(([, many]) => many > 0);
  const whole = zones.reduce((sum, [, many]) => sum + many, 0) || 1;

  let x = 0;
  const parts = zones.map(([zone, many]) => {
    const wide = (many / whole) * width;
    const block = tip(svg("rect", { class: `zone-${zone}`, x, y: 0, width: wide, height: 11 }),
                      `${many} ${ZONE_WORDS[zone]}`);
    x += wide;
    return block;
  });

  return svg("svg", { viewBox: `0 0 ${width} ${height}`, class: "strip", role: "img" }, parts);
}

function bufferBars(rows) {
  // A slim line an article: the share of its own buffer it is holding, so a buffer of
  // ten and a buffer of ten thousand sit on the same scale. The numbers are on hover.
  const width = 640;
  const rowHeight = 15;
  const pad = { left: 150, right: 44, top: 4 };
  const height = pad.top + rows.length * rowHeight + 4;
  const plotWidth = width - pad.left - pad.right;
  const parts = [];

  rows.forEach((row, index) => {
    const y = pad.top + index * rowHeight;
    const share = Math.max(Math.min(row.buffer_share, 100), 0);
    const zone = row.zone === "uncounted" ? "uncounted" : row.zone;
    parts.push(svg("text", { x: 0, y: y + 9 }, [short(row.article, 24)]));
    parts.push(svg("rect", { class: "track", x: pad.left, y: y + 1, width: plotWidth, height: 9 }));
    parts.push(tip(svg("rect", {
      class: `zone-${zone}`, x: pad.left, y: y + 1,
      width: Math.max((share / 100) * plotWidth, 1.5), height: 9,
    }), `${row.article}\n${showNum(row.net_flow)} against a top of green of `
       + `${showNum(row.top_of_green)} · ${ZONE_WORDS[zone]}`
       + (row.to_order ? `\norder ${showNum(row.to_order)}` : "")));
    parts.push(svg("text", { class: "value", x: width - pad.right + 4, y: y + 9 },
                   [`${showNum(row.buffer_share)}%`]));
  });

  return svg("svg", { viewBox: `0 0 ${width} ${height}`, role: "img" }, parts);
}

function drawBufferCharts() {
  if (!buffers) return;
  const rows = (buffers.articles ?? []).slice(0, BUFFER_SHOWN);
  const counts = buffers.counts ?? {};
  const tiles = [
    ["articles", showNum(counts.articles ?? 0)],
    ["red", showNum(counts.red ?? 0)],
    ["yellow", showNum(counts.yellow ?? 0)],
    ["green", showNum(counts.green ?? 0)],
    ["over the top", showNum(counts.over ?? 0)],
    ["not counted", showNum(counts.uncounted ?? 0)],
  ];
  $("ddmrp-stats").replaceChildren(...tiles.map(([name, value]) =>
    el("span", { className: "stat" }, [
      el("span", { className: "stat-name", textContent: name }),
      el("strong", { textContent: value }),
    ])));

  const empty = (text) => el("p", { className: "muted", textContent: text });
  $("zone-strip").replaceChildren(zoneStrip(counts));
  $("buffer-chart").replaceChildren(rows.length
    ? bufferBars(rows) : empty("Nothing to size a buffer from yet."));
}

function drawBuffers() {
  if (!buffers) return;
  const rows = bufferAll
    ? buffers.articles
    : buffers.articles.filter((row) => row.zone === "red" || row.zone === "yellow");

  renderTable($("table-buffers"), [
    {
      label: "Article",
      cell: (row) => {
        const box = el("span", {}, [el("strong", { textContent: row.article })]);
        const under = [row.supplier, `${row.lead_class} lead ${showNum(row.lead_days)} d`,
                       row.variability].filter(Boolean).join(" · ");
        box.append(el("div", { className: "desig muted", textContent: under }));
        return box;
      },
    },
    { label: "weekly avg cons", numeric: true, cell: (row) => showNum(row.per_week) },
    { label: "On hand", numeric: true, cell: (row) => countCell(row) },
    { label: "Red", numeric: true, cell: (row) => showNum(row.red) },
    { label: "Yellow", numeric: true, cell: (row) => showNum(row.yellow) },
    { label: "Green", numeric: true, cell: (row) => showNum(row.green) },
    { label: "Top", numeric: true, cell: (row) => showNum(row.top_of_green) },
    { label: "Net flow", numeric: true, cell: (row) => showNum(row.net_flow) },
    { label: "Buffer", numeric: true, cell: (row) => `${showNum(row.buffer_share)}%` },
    { label: "Zone", cell: (row) => zonePill(row.zone) },
    { label: "To order", numeric: true, cell: (row) => (row.to_order ? showNum(row.to_order) : "—") },
    { label: "Spike risk", numeric: true, cell: (row) => `${showNum(row.spike_risk)}%` },
  ], rows, bufferAll ? "Nothing stored yet." : "No article in the red or the yellow.");

  const counts = buffers.counts;
  $("buffer-counts").textContent =
    `${counts.articles} article(s) · ${counts.red} red · ${counts.yellow} yellow · `
    + `${counts.green} green · ${counts.uncounted} not counted`;
  $("buffer-risky").setAttribute("aria-pressed", String(!bufferAll));
  $("buffer-all").setAttribute("aria-pressed", String(bufferAll));
  drawBufferCharts();

  const chain = buffers.chain;
  const states = chain.states;
  renderTable($("table-chain"), [
    { label: "This week", cell: (state) => el("strong", { textContent: state }) },
    ...states.map((next) => ({
      label: `→ ${next}`,
      numeric: true,
      cell: (state) => `${Math.round(100 * (chain.matrix[state]?.[next] ?? 0))}%`,
    })),
  ], states, "");

  const share = states
    .map((state) => `${state} ${Math.round(100 * (chain.steady[state] ?? 0))}%`)
    .join(" · ");
  notice($("buffer-note"), buffers.thin ? "warn" : "",
    `${chain.transitions} week to week move(s) counted.`);

  const { cycle, horizon } = bufferRules();
  for (const [id, fmt] of [["buffer-csv", "csv"], ["buffer-xlsx", "xlsx"], ["buffer-sql", "sql"]]) {
    $(id).href = `/export/safety-stock.${fmt}?cycle=${cycle}&horizon=${horizon}`;
  }
}

async function renderBuffers() {
  const { cycle, horizon } = bufferRules();
  try {
    buffers = await api(`/api/ddmrp?cycle=${cycle}&horizon=${horizon}`);
  } catch (e) {
    notice($("buffer-note"), "bad", e.message);
    return;
  }
  drawBuffers();
}

$("buffer-apply").addEventListener("click", renderBuffers);
$("buffer-all").addEventListener("click", () => { bufferAll = true; drawBuffers(); });
$("buffer-risky").addEventListener("click", () => { bufferAll = false; drawBuffers(); });

function renderDraft() {
  const drawn = orderDraft != null && orderDraft.lines.length > 0;
  $("draft").hidden = !drawn;
  $("drop-draft").hidden = !drawn;
  if (!drawn) return;

  const head = ["Article", "Quantity", "Unit", "Supplier", "Why", "Learned"];
  $("table-draft").replaceChildren(
    el("thead", {}, [el("tr", {}, head.map((name) => el("th", { textContent: name })))]),
    el("tbody", {}, orderDraft.lines.map((line, index) => {
      const quantity = el("input", {
        className: "cell", type: "number", step: "any", min: "0", value: line.quantity,
      });
      quantity.addEventListener("change", () => {
        orderDraft.lines[index].quantity = Number(quantity.value) || 0;
      });
      return el("tr", {}, [
        el("td", {}, [
          el("div", {}, [el("strong", { textContent: line.article })]),
          line.reference
            ? el("div", { className: "desig", textContent: line.reference })
            : el("span"),
        ]),
        el("td", { className: "num" }, [quantity]),
        el("td", { textContent: line.unit ?? "—" }),
        el("td", { textContent: line.supplier ?? "—" }),
        el("td", { className: "desig", textContent: line.reason }),
        el("td", { className: "desig", textContent: line.learned
          ? `x${line.learned.qty_factor.toFixed(2)} order, x${line.learned.safety_factor.toFixed(2)} safety: ${line.learned.last_reason ?? ""}`
          : "" }),
      ]);
    })),
  );
  $("draft-reason").textContent = orderDraft.note
    ? `${orderDraft.note} — drafted by ${orderDraft.model} from ${orderDraft.drafted_from} article(s).`
    : `Drafted by ${orderDraft.model} from ${orderDraft.drafted_from} article(s).`;
}

async function renderAgents() {
  // The agents run the Claude command line on this machine; there is no key to check.
  try {
    const data = await api("/api/agents");
    $("ask-agent").disabled = !data.ready;
    if (!data.ready) notice($("agent-note"), "warn", data.detail);
  } catch {
    $("ask-agent").disabled = true;
  }
}

async function loadMovements() {
  try {
    moves = await api(`/api/movements?period=${period}`);
  } catch (e) {
    notice($("ddmrp-notice"), "bad", e.message);
    return;
  }
  renderMovements();
}

async function loadPlan(options = {}) {
  try {
    plan = await api("/api/forecast");
  } catch (e) {
    notice($("ddmrp-notice"), "bad", e.message);
    return;
  }
  if (!options.keepDraft) orderDraft = null;

  renderCharts();
  renderDraft();
  renderBuffers();
}

function loadDdmrp(options) {
  return Promise.all([loadPlan(options), loadMovements(), renderAgents()]);
}

// No refresh button: the screen reads the numbers again by itself while it is open, and a
// draft being looked at is kept.
setInterval(() => {
  if (!document.hidden && !$("panel-ddmrp").hidden) loadDdmrp({ keepDraft: true });
}, 30000);
$("drop-draft").addEventListener("click", () => { orderDraft = null; renderDraft(); });

function showPeriod(name) {
  period = name;
  return loadMovements();
}

for (const name of ["week", "month", "half"]) {
  $(`period-${name}`).addEventListener("click", () => showPeriod(name));
}

$("ask-agent").addEventListener("click", async () => {
  const button = $("ask-agent");
  button.disabled = true;
  notice($("agent-note"), "", "Plutus is reading the numbers...");
  try {
    // The route is /api/forecast/draft. It was being asked for /api/forecast/orderDraft,
    // which is no route at all, so the button answered 404 and nothing was ever drafted.
    // An empty article field leaves Plutus to pick; naming one asks for that one.
    const named = $("order-article").value.split(",")
      .map((name) => name.trim()).filter(Boolean);
    orderDraft = await api("/api/forecast/draft", postJson({
      site: $("order-site").value.trim() || null,
      work_item: $("order-work").value.trim() || null,
      only: named.length ? named : null,
    }));
    for (const line of orderDraft.lines) line.proposed = line.quantity;
    const ruled = [...(orderDraft.cuts ?? []).map((c) => `${c.article}: cut, ${c.why}`),
                   ...(orderDraft.adjusted ?? []).map((c) => `${c.article}: ${c.from} to ${c.to}, ${c.why}`)];
    const read = (orderDraft.read ?? []).join(", ");
    notice($("agent-note"), orderDraft.lines.length ? "ok" : "warn",
      orderDraft.lines.length
        ? `${orderDraft.lines.length} line(s) drafted from ${orderDraft.drafted_from} `
          + `article(s). Check them, then save.`
        : `Nothing worth ordering${read ? ` in ${read}` : ""}. ${orderDraft.note}`);
    if (ruled.length) {
      $("agent-note").append(el("p", { className: "muted", textContent: `Plutus's charter applied: ${ruled.join("; ")}.` }));
    }
    renderDraft();
  } catch (e) {
    notice($("agent-note"), "bad", e.message);
  } finally {
    button.disabled = false;
  }
});

$("save-draft").addEventListener("click", async () => {
  if (!orderDraft) return;
  const button = $("save-draft");
  const lines = orderDraft.lines
    .filter((line) => line.quantity > 0)
    .map((line) => ({
      article: line.article,
      designation: line.reason || null,
      reference: line.reference || null,
      quantity: line.quantity,
      in_stock: false,
    }));
  if (!lines.length) {
    notice($("agent-note"), "warn", "Every line is at zero. Nothing to save.");
    return;
  }

  button.disabled = true;
  try {
    const answer = await api("/api/orders", postJson({
      list: {
        list_date: new Date().toISOString().slice(0, 10),
        site: $("order-site").value.trim() || null,
        work_item: $("order-work").value.trim() || null,
        drafter: "PLUTUS",
        bill_company: $("bill-company").value.trim() || null,
        bill_contact: $("bill-contact").value.trim() || null,
        bill_address: $("bill-address").value.trim() || null,
        lines,
      },
      proposal: orderDraft.lines.map((line) => ({
        article: line.article, proposed: line.proposed ?? line.quantity,
        final: line.quantity > 0 ? line.quantity : 0 })),
    }));
    notice($("agent-note"), "ok",
      `Saved as purchase order #${answer.list_id}. It is in the Purchase orders tab now.`);
    orderDraft = null;
    renderDraft();
    await loadDdmrp();
  } catch (e) {
    notice($("agent-note"), "bad", e.message);
  } finally {
    button.disabled = false;
  }
});
