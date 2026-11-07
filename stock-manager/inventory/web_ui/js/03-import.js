"use strict";

function pickPhoto(input, nameLabel, thumbId, button, onFile) {
  input.addEventListener("change", (event) => {
    const file = event.target.files?.[0] ?? null;
    onFile(file);
    button.disabled = !file;
    nameLabel.textContent = file ? file.name : "Take a picture";
    const thumb = $(thumbId);
    if (file) {
      thumb.src = URL.createObjectURL(file);
      thumb.hidden = false;
    } else {
      thumb.hidden = true;
    }
  });
}

pickPhoto($("photo"), $("photo-name"), "thumb", $("read-btn"), (file) => { draftFile = file; });
pickPhoto($("photo-list"), $("photo-list-name"), "thumb-list", $("read-list"), (file) => { listFile = file; });

function spinner(target, text) {
  target.hidden = false;
  target.replaceChildren(el("p", { className: "notice" }, [
    el("span", { className: "spin", textContent: "◐" }), ` ${text}`,
  ]));
}

let reading = null;
$("cancel-read").addEventListener("click", () => reading?.abort());

$("read-btn").addEventListener("click", async () => {
  if (!draftFile) return;
  const status = $("read-status");
  $("read-btn").disabled = true;
  $("cancel-read").hidden = false;
  spinner(status, "Reading the note... (10 to 40 seconds)");

  reading = new AbortController();
  const form = new FormData();
  form.append("photo", draftFile);
  try {
    const result = await api("/api/extract", { method: "POST", body: form, signal: reading.signal });
    draftSha = result.sha256;
    status.hidden = true;
    openReview(result.note);
  } catch (e) {
    if (e.name === "AbortError") status.hidden = true;
    else notice(status, "bad", e.message);
  } finally {
    reading = null;
    $("cancel-read").hidden = true;
    $("read-btn").disabled = !draftFile;
  }
});

let queue = [];
let mapping = null;
let intakeFiles = [];

$("intake-files").addEventListener("change", (event) => {
  intakeFiles = [...(event.target.files ?? [])];
  $("intake-name").textContent = intakeFiles.length
    ? `${intakeFiles.length} file(s) chosen`
    : "Open Files";
  $("intake-send").disabled = !intakeFiles.length;
});

$("intake-send").addEventListener("click", async () => {
  if (!intakeFiles.length) return;
  const button = $("intake-send");
  button.disabled = true;
  spinner($("intake-note"), "Adding to the queue...");
  const form = new FormData();
  for (const file of intakeFiles) form.append("files", file);
  try {
    const answer = await api("/api/intake", { method: "POST", body: form });
    const added = answer.added.filter((item) => !item.duplicate).length;
    const again = answer.added.length - added;
    const refused = (answer.refused ?? []).map((item) => `${item.name}: ${item.why}`);
    notice($("intake-note"), refused.length ? "warn" : "ok",
      `${added} file(s) queued`
      + (again ? `, ${again} already there` : "")
      + (refused.length ? ` — ${refused.join("; ")}` : ""));
    intakeFiles = [];
    $("intake-files").value = "";
    $("intake-name").textContent = "Choose files";
    await loadQueue();
  } catch (e) {
    notice($("intake-note"), "bad", e.message);
  } finally {
    button.disabled = !intakeFiles.length;
  }
});

$("intake-refresh").addEventListener("click", () => loadQueue());

$("mail-fetch").addEventListener("click", async () => {
  const button = $("mail-fetch");
  button.disabled = true;
  spinner($("mail-note"), "Asking the mailbox...");
  try {
    const answer = await api("/api/mail/fetch", postJson({}));
    notice($("mail-note"), "ok",
      `${answer.messages} message(s) read, ${answer.queued} attachment(s) queued`
      + (answer.duplicates ? `, ${answer.duplicates} already there` : "")
      + (answer.read ? `, ${answer.read.stored} note(s) stored, ${answer.read.left} left to you` : ""));
    await loadQueue();
  } catch (e) {
    notice($("mail-note"), "bad", e.message);
  } finally {
    button.disabled = false;
  }
});

