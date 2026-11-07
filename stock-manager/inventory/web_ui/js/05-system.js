"use strict";

const DATASETS = [
  ["inventory", "Inventory lines"],
  ["purchase_orders", "Purchase order lines"],
  ["bills", "Supplier bills"],
  ["safety-stock", "Safety stock watch"],
];

function renderExport() {
  const link = (dataset, fmt) =>
    el("a", { className: "mini", href: `/export/${dataset}.${fmt}`, textContent: fmt });

  renderTable($("table-export"), [
    { label: "Data", cell: ([, label]) => el("strong", { textContent: label }) },
    { label: "CSV", cell: ([dataset]) => link(dataset, "csv") },
    { label: "XLSX", cell: ([dataset]) => link(dataset, "xlsx") },
    { label: "PostgreSQL", cell: ([dataset]) => link(dataset, "sql") },
  ], DATASETS, "");
}

async function renderStorage() {
  let info;
  try {
    info = await api("/api/storage");
  } catch (e) {
    notice($("storage-note"), "bad", e.message);
    return;
  }

  const lines = [
    ["Database", mb(info.database_bytes), `${info.rows.inventory_lines} delivery line(s)`],
    ["Photos", mb(info.photo_bytes), `${info.photo_count} file(s)`],
    ["Data folder", mb(info.data_bytes),
      `${info.rows.notes} note(s) · ${info.rows.purchase_orders} list(s)`],
    ["Disk used", gb(info.disk_used_bytes), `of ${gb(info.disk_total_bytes)} in total`],
    ["Disk free", gb(info.disk_free_bytes), ""],
  ];
  renderTable($("table-storage"), [
    { label: "Item", cell: ([name]) => el("strong", { textContent: name }) },
    { label: "Size", numeric: true, cell: ([, size]) => size },
    { label: "Detail", cell: ([, , detail]) => el("span", { className: "muted", textContent: detail }) },
  ], lines, "");

  notice($("storage-note"), "",
    `Images deletd after ${info.photo_retention_days} days`);
}

async function renderChecks() {
  let state;
  try {
    state = await api("/api/checks");
  } catch (e) {
    notice($("checks-note"), "bad", e.message);
    return;
  }

  const ok = state.status === "ok";
  const lines = [
    ["State", ok ? "OK" : (state.status === "unknown" ? "never checked" : "needs attention")],
    ["Last check", showTime(state.checked_at)],
    ["Site answer", state.http ? `http ${state.http}` : "—"],
    ["Disk free", state.disk_free_percent != null ? `${state.disk_free_percent} %` : "—"],
    ["Last backup", state.backup_age_hours >= 0 ? `${state.backup_age_hours} h ago` : "none"],
  ];
  renderTable($("table-checks"), [
    { label: "Check", cell: ([name]) => el("strong", { textContent: name }) },
    { label: "Value", cell: ([, value]) => value },
  ], lines, "");
}

async function renderModels() {
  let data;
  try {
    data = await api("/api/models");
  } catch (e) {
    notice($("model-note"), "bad", e.message);
    return;
  }

  $("models").replaceChildren(...(data.providers ?? []).map((provider) => {
    const row = el("div", { className: provider.current ? "model current" : "model" }, [
      el("span", { className: `dot ${provider.state}` }),
      el("strong", { textContent: provider.name }),
      el("span", { className: "detail", textContent: provider.detail }),
      el("span", { className: "spacer" }),
    ]);
    if (provider.current) {
      row.append(el("span", { className: "pill shared", textContent: "in use" }));
    }

    const check = el("button", { className: "mini", textContent: "check" });
    check.addEventListener("click", async () => {
      check.disabled = true;
      notice($("model-note"), "", `checking ${provider.name}...`);
      try {
        const answer = await api("/api/models/check", postJson({ id: provider.id }));
        notice($("model-note"), answer.ok ? "ok" : "warn", `${provider.name}: ${answer.detail}`);
      } catch (e) {
        notice($("model-note"), "bad", e.message);
      } finally {
        check.disabled = false;
      }
    });
    row.append(check);

    if (!provider.current && provider.state !== "unsupported") {
      const use = el("button", { className: "mini", textContent: "use" });
      use.addEventListener("click", async () => {
        use.disabled = true;
        try {
          const answer = await api("/api/models/use", postJson({ id: provider.id }));
          notice($("model-note"), "ok", `reading with ${provider.name} from now on`);
          await renderModels();
          return answer;
        } catch (e) {
          notice($("model-note"), "bad", e.message);
        } finally {
          use.disabled = false;
        }
      });
      row.append(use);
    }
    return row;
  }));
}

