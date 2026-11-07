"use strict";

// ---- menu bar: a menu picks the one card a screen shows -------------------

const SCREEN_MENUS = [
  { menu: "system-menu", drop: "system-drop", button: "menu-system",
    panel: "panel-settings", tab: "settings", key: "settings-card", title: "System",
    // Nine entries nobody used. Their cards stay in the page so the settings behind them
    // still load, but no menu line reaches them.
    retired: ["pack-card", "site-stock-card", "stock-moves-card", "cycle-card", "wallet-card",
              "policies-card", "charters-card", "calendar-card", "archive-card"],
    skip: [] },
  { menu: "inv-menu", drop: "inv-drop", button: "menu-inventory",
    panel: "panel-inventory", tab: "inventory", key: "inventory-card", title: "Inventory",
    skip: [] },
  { menu: "input-menu", drop: "input-drop", button: "menu-note",
    panel: "panel-capture", tab: "capture", key: "capture-card", title: "Input",
    skip: ["review", "mapping"] },
  { menu: "chain-menu", drop: "chain-drop", button: "menu-chain",
    panel: "panel-chain", tab: "chain", key: "chain-card-shown", title: "Blockchain",
    // The round is the screen itself, so the menu lists what sits under it.
    skip: ["chain-card"], go: openChain },
  { menu: "tools-menu", drop: "tools-drop", button: "menu-ddmrp",
    panel: "panel-ddmrp", tab: "ddmrp", key: "ddmrp-card", title: "DDMRP",
    // The buffers show only when the DDMRP name on the bar is pressed; an entry under it
    // shows its own card and nothing else.
    skip: ["ddmrp-landing"], landing: "ddmrp-landing", go: openPredictions,
    bar: () => {
      openPredictions();
      showLanding(ALL_MENUS.find((one) => one.menu === "tools-menu"));
    } },
];

// A menu that walks to another screen instead of picking a card on this one.
const LINK_MENUS = [
  { menu: "orders-menu", drop: "orders-drop", button: "menu-orders", title: "Purchase orders",
    links: [["Purchase orders", "orders"], ["Quotes", "quotes"], ["Site requests", "requests"], ["Bills", "bills"]],
    // Each of these gets a line of its own and opens that card by itself.
    // No Stored PO line: it holds the same orders the Purchase orders line walks to.
    cards: [["Scan a purchase order", "order-scan"], ["Contacts", "contacts-card"]],
    sites: true, holdStill: true },
];

const ALL_MENUS = [...SCREEN_MENUS, ...LINK_MENUS];

function cardName(card) {
  return (card.querySelector("h2")?.textContent ?? "").trim();
}

function menuCards(spec) {
  return [...document.querySelectorAll(`#${spec.panel} .card`)]
    .filter((card) => !spec.skip.includes(card.id) && !(spec.retired ?? []).includes(card.id)
                      && card.querySelector("h2"));
}

function showOnly(spec, card) {
  for (const other of menuCards(spec)) {
    other.hidden = other !== card;
  }
  // The screen's own card sits above whatever is picked, except for the cards that
  // want the whole screen to themselves.
  for (const id of spec.skip) {
    const own = $(id);
    if (own && spec.landing) own.hidden = true;
    else if (own && spec.alone) own.hidden = spec.alone.includes(card.id);
  }
  for (const entry of document.querySelectorAll(`#${spec.drop} button`)) {
    entry.setAttribute("aria-current", String(entry.dataset.card === cardName(card)));
  }
  try {
    localStorage.setItem(spec.key, cardName(card));
  } catch {
  }
}

// The screen's own card on its own: every card the menu lists is put away.
function showLanding(spec) {
  for (const other of menuCards(spec)) other.hidden = true;
  $(spec.landing).hidden = false;
  for (const entry of document.querySelectorAll(`#${spec.drop} button`)) {
    entry.setAttribute("aria-current", "false");
  }
}

function closeMenu(spec) {
  $(spec.menu).classList.remove("open");
  $(spec.button).setAttribute("aria-expanded", "false");
}

