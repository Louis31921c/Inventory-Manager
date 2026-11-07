"use strict";

// ---- Plutus: who the order is billed to ----------

async function loadBillTo() {
  try {
    const kept = await api("/api/bill-to");
    $("bill-company").value = kept.company;
    $("bill-contact").value = kept.contact;
    $("bill-address").value = kept.address;
  } catch {
  }
}

// What is typed here is remembered for the next order, and goes on this one when it is saved.
for (const id of ["bill-company", "bill-contact", "bill-address"]) {
  $(id).addEventListener("change", async () => {
    try {
      await api("/api/bill-to", putJson({
        company: $("bill-company").value, contact: $("bill-contact").value,
        address: $("bill-address").value }));
    } catch (e) {
      notice($("agent-note"), "bad", e.message);
    }
  });
}

// ---- policies and the bots' charters -----------------------------------------------

function drawPolicies(data) {
  $("pol-days").textContent = data.min_dry_days;
  $("pol-ceiling").textContent = data.hard_ceiling;
  $("pol-switch").textContent = data.enabled ? "Policies are ON: turn them off" : "Policies are OFF: turn them on";
  $("pol-switch").dataset.on = data.enabled ? "1" : "0";
  const act = (path, options) => async () => {
    try { drawPolicies(await api(path, options)); }
    catch (e) { notice($("pol-note"), "bad", e.message); }
  };
  renderTable($("table-policies"), [
    { label: "Name", cell: (row) => td(row.name) },
    { label: "Does", cell: (row) => td({ send_order: "sends orders", send_chase: "sends reminders",
        approve: "approves baskets" }[row.action]) },
    { label: "Supplier", cell: (row) => td(row.supplier === "*" ? "any" : row.supplier) },
    { label: "Limits", cell: (row) => td([`max ${row.max_total} EUR`,
        row.per_day ? `${row.per_day}/day` : null, row.per_month ? `${row.per_month}/month` : null,
        `price ±${Math.round(row.price_band * 100)}%`, `${row.hours_from}h-${row.hours_to}h`,
        row.action === "approve" ? `reasons: ${row.allowed_reasons || "none"}` : `${row.min_deliveries}+ deliveries`]
        .filter(Boolean).join(", ")) },
    { label: "State", cell: (row) => el("span", { className: row.active ? (row.mode === "live" ? "bad" : "ok") : "muted",
        textContent: !row.active ? "inactive" : (row.mode === "live" ? "LIVE" : "dry run")
          + ` (${row.dry_days} day(s) old)` }) },
    { label: "", cell: (row) => {
      const box = el("span");
      const add = (label, path, options, cls = "mini") => {
        const b = el("button", { className: cls, textContent: label });
        b.addEventListener("click", act(path, options));
        box.append(b, " ");
      };
      add(row.active ? "switch off" : "switch on", `/api/policies/${row.policy_id}`, patchJson({ active: !row.active }));
      add(row.mode === "live" ? "back to dry run" : "go live", `/api/policies/${row.policy_id}`,
          patchJson({ mode: row.mode === "live" ? "dry_run" : "live" }));
      add("delete", `/api/policies/${row.policy_id}`, { method: "DELETE" });
      return box;
    } },
  ], data.policies ?? [], "No policy. Nothing goes through without a person.");
  renderTable($("table-policy-summary"), [
    { label: "Policy", cell: (row) => td(row.name) },
    { label: "Mode", cell: (row) => td(row.mode) },
    { label: "Allowed", numeric: true, cell: (row) => td(`${row.allowed} (${money(row.allowed_value)})`) },
    { label: "Refused", numeric: true, cell: (row) => td(row.denied) },
  ], data.summary ?? [], "");
  renderTable($("table-policy-log"), [
    { label: "When", cell: (row) => td(row.at.replace("T", " ")) },
    { label: "Policy", cell: (row) => td(row.policy) },
    { label: "What", cell: (row) => td(`${row.action} ${row.subject}${row.supplier ? ` (${row.supplier})` : ""}`) },
    { label: "EUR", numeric: true, cell: (row) => td(row.amount == null ? "" : row.amount.toFixed(2)) },
    { label: "Decision", cell: (row) => el("span", { className: row.decision === "allowed" ? "ok" : "muted",
        textContent: row.decision + (row.decision === "allowed" ? (row.acted ? ", done" : ", dry run") : "") }) },
    { label: "Why", cell: (row) => td(row.reasons) },
  ], data.log ?? [], "Nothing logged yet.");
}