async function renderErrors() {
  let data;
  try {
    data = await api("/api/errors?limit=25");
  } catch (e) {
    if ($("error-note")) notice($("error-note"), "bad", e.message);
    return;
  }

  const alerting = data.alerting ?? {};
  $("alerting").textContent = alerting.enabled
    ? `Texting ${alerting.to} through ${alerting.provider} · at most `
      + `${alerting.max_per_hour} message(s) per hour`
    : "";

  if (!$("table-errors")) return;

  renderTable($("table-errors"), [
    { label: "#", numeric: true, cell: (error) => String(error.error_no) },
    { label: "When", cell: (error) => showTime(error.at) },
    { label: "Kind", cell: (error) => error.kind },
    {
      label: "Description",
      cell: (error) => {
        const box = el("span", {}, [el("strong", { textContent: error.description })]);
        if (error.context) {
          box.append(el("div", { className: "desig muted", textContent: error.context }));
        }
        return box;
      },
    },
    {
      label: "Message",
      cell: (error) => el("span", {
        className: error.sms_status === "sent" ? "" : "muted",
        textContent: error.sms_status,
      }),
    },
  ], data.errors ?? [], "No error reported.");
}

$("error-test").addEventListener("click", async () => {
  // The errors table can be taken off the screen; the button still has to say what happened.
  const tell = (kind, text) => {
    if ($("error-note")) notice($("error-note"), kind, text);
    else say(text, kind === "bad" ? "bad" : "");
  };
  $("error-test").disabled = true;
  tell("", "Sending...");
  try {
    const result = await api("/api/errors/test", { method: "POST" });
    tell(result.sms_status === "sent" ? "ok" : "warn",
         `Error #${result.error_no}, message: ${result.sms_status}`);
    await renderErrors();
  } catch (e) {
    tell("bad", e.message);
  } finally {
    $("error-test").disabled = false;
  }
});

async function renderBackups() {
  let data;
  try {
    data = await api("/api/backups");
  } catch (e) {
    notice($("backup-note"), "bad", e.message);
    return;
  }

  const files = data.backups ?? [];
  $("backup-summary").textContent = files.length
    ? `${files.length} backup(s) · latest ${files[0].date.replace("T", " ")}`
    : "no backup";

  const csv = (file, table) => el("a", {
    className: "mini", textContent: "csv",
    href: `/export/backup/${file.name}.csv?table=${table}`,
  });

  renderTable($("table-backups"), [
    { label: "Date", cell: (file) => el("strong", { textContent: file.date.replace("T", " ") }) },
    { label: "Size", numeric: true, cell: (file) => `${(file.bytes / 1000).toFixed(0)} kB` },
    { label: "Encrypted", cell: (file) => (file.encrypted ? "yes" : "no") },
    { label: "Inventory", cell: (file) => csv(file, "inventory_lines") },
    { label: "Purchase orders", cell: (file) => csv(file, "purchase_order_lines") },
  ], files.slice(0, 10), "No backup yet.");
}

$("backup-run").addEventListener("click", async () => {
  $("backup-run").disabled = true;
  notice($("backup-note"), "", "Backing up...");
  try {
    const result = await api("/api/backups", { method: "POST" });
    notice($("backup-note"), "ok", result.latest
      ? `Backup done: ${result.latest.name} (${(result.latest.bytes / 1000).toFixed(0)} kB)`
      : "Backup done.");
    await renderBackups();
  } catch (e) {
    notice($("backup-note"), "bad", e.message);
  } finally {
    $("backup-run").disabled = false;
  }
});

