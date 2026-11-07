"use strict";

// ---- Bills: what the deliveries end up costing -----------------------------

let billMonths = [];
let billDraft = null;

const BILL_FIELDS = ["supplier", "number", "bill_date", "due_date", "currency", "net", "tax",
                     "total", "site", "order_ref", "note"];

function money(value, currency = "EUR") {
  if (value == null || isNaN(value)) return "—";
  return `${Number(value).toLocaleString(undefined, { minimumFractionDigits: 2,
                                                     maximumFractionDigits: 2 })} ${currency}`;
}

function openBillReview(bill, source) {
  billDraft = source ?? {};
  for (const name of BILL_FIELDS) {
    const box = $(`b-${name}`);
    const value = bill?.[name];
    box.value = value == null ? (name === "currency" ? "EUR" : "") : String(value);
  }
  $("bill-review").hidden = false;
  $("bill-review").scrollIntoView({ block: "start" });
}

function closeBillReview() {
  $("bill-review").hidden = true;
  billDraft = null;
  notice($("bill-review-note"), "", "");
}

function billFromForm() {
  const bill = {};
  for (const name of BILL_FIELDS) bill[name] = $(`b-${name}`).value.trim() || null;
  return bill;
}

async function readQueuedBill(item) {
  spinner($("queue-note"), `Reading ${item.name} as a bill...`);
  try {
    const answer = await api(`/api/intake/${item.intake_id}/bill`, postJson({}));
    $("queue-note").hidden = true;
    openTab("bills");
    openBillReview(answer.bill, { sha256: answer.sha256, intake_id: answer.intake_id,
                                  sender: answer.sender });
  } catch (e) {
    notice($("queue-note"), "bad", e.message);
  }
}

$("bill-save").addEventListener("click", async () => {
  const button = $("bill-save");
  button.disabled = true;
  try {
    const answer = await api("/api/bills", postJson({ bill: billFromForm(), ...billDraft }));
    closeBillReview();
    say(`Bill #${answer.bill_id} saved`);
    await loadBills();
    await loadQueue();
  } catch (e) {
    notice($("bill-review-note"), "bad", e.message);
  } finally {
    button.disabled = false;
  }
});

$("bill-discard").addEventListener("click", closeBillReview);

function renderBillMonths(summary) {
  $("bill-totals").replaceChildren(...[
    ["This month", money(summary.this_month?.total ?? 0)],
    ["Still to pay", money(summary.owed)],
    ["Late", money(summary.late)],
    ["Month on average", money(summary.per_month)],
    ["Suppliers", String(summary.suppliers)],
  ].map(([name, value]) => el("div", { className: "stat" }, [
    el("span", { className: "stat-name", textContent: name }),
    el("strong", { textContent: value }),
  ])));

  renderTable($("table-bill-months"), [
    { label: "Month", cell: (row) => el("strong", { textContent: row.month }) },
    { label: "Bills", numeric: true, cell: (row) => showNum(row.bills) },
    { label: "Net", numeric: true, cell: (row) => showNum(row.net) },
    { label: "Total", numeric: true, cell: (row) => money(row.total) },
    { label: "Still to pay", numeric: true, cell: (row) => money(row.owed) },
  ], billMonths, "No bill stored yet.");
}

async function loadBills() {
  let data;
  try {
    data = await api("/api/bills");
  } catch (e) {
    notice($("global-notice"), "bad", e.message);
    return;
  }
  billMonths = data.months ?? [];
  const summary = data.summary ?? {};
  $("tree-bills").textContent = `(${summary.bills ?? 0})`;
  renderBillMonths(summary);
}

async function loadMatch() {
  let data;
  try {
    data = await api("/api/match");
  } catch {
    return;
  }
  const found = data.bills ?? {};
  const unbilled = found.unbilled ?? [];
  // Billed for more than came: a bill with nothing delivered behind it, or over the deliveries.
  const unpaid = (found.bills ?? []).filter((row) => ["nothing delivered", "over"].includes(row.verdict));

  renderTable($("table-unbilled"), [
    { label: "Supplier", cell: (row) => document.createTextNode(row.supplier) },
    { label: "Deliveries", numeric: true, cell: (row) => document.createTextNode(String(row.deliveries)) },
    { label: "Since", cell: (row) => document.createTextNode(row.since) },
    { label: "Days", numeric: true, cell: (row) => document.createTextNode(String(row.days)) },
    { label: "Worth", numeric: true, cell: (row) => document.createTextNode(money(row.worth)) },
  ], unbilled, "Every delivery has been billed, or is recent.");

  renderTable($("table-match"), [
    { label: "Supplier", cell: (row) => document.createTextNode(row.supplier) },
    { label: "Bill", cell: (row) => document.createTextNode(row.number ?? `#${row.bill_id}`) },
    { label: "Date", cell: (row) => document.createTextNode(row.bill_date) },
    { label: "Billed", numeric: true, cell: (row) => document.createTextNode(
        row.billed == null ? "—" : money(row.billed)) },
    { label: "Delivered", numeric: true, cell: (row) => document.createTextNode(
        row.deliveries ? money(row.expected) : "—") },
    { label: "Gap", cell: (row) => el("span", { className: "bad", textContent:
        row.verdict === "nothing delivered" ? "nothing delivered" : `over by ${money(row.gap)}` }) },
  ], unpaid, "Every bill is covered by a delivery.");
}

// One list open at a time: the two buttons on the Bills card develop their own table.
function wireMatchButtons() {
  const pairs = [["match-unbilled-btn", "match-unbilled"], ["match-unpaid-btn", "match-unpaid"]];
  for (const [button, box] of pairs) {
    $(button).addEventListener("click", () => {
      const open = $(box).hidden;
      for (const [otherButton, otherBox] of pairs) {
        const on = open && otherBox === box;
        $(otherBox).hidden = !on;
        $(otherButton).setAttribute("aria-expanded", String(on));
      }
    });
  }
}
wireMatchButtons();