$("mail-check").addEventListener("click", async () => {
  spinner($("mail-note"), "Knocking...");
  try {
    const answer = await api("/api/mail/check", postJson({}));
    notice($("mail-note"), answer.ok ? "ok" : "warn", answer.detail);
  } catch (e) {
    notice($("mail-note"), "bad", e.message);
  }
});

async function loadQueue() {
  if (!$("table-intake")) return;

  let data;
  try {
    data = await api("/api/intake");
  } catch (e) {
    notice($("queue-note"), "bad", e.message);
    return;
  }
  queue = data.waiting ?? [];

  renderTable($("table-intake"), [
    { label: "File", cell: (item) => {
        const box = el("span", {}, [el("strong", { textContent: item.name })]);
        const under = [item.source === "mail" ? item.sender : item.source,
                       item.subject].filter(Boolean).join(" · ");
        if (under) box.append(el("div", { className: "desig", textContent: under }));
        return box;
      } },
    { label: "Kind", cell: (item) => (item.kind === "sheet" ? "spreadsheet" : "scan") },
    { label: "Size", numeric: true, cell: (item) => `${Math.round((item.bytes ?? 0) / 1024)} kB` },
    { label: "Arrived", cell: (item) => item.arrived_at ?? "" },
    { label: "", cell: (item) => {
        const row = el("span", { className: "row tight" });
        const open = el("button", {
          className: "mini",
          textContent: item.kind === "sheet" ? "map columns" : "read",
        });
        open.addEventListener("click", () =>
          (item.kind === "sheet" ? openMapping(item) : readQueued(item)));
        row.append(open);
        if (item.kind !== "sheet") {
          const asBill = el("button", { className: "mini", textContent: "bill" });
          asBill.addEventListener("click", () => readQueuedBill(item));
          row.append(asBill);
        }
        const gone = el("button", { className: "mini", textContent: "remove" });
        gone.addEventListener("click", async () => {
          try {
            await api(`/api/intake/${item.intake_id}`, { method: "DELETE" });
            await loadQueue();
          } catch (e) {
            notice($("queue-note"), "bad", e.message);
          }
        });
        row.append(gone);
        return row;
      } },
  ], queue, "...");
}

async function readQueued(item) {
  spinner($("queue-note"), `Reading ${item.name}...`);
  try {
    const answer = await api(`/api/intake/${item.intake_id}/read`, postJson({}));
    draftSha = answer.sha256;
    draftFile = null;
    $("queue-note").hidden = true;
    openReview(answer.note);
    $("review").scrollIntoView({ block: "start" });
  } catch (e) {
    notice($("queue-note"), "bad", e.message);
  }
}

async function openMapping(item) {
  const target = $("mapping-target").value;
  try {
    mapping = await api(`/api/import/${item.intake_id}?target=${target}`);
  } catch (e) {
    notice($("queue-note"), "bad", e.message);
    return;
  }
  $("mapping").hidden = false;
  $("mapping-file").textContent = `${mapping.name} · ${mapping.rows} row(s)`;
  drawMapping();
  $("mapping").scrollIntoView({ block: "start" });
}

function drawMapping() {
  if (!mapping) return;
  renderTable($("table-mapping"), [
    { label: "Field", cell: (field) => el("strong", { textContent: field.replace("_", " ") }) },
    { label: "Column in the file", cell: (field) => {
        const pick = el("select", {});
        pick.append(el("option", { value: "", textContent: "— not in the file —" }));
        mapping.header.forEach((name, index) => {
          pick.append(el("option", { value: String(index), textContent: `${index + 1}. ${name}` }));
        });
        pick.value = mapping.mapping[field] == null ? "" : String(mapping.mapping[field]);
        pick.addEventListener("change", () => {
          if (pick.value === "") delete mapping.mapping[field];
          else mapping.mapping[field] = Number(pick.value);
        });
        return pick;
      } },
  ], mapping.fields, "");
  $("mapping-sample").textContent = (mapping.sample ?? [])
    .slice(0, 3)
    .map((row) => row.join(" | "))
    .join("\n");
}