async function renderAccuracy() {
  let data;
  try {
    data = await api("/api/accuracy");
  } catch (e) {
    notice($("accuracy-note"), "bad", e.message);
    return;
  }

  const series = data.series ?? [];
  if (!series.length) {
    $("accuracy-graph").replaceChildren();
    notice($("accuracy-note"), "",
      "Nothing measured yet: it fills in as notes are checked and saved.");
    return;
  }

  const graph = el("div", { className: "graph" });
  for (const day of series.slice(-30)) {
    const bar = el("div", {
      className: day.accuracy < 90 ? "bar bad" : "bar",
      title: `${showDate(day.day)}: ${day.accuracy}% over ${day.documents} document(s)`,
    });
    bar.style.height = `${Math.max(2, Math.round(day.accuracy))}%`;
    graph.append(bar);
  }

  $("accuracy-graph").replaceChildren(graph, el("p", {
    className: "graph-legend",
    textContent: `one day per bar · ${data.documents} document(s) reviewed · `
      + `${data.corrected} field(s) corrected out of ${data.fields}`,
  }));
  notice($("accuracy-note"), data.accuracy != null && data.accuracy < 90 ? "warn" : "",
    data.accuracy != null ? `Overall accuracy: ${data.accuracy}%` : "");
}

async function renderVocabulary() {
  if (!$("table-vocabulary")) return;

  let data;
  try {
    data = await api("/api/vocabulary");
  } catch (e) {
    notice($("merge-note"), "bad", e.message);
    return;
  }

  const articles = data.articles ?? [];
  $("vocabulary-summary").textContent = `${data.total} name(s) · ${data.shared} used on both `
    + `delivery notes and purchase orders · ${(data.aliases ?? []).length} alias(es)`;

  renderTable($("table-vocabulary"), [
    {
      label: "Article",
      cell: (entry) => {
        const box = el("span", {}, [el("strong", { textContent: entry.article })]);
        if (entry.shared) {
          box.append(" ", el("span", { className: "pill shared", textContent: "shared" }));
        }
        if (entry.aliases.length) {
          box.append(el("div", {
            className: "desig muted", textContent: `= ${entry.aliases.join(", ")}`,
          }));
        }
        return box;
      },
    },
    { label: "Lines", numeric: true, cell: (entry) => String(entry.inventory) },
    { label: "Orders", numeric: true, cell: (entry) => String(entry.orders) },
    { label: "Confirmed", numeric: true, cell: (entry) => String(entry.seen) },
    {
      label: "",
      cell: (entry) => {
        const pick = el("button", { className: "mini", textContent: "merge into..." });
        pick.addEventListener("click", () => {
          $("merge-alias").value = entry.article;
          $("merge-article").focus();
        });
        return pick;
      },
    },
  ], articles.slice(0, 60), "No article name yet.");
}

$("merge-run")?.addEventListener("click", async () => {
  const alias = $("merge-alias").value.trim();
  const article = $("merge-article").value.trim();
  if (!alias || !article) {
    notice($("merge-note"), "warn", "Give the name to merge and the name to keep.");
    return;
  }

  $("merge-run").disabled = true;
  try {
    const result = await api("/api/vocabulary/merge", postJson({ alias, article }));
    notice($("merge-note"), "ok",
      `${result.alias} is now ${result.article}, ${result.inventory} delivery line(s) and `
      + `${result.order_lines} list line(s) rewritten.`);
    $("merge-alias").value = "";
    $("merge-article").value = "";
    await refresh();
  } catch (e) {
    notice($("merge-note"), "bad", e.message);
  } finally {
    $("merge-run").disabled = false;
  }
});

async function renderAudit() {
  let data;
  try {
    data = await api("/api/audit?limit=60");
  } catch (e) {
    notice($("audit-note"), "bad", e.message);
    return;
  }

  renderTable($("table-users"), [
    { label: "User", cell: (user) => el("strong", { textContent: user.user }) },
    { label: "Notes", numeric: true, cell: (user) => String(user.notes) },
    { label: "Orders", numeric: true, cell: (user) => String(user.orders) },
    { label: "Corrections", numeric: true, cell: (user) => String(user.corrections) },
    { label: "Events", numeric: true, cell: (user) => String(user.events) },
    { label: "Last seen", cell: (user) => showTime(user.last_seen) },
  ], data.users ?? [], "Nobody has signed in yet.");

  renderTable($("table-audit"), [
    { label: "When", cell: (event) => showTime(event.at) },
    { label: "User", cell: (event) => el("strong", { textContent: event.user }) },
    { label: "Action", cell: (event) => event.action },
    { label: "Target", cell: (event) => event.target ?? "—" },
    { label: "Detail", cell: (event) => el("span", { className: "muted", textContent: event.detail ?? "" }) },
  ], data.events ?? [], "No event recorded.");
}
