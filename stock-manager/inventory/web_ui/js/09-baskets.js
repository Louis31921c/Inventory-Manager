"use strict";

// ---- Baskets filled from the group chat -----------------------------------

async function loadBaskets() {
  // The card these lines drew into has been taken off the screen. Nothing else reads
  // them here, and reaching for it would throw inside the refresh that loads the lot.
  if (!$("basket-card")) return;
  let data;
  try {
    data = await api("/api/baskets");
  } catch (e) {
    notice($("basket-note"), "bad", e.message);
    return;
  }
  const open = data.baskets ?? [];
  const chat = data.chat ?? {};
  const slack = data.slack ?? {};
  const onAir = [chat.ready && "Discord", slack.ready && "Slack"].filter(Boolean);
  $("basket-card").hidden = open.length === 0 && !onAir.length;

  $("basket-state").textContent = onAir.length
    ? `${onAir.join(" and ")} on air. /basket fills these, /purchase turns one into an order.`
    : (chat.installed || slack.installed
        ? "No bot token yet: Settings, then Chat bot."
        : "Neither library is installed: pip install 'stock-manager[discord,slack]'.");

  if ($("chat-state")) {
    $("chat-state").textContent = chat.ready
      ? `Bot ready · ${chat.channels.length ? chat.channels.join(", ") : "every channel"}`
      : (chat.installed ? (chat.token_set ? "Token stored, bot starting with the app."
                                          : "No token stored yet.")
                        : "Library missing: pip install 'stock-manager[discord]'");
  }
  $("chat-channels").value = (chat.channels ?? []).join(", ");

  // Slack wants both tokens before it can open its socket, so say which one is missing.
  $("slack-state").textContent = slack.ready
    ? `Bot ready · ${slack.channels.length ? slack.channels.join(", ") : "every channel"}`
    : (!slack.installed ? "Library missing: pip install 'stock-manager[slack]'"
      : !slack.bot_token_set && !slack.app_token_set ? "No token stored yet."
      : !slack.bot_token_set ? "The bot token (xoxb-) is missing."
      : !slack.app_token_set ? "The app token (xapp-) is missing."
      : "Both tokens stored, bot starting with the app.");
  $("slack-channels").value = (slack.channels ?? []).join(", ");

  $("baskets").replaceChildren(...open.map((basket) => {
    const head = el("div", { className: "row tight" }, [
      el("strong", { textContent: basket.shop || "no shop named" }),
      el("span", { className: "muted",
                   textContent: `channel ${basket.channel} · ${basket.lines} line(s)`
                                + (basket.site ? ` · ${basket.site}` : "")
                                + (basket.last_at ? ` · ${basket.last_at}` : "") }),
      el("span", { className: "grow" }),
    ]);
    const order = el("button", { className: "mini", textContent: "make it a purchase order" });
    order.addEventListener("click", async () => {
      order.disabled = true;
      try {
        const answer = await api(`/api/baskets/${encodeURIComponent(basket.channel)}/order`,
                                 postJson({}));
        notice($("basket-note"), "ok",
          `Purchase order #${answer.list_id} written, ${answer.lines} line(s).`);
        await Promise.all([loadBaskets(), refresh()]);
      } catch (e) {
        notice($("basket-note"), "bad", e.message);
        order.disabled = false;
      }
    });
    const gone = el("button", { className: "mini", textContent: "empty it" });
    gone.addEventListener("click", async () => {
      try {
        await api(`/api/baskets/${encodeURIComponent(basket.channel)}`, { method: "DELETE" });
        await loadBaskets();
      } catch (e) {
        notice($("basket-note"), "bad", e.message);
      }
    });
    head.append(order, gone);

    const table = el("table");
    renderTable(table, [
      { label: "Article", cell: (row) => el("strong", { textContent: row.article }) },
      { label: "Quantity", numeric: true, cell: (row) => showNum(row.quantity) },
      { label: "Work item", cell: (row) => row.work_item ?? "" },
      { label: "Asked by", cell: (row) => el("span", { className: "muted",
                                                       textContent: row.added_by ?? "" }) },
    ], basket.items ?? [], "");
    return el("div", { className: "sheet" }, [head, el("div", { className: "scroll-x" }, [table])]);
  }));
}