async function loadPolicies() { try { drawPolicies(await api("/api/policies")); } catch {} }

$("pol-switch").addEventListener("click", async () => {
  try { drawPolicies(await api("/api/policies/switch", postJson({ on: $("pol-switch").dataset.on !== "1" }))); }
  catch (e) { notice($("pol-note"), "bad", e.message); }
});
$("pol-run").addEventListener("click", async () => {
  try {
    const done = await api("/api/policies/run", { method: "POST" });
    drawPolicies(done);
    notice($("pol-note"), "ok", done.skipped ? `Nothing run: ${done.skipped}.` : `${done.acted} thing(s) done.`);
  } catch (e) { notice($("pol-note"), "bad", e.message); }
});
$("np-go").addEventListener("click", async () => {
  const num = (id) => ($(id).value.trim() === "" ? null : Number($(id).value));
  try {
    const done = await api("/api/policies", postJson({
      name: $("np-name").value, action: $("np-action").value, supplier: $("np-supplier").value || null,
      max_total: num("np-max"), per_day: num("np-day"), per_month: num("np-month"),
      price_band: (num("np-band") ?? 10) / 100, min_deliveries: num("np-hist") ?? 3,
      hours_from: num("np-from") ?? 8, hours_to: num("np-to") ?? 17, per_supplier_day: num("np-per") ?? 2,
      max_chases: num("np-chases") ?? 2, allowed_reasons: $("np-reasons").value.replace(/\s+/g, "") }));
    drawPolicies(done);
    notice($("pol-note"), "ok", "Created: inactive and in dry run. Switch it on to see what it would do.");
  } catch (e) { notice($("pol-note"), "bad", e.message); }
});

function drawCharters(data) {
  const body = $("charters-body");
  body.replaceChildren();
  for (const bot of ["plutus", "terminus"]) {
    const c = data[bot];
    const box = el("div", { className: "sheet" }, [
      el("div", { className: "sheet-title" }, [el("strong", { textContent: (bot === "plutus" ? "Plutus, the buyer" : "Terminus, the gate keeper") + ` (${c.source})` })]),
      el("p", { className: "eyebrow", textContent: "Free to" }),
      el("ul", {}, c.freedom.map((t) => el("li", { textContent: t }))),
      el("p", { className: "eyebrow", textContent: "Never" }),
      el("ul", {}, c.never.map((t) => el("li", { textContent: t }))),
      el("p", { className: "eyebrow", textContent: "Limits" }),
    ]);
    const table = el("table", {});
    renderTable(table, [
      { label: "Limit", cell: (row) => td(row.key) },
      { label: "Value", cell: (row) => {
        const input = el("input", { className: "cell", size: 7, inputMode: "decimal", value: row.value });
        const save = el("button", { className: "mini", textContent: "save" });
        save.addEventListener("click", async () => {
          try { drawCharters(await api(`/api/charters/${bot}/${row.key}`, putJson({ value: Number(input.value) })));
                notice($("charters-note"), "ok", "Saved."); }
          catch (e) { notice($("charters-note"), "bad", e.message); }
        });
        return el("span", {}, [input, save]);
      } },
      { label: "Default", numeric: true, cell: (row) => td(row.default) },
      { label: "What it does", cell: (row) => td(row.what) },
      { label: "", cell: (row) => {
        if (!row.changed) return td("");
        const back = el("button", { className: "mini", textContent: "reset" });
        back.addEventListener("click", async () => {
          try { drawCharters(await api(`/api/charters/${bot}/${row.key}`, { method: "DELETE" })); }
          catch (e) { notice($("charters-note"), "bad", e.message); }
        });
        return back;
      } },
    ], c.limits, "");
    box.append(el("div", { className: "scroll-x" }, [table]));
    body.append(box);
  }
}

async function loadCharters() { try { drawCharters(await api("/api/charters")); } catch {} }

function drawJudge(data) {
  $("jd-mode").value = data.mode;
  $("jd-state").textContent = data.mode === "off"
    ? "Terminus only reads names. Nothing is approved or cancelled by it."
    : data.mode === "dry_run"
      ? `Dry run, ${data.dry_days} of ${data.min_dry_days} days: it logs what it would approve or cancel and does nothing.`
      : "Live: it approves and cancels baskets inside its limits, signed TERMINUS in the gate's log.";
}

