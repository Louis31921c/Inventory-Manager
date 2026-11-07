"use strict";

function renderTable(table, columns, data, empty) {
  table.replaceChildren();
  if (!data.length) {
    table.append(el("tbody", {}, [el("tr", {}, [el("td", { className: "muted", textContent: empty })])]));
    return;
  }
  table.append(el("thead", {}, [
    el("tr", {}, columns.map((col) => el("th", { textContent: col.label }))),
  ]));
  table.append(el("tbody", {}, data.map((item) =>
    el("tr", {}, columns.map((col) => {
      const cell = el("td", { className: col.numeric ? "num" : "" });
      cell.append(col.cell(item));
      return cell;
    })))));
}

let treePick = null;

function filtered() {
  const search = upper($("q-search").value);
  const site = $("q-site").value;

  return rows.filter((row) => {
    if (treePick) {
      if ((row.site ?? "") !== treePick.site) return false;
      if (treePick.work !== null && (row.work_item ?? "") !== treePick.work) return false;
    }
    if (site && (row.site ?? "") !== site) return false;
    if (!search) return true;
    const haystack = upper(`${row.article} ${row.designation} ${row.supplier} ${row.site ?? ""}`);
    return haystack.includes(search);
  });
}

function workItemCell(row) {
  const input = el("input", {
    className: "cell", value: row.work_item ?? "", placeholder: "—",
    "aria-label": `Work item of ${row.article}`,
  });
  let stored = row.work_item ?? "";

  const save = async () => {
    const value = input.value.trim();
    if (value === stored) return;
    try {
      await api(`/api/inventory/${row.note_id}/${row.line_no}`, patchJson({ work_item: value }));
      stored = value;
      row.work_item = value || null;
      input.classList.add("saved");
      setTimeout(() => input.classList.remove("saved"), 900);
    } catch (e) {
      input.value = stored;
      notice($("global-notice"), "bad", e.message);
    }
  };

  input.addEventListener("change", save);
  input.addEventListener("blur", save);
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      input.blur();
    }
    if (event.key === "Escape") {
      input.value = stored;
      input.blur();
    }
  });
  return input;
}

function statTile(name, value) {
  return el("span", { className: "stat" }, [
    el("span", { className: "stat-name", textContent: name }),
    el("strong", { textContent: value }),
  ]);
}

function renderDeliveries() {
  const data = filtered();
  const units = data.reduce((sum, row) => sum + (row.quantity ?? 0), 0);
  const backorders = data.filter((row) => row.backorder).length;
  const tiles = [statTile("lines", showNum(data.length)),
                 statTile("units", showNum(units))];
  if (backorders) tiles.push(statTile("on backorder", showNum(backorders)));
  if (treePick) {
    tiles.push(statTile("picked", `${treePick.site || "no site"} · `
      + (treePick.work === null ? "all work items" : treePick.work || "no work item")));
  }
  $("q-stats").replaceChildren(...tiles);

  renderTable($("table-inventory"), [
    {
      label: "Article",
      cell: (row) => {
        const box = el("span", {}, [el("strong", { textContent: row.article })]);
        if (row.backorder) {
          box.append(" ", el("span", { className: "pill backorder", textContent: "backorder" }));
        }
        if (row.designation) {
          box.append(el("div", { className: "desig muted", textContent: row.designation }));
        }
        return box;
      },
    },
    { label: "Qty", numeric: true, cell: (row) => showNum(row.quantity) },
    { label: "Supplier", cell: (row) => row.supplier },
    {
      label: "Delivered", numeric: true,
      cell: (row) => showDate(row.delivery_date) + (row.weekday ? ` · ${row.weekday.slice(0, 3)}` : ""),
    },
    { label: "Site", cell: (row) => row.site ?? "—" },
    { label: "Work item", cell: workItemCell },
  ], data, "No line matches this filter.");

  renderTable($("table-notes"), [
    { label: "#", numeric: true, cell: (note) => String(note.id) },
    { label: "Note", cell: (note) => note.supplier },
    { label: "Delivered", numeric: true, cell: (note) => showDate(note.delivery_date) },
    { label: "Site", cell: (note) => note.site ?? "—" },
    { label: "Lines", numeric: true, cell: (note) => String((note.lines ?? []).length) },
    { label: "Entered by", cell: (note) => note.confirmed_by ?? "—" },
    {
      label: "Photo",
      cell: (note) => (note.photo
        ? el("a", { href: `/api/photo/${note.id}`, target: "_blank" }, [
          el("img", {
            className: "thumb", src: `/api/photo/${note.id}`, alt: "", loading: "lazy",
            style: "max-height:44px",
          })])
        : "—"),
    },
    { label: "", cell: (note) => confirmButton("Delete", () => api(`/api/notes/${note.id}`, { method: "DELETE" })) },
  ], notes, "No note stored.");

  refreshSiteFilter();
}

