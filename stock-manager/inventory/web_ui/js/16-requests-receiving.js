"use strict";

// ---- site requests -----------------------------------------------------------

function drawRequests(data) {
  const open = data.requests ?? [];
  $("tree-requests").textContent = open.length ? `(${open.length})` : "";
  $("rq-summary").textContent = open.length
    ? `${open.length} open request(s), ${open.filter((one) => one.late).length} late.`
    : "No open request.";
  $("rq-list").replaceChildren(...open.map((one) => {
    const title = el("div", { className: "sheet-title" }, [
      el("strong", { textContent: `#${one.req_id} · ${one.site} / ${one.work_item}` }),
      el("span", { className: one.late ? "bad" : "muted",
                   textContent: [one.needed_by ? `needed ${one.needed_by}` : "", one.late ? "LATE" : "",
                                 one.requester ? `by ${one.requester}` : ""].filter(Boolean).join(" · ") }),
    ]);
    const cancel = confirmButton("Cancel", () => api(`/api/requisitions/${one.req_id}/cancel`, { method: "POST" }));
    title.append(el("span", { className: "row tight", style: "margin-left:auto" }, [cancel]));
    const table = el("table");
    renderTable(table, [
      { label: "Article", cell: (line) => td(line.article) },
      { label: "Asked", numeric: true, cell: (line) => td(showNum(line.quantity)) },
      { label: "From the shelf", numeric: true, cell: (line) => td(showNum(line.reserved)) },
      { label: "To order", numeric: true, cell: (line) => td(showNum(line.to_order)) },
      { label: "State", cell: (line) => td(line.says) },
    ], one.lines, "");
    return el("div", { className: "sheet" }, [title, el("div", { className: "scroll-x short-box" }, [table])]);
  }));
}

async function loadRequests() {
  try {
    drawRequests(await api("/api/requisitions"));
  } catch {
  }
}

$("rq-send").addEventListener("click", async () => {
  $("rq-send").disabled = true;
  try {
    const done = await api("/api/requisitions", postJson({
      site: $("rq-site").value, work_item: $("rq-work").value,
      needed_by: $("rq-needed").value || null, text: $("rq-items").value }));
    $("rq-items").value = "";
    notice($("rq-note"), "ok", `Request #${done.request.req_id}: ${done.request.summary}.`);
    drawRequests(done);
  } catch (e) {
    notice($("rq-note"), "bad", e.message);
  } finally {
    $("rq-send").disabled = false;
  }
});
setInterval(() => { if (!document.hidden && !$("panel-requests").hidden) loadRequests(); }, 60000);

// ---- connections: order webhooks and the bank drop ---------------------------------

function drawConnections(data) {
  renderTable($("table-channels"), [
    { label: "Supplier", cell: (row) => td(row.supplier) },
    { label: "Address", cell: (row) => td(row.url) },
    { label: "Token", cell: (row) => td(row.has_token ? "set" : "missing") },
    { label: "Last order posted", cell: (row) => td(row.last) },
    { label: "", cell: (row) => {
      const test = el("button", { className: "mini", type: "button", textContent: "test" });
      test.addEventListener("click", async () => {
        try {
          const done = await api(`/api/channels/${encodeURIComponent(row.supplier)}/test`, { method: "POST" });
          notice($("ch-note"), "ok", `${row.supplier} answered ${done.status} to a test (nothing was ordered).`);
        } catch (e) { notice($("ch-note"), "bad", e.message); }
      });
      const gone = el("button", { className: "mini", type: "button", textContent: "remove" });
      gone.addEventListener("click", async () => {
        try {
          drawConnections(await api(`/api/channels/${encodeURIComponent(row.supplier)}`, { method: "DELETE" }));
          notice($("ch-note"), "ok", `${row.supplier} is mailed again.`);
        } catch (e) { notice($("ch-note"), "bad", e.message); }
      });
      return el("span", { className: "row tight" }, [test, gone]);
    } },
  ], data.channels ?? [], "Every supplier is mailed.");
  const bank = data.bank ?? {};
  for (const [id, key] of [["bk-host", "host"], ["bk-port", "port"], ["bk-user", "user"], ["bk-key", "key"],
                           ["bk-dir", "dir"], ["bk-status", "status_dir"]]) {
    if (document.activeElement !== $(id)) $(id).value = bank[key] ?? "";
  }
}

async function loadConnections() {
  try {
    drawConnections(await api("/api/connections"));
  } catch {
  }
}

$("ch-save").addEventListener("click", async () => {
  try {
    drawConnections(await api("/api/channels", putJson({
      supplier: $("ch-supplier").value, url: $("ch-url").value, token: $("ch-token").value })));
    $("ch-token").value = "";
    notice($("ch-note"), "ok", `Saved. ${$("ch-supplier").value.toUpperCase()} now gets its orders posted.`);
  } catch (e) {
    notice($("ch-note"), "bad", e.message);
  }
});

$("bk-save").addEventListener("click", async () => {
  try {
    drawConnections(await api("/api/bank-drop", putJson({
      host: $("bk-host").value, port: $("bk-port").value, user: $("bk-user").value, key: $("bk-key").value,
      dir: $("bk-dir").value, status_dir: $("bk-status").value })));
    notice($("bk-note"), "ok", "Saved.");
  } catch (e) {
    notice($("bk-note"), "bad", e.message);
  }
});

$("bk-test").addEventListener("click", async () => {
  $("bk-test").disabled = true;
  notice($("bk-note"), "", "Logging in...");
  try {
    await api("/api/bank-drop/test", { method: "POST" });
    notice($("bk-note"), "ok", "The bank folder answered.");
  } catch (e) {
    notice($("bk-note"), "bad", e.message);
  } finally {
    $("bk-test").disabled = false;
  }
});