async function loadJudge() { try { drawJudge(await api("/api/judge")); } catch {} }

$("jd-save").addEventListener("click", async () => {
  try { drawJudge(await api("/api/judge/mode", postJson({ mode: $("jd-mode").value }))); notice($("jd-note"), "ok", "Set."); }
  catch (e) { notice($("jd-note"), "bad", e.message); loadJudge(); }
});
$("jd-run").addEventListener("click", async () => {
  try {
    const done = await api("/api/judge/run", { method: "POST" });
    notice($("jd-note"), "ok", done.decided.length
      ? done.decided.map((d) => `#${d.request_id} ${d.does}`).join(", ") : "Nothing to decide.");
    loadPolicies(); loadGate();
  } catch (e) { notice($("jd-note"), "bad", e.message); }
});

// ---- the wallet, Plutus's purchasing, the payments --------------------------------

function drawWallet(data) {
  $("wl-days").textContent = data.plutus.min_dry_days;
  $("wl-tiles").replaceChildren(statTile("balance", money(data.balance)), statTile("committed to orders", money(data.committed)),
                                statTile("free to spend", money(data.available)));
  $("pl-mode").value = data.plutus.mode;
  $("pl-state").textContent = data.plutus.mode === "off" ? "Plutus does not send, approve, place or pay."
    : data.plutus.mode === "dry_run" ? `Dry run, ${data.plutus.dry_days} of ${data.plutus.min_dry_days} days: it logs what it would do and does nothing.`
      : "Live: it sends orders, approves and places baskets and pays matched bills, from the wallet.";
  $("py-name").value = data.payer.name; $("py-iban").value = data.payer.iban; $("py-bic").value = data.payer.bic;
  renderTable($("table-plutus-log"), [
    { label: "When", cell: (row) => td(row.at.replace("T", " ")) },
    { label: "What", cell: (row) => td(`${row.action} ${row.subject}${row.supplier ? ` (${row.supplier})` : ""}`) },
    { label: "EUR", numeric: true, cell: (row) => td(row.amount == null ? "" : row.amount.toFixed(2)) },
    { label: "Decision", cell: (row) => el("span", { className: row.decision === "allowed" ? "ok" : "muted",
        textContent: row.decision + (row.decision === "allowed" ? (row.acted ? ", done" : ", dry run") : "") }) },
    { label: "Why not", cell: (row) => td(row.reasons) },
  ], data.log ?? [], "Nothing yet.");
  renderTable($("table-wallet"), [
    { label: "When", cell: (row) => td(row.at.replace("T", " ")) },
    { label: "What", cell: (row) => td(row.kind) },
    { label: "EUR", numeric: true, cell: (row) => td(row.amount.toFixed(2)) },
    { label: "Balance", numeric: true, cell: (row) => td(row.balance.toFixed(2)) },
    { label: "For", cell: (row) => td([row.supplier, row.ref, row.note].filter(Boolean).join(" · ")) },
    { label: "By", cell: (row) => td(row.by) },
  ], data.ledger ?? [], "The wallet is empty.");
  const pay = data.payments ?? {};
  const call = (path, options) => async () => {
    try { drawWallet(await api(path, options)); } catch (e) { notice($("wl-note-out"), "bad", e.message); }
  };
  renderTable($("table-pay-batches"), [
    { label: "Batch", numeric: true, cell: (row) => td(`#${row.batch_id}`) },
    { label: "State", cell: (row) => td(row.state) },
    { label: "", cell: (row) => {
      const box = el("span");
      if (row.state !== "executed") {
        box.append(el("a", { className: "mini", href: `/api/payments/batch/${row.batch_id}.xml`, textContent: "bank file (SEPA)" }), " ");
      }
      if (row.state === "exported") {
        const done = el("button", { className: "mini", textContent: "the bank took it" });
        done.addEventListener("click", call(`/api/payments/batch/${row.batch_id}/executed`, { method: "POST" }));
        box.append(done);
      }
      return box;
    } },
  ], pay.batches ?? [], "No payment instructed yet.");
  renderTable($("table-payments"), [
    { label: "Bill", numeric: true, cell: (row) => td(`#${row.bill_id}`) },
    { label: "Supplier", cell: (row) => td(row.supplier) },
    { label: "EUR", numeric: true, cell: (row) => td(row.amount.toFixed(2)) },
    { label: "Batch", numeric: true, cell: (row) => td(`#${row.batch_id}`) },
    { label: "State", cell: (row) => td(row.state) },
    { label: "", cell: (row) => {
      if (row.state !== "instructed") return td("");
      const stop = el("button", { className: "mini", textContent: "cancel" });
      stop.addEventListener("click", call(`/api/payments/${row.payment_id}/cancel`, { method: "POST" }));
      return stop;
    } },
  ], pay.payments ?? [], "");
  renderTable($("table-banks"), [
    { label: "Supplier", cell: (row) => td(row.supplier) },
    { label: "IBAN", cell: (row) => td(row.iban) },
    { label: "BIC", cell: (row) => td(row.bic) },
  ], pay.banks ?? [], "No bank details yet: Plutus pays no bill without them.");
}

