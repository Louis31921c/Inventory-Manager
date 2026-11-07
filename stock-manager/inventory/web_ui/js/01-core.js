"use strict";

const missingBoxes = new Set();
const $ = (id) => {
  const found = document.getElementById(id);
  if (found) return found;
  // This page gets edited by hand. A box that has been taken out must not stop
  // every screen below it from being wired up, so hand back a throwaway one.
  if (!missingBoxes.has(id)) {
    missingBoxes.add(id);
    console.warn(`No element #${id} on the page; whatever uses it does nothing.`);
  }
  return document.createElement("input");
};

function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key.includes("-")) node.setAttribute(key, value);
    else node[key] = value;
  }
  for (const child of children) node.append(child);
  return node;
}

const SVG = "http://www.w3.org/2000/svg";

function svg(tag, props = {}, children = []) {
  const node = document.createElementNS(SVG, tag);
  for (const [key, value] of Object.entries(props)) node.setAttribute(key, value);
  for (const child of children) node.append(child);
  return node;
}

const short = (text, size = 22) =>
  String(text ?? "").length > size ? `${String(text).slice(0, size - 1)}…` : String(text ?? "");

const DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];
const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;

const upper = (text) => String(text ?? "").toUpperCase().replace(/\s+/g, " ").trim();
const asNumber = (value) => (value === "" || value == null ? null : Number(value));
const showNum = (value) => (value == null || isNaN(value) ? "—" : Number(value).toLocaleString());
const showDate = (iso) => (ISO_DATE.test(iso ?? "") ? String(iso).slice(0, 10) : "");
const showTime = (iso) => (iso ? String(iso).replace("T", " ").slice(0, 16) : "—");
const mb = (bytes) => `${(bytes / 1e6).toFixed(1)} MB`;
const gb = (bytes) => `${(bytes / 1e9).toFixed(3)} GB`;

function weekdayOf(iso) {
  if (!ISO_DATE.test(iso ?? "")) return null;
  const [year, month, day] = iso.split("-").map(Number);
  return DAYS[new Date(Date.UTC(year, month - 1, day)).getUTCDay()];
}

function notice(target, kind, text) {
  target.replaceChildren(text ? el("p", { className: `notice ${kind}`, textContent: text }) : "");
  target.hidden = !text;
}

async function api(path, options) {
  const response = await fetch(path, options);
  let payload = null;
  try {
    payload = await response.json();
  } catch {
  }
  if (!response.ok) throw new Error(payload?.detail ?? `Error ${response.status}`);
  return payload;
}

const postJson = (body) => ({
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

const patchJson = (body) => ({ ...postJson(body), method: "PATCH" });
const putJson = (body) => ({ ...postJson(body), method: "PUT" });

const QUIET_TYPES = new Set(["number", "date", "password", "file", "checkbox", "radio",
                             "range", "color", "hidden"]);

function shout(event) {
  const box = event.target;
  if (box.tagName !== "INPUT" && box.tagName !== "TEXTAREA") return;
  if (QUIET_TYPES.has(box.type)) return;
  const loud = box.value.toUpperCase();
  if (loud === box.value) return;
  const start = box.selectionStart;
  const end = box.selectionEnd;
  box.value = loud;
  try {
    box.setSelectionRange(start, end);
  } catch {
  }
}

document.addEventListener("input", shout, true);

let notes = [];
let rows = [];
let lists = [];
let draft = null;
let draftFile = null;
let draftSha = null;
let proposed = null;
let listDraft = null;
let listFile = null;
let listSha = null;
let listProposed = null;

function flatten() {
  rows = notes.flatMap((note) =>
    (note.lines ?? []).map((line) => ({
      article: line.article ?? "",
      quantity: line.quantity == null ? null : Number(line.quantity),
      supplier: note.supplier ?? "",
      delivery_date: note.delivery_date ?? "",
      weekday: weekdayOf(note.delivery_date),
      order_date: note.order_date ?? null,
      site: note.site ?? null,
      work_item: line.work_item !== undefined ? line.work_item : (note.work_item ?? null),
      backorder: line.backorder ? 1 : 0,
      designation: line.designation ?? "",
      note_id: note.id,
      line_no: line.line_no ?? 1,
    })));
}