function confirmButton(label, action) {
  const button = el("button", { className: "mini", textContent: label });
  let armed = false;
  button.addEventListener("click", async () => {
    if (!armed) {
      armed = true;
      button.textContent = "Confirm?";
      return;
    }
    try {
      await action();
      await refresh();
    } catch {
      button.textContent = "Failed";
    }
  });
  return button;
}

function siteIndex() {
  const sites = new Map();
  const touch = (site, work, field, by = 1) => {
    site = site ?? "";
    work = work ?? "";
    if (!sites.has(site)) sites.set(site, new Map());
    const items = sites.get(site);
    if (!items.has(work)) items.set(work, { lines: 0, orders: 0 });
    items.get(work)[field] += by;
  };

  for (const row of rows) touch(row.site, row.work_item, "lines");
  for (const sheet of lists) {
    touch(sheet.site, sheet.work_item, "orders");
  }
  return sites;
}

function renderSiteTree() {
  // The sites hang under the Purchase orders menu now, nowhere else.
  for (const spec of LINK_MENUS) {
    if (spec.sites) renderLinkMenu(spec);
  }
}

function refreshSiteFilter() {
  const select = $("q-site");
  const current = select.value;
  const sites = [...new Set(rows.map((row) => row.site).filter(Boolean))].sort();
  select.replaceChildren(
    el("option", { value: "", textContent: "All" }),
    ...sites.map((site) => el("option", { value: site, textContent: site })),
  );
  select.value = sites.includes(current) ? current : "";
  siteMenu.refresh();
}

for (const id of ["q-search", "q-site"]) {
  $(id).addEventListener("input", renderDeliveries);
}

for (const id of ["o-site", "o-work", "o-state"]) {
  $(id).addEventListener("input", renderOrders);
}

