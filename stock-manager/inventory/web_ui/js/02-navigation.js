"use strict";

const TABS = ["home", "capture", "inventory", "query", "orders", "quotes", "requests", "bills",
              "route", "settings", "ddmrp", "chain"];

// One card of the Purchase orders screen at a time; the menu says which.
const ORDER_CARDS = ["order-scan", "contacts-card", "stored-po"];
let orderCard = "stored-po";

function showCard(id) {
  orderCard = ORDER_CARDS.includes(id) ? id : orderCard;
  for (const other of ORDER_CARDS) $(other).hidden = other !== orderCard;
}

function openTab(name) {
  for (const other of TABS) {
    $(`panel-${other}`).hidden = other !== name;
  }
  if (name !== "chain") watchChain(false);
  if (typeof showDashboardButton === "function") showDashboardButton();
  // Browsing a site is reading, not scanning: the capture cards stay out of the way.
  if (treePick) {
    orderCard = "stored-po";
    $("list-review").hidden = true;
  }
  showCard(orderCard);
  for (const node of document.querySelectorAll(".tree button[data-goes]")) {
    node.setAttribute("aria-current", String(node.dataset.goes === name));
  }
  try {
    localStorage.setItem("tab", name);
  } catch {
  }
}

const treeToggle = $("tree-toggle");
treeToggle.addEventListener("click", () => {
  const open = treeToggle.getAttribute("aria-expanded") === "true";
  treeToggle.setAttribute("aria-expanded", String(!open));
  $("tree-list").hidden = open;
  try {
    localStorage.setItem("tree-open", String(!open));
  } catch {
  }
});
try {
  if (localStorage.getItem("tree-open") === "false") treeToggle.click();
} catch {
}

// A card of another screen, by its id where it has one, by its heading otherwise. The
// id, because a heading is the sort of thing that gets reworded.
function openCard(menu, key) {
  const spec = ALL_MENUS.find((one) => one.menu === menu);
  const cards = spec ? menuCards(spec) : [];
  const card = cards.find((one) => one.id === key) ?? cards.find((one) => cardName(one) === key);
  if (!card) return;
  if (spec.go) spec.go(); else openTab(spec.tab);
  showOnly(spec, card);
}

$("tree-new-quote").addEventListener("click", () => {
  treePick = null;
  openTab("quotes");
  $("quote-supplier").focus();
  say("Quotes · ask for a quote");
});

$("tree-plutus").addEventListener("click", () => {
  treePick = null;
  openCard("tools-menu", "plutus-card");
  say("DDMRP · Plutus");
});

for (const node of document.querySelectorAll(".tree button[data-goes]")) {
  node.addEventListener("click", () => {
    treePick = null;
    renderSiteTree();
    renderDeliveries();
    renderOrders();
    openTab(node.dataset.goes);
    if (node.dataset.goes === "ddmrp") {
      loadDdmrp();
      showLanding(ALL_MENUS.find((one) => one.menu === "tools-menu"));
    }
    say("Ready");
  });
}

function say(message, kind = "") {
  // The status line can be off the page; the screens say it themselves then.
  const field = $("status-text");
  if (!field) return;
  field.textContent = message;
  field.style.color = kind === "bad" ? "#b00020" : "";
}

$("menu-route").addEventListener("click", () => { openTab("route"); loadRoute(); });
let chainWatch = null;

function watchChain(on) {
  // The round is only worth redrawing while somebody is looking at it.
  clearInterval(chainWatch);
  chainWatch = on ? setInterval(loadChain, 4000) : null;
}

function openChain() {
  openTab("chain");
  loadChain();
  watchChain(true);
}
$("menu-goto").addEventListener("click", () => openTab("query"));
$("menu-help").addEventListener("click", () => { window.location.href = "/license"; });

function openPredictions() {
  openTab("ddmrp");
  loadDdmrp();
}

try {
  const saved = localStorage.getItem("tab");
  if (saved && TABS.includes(saved)) openTab(saved);
} catch {
}