async function saveChat(forget) {
  const button = forget ? $("chat-forget") : $("chat-save");
  button.disabled = true;
  try {
    await api("/api/chat/settings", postJson({
      token: $("chat-token").value || null,
      channels: $("chat-channels").value,
      forget,
    }));
    $("chat-token").value = "";
    notice($("chat-note"), "ok", forget
      ? "Token forgotten. The bot stops at the next start."
      : "Saved. Restart Stock Manager for the bot to sign in.");
    await loadBaskets();
  } catch (e) {
    notice($("chat-note"), "bad", e.message);
  } finally {
    button.disabled = false;
  }
}

async function saveSlack(forget) {
  const button = forget ? $("slack-forget") : $("slack-save");
  button.disabled = true;
  try {
    await api("/api/slack/settings", postJson({
      bot_token: $("slack-bot-token").value || null,
      app_token: $("slack-app-token").value || null,
      channels: $("slack-channels").value,
      forget,
    }));
    $("slack-bot-token").value = "";
    $("slack-app-token").value = "";
    notice($("slack-note"), "ok", forget
      ? "Both tokens forgotten. The bot stops at the next start."
      : "Saved. Restart Stock Manager for the bot to sign in.");
    await loadBaskets();
  } catch (e) {
    notice($("slack-note"), "bad", e.message);
  } finally {
    button.disabled = false;
  }
}

$("slack-check").addEventListener("click", async () => {
  const button = $("slack-check");
  button.disabled = true;
  spinner($("slack-note"), "Asking Slack...");
  try {
    const answer = await api("/api/slack/check", { method: "POST" });
    notice($("slack-note"), answer.ok ? "ok" : "bad", answer.detail);
  } catch (e) {
    notice($("slack-note"), "bad", e.message);
  } finally {
    button.disabled = false;
  }
});

$("slack-save").addEventListener("click", () => saveSlack(false));
$("slack-forget").addEventListener("click", () => saveSlack(true));

$("chat-save").addEventListener("click", () => saveChat(false));
$("chat-forget").addEventListener("click", () => saveChat(true));

// ---- what the shelf holds -------------------------------------------------

let stockRows = [];

function renderStockStats(data) {
  const tiles = [
    ["articles", showNum(data.articles)],
    ["came in", showNum(data.received)],
    ["shipped out", showNum(data.shipped)],
    ["being picked", showNum(data.picking)],
    ["on the shelf", showNum(data.on_hand)],
    ["below zero", showNum(data.negative)],
  ];
  $("stock-stats").replaceChildren(...tiles.map(([name, value]) =>
    el("span", { className: "stat" }, [
      el("span", { className: "stat-name", textContent: name }),
      el("strong", { textContent: value }),
    ])));
  
}

function renderStock() {
  const needle = upper($("stock-search").value.trim());
  const shortOnly = $("stock-short").getAttribute("aria-pressed") === "true";
  const shown = stockRows.filter((row) =>
    (!needle || row.article.includes(needle)) && (!shortOnly || row.on_hand < 0));
  renderTable($("table-stock"), [
    { label: "Article", cell: (row) => document.createTextNode(row.article) },
    { label: "In", numeric: true, cell: (row) => document.createTextNode(showNum(row.received)) },
    { label: "Out", numeric: true, cell: (row) => document.createTextNode(showNum(row.shipped)) },
    { label: "Picking", numeric: true,
      cell: (row) => document.createTextNode(showNum(row.picking)) },
    { label: "On hand", numeric: true, cell: (row) =>
      el("strong", { className: row.on_hand < 0 ? "bad" : "", textContent: showNum(row.on_hand) }) },
    { label: "Counted", numeric: true,
      cell: (row) => document.createTextNode(row.counted == null ? "—" : showNum(row.counted)) },
    { label: "Gap", numeric: true,
      cell: (row) => document.createTextNode(row.gap == null ? "—" : showNum(row.gap)) },
    { label: "Last in", cell: (row) => document.createTextNode(showDate(row.last_in)) },
    { label: "Last out", cell: (row) => document.createTextNode(showDate(row.last_out)) },
  ], shown, "Nothing on the shelf yet.");
}

async function loadStock() {
  try {
    const data = await api("/api/stock");
    stockRows = data.rows ?? [];
    renderStockStats(data);
    renderStock();
  } catch {
  }
}

$("stock-search").addEventListener("input", renderStock);
$("stock-short").addEventListener("click", () => {
  const on = $("stock-short").getAttribute("aria-pressed") === "true";
  $("stock-short").setAttribute("aria-pressed", String(!on));
  renderStock();
});