function dropdown(select) {
  select.classList.add("replaced");
  const toggle = el("button", { className: "dd-toggle", type: "button", "aria-haspopup": "listbox" });
  toggle.setAttribute("aria-expanded", "false");
  const list = el("ul", { className: "dd-list", role: "listbox", hidden: true });
  const wrap = el("div", { className: "dropdown" }, [toggle, list]);
  select.after(wrap);
  wrap.append(select);

  const close = () => {
    list.hidden = true;
    toggle.setAttribute("aria-expanded", "false");
  };

  const choose = (value) => {
    select.value = value;
    select.dispatchEvent(new Event("input", { bubbles: true }));
    close();
    toggle.focus();
  };

  const refreshList = () => {
    const current = select.value;
    toggle.textContent = select.selectedOptions[0]?.textContent ?? "All";
    list.replaceChildren(...[...select.options].map((option) => {
      const item = el("li", { role: "option", textContent: option.textContent, tabIndex: -1 });
      item.setAttribute("aria-selected", String(option.value === current));
      item.addEventListener("click", () => choose(option.value));
      return item;
    }));
  };

  toggle.addEventListener("click", () => {
    const opening = list.hidden;
    list.hidden = !opening;
    toggle.setAttribute("aria-expanded", String(opening));
    if (opening) {
      const selected = list.querySelector('[aria-selected="true"]') ?? list.firstElementChild;
      selected?.classList.add("active");
    }
  });

  wrap.addEventListener("keydown", (event) => {
    const items = [...list.children];
    const index = items.findIndex((item) => item.classList.contains("active"));
    if (event.key === "Escape") {
      close();
      toggle.focus();
    } else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      if (list.hidden) {
        toggle.click();
        return;
      }
      const step = event.key === "ArrowDown" ? 1 : -1;
      const next = Math.max(0, Math.min(items.length - 1, index + step));
      for (const item of items) item.classList.remove("active");
      items[next].classList.add("active");
      items[next].scrollIntoView({ block: "nearest" });
    } else if (event.key === "Enter" && !list.hidden && index >= 0) {
      event.preventDefault();
      items[index].click();
    }
  });

  document.addEventListener("click", (event) => {
    if (!wrap.contains(event.target)) close();
  });

  refreshList();
  return { refresh: refreshList };
}

const siteMenu = dropdown($("q-site"));
const orderMenu = dropdown($("o-site"));
const workMenu = dropdown($("o-work"));
const stateMenu = dropdown($("o-state"));

let asking = null;
$("cancel-ask").addEventListener("click", () => asking?.abort());

$("ask-btn").addEventListener("click", async () => {
  const question = $("question").value.trim();
  if (!question) return;

  const status = $("ask-status");
  $("ask-btn").disabled = true;
  $("cancel-ask").hidden = false;
  $("ask-sql").hidden = true;
  $("ask-note").replaceChildren();
  $("table-answer").replaceChildren();
  spinner(status, "Working...");

  asking = new AbortController();
  const sql = question.replace(/^\s*\/?sql\s+/i, "");
  const direct = /^\s*(select|with)\b/i.test(sql);

  try {
    const answer = await api(direct ? "/api/sql" : "/api/question", {
      ...postJson(direct ? { sql } : { question }),
      signal: asking.signal,
    });
    status.hidden = true;

    if (answer.note) {
      $("ask-note").replaceChildren(el("p", { className: "notice", textContent: answer.note }));
    }
    if (!answer.sql) return;

    $("ask-sql").textContent = answer.sql;
    $("ask-sql").hidden = false;

    const columns = answer.columns.map((name, index) => ({
      label: name,
      numeric: answer.rows.some((row) => typeof row[index] === "number"),
      cell: (row) => {
        const value = row[index];
        if (value == null) return "—";
        if (typeof value === "number") return showNum(value);
        if (typeof value === "boolean") return value ? "yes" : "no";
        return String(value);
      },
    }));
    renderTable($("table-answer"), columns, answer.rows, "No result.");
  } catch (e) {
    if (e.name === "AbortError") status.hidden = true;
    else notice(status, "bad", e.message);
  } finally {
    asking = null;
    $("cancel-ask").hidden = true;
    $("ask-btn").disabled = false;
  }
});

let readingList = null;
$("cancel-list").addEventListener("click", () => readingList?.abort());

$("read-list").addEventListener("click", async () => {
  if (!listFile) return;
  const status = $("list-status");
  $("read-list").disabled = true;
  $("cancel-list").hidden = false;
  spinner(status, "Reading the list... (handwriting: 1 to 2 minutes)");

  readingList = new AbortController();
  const form = new FormData();
  form.append("photo", listFile);
  try {
    const result = await api("/api/orders/extract", {
      method: "POST", body: form, signal: readingList.signal,
    });
    listSha = result.sha256;
    status.hidden = true;
    openListReview(result.list);
  } catch (e) {
    if (e.name === "AbortError") status.hidden = true;
    else notice(status, "bad", e.message);
  } finally {
    readingList = null;
    $("cancel-list").hidden = true;
    $("read-list").disabled = !listFile;
  }
});