$("mapping-target").addEventListener("change", async () => {
  if (!mapping) return;
  const item = queue.find((row) => row.intake_id === mapping.intake_id);
  if (item) await openMapping(item);
});

$("mapping-guess").addEventListener("click", async () => {
  if (!mapping) return;
  const item = queue.find((row) => row.intake_id === mapping.intake_id);
  if (item) await openMapping(item);
});

async function runImport(dry) {
  if (!mapping) return;
  spinner($("mapping-note"), dry ? "Checking..." : "Importing...");
  try {
    const answer = await api(`/api/import/${mapping.intake_id}`, postJson({
      target: $("mapping-target").value,
      mapping: mapping.mapping,
      dry_run: dry,
    }));
    const dropped = (answer.dropped ?? []).length;
    if (dry) {
      notice($("mapping-note"), "ok",
        `${answer.kept} line(s) would be imported`
        + (dropped ? `, ${dropped} skipped (${answer.dropped[0].why}, row ${answer.dropped[0].row})` : ""));
      return;
    }
    notice($("mapping-note"), "ok",
      `${answer.lines} line(s) imported`
      + (answer.note_id ? ` as note #${answer.note_id}` : "")
      + (dropped ? `, ${dropped} skipped` : ""));
    mapping = null;
    $("mapping").hidden = true;
    await loadQueue();
    await refresh();
  } catch (e) {
    notice($("mapping-note"), "bad", e.message);
  }
}

$("mapping-dry").addEventListener("click", () => runImport(true));
$("mapping-run").addEventListener("click", () => runImport(false));

function openReview(note) {
  proposed = structuredClone(note);
  draft = {
    supplier: note.supplier ?? "",
    delivery_date: note.delivery_date ?? "",
    order_date: note.order_date ?? "",
    site: note.site ?? "",
    work_item: note.work_item ?? "",
    lines: (note.lines ?? []).map((line) => ({
      article: String(line.article ?? ""),
      quantity: line.quantity == null ? null : Number(line.quantity),
      backorder: Boolean(line.backorder),
      designation: String(line.designation ?? ""),
      known: Boolean(line.known),
    })),
    warnings: note.warnings ?? [],
  };

  $("f-supplier").value = draft.supplier;
  $("f-site").value = draft.site;
  $("f-work_item").value = draft.work_item;
  $("f-delivery_date").value = draft.delivery_date;
  $("f-order_date").value = draft.order_date;
  $("warnings").replaceChildren(...draft.warnings.map((text) =>
    el("p", { className: "notice warn", textContent: `Check: ${text}` })));

  renderLines();
  checkProblems();
  $("review").hidden = false;
  $("review").scrollIntoView({ behavior: "smooth", block: "start" });
}

for (const field of ["supplier", "site", "work_item", "delivery_date", "order_date"]) {
  $(`f-${field}`).addEventListener("input", (event) => {
    if (!draft) return;
    draft[field] = event.target.value;
    checkProblems();
  });
}

