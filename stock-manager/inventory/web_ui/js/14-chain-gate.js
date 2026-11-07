"use strict";

// ---- the chain, and what the two of them have been doing ------------------

const CHAIN_WORDS = {
  requested: "a basket was held",
  checked: "Terminus read the names",
  approved: "a person approved it",
  cancelled: "it was cancelled",
  ordered: "the order was placed",
};

// A glyph a step, drawn in a 24 box so they can all be placed the same way.
const STEP_ICONS = {
  1: "M3 5h18v11H8l-5 4z M7 9h10 M7 12h7",
  2: "M3 7l9-4 9 4v10l-9 4-9-4z M3 7l9 4 9-4 M12 11v10",
  3: "M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14z M16 16l5 5",
  4: "M12 3v18 M4 8h16 M4 8l-2 6h4z M20 8l-2 6h4z",
  5: "M12 3a4 4 0 1 0 0 8 4 4 0 0 0 0-8z M4 21c0-4 4-7 8-7s8 3 8 7",
  6: "M3 7a2 2 0 0 1 2-2h12v14a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z M17 10h4v5h-4z",
  7: "M2 6h3l3 10h10 M9 20a1 1 0 1 0 0-2 1 1 0 0 0 0 2z M18 20a1 1 0 1 0 0-2 1 1 0 0 0 0 2z"
     + " M8 8h13l-2 6H9",
};

// The chain in the middle, which is the one thing the round is about.
const HUB_GLYPH = "M9 13a4 4 0 0 1 0-6l2-2a4 4 0 0 1 6 6l-1 1 M15 11a4 4 0 0 1 0 6l-2 2a4 4"
  + " 0 0 1-6-6l1-1";

function turnedIcon(path, x, y, size, className) {
  const scale = size / 24;
  return svg("path", {
    class: className, d: path,
    transform: `translate(${x - size / 2} ${y - size / 2}) scale(${scale})`,
  });
}

function roundDiagram(steps, atStep, faults = {}, pick = null) {
  // A ring of wedges, the way a process circle is drawn on a slide: a thick band cut into
  // one segment a step, a notch pointing at the hub, the name outside it. Colour carries
  // the state — green behind, red where we are, yellow ahead, grey for what is not built.
  const wide = 760;
  const tall = 620;
  const cx = wide / 2;
  const cy = tall / 2;
  const inner = 118;
  const outer = 205;
  const gap = 0.045;          // radians of daylight between two wedges
  const parts = [];

  const at = (radius, angle) => [cx + radius * Math.cos(angle), cy + radius * Math.sin(angle)];
  const slice = (Math.PI * 2) / steps.length;

  steps.forEach((step, index) => {
    const group = [];
    const from = -Math.PI / 2 + index * slice + gap / 2;
    const to = from + slice - gap;
    const middle = (from + to) / 2;

    const [ox1, oy1] = at(outer, from);
    const [ox2, oy2] = at(outer, to);
    const [ix2, iy2] = at(inner, to);
    const [ix1, iy1] = at(inner, from);

    // Green is behind, yellow is the one being waited on, red is something wrong. What
    // has not come round yet stays pale, and so do the two that are not built.
    const wrong = faults[String(step.number)];
    const state = wrong ? "fault"
      : !step.built ? "unbuilt"
      : step.number === atStep ? "pending"
      : (atStep && step.number < atStep) ? "done" : "ahead";

    const wedge = tip(svg("path", {
      class: `wedge ${state}`,
      d: `M ${ox1} ${oy1} A ${outer} ${outer} 0 0 1 ${ox2} ${oy2}`
         + ` L ${ix2} ${iy2} A ${inner} ${inner} 0 0 0 ${ix1} ${iy1} Z`,
    }), `${step.number}. ${step.name}\n${wrong ?? step.detail}\nclick: what has happened here`);
    group.push(wedge);

    // The notch: a tongue of the same colour reaching in towards the hub, which is what
    // makes a ring of wedges read as a round rather than a pie.
    const [nx, ny] = at(inner - 20, middle);
    const [ax, ay] = at(inner, middle - slice * 0.16);
    const [bx, by] = at(inner, middle + slice * 0.16);
    group.push(svg("path", { class: `wedge ${state}`,
                             d: `M ${ax} ${ay} L ${nx} ${ny} L ${bx} ${by} Z` }));

    const [gx, gy] = at((inner + outer) / 2, middle);
    group.push(turnedIcon(STEP_ICONS[step.number] ?? STEP_ICONS[1], gx, gy - 6, 30,
                          `wedge-icon ${state}`));
    group.push(svg("text", { class: `wedge-number ${state}`, x: gx, y: gy + 30,
                             "text-anchor": "middle" }, [String(step.number)]));

    // The name outside the band, turned to the side of the circle it is on.
    const [lx, ly] = at(outer + 16, middle);
    const side = Math.cos(middle);
    const anchor = Math.abs(side) < 0.25 ? "middle" : (side > 0 ? "start" : "end");
    const lines = wrapLabel(step.name, 16);
    lines.forEach((line, row) => {
      group.push(svg("text", {
        class: `wedge-label ${state}`, x: lx,
        y: ly + (row - (lines.length - 1) / 2) * 13 + 4, "text-anchor": anchor,
      }, [line]));
    });

    // The whole wedge is one button: its history opens in a small window of its own.
    const button = svg("g", {
      class: "step", role: "button", tabindex: "0",
      "aria-label": `${step.number}. ${step.name}: what has happened here`,
    }, group);
    if (pick) {
      button.addEventListener("click", () => pick(step));
      button.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          pick(step);
        }
      });
    }
    parts.push(button);
  });

  // The hub, white and plain, so the colour around it is the thing that is read.
  parts.push(svg("circle", { class: "hub-ring", cx, cy, r: inner - 26 }));
  parts.push(svg("circle", { class: "hub-dot", cx, cy: cy - 12, r: 28 }));
  parts.push(turnedIcon(HUB_GLYPH, cx, cy - 12, 28, "hub-glyph"));
  const here = steps.find((one) => one.number === atStep);
  const broken = Object.keys(faults).length > 0;
  parts.push(svg("text", { class: `hub-word ${broken ? "fault" : ""}`, x: cx, y: cy + 36,
                           "text-anchor": "middle" },
                 [broken ? "SEE BELOW" : here ? `STEP ${here.number}` : "AT REST"]));

  return svg("svg", { viewBox: `0 0 ${wide} ${tall}`, class: "round", role: "img" }, parts);
}