function openListReview(sheet) {
  listProposed = structuredClone(sheet);
  listDraft = {
    list_date: sheet.list_date ?? "",
    site: sheet.site ?? "",
    work_item: sheet.work_item ?? "",
    drafter: sheet.drafter ?? "",
    lines: (sheet.lines ?? []).map((line) => ({
      article: String(line.article ?? ""),
      designation: String(line.designation ?? ""),
      reference: line.reference ?? null,
      quantity: line.quantity == null ? null : Number(line.quantity),
      stock: line.stock == null ? null : Number(line.stock),
    })),
    warnings: sheet.warnings ?? [],
  };

  $("l-list_date").value = listDraft.list_date;
  $("l-site").value = listDraft.site;
  $("l-work_item").value = listDraft.work_item;
  $("l-drafter").value = listDraft.drafter;
  $("list-warnings").replaceChildren(...listDraft.warnings.map((text) =>
    el("p", { className: "notice warn", textContent: `Check: ${text}` })));

  renderListLines();
  checkList();
  $("list-review").hidden = false;
  $("list-review").scrollIntoView({ behavior: "smooth", block: "start" });
}

for (const field of ["list_date", "site", "work_item", "drafter"]) {
  $(`l-${field}`).addEventListener("input", (event) => {
    if (listDraft) listDraft[field] = event.target.value;
  });
}

function renderListLines() {
  $("list-lines").replaceChildren(...listDraft.lines.map((line, index) => {
    const article = el("input", { value: line.article, "aria-label": `Article ${index + 1}` });
    article.addEventListener("input", () => {
      line.article = article.value;
      checkList();
    });

    const quantity = el("input", {
      className: "qty", type: "number", min: "0", step: "any",
      value: line.quantity ?? "", "aria-label": `Quantity ${index + 1}`,
    });
    quantity.addEventListener("input", () => { line.quantity = asNumber(quantity.value); });

    const remove = el("button", { className: "mini", textContent: "x", title: "Remove the line" });
    remove.addEventListener("click", () => {
      listDraft.lines.splice(index, 1);
      renderListLines();
      checkList();
    });

    const row = el("div", { className: "line" }, [
      el("span", { className: "num", textContent: index + 1 }),
      article,
      el("span", { className: "qty-wrap" }, [quantity]),
      remove,
    ]);
    if (line.designation && line.designation !== line.article) {
      row.append(el("span", { className: "desig", textContent: line.designation }));
    }
    return row;
  }));
}

$("list-add").addEventListener("click", () => {
  if (!listDraft) return;
  listDraft.lines.push({ article: "", designation: "", reference: null, quantity: null, stock: null });
  renderListLines();
  checkList();
});

function checkList() {
  const found = [];
  if (!listDraft.lines.length) found.push("no line");
  listDraft.lines.forEach((line, index) => {
    if (!line.article.trim()) found.push(`line ${index + 1} has no article`);
  });

  notice($("list-problems"), "warn", found.length ? `To fix: ${found.join(", ")}` : "");
  $("list-save").disabled = found.length > 0;
  return found;
}

$("list-discard").addEventListener("click", () => {
  listDraft = listFile = listSha = null;
  $("list-review").hidden = true;
  $("photo-list").value = "";
  $("thumb-list").hidden = true;
  $("photo-list-name").textContent = "Take a picture";
  $("read-list").disabled = true;
});

