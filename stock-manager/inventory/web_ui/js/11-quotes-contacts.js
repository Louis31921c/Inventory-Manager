"use strict";

// ---- quote requests -------------------------------------------------------

let quoteRows = [];
let quoteOpen = null;

function renderQuotes() {
  renderTable($("table-quotes"), [
    { label: "#", numeric: true, cell: (row) => {
      const open = el("button", { className: "mini", type: "button",
                                  textContent: `#${row.quote_id}` });
      open.addEventListener("click", () => openQuote(row.quote_id));
      return open;
    } },
    { label: "Supplier", cell: (row) => document.createTextNode(row.supplier ?? "—") },
    { label: "Lines", numeric: true, cell: (row) => document.createTextNode(showNum(row.lines)) },
    { label: "Site", cell: (row) => document.createTextNode(row.site ?? "—") },
    { label: "Work item", cell: (row) => document.createTextNode(row.work_item ?? "—") },
    { label: "Needed by", cell: (row) => document.createTextNode(showDate(row.needed_by)) },
    { label: "State", cell: (row) => el("span", { className: "pill", textContent: row.state }) },
    { label: "Asked", cell: (row) => document.createTextNode(showTime(row.asked_at)) },
  ], quoteRows, "No quote asked for yet.");
}

async function openQuote(quoteId) {
  try {
    const quote = await api(`/api/quotes/${quoteId}`);
    quoteOpen = quote;
    $("quote-open").hidden = false;
    $("quote-text").textContent = quote.text;
    say(`Quote #${quoteId}`);
  } catch (e) {
    notice($("quote-note"), "bad", e.message);
  }
}

async function setQuoteState(state) {
  if (!quoteOpen) return;
  try {
    await api(`/api/quotes/${quoteOpen.quote_id}`, patchJson({ state }));
    notice($("quote-note"), "ok", `Quote #${quoteOpen.quote_id} is ${state}.`);
    await loadQuotes();
    await openQuote(quoteOpen.quote_id);
  } catch (e) {
    notice($("quote-note"), "bad", e.message);
  }
}

async function loadQuotes() {
  try {
    const data = await api("/api/quotes");
    quoteRows = data.quotes ?? [];
    $("tree-quotes").textContent = `(${quoteRows.length})`;
    $("quote-summary").textContent =
      `${data.summary?.quotes ?? 0} request(s), ${data.summary?.open ?? 0} waiting on a price`;
    renderQuotes();
  } catch {
  }
}

$("quote-save").addEventListener("click", async () => {
  const body = {
    supplier: $("quote-supplier").value.trim() || null,
    site: $("quote-site").value.trim() || null,
    work_item: $("quote-work").value.trim() || null,
    needed_by: $("quote-needed").value || null,
    text: $("quote-items").value,
  };
  if (!body.text.trim()) {
    notice($("quote-note"), "warn", "Say what has to be priced.");
    return;
  }
  $("quote-save").disabled = true;
  try {
    const saved = await api("/api/quotes", postJson(body));
    $("quote-items").value = "";
    notice($("quote-note"), "ok", `Quote #${saved.quote_id} saved.`);
    await loadQuotes();
    await openQuote(saved.quote_id);
  } catch (e) {
    notice($("quote-note"), "bad", e.message);
  } finally {
    $("quote-save").disabled = false;
  }
});

$("quote-sent").addEventListener("click", () => setQuoteState("sent"));
$("quote-answered").addEventListener("click", () => setQuoteState("answered"));
$("quote-closed").addEventListener("click", () => setQuoteState("closed"));
$("quote-drop").addEventListener("click", async () => {
  if (!quoteOpen) return;
  if (!confirm(`Delete quote #${quoteOpen.quote_id}?`)) return;
  try {
    await api(`/api/quotes/${quoteOpen.quote_id}`, { method: "DELETE" });
    quoteOpen = null;
    $("quote-open").hidden = true;
    await loadQuotes();
  } catch (e) {
    notice($("quote-note"), "bad", e.message);
  }
});

// ---- who an order goes to -------------------------------------------------

let contactRows = [];
let incoterms = [];

function contactChoices(select, chosen) {
  select.replaceChildren(
    el("option", { value: "", textContent: "no contact" }),
    ...contactRows.map((row) => el("option", {
      value: String(row.contact_id),
      textContent: row.person ? `${row.company} — ${row.person}` : row.company,
    })));
  select.value = chosen == null ? "" : String(chosen);
}

function incotermChoices(select, chosen) {
  select.replaceChildren(
    el("option", { value: "", textContent: "none" }),
    ...incoterms.map((term) => el("option", { value: term.id, textContent: term.label })));
  select.value = chosen ?? "";
}

async function loadContacts() {
  try {
    const data = await api("/api/contacts");
    contactRows = data.contacts ?? [];
    incoterms = data.incoterms ?? [];
    // Said only when there is nothing to ask: the path of a file that is there is noise.
    $("contact-file").textContent = data.file
      ? ""
      : "No supplier-contacts.csv found on the disk.";
    renderOrders();
  } catch {
  }
}

// The card asks the file on the disk, the way a query asks a table: a name, a
// company, and the lines that answer to both.
async function askContactFile(everything = false) {
  const name = $("contact-person").value.trim();
  const company = $("contact-company").value.trim();
  // Both fields empty is not a question: nothing is asked and nothing is said.
  if (!everything && !name && !company) return;
  const button = $(everything ? "contact-all" : "contact-find");
  button.disabled = true;
  try {
    const asked = new URLSearchParams();
    if (name) asked.set("name", name);
    if (company) asked.set("company", company);
    if (everything) asked.set("all", "true");
    const answer = await api(`/api/contacts/file?${asked}`);
    renderTable($("table-contacts"),
                [...answer.columns.map((field) => ({
                  label: field === "email" ? "e-mail" : field,
                  cell: (row) => document.createTextNode(row[field] || "—"),
                })), { label: "Lead time", numeric: true, cell: (row) => document.createTextNode(
                  row.lead_days == null ? (row.lead_n ? `${row.lead_n} delivery(ies), too few` : "—")
                    : `${showNum(row.lead_days)} days (${row.lead_n})`) }],
                answer.rows, "No line answers to that.");
    const held = answer.found > answer.rows.length
      ? `${answer.found} line(s) found, the first ${answer.rows.length} are shown.`
      : `${answer.found} line(s) found.`;
    notice($("contact-note"), answer.found ? "ok" : "warn", held);
  } catch (e) {
    notice($("contact-note"), "bad", e.message);
  } finally {
    button.disabled = false;
  }
}

$("contact-find").addEventListener("click", () => askContactFile());
$("contact-all").addEventListener("click", () => askContactFile(true));
for (const id of ["contact-person", "contact-company"]) {
  $(id).addEventListener("keydown", (event) => {
    if (event.key === "Enter") askContactFile();
  });
}