function openMenu(spec) {
  for (const other of ALL_MENUS) {
    if (other !== spec) closeMenu(other);
  }
  $(spec.menu).classList.add("open");
  $(spec.button).setAttribute("aria-expanded", "true");
}

function buildMenu(spec) {
  const cards = menuCards(spec);
  if (!cards.length) return;

  $(spec.drop).replaceChildren(...cards.map((card) => {
    const entry = el("button", { type: "button", textContent: cardName(card), role: "menuitem" });
    entry.dataset.card = cardName(card);
    entry.addEventListener("click", () => {
      if (spec.go) spec.go(); else openTab(spec.tab);
      showOnly(spec, card);
      closeMenu(spec);
      say(`${spec.title} · ${cardName(card)}`);
    });
    return entry;
  }));

  for (const id of spec.retired ?? []) {
    if ($(id)) $(id).hidden = true;
  }
  let wanted = null;
  try {
    const saved = localStorage.getItem(spec.key);
    wanted = cards.find((card) => cardName(card) === saved) ?? null;
  } catch {
  }
  if (spec.landing) showLanding(spec);
  else showOnly(spec, wanted ?? cards[0]);
  hover(spec, spec.bar ?? spec.go ?? (() => openTab(spec.tab)));
}

function hover(spec, onClick) {
  const holder = $(spec.menu);
  let timer = null;
  holder.addEventListener("pointerenter", () => {
    clearTimeout(timer);
    openMenu(spec);
  });
  holder.addEventListener("pointerleave", () => {
    timer = setTimeout(() => closeMenu(spec), 250);
  });
  holder.addEventListener("focusout", (event) => {
    if (!holder.contains(event.relatedTarget)) closeMenu(spec);
  });
  $(spec.button).addEventListener("click", () => {
    onClick();
    if (holder.classList.contains("open")) closeMenu(spec);
    else openMenu(spec);
  });
}

function buildLinkMenu(spec) {
  renderLinkMenu(spec);
  // holdStill: the name on the bar only opens the menu, it does not walk anywhere.
  hover(spec, spec.holdStill ? () => {} : () => openTab(spec.links[0][1]));
}

function pickFromMenu(spec, site, work, lines) {
  treePick = { site, work, lines };
  orderCard = "stored-po";
  closeMenu(spec);
  renderSiteTree();
  renderDeliveries();
  renderOrders();
  openTab("orders");
  say(`${site || "no site"}`
      + (work === null ? ", all work items" : `, ${work || "no work item"}`));
}

function siteEntries(spec) {
  // Second level: a site opens the list of its work items.
  const sites = [...siteIndex()].sort((one, two) => one[0].localeCompare(two[0]));
  if (!sites.length) return [];

  const tally = (items) => [...items.values()].reduce(
    (sum, item) => ({ lines: sum.lines + item.lines, orders: sum.orders + item.orders }),
    { lines: 0, orders: 0 });
  const count = (item) => [item.lines ? `${item.lines} line(s)` : "",
                           item.orders ? `${item.orders} PO` : ""].filter(Boolean).join(" · ")
                          || "nothing yet";

  const out = [];
  for (const [site, items] of sites) {
    const whole = tally(items);
    const head = el("button", {
      type: "button", role: "menuitem", className: "sub-head",
      textContent: `${site || "no site"} — ${count(whole)}`,
    });
    head.setAttribute("aria-haspopup", "true");
    head.setAttribute("aria-current", String(Boolean(treePick) && treePick.site === site));
    head.addEventListener("click", () => pickFromMenu(spec, site, null, whole.lines));

    const work = [el("button", {
      type: "button", role: "menuitem", textContent: `All work items — ${count(whole)}`,
    })];
    work[0].addEventListener("click", () => pickFromMenu(spec, site, null, whole.lines));
    for (const [name, item] of [...items].sort((one, two) => one[0].localeCompare(two[0]))) {
      const entry = el("button", {
        type: "button", role: "menuitem",
        textContent: `${name || "no work item"} — ${count(item)}`,
      });
      entry.setAttribute("aria-current", String(
        Boolean(treePick) && treePick.site === site && treePick.work === name));
      entry.addEventListener("click", () => pickFromMenu(spec, site, name, item.lines));
      work.push(entry);
    }
    out.push(el("span", { className: "sub" },
                [head, el("div", { className: "menu-drop", role: "menu" }, work)]));
  }
  return out;
}