$("list-save").addEventListener("click", async () => {
  if (!listDraft || checkList().length) return;
  $("list-save").disabled = true;
  notice($("list-problems"), "ok", "Saving...");

  const sheet = {
    list_date: ISO_DATE.test(listDraft.list_date) ? listDraft.list_date : null,
    site: listDraft.site.trim() || null,
    work_item: listDraft.work_item.trim() || null,
    drafter: listDraft.drafter.trim() || null,
    lines: listDraft.lines.map((line) => ({
      article: line.article.trim(),
      designation: line.designation || null,
      reference: line.reference,
      quantity: line.quantity,
      stock: line.stock,
      in_stock: null,
    })),
  };
  const body = {
    sha256: listSha,
    list: sheet,
    measurement: measure(listProposed, sheet,
      ["list_date", "site", "work_item", "drafter"], ["article", "quantity"]),
  };

  try {
    const stored = await api("/api/orders", postJson(body));
    notice($("list-problems"), "ok", `List #${stored.list_id} saved.`);
    await refresh();
    setTimeout(() => $("list-discard").click(), 1200);
  } catch (e) {
    notice($("list-problems"), "bad", e.message);
    $("list-save").disabled = false;
  }
});

function inStock(line) {
  const wanted = line.quantity ?? 0;
  return wanted > 0 && (line.have ?? 0) >= wanted;
}

function stockWord(line) {
  // Nothing to press: the delivery notes say what is standing there.
  const have = line.have ?? 0;
  const wanted = line.quantity ?? 0;
  const enough = inStock(line);
  const word = el("span", { className: `stock-word ${enough ? "ok" : "short"}`,
                            textContent: enough ? "in stock" : "to order" });
  let said;
  if (enough) said = `${showNum(have)} on the notes`;
  else if (have > 0) said = `${showNum(have)} on the notes, ${showNum(wanted - have)} short`;
  else if (have < 0) said = `nothing here, ${showNum(-have)} already promised elsewhere`;
  else said = "nothing on the notes for this place";
  word.title = said;
  return el("span", {}, [word, el("div", { className: "desig", textContent: said })]);
}


function refreshOrderPicks() {
  const site = $("o-site");
  const keptSite = site.value;
  const sites = [...new Set(lists.map((sheet) => sheet.site).filter(Boolean))].sort();
  site.replaceChildren(
    el("option", { value: "", textContent: "every site" }),
    ...sites.map((name) => el("option", { value: name, textContent: name })),
  );
  site.value = sites.includes(keptSite) ? keptSite : "";

  // The work items are the ones of the site that is picked, not of every site.
  const work = $("o-work");
  const keptWork = work.value;
  const items = [...new Set(lists
    .filter((sheet) => !site.value || (sheet.site ?? "") === site.value)
    .map((sheet) => sheet.work_item).filter(Boolean))].sort();
  work.replaceChildren(
    el("option", { value: "", textContent: "every work item" }),
    ...items.map((name) => el("option", { value: name, textContent: name })),
  );
  work.value = items.includes(keptWork) ? keptWork : "";

  orderMenu.refresh();
  workMenu.refresh();
  stateMenu.refresh();
}

// Run lists the orders whatever is filled in; a pick from a menu does it by itself.
var orderRan = false;

function orderPicked() {
  return orderRan || Boolean(treePick) || Boolean($("o-site").value) || Boolean($("o-state").value);
}

$("o-run").addEventListener("click", () => {
  orderRan = true;
  renderOrders();
});

function filteredOrders() {
  const site = $("o-site").value;
  const work = $("o-work").value;
  const state = $("o-state").value;

  return lists
    .filter((sheet) => !treePick || (sheet.site ?? "") === treePick.site)
    .filter((sheet) => !treePick || treePick.work === null
                       || (sheet.work_item ?? "") === treePick.work)
    .filter((sheet) => !site || (sheet.site ?? "") === site)
    .filter((sheet) => !work || (sheet.work_item ?? "") === work)
    .map((sheet) => {
      if (!state) return sheet;
      const lines = (sheet.lines ?? []).filter(
        (line) => inStock(line) === (state === "stock"));
      return { ...sheet, lines };
    })
    .filter((sheet) => (sheet.lines ?? []).length);
}