function renderLines() {
  $("lines").replaceChildren(...draft.lines.map((line, index) => {
    const article = el("input", { value: line.article, "aria-label": `Article ${index + 1}` });
    article.addEventListener("input", () => {
      line.article = article.value;
      line.known = false;
      checkProblems();
    });

    const quantity = el("input", {
      className: "qty", type: "number", min: "0", step: "any",
      value: line.quantity ?? "", "aria-label": `Quantity line ${index + 1}`,
    });
    quantity.addEventListener("input", () => { line.quantity = asNumber(quantity.value); });

    const backorder = el("input", { type: "checkbox", checked: line.backorder, id: `bo-${index}` });
    backorder.addEventListener("change", () => { line.backorder = backorder.checked; });

    const remove = el("button", { className: "mini", textContent: "x", title: "Remove the line" });
    remove.addEventListener("click", () => {
      draft.lines.splice(index, 1);
      renderLines();
      checkProblems();
    });

    const row = el("div", { className: "line" }, [
      el("span", { className: "num", textContent: index + 1 }),
      article,
      el("span", { className: "qty-wrap" }, [quantity]),
      el("label", { className: "rel", htmlFor: `bo-${index}` }, [backorder, "backorder"]),
      remove,
    ]);
    if (line.designation) {
      row.append(el("span", {
        className: "desig",
        textContent: (line.known ? "known name: " : "") + line.designation,
      }));
    }
    return row;
  }));
}

$("add-line").addEventListener("click", () => {
  if (!draft) return;
  draft.lines.push({ article: "", quantity: null, backorder: false, designation: "", known: false });
  renderLines();
  checkProblems();
});

function checkProblems() {
  const found = [];
  if (!draft.supplier.trim()) found.push("supplier missing");
  if (!ISO_DATE.test(draft.delivery_date)) found.push("delivery date missing");
  if (!draft.lines.length) found.push("no article");
  draft.lines.forEach((line, index) => {
    if (!line.article.trim()) found.push(`article ${index + 1} has no name`);
  });

  notice($("review-problems"), "warn", found.length ? `To fix: ${found.join(", ")}` : "");
  $("save-btn").disabled = found.length > 0;
  return found;
}

$("discard-btn").addEventListener("click", () => {
  draft = draftFile = draftSha = null;
  $("review").hidden = true;
  $("photo").value = "";
  $("thumb").hidden = true;
  $("photo-name").textContent = "Take a picture";
  $("read-btn").disabled = true;
});

function measure(before, after, headerFields, lineFields) {
  if (!before) return null;
  let fields = 0;
  let corrected = 0;

  const compare = (a, b) => {
    fields += 1;
    const left = a === "" || a == null ? null : String(a).trim().toLowerCase();
    const right = b === "" || b == null ? null : String(b).trim().toLowerCase();
    if (left !== right) corrected += 1;
  };

  for (const field of headerFields) compare(before[field], after[field]);

  const linesBefore = before.lines ?? [];
  const linesAfter = after.lines ?? [];
  for (let i = 0; i < Math.max(linesBefore.length, linesAfter.length); i += 1) {
    if (!linesBefore[i] || !linesAfter[i]) {
      fields += lineFields.length;
      corrected += lineFields.length;
      continue;
    }
    for (const field of lineFields) compare(linesBefore[i][field], linesAfter[i][field]);
  }
  return { fields, corrected, lines: linesAfter.length };
}

$("save-btn").addEventListener("click", async () => {
  if (!draft || checkProblems().length) return;
  $("save-btn").disabled = true;
  notice($("review-problems"), "ok", "Saving...");

  const note = {
    supplier: draft.supplier.trim(),
    delivery_date: draft.delivery_date,
    order_date: ISO_DATE.test(draft.order_date) ? draft.order_date : null,
    site: draft.site.trim() || null,
    work_item: draft.work_item.trim() || null,
    lines: draft.lines.map((line) => ({
      article: line.article.trim(),
      quantity: line.quantity,
      backorder: Boolean(line.backorder),
      designation: line.designation || null,
    })),
  };
  const body = {
    sha256: draftSha,
    note,
    measurement: measure(proposed, note,
      ["supplier", "delivery_date", "order_date", "site", "work_item"],
      ["article", "quantity", "backorder"]),
  };

  try {
    const stored = await api("/api/notes", postJson(body));
    notice($("review-problems"), "ok", `Note #${stored.note_id} saved.`);
    await refresh();
    setTimeout(() => $("discard-btn").click(), 1200);
  } catch (e) {
    notice($("review-problems"), "bad", e.message);
    $("save-btn").disabled = false;
  }
});