function wrapLabel(text, most = 18) {
  const lines = [];
  let line = "";
  for (const word of text.split(" ")) {
    if (line && (line + " " + word).length > most) {
      lines.push(line);
      line = word;
    } else {
      line = line ? `${line} ${word}` : word;
    }
  }
  if (line) lines.push(line);
  return lines;
}

// ---- the audit behind one wedge --------------------------------------------

const stepPop = () => $("step-pop");

function closeStepAudit() {
  stepPop().hidden = true;
}

async function openStepAudit(step) {
  // Straight to the data: no title, no note. Esc or a click outside closes it.
  $("step-dialog").setAttribute("aria-label", `${step.number}. ${step.name}`);
  renderTable($("table-step"), [], [], "Reading...");
  stepPop().hidden = false;
  try {
    const history = await api(`/api/chain/step/${step.number}`);
    renderTable($("table-step"), [
      { label: "When", cell: (row) => document.createTextNode(
        (row.at ?? "").replace("T", " ").slice(0, 16) || "-") },
      { label: "Who", cell: (row) => document.createTextNode(row.who ?? "-") },
      { label: "What", cell: (row) => el("strong", { textContent: row.title }) },
      { label: "Detail", cell: (row) => el("span", { className: "desig",
                                                      textContent: row.detail ?? "" }) },
    ], history.rows, history.built
      ? "Nothing has happened in this step yet."
      : "Nothing to show: this step is not built.");
  } catch (e) {
    renderTable($("table-step"), [], [], e.message);
  }
}

// A click on the dimmed ground, outside the window, closes it as well.
stepPop().addEventListener("click", (event) => {
  if (event.target === stepPop()) closeStepAudit();
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !stepPop().hidden) closeStepAudit();
});

let roundDrawn = "";