async function loadWallet() { try { drawWallet(await api("/api/wallet")); } catch {} }

async function walletAsk(path, body, said) {
  try {
    drawWallet(await api(path, postJson(body)));
    notice($("wl-note-out"), "ok", said);
  } catch (e) { notice($("wl-note-out"), "bad", e.message); loadWallet(); }
}
$("wl-in").addEventListener("click", () => walletAsk("/api/wallet/deposit", { amount: Number($("wl-amount").value), note: $("wl-note").value }, "Put in."));
$("wl-out").addEventListener("click", () => walletAsk("/api/wallet/withdraw", { amount: Number($("wl-amount").value), note: $("wl-note").value }, "Taken out."));
$("pl-set").addEventListener("click", () => walletAsk("/api/plutus/mode", { mode: $("pl-mode").value }, "Set."));
$("pl-run").addEventListener("click", async () => {
  try {
    const done = await api("/api/plutus/run", { method: "POST" });
    drawWallet(done);
    notice($("wl-note-out"), "ok", done.skipped ? `Nothing run: ${done.skipped}.`
      : `sent ${done.sent}, approved ${done.approved}, placed ${done.placed}, paid ${done.paid}.`);
  } catch (e) { notice($("wl-note-out"), "bad", e.message); }
});
$("py-save").addEventListener("click", async () => {
  try { drawWallet(await api("/api/payer", putJson({ name: $("py-name").value, iban: $("py-iban").value, bic: $("py-bic").value })));
        notice($("wl-note-out"), "ok", "Saved."); } catch (e) { notice($("wl-note-out"), "bad", e.message); }
});
$("bk-save").addEventListener("click", async () => {
  try { drawWallet(await api("/api/bank", putJson({ supplier: $("bk-supplier").value, iban: $("bk-iban").value, bic: $("bk-bic").value })));
        notice($("wl-note-out"), "ok", "Saved."); } catch (e) { notice($("wl-note-out"), "bad", e.message); }
});

// ---- calendar, supplier terms, exchange rates, the photo archive ---------

function drawCalendar(data) {
  renderTable($("table-terms"), [
    { label: "Supplier", cell: (row) => td(row.supplier) },
    { label: "Cut-off", cell: (row) => td(row.cutoff) },
    { label: "Shut on", cell: (row) => td(row.closed_days) },
    { label: "Pays in", cell: (row) => td(row.payment_days) },
    { label: "Currency", cell: (row) => td(row.currency) },
  ], data.terms ?? [], "No supplier terms yet.");
  renderTable($("table-closed"), [
    { label: "Day", cell: (row) => td(row.day) },
    { label: "Why", cell: (row) => td(row.label) },
    { label: "", cell: (row) => {
      if (!row.own) return td("public holiday");
      const open = el("button", { className: "mini", textContent: "open it again" });
      open.addEventListener("click", async () => {
        try { drawCalendar(await api(`/api/calendar/${row.day}`, { method: "DELETE" })); }
        catch (e) { notice($("calendar-note"), "bad", e.message); }
      });
      return open;
    } },
  ], data.days ?? [], "");
}

async function calendarAsk(path, options, said) {
  try {
    const data = await api(path, options);
    drawCalendar(data);
    notice($("calendar-note"), "ok", said ? said(data) : "Saved.");
  } catch (e) {
    notice($("calendar-note"), "bad", e.message);
  }
}

async function loadCalendar() {
  try { drawCalendar(await api("/api/calendar")); } catch {}
  try {
    const a = await api("/api/archive");
    $("archive-summary").textContent = a.pages
      ? `${a.pages} page(s) kept as packed arrays, ${(a.bytes / 1e6).toFixed(2)} MB where the photos were `
        + `${(a.from_bytes / 1e6).toFixed(1)} MB. Kept ${a.years} years.`
      : `Nothing archived yet. A photo older than the retention period is packed into a small `
        + `black-and-white array and the file is deleted; it is kept ${a.years} years.`;
  } catch {}
}