function renderLinkMenu(spec) {
  const entries = spec.links.map(([label, tab]) => {
    const entry = el("button", { type: "button", textContent: label, role: "menuitem" });
    entry.addEventListener("click", () => {
      if (tab === "orders") {
        treePick = null;
        orderRan = false;
        orderCard = "stored-po";
      }
      openTab(tab);
      closeMenu(spec);
      renderSiteTree();
      renderDeliveries();
      renderOrders();
      say(`Screen ${tab.toUpperCase()}`);
    });

    // Purchase orders itself rolls out once more: every site, then its work items.
    if (!spec.sites || tab !== "orders") return entry;
    const sites = siteEntries(spec);
    if (!sites.length) return entry;
    entry.setAttribute("aria-haspopup", "true");
    const all = el("button", { type: "button", role: "menuitem",
                               textContent: "All purchase orders" });
    all.addEventListener("click", () => {
      treePick = null;
      orderRan = false;
      orderCard = "stored-po";
      closeMenu(spec);
      renderSiteTree();
      renderDeliveries();
      renderOrders();
      openTab("orders");
      say("Screen ORDERS");
    });
    return el("span", { className: "sub" },
              [entry, el("div", { className: "menu-drop", role: "menu" }, [all, ...sites])]);
  });
  $(spec.drop).replaceChildren(...entries, ...cardEntries(spec));
}

function cardEntries(spec) {
  return (spec.cards ?? []).map(([label, id]) => {
    const entry = el("button", { type: "button", textContent: label, role: "menuitem" });
    entry.addEventListener("click", () => {
      closeMenu(spec);
      treePick = null;
      orderCard = id;
      openTab("orders");
      say(`${spec.title} · ${label}`);
    });
    return entry;
  });
}

function buildScreenMenus() {
  for (const spec of SCREEN_MENUS) buildMenu(spec);
  for (const spec of LINK_MENUS) buildLinkMenu(spec);
}

document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  for (const spec of ALL_MENUS) closeMenu(spec);
});

// ---- the five doors beside the logo --------------------------------
// This window is WebKit, not a browser: following an https link in it would walk the app
// off its own page, and window.open only makes a second window of the same app. So the
// page asks the server by name and the server hands the target to the desktop. The names
// are fixed on the server side; nothing here sends it a url or a path of its own.
async function wireHomeDoors() {
  const note = $("home-doors-note");
  const buttons = [...$("home-doors").querySelectorAll(".door")];

  // Wired before anything is asked of the server, so a window left open across an
  // install still answers the click, with whatever went wrong, instead of sitting dead.
  for (const button of buttons) {
    button.addEventListener("click", async () => {
      // Inside the desktop window the window itself asks for it, which is what lets the
      // page come to the front. In a plain browser the server opens it.
      const asker = window.webkit?.messageHandlers?.door;
      if (asker) {
        asker.postMessage(button.dataset.door);
        return;
      }
      button.disabled = true;
      try {
        const opened = await api(`/api/shortcuts/${button.dataset.door}`, { method: "POST" });
        notice(note, "ok", `${opened.name} is opening outside this window.`);
      } catch (e) {
        notice(note, "bad", e.message);
      } finally {
        button.disabled = false;
      }
    });
  }

  // Asked only to find out that the server is the old one, which has no doors.
  try {
    await api("/api/shortcuts");
  } catch (e) {
    notice(note, "bad", `${e.message} - restart Stock Manager if it was open during an update.`);
    return;
  }
  // Nothing is written on the buttons: a title would show the address on hover. A contact
  // sheet that is not there says so when it is clicked, which is when it matters.
}

// The landing screen is the logo, not a card: the work starts from a menu or the tree.
openTab("home");
buildScreenMenus();
wireHomeDoors();
refresh();