async function loadChain() {
  let data;
  try {
    data = await api("/api/chain");
  } catch (e) {
    notice($("chain-warn"), "bad", e.message);
    return;
  }

  const log = data.log ?? {};
  const doings = data.doings ?? {};
  const held = doings.requests ?? {};

  $("chain-stats").replaceChildren(
    statTile("baskets held", showNum(Object.values(held).reduce((sum, one) => sum + one, 0))),
    statTile("waiting on you", showNum((held.waiting ?? 0) + (held.checked ?? 0))),
    statTile("approved", showNum(held.approved ?? 0)),
  );

  const steps = data.steps ?? [];
  const now = data.now ?? {};
  const faults = data.faults ?? {};
  // Redrawn only when something it shows has changed. It is looked at every few seconds,
  // and a wheel rebuilt under the pointer would lose a click that began before the redraw.
  const drawn = JSON.stringify([steps, now.step ?? 0, faults]);
  if (drawn !== roundDrawn || !$("chain-round").firstChild) {
    $("chain-round").replaceChildren(
      roundDiagram(steps, now.step ?? 0, faults, openStepAudit));
    roundDrawn = drawn;
  }

  const step = steps.find((one) => one.number === now.step);
  // Nothing in flight says nothing: an empty round is its own answer.
  $("chain-now").textContent = step
    ? `Step ${step.number}, ${step.name} — ${now.why ?? step.detail}`
      + (now.request_id ? ` (request #${now.request_id})` : "")
    : "";
  $("chain-now").className = step ? "legend here" : "legend";

  // One line under the round, and the worse of the two things it could say. A fault
  // outranks a full shelf, and both outrank saying nothing.
  const wrong = Object.entries(faults)
    .map(([number, why]) => `Step ${number}: ${why}.`).join(" ");
  const full = now.dwelling ?? [];
  if (wrong) {
    notice($("chain-warn"), "bad", wrong);
  } else if (full.length) {
    // Buying more of what is already standing there is the easy mistake; it is said out loud.
    notice($("chain-warn"), "warn", "Already on the shelf: "
      + full.map((one) => `${one.article}, ${showNum(one.on_hand)} for `
                          + `${showNum(one.asked)} asked (${one.times}×)`).join(" · "));
  } else {
    notice($("chain-warn"), "", "");
  }

  const doing = $("chain-doing");
  doing.replaceChildren();
  if (now.request_id && (now.state === "waiting" || now.state === "checked")) {
    const approve = el("button", { className: "action", textContent: "Approve" });
    approve.addEventListener("click", async () => {
      await gateSay(now.request_id, "approve");
      await loadChain();
    });
    const cancel = el("button", { className: "action ghost", textContent: "Cancel" });
    cancel.addEventListener("click", async () => {
      await gateSay(now.request_id, "cancel");
      await loadChain();
    });
    doing.append(approve, cancel);
  } else if (now.request_id && now.state === "approved") {
    const cancel = el("button", { className: "action ghost",
                                  textContent: "Cancel the approval" });
    cancel.addEventListener("click", async () => {
      await gateSay(now.request_id, "cancel");
      await loadChain();
    });
    doing.append(cancel);
  }

  renderTable($("table-approval"), [
    { label: "#", numeric: true, cell: (row) => String(row.request_id) },
    { label: "Where", cell: (row) => el("span", {}, [
      el("strong", { textContent: row.shop || "no shop named" }),
      el("div", { className: "desig", textContent:
        [row.site, row.work_item, row.asked_by].filter(Boolean).join(" · ") || "—" }),
    ]) },
    { label: "Total", numeric: true,
      cell: (row) => `${Number(row.total ?? 0).toFixed(2)}` },
    { label: "Why it is held", cell: (row) => el("span", { className: "desig",
      textContent: (row.reasons ?? []).map((one) => gateWords[one] ?? one).join(" · ") }) },
    { label: "Terminus", cell: (row) => (row.agent_verdict
      ? el("span", { className: `pill ${row.agent_verdict === "same" ? "green" : "yellow"}`,
                     textContent: row.agent_verdict })
      : el("span", { className: "muted", textContent: "not read yet" })) },
    { label: "", cell: (row) => {
      const box = el("span", { className: "row tight" });
      const approve = el("button", { className: "mini", textContent: "Approve" });
      approve.addEventListener("click", async () => {
        await gateSay(row.request_id, "approve");
        await loadChain();
      });
      const cancel = el("button", { className: "mini", textContent: "Cancel" });
      cancel.addEventListener("click", async () => {
        await gateSay(row.request_id, "cancel");
        await loadChain();
      });
      box.append(approve, cancel);
      return box;
    } },
  ], data.approval ?? [], "Nothing is waiting on you.");

  renderTable($("table-unbought"), [
    { label: "Channel", cell: (row) => String(row.channel) },
    { label: "Shop", cell: (row) => row.shop || "—" },
    { label: "Lines", numeric: true, cell: (row) => showNum(row.lines) },
    { label: "What", cell: (row) => el("span", { className: "desig", textContent:
      (row.items ?? []).map((one) => `${showNum(one.quantity)} × ${one.article}`)
        .join(", ") || "—" }) },
    { label: "Site", cell: (row) => row.site || "—" },
    { label: "Last added", cell: (row) => (row.last_at ?? "").replace("T", " ") },
    { label: "By", cell: (row) => row.last_by ?? "—" },
  ], data.baskets ?? [], "Every basket has been turned into an order.");

  renderTable($("table-plutus"), [
    { label: "When", cell: (row) => (row.at ?? "").replace("T", " ") },
    { label: "Asked by", cell: (row) => row.by ?? "—" },
    { label: "Drafted", cell: (row) => row.lines ?? "—" },
    { label: "How", cell: (row) => row.detail ?? "—" },
  ], doings.plutus?.drafts ?? [], "Plutus has not drafted anything yet.");
}