$("tm-go").addEventListener("click", () => calendarAsk("/api/terms", putJson({
  supplier: $("tm-supplier").value, cutoff: $("tm-cutoff").value || null,
  closed_days: $("tm-closed").value || null, payment_days: Number($("tm-pay").value) || null,
  currency: $("tm-cur").value || null }), () => "Saved.").then(loadCalendar));
$("cd-go").addEventListener("click", () => $("cd-day").value && calendarAsk("/api/calendar/close",
  postJson({ day: $("cd-day").value, label: $("cd-label").value || null })));
$("fx-go").addEventListener("click", () => calendarAsk("/api/rates", postJson({
  day: new Date().toISOString().slice(0, 10), currency: $("fx-cur").value,
  per_eur: Number($("fx-rate").value) })));
$("fx-fetch").addEventListener("click", () => calendarAsk("/api/rates/fetch", { method: "POST" },
  (d) => `${d.stored} rate(s) for ${d.day}.`));
$("archive-run").addEventListener("click", async () => {
  $("archive-run").disabled = true;
  try {
    const done = await api("/api/archive/run", { method: "POST" });
    notice($("archive-note"), "ok", `${done.pages} page(s) packed, ${(done.before / 1e6).toFixed(1)} MB `
      + `became ${(done.after / 1e6).toFixed(2)} MB.`);
    loadCalendar();
  } catch (e) {
    notice($("archive-note"), "bad", e.message);
  } finally {
    $("archive-run").disabled = false;
  }
});

// ---- pack sizes ---------------------------------------------------------------

function drawPacks(data) {
  renderTable($("table-packs"), [
    { label: "Article", cell: (row) => td(row.article) },
    { label: "Supplier", cell: (row) => td(row.supplier || "anyone") },
    { label: "Pack", cell: (row) => td(`${row.pack_label || "pack"} of ${showNum(row.pack_qty)} ${row.base_unit}`) },
    { label: "Min packs", numeric: true, cell: (row) => td(row.moq_packs) },
    { label: "", cell: (row) => {
      const gone = el("button", { className: "mini", textContent: "remove" });
      gone.addEventListener("click", async () => {
        try {
          drawPacks(await api(`/api/units?article=${encodeURIComponent(row.article)}&supplier=${encodeURIComponent(row.supplier)}`,
                              { method: "DELETE" }));
        } catch (e) { notice($("pack-note"), "bad", e.message); }
      });
      return gone;
    } },
  ], data.packs ?? [], "No pack size yet.");
}

async function loadPacks() { try { drawPacks(await api("/api/units")); } catch {} }

$("pk-go").addEventListener("click", async () => {
  try {
    drawPacks(await api("/api/units", putJson({
      article: $("pk-article").value, supplier: $("pk-supplier").value, pack_qty: Number($("pk-qty").value),
      base_unit: $("pk-unit").value, pack_label: $("pk-label").value, moq_packs: Number($("pk-moq").value) || 1 })));
    notice($("pack-note"), "ok", "Saved.");
  } catch (e) {
    notice($("pack-note"), "bad", e.message);
  }
});

async function loadComing() {
  try {
    const data = await api("/api/planned-orders");
    const c = data.counts ?? {};
    $("coming-counts").replaceChildren(statTile("to place", String(c["to place"] ?? 0)),
      statTile("waiting 15 days", String(c.waiting ?? 0)), statTile("sent", String(c.sent ?? 0)),
      statTile("confirmed", String(c.confirmed ?? 0)));
    renderTable($("table-coming"), [
      { label: "Article", cell: (row) => td(row.article) },
      { label: "Qty", numeric: true, cell: (row) => td(row.packs ? `${row.packs} ${row.pack_label} (${showNum(row.quantity)})` : showNum(row.quantity)) },
      { label: "Supplier", cell: (row) => td(row.supplier) },
      { label: "Planned by", cell: (row) => td(row.source) },
      { label: "Order on", cell: (row) => td(row.order_on) },
      { label: "Expected", cell: (row) => td(row.expected_on) },
      { label: "Status", cell: (row) => td(row.status) },
    ], data.rows ?? [], "No order is planned or on its way.");
  } catch {}
}