function renderOrders() {
  refreshOrderPicks();
  // A year of orders at once says nothing: a site, a work item or a state says it.
  if (!orderPicked()) {
    $("o-summary").textContent =
      "Pick a site, or press run to list every purchase order.";
    $("lists").replaceChildren();
    return;
  }

  const sheets = filteredOrders();
  const lineCount = sheets.reduce((sum, sheet) => sum + (sheet.lines ?? []).length, 0);
  $("o-summary").textContent =
    `${sheets.length} purchase order(s) · ${lineCount} line(s)`;

  if (!sheets.length) {
    $("lists").replaceChildren(el("p", {
      className: "muted",
      textContent: lists.length ? "Nothing matches these three." : "No purchase order stored.",
    }));
    return;
  }

  $("lists").replaceChildren(...sheets.map((sheet) => {
    const lines = sheet.lines ?? [];
    const title = el("div", { className: "sheet-title" }, [
      el("strong", { textContent: sheet.site ?? "no site" }),
      el("span", { className: "muted", textContent: sheet.work_item ?? "" }),
    ]);

    const actions = el("span", { className: "row tight", style: "margin-left:auto" });
    if (sheet.photo) {
      actions.append(el("a", {
        className: "mini", href: `/api/order-photo/${sheet.id}`, target: "_blank",
        textContent: "photo",
      }));
    }
    actions.append(confirmButton("Delete", () => api(`/api/orders/${sheet.id}`, { method: "DELETE" })));
    title.append(actions);

    // Who it ships to and on what terms, saved as you pick it.
    const terms = el("div", { className: "row tight ship" });
    const contact = el("select", { className: "cell" });
    contactChoices(contact, sheet.contact_id);
    const incoterm = el("select", { className: "cell" });
    incotermChoices(incoterm, sheet.incoterm);
    const shipBy = el("input", { className: "cell", type: "date", value: sheet.ship_by ?? "" });
    const save = async () => {
      // A contact with its own terms fills the incoterm in when the order has none.
      const chosen = contactRows.find((row) => String(row.contact_id) === contact.value);
      if (!incoterm.value && chosen?.incoterm) incoterm.value = chosen.incoterm;
      try {
        await api(`/api/orders/${sheet.id}/terms`, patchJson({
          contact_id: contact.value ? Number(contact.value) : null,
          incoterm: incoterm.value || "",
          ship_by: shipBy.value || "",
        }));
        await refresh();
      } catch (e) {
        notice($("global-notice"), "bad", e.message);
      }
    };
    for (const box of [contact, incoterm, shipBy]) box.addEventListener("change", save);
    terms.append(el("span", { className: "legend", textContent: "ships to" }), contact,
                 el("span", { className: "legend", textContent: "incoterm" }), incoterm,
                 el("span", { className: "legend", textContent: "ship by" }), shipBy);
    if (sheet.contact) {
      const reach = [sheet.contact.person, sheet.contact.phone, sheet.contact.email,
                     sheet.contact.address].filter(Boolean).join(" · ");
      if (reach) terms.append(el("span", { className: "desig", textContent: reach }));
    }

    const table = el("table", { className: "order-table" });
    renderTable(table, [
      { label: "#", numeric: true, cell: (line) => String(line.line_no) },
      {
        label: "Article",
        cell: (line) => {
          const box = el("span", {}, [el("strong", { textContent: line.article })]);
          const under = [line.reference, line.designation].filter(Boolean).join(" · ");
          if (under) box.append(el("div", { className: "desig", textContent: under }));
          return box;
        },
      },
      { label: "Quantity", numeric: true, cell: (line) => showNum(line.quantity) },
      { label: "On the notes", cell: stockWord },
    ], lines, "No line on this sheet.");

    return el("div", { className: "sheet" }, [
      title,
      terms,
      el("div", { className: "sheet-body" }, [el("div", { className: "scroll-x" }, [table])]),
    ]);
  }));
}