// ---- the gate on what the bot buys ----------------------------------------

let gateWords = {};

function gateLines(rows, total) {
  const table = el("table", { className: "order-table" });
  renderTable(table, [
    { label: "Article", cell: (row) => {
      const box = el("span", {}, [el("strong", { textContent: row.article ?? "?" })]);
      const found = (row.found ?? []).map((one) => gateWords[one] ?? one).join(" · ");
      if (found) box.append(el("div", { className: "desig", textContent: found }));
      return box;
    } },
    { label: "Qty", numeric: true, cell: (row) => showNum(row.quantity) },
    { label: "Supplier", cell: (row) => row.supplier ?? "—" },
    { label: "Price", numeric: true, cell: (row) => (row.unit_price == null
      ? el("span", { className: "muted", textContent: "no price" })
      : document.createTextNode(`${Number(row.unit_price).toFixed(2)}`)) },
    { label: "Usually", numeric: true, cell: (row) => (row.usual == null
      ? "—" : `${Number(row.usual).toFixed(2)}`) },
  ], rows, "Nothing on this one.");
  // The sum of what the basket would cost, at the end of the lines.
  if (rows.length && total != null) {
    table.append(el("tfoot", {}, [el("tr", {}, [
      el("td", {}, [el("strong", { textContent: "Total" })]),
      el("td"), el("td"),
      el("td", { className: "num" }, [el("strong", { textContent: `${Number(total).toFixed(2)} EUR` })]),
      el("td"),
    ])]));
  }
  return table;
}

function gateRow(held) {
  const title = el("div", { className: "sheet-title" }, [
    el("strong", { textContent: `#${held.request_id}` }),
    el("span", { className: "muted", textContent:
      [held.shop, held.site, held.work_item].filter(Boolean).join(" · ") }),
  ]);

  const why = el("div", { className: "row tight" },
    (held.reasons ?? []).map((one) => el("span", {
      className: "pill backorder", textContent: gateWords[one] ?? one })));

  // The lines are behind a button: the card says why it was held, not what is in it.
  const detail = el("div", { hidden: true }, [
    el("div", { className: "sheet-body" }, [
      el("div", { className: "scroll-x" }, [gateLines(held.lines ?? [], held.total)])]),
  ]);

  const buttons = el("div", { className: "row tight" });
  const approve = el("button", { className: "action", textContent: "Approve" });
  approve.addEventListener("click", () => gateSay(held.request_id, "approve"));
  const cancel = el("button", { className: "action ghost", textContent: "Cancel" });
  cancel.addEventListener("click", () => gateSay(held.request_id, "cancel"));
  const develop = el("button", { className: "mini", textContent: "Develop database" });
  develop.addEventListener("click", () => {
    detail.hidden = !detail.hidden;
    develop.textContent = detail.hidden ? "Develop database" : "Fold database";
  });
  buttons.append(approve, cancel, el("span", { className: "grow" }), develop);

  return el("div", { className: "sheet" }, [title, why, buttons, detail]);
}

async function gateSay(requestId, word) {
  try {
    say(word === "check" ? "Asking Terminus..." : "...");
    await api(`/api/gate/${requestId}/${word}`, { method: "POST" });
    await loadGate();
    say(word === "approve" ? `Request #${requestId} approved`
      : word === "cancel" ? `Request #${requestId} cancelled` : "The agent has answered");
  } catch (e) {
    notice($("gate-note"), "bad", e.message);
  }
}

async function loadGate() {
  let data;
  try {
    data = await api("/api/gate");
  } catch {
    return;
  }
  gateWords = data.words ?? {};
  const rows = data.waiting ?? [];
  $("gate-card").hidden = !rows.length;
  if (!rows.length) return;

  // Said only when something is wrong: a broken seal is the one thing here that is not
  // about a basket.
  const log = data.log ?? {};
  $("gate-note").hidden = Boolean(log.whole);
  $("gate-note").textContent = log.whole ? "" : `THE LOG IS BROKEN at entry ${log.broken_at}`;
  $("gate-note").className = log.whole ? "muted" : "bad";
  $("gate-list").replaceChildren(...rows.map(gateRow));
}