// ---- receiving and counting --------------------------------------

function drawReceiving(data) {
  const lines = data.to_put_away ?? [];
  const sum = data.summary ?? {};
  $("rv-summary").textContent = lines.length
    ? `${sum.waiting} line(s) to put away, ${sum.unexpected} not as ordered, ${sum.suggested} with a learned place.`
    : "Everything that arrived has been put away.";
  $("rv-confirm").disabled = !sum.suggested;
  renderTable($("table-putaway"), [
    { label: "Article", cell: (row) => td(row.article) },
    { label: "Qty", numeric: true, cell: (row) => td(showNum(row.quantity)) },
    { label: "Site", cell: (row) => td(row.site) },
    { label: "From", cell: (row) => td(row.supplier) },
    { label: "Against the orders", cell: (row) => {
      const word = el("span", { className: row.check_word === "as ordered" ? "ok" : "bad", textContent: row.check_word });
      word.title = row.check_detail ?? "";
      return word;
    } },
    { label: "Place", cell: (row) => {
      const place = el("input", { className: "cell", size: 14, value: row.suggested ?? "" });
      const go = el("button", { className: "mini", type: "button", textContent: "put away" });
      go.addEventListener("click", async () => {
        try {
          drawReceiving(await api(`/api/receiving/${row.putaway_id}/put-away`, postJson({ location: place.value })));
          notice($("rv-note"), "", "");
        } catch (e) { notice($("rv-note"), "bad", e.message); }
      });
      return el("span", { className: "row tight" }, [place, go]);
    } },
  ], lines, "Nothing to put away.");
}

async function loadReceiving() { try { drawReceiving(await api("/api/receiving")); } catch {} }

$("rv-confirm").addEventListener("click", async () => {
  try { drawReceiving(await api("/api/receiving/confirm-suggested", { method: "POST" })); }
  catch (e) { notice($("rv-note"), "bad", e.message); }
});

function countAnswer(done) {
  const cause = done.causes_said ? ` ${done.causes_said}.` : "";
  return {
    posted: ["ok", `${done.article}: counted ${showNum(done.counted)}, the books are corrected (${done.variance >= 0 ? "+" : ""}${showNum(done.variance)}).`],
    confirmed: ["ok", `${done.article}: the two counts agree, the books now say ${showNum(done.counted)} (${done.variance >= 0 ? "+" : ""}${showNum(done.variance)}).${cause}`],
    recount: ["warn", `${done.article}: that is far from the books. Count it again; nothing was changed.${cause}`],
    disputed: ["bad", `${done.article}: the two counts disagree, so a person settles it below.`],
  }[done.state];
}

function drawCounting(data) {
  const say = (box, text) => notice($("ct-note"), box, text);
  const ask = (article, input, button) => async () => {
    try {
      const done = await api("/api/counting/count", postJson({ article, quantity: Number(input.value) }));
      const [kind, text] = countAnswer(done.result);
      say(kind, text);
      drawCounting(done);
    } catch (e) { say("bad", e.message); }
  };
  const counter = (row) => {
    const input = el("input", { className: "cell", size: 7, inputMode: "decimal", placeholder: "counted" });
    const go = el("button", { className: "mini", type: "button", textContent: "count" });
    go.addEventListener("click", ask(row.article, input, go));
    return el("span", { className: "row tight" }, [input, go]);
  };
  renderTable($("table-counts"), [
    { label: "Article", cell: (row) => td(row.article) },
    { label: "Where", cell: (row) => td(row.where) },
    { label: "", cell: counter },
  ], data.tasks ?? [], "Nothing to count today.");
  renderTable($("table-recounts"), [
    { label: "Article", cell: (row) => td(row.article) },
    { label: "Asked", cell: (row) => td(row.asked_at) },
    { label: "", cell: counter },
  ], data.recounts ?? [], "No second count is waiting.");
  renderTable($("table-disputed"), [
    { label: "Article", cell: (row) => td(row.article) },
    { label: "Counts", cell: (row) => td(`${showNum(row.first_count)} and ${showNum(row.second_count)}`) },
    { label: "Likely cause", cell: (row) => td(row.explanation) },
    { label: "", cell: (row) => {
      const input = el("input", { className: "cell", size: 7, inputMode: "decimal", placeholder: "the right number" });
      const go = el("button", { className: "mini", type: "button", textContent: "settle" });
      go.addEventListener("click", async () => {
        try { drawCounting(await api(`/api/counting/${row.check_id}/settle`, postJson({ quantity: Number(input.value) }))); }
        catch (e) { say("bad", e.message); }
      });
      return el("span", { className: "row tight" }, [input, go]);
    } },
  ], data.disputed ?? [], "None.");
}

async function loadCounting() { try { drawCounting(await api("/api/counting")); } catch {} }

$("ct-go").addEventListener("click", async () => {
  try {
    const done = await api("/api/counting/count", postJson({ article: $("ct-article").value, quantity: Number($("ct-qty").value) }));
    const [kind, text] = countAnswer(done.result);
    notice($("ct-note"), kind, text);
    $("ct-article").value = "";
    $("ct-qty").value = "";
    drawCounting(done);
  } catch (e) { notice($("ct-note"), "bad", e.message); }
});

setInterval(() => {
  if (!document.hidden && !$("panel-inventory").hidden) { loadReceiving(); loadCounting(); }
}, 60000);
