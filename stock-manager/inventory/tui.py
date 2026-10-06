"""The program itself: a full screen terminal app, green on black, keyboard driven."""

import asyncio
import curses
import hashlib
import mimetypes
import time
from datetime import date
from pathlib import Path

from . import alerts, audit, backup, catalog, db, llm, models_info, vocabulary
from .models import Note, Line, parse_date
from .screen import BRIGHT, DIM, INVERT, KEY, NORMAL, TAB, TAB_ON, WARN, Screen, columns, pad, truncate

TABS = ("NEW NOTE", "INVENTORY", "SQL", "PURCHASE ORDERS", "SETTINGS")
FKEYS = (("F1", "SQL"), ("F2", "RUN"), ("F3", "INVENTORY"), ("F4", "ORDERS"),
         ("F5", "EXPORT CSV"), ("F6", "EXPORT PARQUET"), ("F7", "QUIT"))
IMAGE_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
               ".webp": "image/webp", ".pdf": "application/pdf"}
NOTE_FIELDS = (("supplier", "SUPPLIER"), ("delivery_date", "DELIVERY DATE"),
               ("order_date", "ORDER DATE"), ("site", "SITE"), ("work_item", "WORK ITEM"))
LIST_FIELDS = (("list_date", "DATE"), ("site", "SITE"), ("work_item", "WORK ITEM"),
               ("drafter", "DRAFTER"))


def read_image(path):
    path = Path(path).expanduser()
    if not path.is_file():
        raise ValueError(f"no such file: {path}")
    media = IMAGE_TYPES.get(path.suffix.lower()) or mimetypes.guess_type(str(path))[0]
    if media not in IMAGE_TYPES.values():
        raise ValueError(f"unsupported file: {path.suffix or path.name}")
    return path.read_bytes(), media


class Prompt:
    def __init__(self, label, value, done, hint=""):
        self.label = label
        self.value = value or ""
        self.done = done
        self.hint = hint

    def key(self, code, char):
        if code in (curses.KEY_BACKSPACE, 127, 8):
            self.value = self.value[:-1]
        elif code == 21:
            self.value = ""
        elif code in (curses.KEY_ENTER, 10, 13):
            return "accept"
        elif code == 27:
            return "cancel"
        elif char and char.isprintable():
            self.value += char
        return None


class App:
    def __init__(self, config):
        self.config = config
        self.tab = 0
        self.status = ""
        self.running = False
        self.busy = ""
        self.prompt = None
        self.notes = []
        self.rows = []
        self.sheets = []
        self.draft = None
        self.draft_sha = None
        self.draft_proposed = None
        self.draft_sel = 0
        self.sheet_draft = None
        self.sheet_sha = None
        self.sheet_sel = 0
        self.search = ""
        self.only_backorders = False
        self.search_sel = 0
        self.question = ""
        self.answer = None
        self.sheet_cursor = 0
        self.line_cursor = 0
        self.settings_sel = 0
        self.backend = None
        self.painter = lambda: None

    def start(self):
        db.init(self.config.db_path)
        self.config.images_dir.mkdir(parents=True, exist_ok=True)
        audit.seen(self.config.db_path, self.config.user)
        self.reload()
        self.status = f"{self.config.data_dir}"

    def reload(self):
        self.notes = db.notes_with_lines(self.config.db_path)
        self.rows = db.inventory_rows(self.config.db_path)
        self.sheets = db.purchase_orders(self.config.db_path)

    def model(self):
        if self.backend is None:
            self.backend = llm.make_backend(self.config)
        return self.backend

    def say(self, text):
        self.status = text

    def event(self, action, target=None, detail=None):
        audit.record(self.config.db_path, self.config.user, action, target, detail, "terminal")

    def report(self, kind, description, context=None):
        return alerts.report(self.config, kind, description, context, self.config.user)

    def filtered(self):
        needle = self.search.upper().strip()
        out = []
        for row in self.rows:
            if self.only_backorders and not row["backorder"]:
                continue
            if needle:
                hay = " ".join(str(row.get(k) or "") for k in
                               ("article", "designation", "supplier", "site", "work_item")).upper()
                if needle not in hay:
                    continue
            out.append(row)
        return out

    def ask_path(self, label, done):
        self.prompt = Prompt(label, "", done, "path to a photo or PDF")

    def read_note(self, path):
        try:
            data, media = read_image(path)
        except ValueError as e:
            return self.say(str(e))
        sha = hashlib.sha256(data).hexdigest()
        existing = db.find_by_image(self.config.db_path, sha)
        if existing is not None:
            return self.say(f"already stored as note #{existing}")
        known = vocabulary.known_articles(self.config.db_path)
        started = time.monotonic()
        self.busy = "reading the note, 10 to 40 seconds..."
        self.painter()
        try:
            note = asyncio.run(self.model().extract(data, media, known))
        except llm.ExtractionError as e:
            return self.say(str(e))
        except Exception as e:
            reported = self.report("extract", f"note reading failed: {e}", str(path))
            return self.say(f"reading failed (error #{reported['error_no']}): {e}")
        finally:
            self.busy = ""
        note = catalog.apply(self.config.db_path, note)
        (self.config.images_dir / f"{sha}{Path(path).suffix.lower()}").write_bytes(data)
        self.draft = note
        self.draft_proposed = note.model_copy(deep=True)
        self.draft_sha = sha
        self.draft_sel = 0
        self.say(f"read in {time.monotonic() - started:.0f} s, check it, then S to save")

    def save_note(self):
        problems = self.draft.problems()
        if problems:
            return self.say("to fix: " + ", ".join(problems))
        matches = list(self.config.images_dir.glob(f"{self.draft_sha}.*")) if self.draft_sha else []
        measurement = measure_note(self.draft_proposed, self.draft)
        note_id = db.save_note(self.config.db_path, self.draft, self.draft_sha or None,
                               str(matches[0]) if matches else "", self.config.user)
        db.record_reading(self.config.db_path, "note", *measurement)
        self.event("note.save", f"note #{note_id}",
                   f"{self.draft.supplier} - {len(self.draft.lines)} line(s)")
        self.draft = self.draft_proposed = self.draft_sha = None
        self.reload()
        self.say(f"note #{note_id} saved")

    def read_sheet(self, path):
        try:
            data, media = read_image(path)
        except ValueError as e:
            return self.say(str(e))
        sha = hashlib.sha256(data).hexdigest()
        if db.find_order_by_image(self.config.db_path, sha) is not None:
            return self.say("that sheet is already stored")
        known = vocabulary.known_articles(self.config.db_path)
        self.busy = "reading the sheet, handwriting takes a minute or two..."
        self.painter()
        try:
            sheet = asyncio.run(self.model().extract_list(data, media, known))
        except llm.ExtractionError as e:
            return self.say(str(e))
        except Exception as e:
            reported = self.report("extract", f"list reading failed: {e}", str(path))
            return self.say(f"reading failed (error #{reported['error_no']}): {e}")
        finally:
            self.busy = ""
        (self.config.images_dir / f"{sha}{Path(path).suffix.lower()}").write_bytes(data)
        self.sheet_draft = sheet
        self.sheet_sha = sha
        self.sheet_sel = 0
        self.say("check the sheet, then S to save")

    def save_sheet(self):
        sheet = dict(self.sheet_draft)
        lines = [l for l in sheet.get("lines", []) if (l.get("article") or "").strip()]
        if not lines:
            return self.say("to fix: no line")
        sheet["lines"] = lines
        sheet["list_date"] = parse_date(sheet.get("list_date")) if sheet.get("list_date") else None
        matches = list(self.config.images_dir.glob(f"{self.sheet_sha}.*")) if self.sheet_sha else []
        list_id = db.save_purchase_order(self.config.db_path, sheet, self.sheet_sha or None,
                                        str(matches[0]) if matches else "", self.config.user)
        self.event("order.save", f"order #{list_id}", f"{sheet.get('site') or 'no site'} - "
                                                    f"{len(lines)} line(s)")
        self.sheet_draft = self.sheet_sha = None
        self.reload()
        self.say(f"order #{list_id} saved")

    def run_question(self, text):
        text = text.strip()
        if not text:
            return
        self.question = text
        direct = text.lower().startswith(("select", "with"))
        if not direct:
            self.busy = "asking the model for the SQL..."
            self.painter()
        try:
            if direct:
                sql, note = text, None
            else:
                sql, note = asyncio.run(self.model().to_sql(text))
            if not sql:
                self.answer = None
                return self.say(note or "that cannot be answered from this data")
            result = db.run_readonly(self.config.db_path, sql)
        except llm.ExtractionError as e:
            return self.say(str(e))
        except db.QueryError as e:
            self.answer = None
            return self.say(str(e))
        finally:
            self.busy = ""
        self.answer = {"sql": sql, "note": note, "columns": result.columns, "rows": result.rows,
                       "truncated": result.truncated}
        self.event("sql" if direct else "question", None, text[:200])
        self.say(f"{len(result.rows)} row(s)")

    def export(self, dataset, fmt):
        out_dir = self.config.data_dir / "exports"
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"{dataset}.{fmt}"
        db.export_to_file(self.config.db_path, dataset, out)
        self.event("export", out.name)
        self.say(f"written: {out}")

    def run_backup(self):
        try:
            path = backup.snapshot(self.config)
        except Exception as e:
            reported = self.report("backup", f"backup failed: {e}", "backup")
            return self.say(f"backup failed (error #{reported['error_no']}): {e}")
        self.event("backup.run", path.name)
        self.say(f"backup written: {path}")

    def test_message(self):
        result = self.report("test", f"test message requested by {self.config.user}", "settings")
        self.event("error.test", f"error #{result['error_no']}", result["sms_status"])
        self.say(f"error #{result['error_no']}, message: {result['sms_status']}")

    def merge_names(self, alias, article):
        try:
            result = vocabulary.merge(self.config.db_path, alias, article, self.config.user)
        except vocabulary.VocabularyError as e:
            return self.say(str(e))
        self.event("vocabulary.merge", result["article"],
                   f"{result['alias']} -> {result['article']}")
        self.reload()
        self.say(f"{result['alias']} is now {result['article']}, "
                 f"{result['inventory']} delivery line(s) rewritten")


def measure_note(before, after):
    if before is None:
        return (0, 0, 0)
    fields = corrected = 0

    def compare(a, b):
        nonlocal fields, corrected
        fields += 1
        left = None if a in (None, "") else str(a).strip().lower()
        right = None if b in (None, "") else str(b).strip().lower()
        if left != right:
            corrected += 1

    for name, _ in NOTE_FIELDS:
        compare(getattr(before, name), getattr(after, name))
    for i in range(max(len(before.lines), len(after.lines))):
        if i >= len(before.lines) or i >= len(after.lines):
            fields += 3
            corrected += 3
            continue
        compare(before.lines[i].article, after.lines[i].article)
        compare(before.lines[i].quantity, after.lines[i].quantity)
        compare(before.lines[i].backorder, after.lines[i].backorder)
    return (fields, corrected, len(after.lines))


def note_rows(draft):
    rows = [(name, label, getattr(draft, name) or "") for name, label in NOTE_FIELDS]
    for i, line in enumerate(draft.lines):
        rows.append((f"line:{i}", f"[{i + 1}]", line))
    return rows


def sheet_rows(sheet):
    rows = [(name, label, sheet.get(name) or "") for name, label in LIST_FIELDS]
    for i, line in enumerate(sheet.get("lines", [])):
        rows.append((f"line:{i}", f"[{i + 1}]", line))
    return rows


def draw_header(screen, app):
    screen.put(0, 2, "STOCK MANAGER", BRIGHT)
    screen.put(0, 18, f"{len(app.notes)} notes - {len(app.rows)} lines", DIM)
    screen.right(0, screen.width - 2, f"user {app.config.user}", DIM)
    screen.rule(1, 2, screen.width - 4)
    col = 2
    for index, name in enumerate(TABS):
        label = f"[{name}]"
        screen.put(2, col, label, TAB_ON if index == app.tab else TAB)
        col += len(label) + 2


def draw_table(screen, top, width, headers, widths, rows, selected=None, height=10):
    screen.put(top, 2, columns(widths, headers), BRIGHT)
    screen.rule(top + 1, 2, width - 4)
    start = 0
    if selected is not None and selected >= height:
        start = selected - height + 1
    for offset, row in enumerate(rows[start:start + height]):
        index = start + offset
        style = INVERT if selected is not None and index == selected else NORMAL
        screen.put(top + 2 + offset, 2, pad(columns(widths, row), width - 4), style)
    return top + 2 + min(len(rows), height)


def draw_new_note(screen, app, top, width, height):
    if app.draft is None:
        screen.put(top, 2, "R  read a photo of a delivery note", BRIGHT)
        screen.put(top + 1, 2, "the model reads it, you check it, S saves it", DIM)
        rows = [[str(n["id"]), n["supplier"] or "", n["delivery_date"] or "",
                 n["site"] or "-", str(len(n["lines"])), n["confirmed_by"] or "-"]
                for n in app.notes[:height - 6]]
        draw_table(screen, top + 3, width, ["#", "SUPPLIER", "DELIVERED", "SITE", "LINES", "BY"],
                   [-4, 26, 12, 20, -6, 10], rows, height=height - 6)
        return
    rows = note_rows(app.draft)
    screen.put(top, 2, "STEP 2 OF 2 - CORRECT, THEN SAVE", BRIGHT)
    line_top = top + 1
    for warning in app.draft.warnings[:2]:
        screen.put(line_top, 2, truncate(f"check: {warning}", width - 4), WARN)
        line_top += 1
    row_top = line_top + 1
    visible = height - (row_top - top) - 2
    start = max(0, app.draft_sel - visible + 1) if app.draft_sel >= visible else 0
    for offset, (key, label, value) in enumerate(rows[start:start + visible]):
        index = start + offset
        marker = ">" if index == app.draft_sel else " "
        style = BRIGHT if index == app.draft_sel else NORMAL
        if key.startswith("line:"):
            line = value
            text = columns([4, 34, -8, 12], [label, line.article or "",
                                             "" if line.quantity is None else f"{line.quantity:g}",
                                             "backorder" if line.backorder else ""])
            screen.put(row_top + offset, 2, f"{marker} {text}", style)
            if line.designation:
                screen.right(row_top + offset, width - 2,
                             truncate(line.designation, max(10, width - 70)), DIM)
        else:
            screen.put(row_top + offset, 2,
                       f"{marker} {pad(label, 14)} {truncate(value, width - 24)}", style)


def draw_search(screen, app, top, width, height):
    rows = app.filtered()
    units = sum(row["quantity"] or 0 for row in rows)
    backorders = sum(1 for row in rows if row["backorder"])
    screen.put(top, 2, f"/ filter: {app.search or '(all)'}", BRIGHT)
    screen.right(top, width - 2, f"B backorders only: {'yes' if app.only_backorders else 'no'}", DIM)
    screen.put(top + 1, 2, f"{len(rows)} line(s) - {units:,.0f} units - "
                           f"{backorders} on backorder", DIM)
    app.search_sel = max(0, min(app.search_sel, max(0, len(rows) - 1)))
    table = [[row["article"], f"{row['quantity']:g}" if row["quantity"] is not None else "",
              row["supplier"], row["delivery_date"] or "", row["site"] or "-",
              row["work_item"] or "-", "BO" if row["backorder"] else ""]
             for row in rows]
    free = max(30, width - 4 - 12 - 7 - 11 - 2 - 16 - 14)
    widths = [int(free * 0.6), -7, 16, 11, 14, free - int(free * 0.6), -2]
    draw_table(screen, top + 3, width,
               ["ARTICLE", "QTY", "SUPPLIER", "DELIVERED", "SITE", "WORK ITEM", ""],
               widths, table, app.search_sel, height - 6)


def draw_sql(screen, app, top, width, height):
    screen.put(top, 2, "ENTER  ask a question in plain words, or type SQL", BRIGHT)
    screen.put(top + 1, 2, truncate(app.question or "(nothing asked yet)", width - 4), DIM)
    if not app.answer:
        for offset, example in enumerate((
                "how many units per supplier last month?",
                "which articles are still on backorder?",
                "what was ordered but never delivered?")):
            screen.put(top + 3 + offset, 4, f"? {example}", DIM)
        return
    row = top + 3
    for sql_line in app.answer["sql"].splitlines()[:4]:
        screen.put(row, 4, truncate(sql_line, width - 8), NORMAL)
        row += 1
    columns_ = app.answer["columns"]
    widths = [max(10, int((width - 6) / max(1, len(columns_)))) for _ in columns_]
    table = [[format_cell(value) for value in line] for line in app.answer["rows"]]
    draw_table(screen, row + 1, width, [c.upper() for c in columns_], widths, table,
               height=height - (row - top) - 4)


def format_cell(value):
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:g}"
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def draw_orders(screen, app, top, width, height):
    if app.sheet_draft is not None:
        screen.put(top, 2, "CHECK THE SHEET, THEN S TO SAVE", BRIGHT)
        rows = sheet_rows(app.sheet_draft)
        for offset, (key, label, value) in enumerate(rows[:height - 4]):
            marker = ">" if offset == app.sheet_sel else " "
            style = BRIGHT if offset == app.sheet_sel else NORMAL
            if key.startswith("line:"):
                quantity = value.get("quantity")
                text = columns([4, 34, -8], [label, value.get("article") or "",
                                             "" if quantity is None else f"{quantity:g}"])
                screen.put(top + 2 + offset, 2, f"{marker} {text}", style)
            else:
                screen.put(top + 2 + offset, 2,
                           f"{marker} {pad(label, 12)} {truncate(value, width - 22)}", style)
        return

    screen.put(top, 2, "R  read a purchase order sheet", BRIGHT)
    screen.put(top + 1, 2, "S in stock   O to order   C clear   D delete sheet", DIM)
    row = top + 3
    for index, sheet in enumerate(app.sheets):
        if row >= top + height - 2:
            break
        chosen = index == app.sheet_cursor
        ticked = sum(1 for line in sheet["lines"] if line["in_stock"] is not None)
        head = (f"{sheet['site'] or 'no site'}  {sheet['work_item'] or '-'}  "
                f"{sheet['list_date'] or ''}  #{sheet['id']}  "
                f"{ticked}/{len(sheet['lines'])} ticked")
        screen.put(row, 2, truncate(head, width - 4), BRIGHT if chosen else DIM)
        row += 1
        for line_index, line in enumerate(sheet["lines"]):
            if row >= top + height - 2:
                break
            state = {True: "in stock", False: "to order", None: "-"}[line["in_stock"]]
            marker = ">" if chosen and line_index == app.line_cursor else " "
            style = INVERT if chosen and line_index == app.line_cursor else NORMAL
            quantity = line.get("quantity")
            text = columns([4, 34, -8, 10],
                           [str(line["line_no"]), line["article"],
                            "" if quantity is None else f"{quantity:g}", state])
            screen.put(row, 2, pad(f"{marker} {text}", width - 4), style)
            row += 1
        row += 1


def draw_settings(screen, app, top, width, height):
    info = db.storage(app.config.db_path, app.config.data_dir, app.config.images_dir)
    accuracy = db.accuracy(app.config.db_path)
    names = vocabulary.vocabulary(app.config.db_path)
    shared = sum(1 for entry in names if entry["shared"])
    people = audit.users(app.config.db_path)
    failures = alerts.recent(app.config.db_path, 3)
    snapshots = backup.list_backups(app.config.data_dir)

    screen.put(top, 2, "B backup now   T test message   M merge two names   "
                       "E export   P purge photos", DIM)
    row = top + 2
    screen.put(row, 2, "STORAGE", BRIGHT)
    screen.put(row + 1, 4, f"{app.config.data_dir}")
    screen.put(row + 2, 4, f"database {info['database_bytes'] / 1e6:.1f} MB   "
                           f"photos {info['photo_bytes'] / 1e6:.1f} MB "
                           f"({info['photo_count']} files)   "
                           f"free {info['disk_free_bytes'] / 1e9:.1f} GB")
    screen.put(row + 3, 4, f"{info['rows']['notes']} notes   "
                           f"{info['rows']['inventory_lines']} delivery lines   "
                           f"{info['rows']['purchase_orders']} sheets   "
                           f"photos kept {app.config.photo_retention_days} days", DIM)

    row += 5
    screen.put(row, 2, "READING ACCURACY", BRIGHT)
    if accuracy["accuracy"] is None:
        screen.put(row + 1, 4, "nothing measured yet", DIM)
    else:
        bars = "".join("#" if (day["accuracy"] or 0) >= 90 else "+"
                       for day in accuracy["series"][-24:])
        screen.put(row + 1, 4, f"{accuracy['accuracy']}% over {accuracy['documents']} document(s), "
                               f"{accuracy['corrected']}/{accuracy['fields']} fields corrected")
        screen.put(row + 2, 4, bars or "-", DIM)

    row += 4
    screen.put(row, 2, "ARTICLE VOCABULARY", BRIGHT)
    screen.put(row + 1, 4, f"{len(names)} name(s), {shared} used on both notes and sheets", DIM)
    for offset, entry in enumerate(names[:3]):
        screen.put(row + 2 + offset, 4,
                   truncate(f"{pad(entry['article'], 34)} notes {entry['inventory']:<4} "
                            f"sheets {entry['orders']:<4} "
                            f"{('= ' + ', '.join(entry['aliases'])) if entry['aliases'] else ''}",
                            width - 6))

    row += 6
    screen.put(row, 2, "USERS", BRIGHT)
    for offset, person in enumerate(people[:2]):
        screen.put(row + 1 + offset, 4,
                   f"{pad(person['user'], 16)} {person['notes']} notes  {person['orders']} sheets  "
                   f"{person['corrections']} corrections  "
                   f"last {(person['last_seen'] or '-').replace('T', ' ')[:16]}")

    row += 4
    screen.put(row, 2, "MODEL", BRIGHT)
    for offset, provider in enumerate(models_info.status(app.config.backend)):
        mark = {"ready": "*", "configured": "+", "missing": "-", "unsupported": "-"}[provider["state"]]
        screen.put(row + 1 + offset, 4,
                   truncate(f"{mark} {pad(provider['name'], 18)} {provider['detail']}", width - 6),
                   BRIGHT if provider["current"] else DIM)

    row += 5
    screen.put(row, 2, "ERRORS AND BACKUPS", BRIGHT)
    destination = alerts.destination(app.config.sms)
    screen.put(row + 1, 4, f"texting {destination['to'] or 'nobody'} "
                           f"({destination['provider']})", DIM)
    for offset, failure in enumerate(failures[:2]):
        screen.put(row + 2 + offset, 4,
                   truncate(f"#{failure['error_no']} {failure['kind']}: "
                            f"{failure['description']}  [{failure['sms_status']}]", width - 6))
    screen.put(row + 4, 4, f"{len(snapshots)} snapshot(s)" +
               (f", latest {snapshots[0]['date']}" if snapshots else ""), DIM)


DRAW = (draw_new_note, draw_search, draw_sql, draw_orders, draw_settings)


def render(app, width, height):
    screen = Screen(width, height)
    draw_header(screen, app)
    body_top = 4
    body_height = height - body_top - 3
    DRAW[app.tab](screen, app, body_top, width, body_height)

    screen.rule(height - 3, 2, width - 4)
    col = 2
    for key, label in FKEYS:
        screen.put(height - 2, col, key, INVERT)
        screen.put(height - 2, col + len(key), f"={label}", KEY)
        col += len(key) + len(label) + 3
    if app.prompt:
        text = f"{app.prompt.label}: {app.prompt.value}"
        screen.put(height - 1, 2, truncate(text, width - 4), BRIGHT)
        screen.put(height - 1, 2 + len(truncate(text, width - 4)), "|", BRIGHT)
    else:
        screen.put(height - 1, 2, truncate(app.busy or app.status, width - 4),
                   WARN if app.busy else DIM)
    return screen


def set_field(app, name, value):
    value = value.strip()
    if name.startswith("line:"):
        return
    setattr(app.draft, name, value or None)


def edit_selected(app):
    rows = note_rows(app.draft)
    key, label, value = rows[app.draft_sel]
    if key.startswith("line:"):
        index = int(key.split(":")[1])
        line = app.draft.lines[index]
        app.prompt = Prompt(f"article {index + 1}", line.article or "",
                            lambda text, i=index: setattr(app.draft.lines[i], "article", text.strip()))
    else:
        app.prompt = Prompt(label.lower(), value,
                            lambda text, name=key: set_field(app, name, text))


def edit_quantity(app):
    rows = note_rows(app.draft)
    key, _, _ = rows[app.draft_sel]
    if not key.startswith("line:"):
        return
    index = int(key.split(":")[1])
    current = app.draft.lines[index].quantity

    def done(text):
        text = text.strip().replace(",", ".")
        if not text:
            app.draft.lines[index].quantity = None
            return
        try:
            app.draft.lines[index].quantity = float(text)
        except ValueError:
            app.say("that is not a number")

    app.prompt = Prompt(f"quantity {index + 1}", "" if current is None else f"{current:g}", done)


def handle_new_note(app, code, char):
    if app.draft is None:
        if char in ("r", "R"):
            app.ask_path("photo", lambda text: app.read_note(text))
        return
    rows = note_rows(app.draft)
    if code == curses.KEY_UP:
        app.draft_sel = max(0, app.draft_sel - 1)
    elif code == curses.KEY_DOWN:
        app.draft_sel = min(len(rows) - 1, app.draft_sel + 1)
    elif code in (curses.KEY_ENTER, 10, 13):
        edit_selected(app)
    elif char in ("q", "Q"):
        edit_quantity(app)
    elif char in ("b", "B"):
        key, _, _ = rows[app.draft_sel]
        if key.startswith("line:"):
            line = app.draft.lines[int(key.split(":")[1])]
            line.backorder = not line.backorder
    elif char in ("a", "A"):
        app.draft.lines.append(Line(article="", quantity=None))
        app.draft_sel = len(note_rows(app.draft)) - 1
    elif char in ("d", "D"):
        key, _, _ = rows[app.draft_sel]
        if key.startswith("line:"):
            del app.draft.lines[int(key.split(":")[1])]
            app.draft_sel = min(app.draft_sel, len(note_rows(app.draft)) - 1)
    elif char in ("s", "S"):
        app.save_note()
    elif char in ("x", "X"):
        app.draft = app.draft_proposed = app.draft_sha = None
        app.say("discarded")


def handle_search(app, code, char):
    rows = app.filtered()
    if code == curses.KEY_UP:
        app.search_sel = max(0, app.search_sel - 1)
    elif code == curses.KEY_DOWN:
        app.search_sel = min(max(0, len(rows) - 1), app.search_sel + 1)
    elif code == curses.KEY_NPAGE:
        app.search_sel = min(max(0, len(rows) - 1), app.search_sel + 10)
    elif code == curses.KEY_PPAGE:
        app.search_sel = max(0, app.search_sel - 10)
    elif char == "/":
        app.prompt = Prompt("filter", app.search, lambda text: setattr(app, "search", text.strip()))
    elif char in ("b", "B"):
        app.only_backorders = not app.only_backorders
    elif char in ("e", "E") and rows:
        row = rows[app.search_sel]

        def done(text):
            value = text.strip() or None
            if db.update_work_item(app.config.db_path, row["note_id"], row["line_no"], value):
                app.event("delivery.edit", f"note #{row['note_id']} line {row['line_no']}",
                          f"work_item = {value or 'empty'}")
                app.reload()
                app.say("work item saved")

        app.prompt = Prompt(f"work item for {row['article']}", row["work_item"] or "", done)


def handle_sql(app, code, char):
    if code in (curses.KEY_ENTER, 10, 13) or char in ("a", "A"):
        app.prompt = Prompt("question", app.question, lambda text: app.run_question(text))


def handle_orders(app, code, char):
    if app.sheet_draft is not None:
        rows = sheet_rows(app.sheet_draft)
        lines = app.sheet_draft.setdefault("lines", [])
        if code == curses.KEY_UP:
            app.sheet_sel = max(0, app.sheet_sel - 1)
        elif code == curses.KEY_DOWN:
            app.sheet_sel = min(len(rows) - 1, app.sheet_sel + 1)
        elif code in (curses.KEY_ENTER, 10, 13):
            key, label, value = rows[app.sheet_sel]
            if key.startswith("line:"):
                index = int(key.split(":")[1])
                app.prompt = Prompt(f"article {index + 1}", lines[index].get("article") or "",
                                    lambda text, i=index: lines[i].update(article=text.strip()))
            else:
                app.prompt = Prompt(label.lower(), str(value),
                                    lambda text, k=key: app.sheet_draft.update({k: text.strip() or None}))
        elif char in ("d", "D"):
            key, _, _ = rows[app.sheet_sel]
            if key.startswith("line:"):
                del lines[int(key.split(":")[1])]
                app.sheet_sel = min(app.sheet_sel, len(sheet_rows(app.sheet_draft)) - 1)
        elif char in ("s", "S"):
            app.save_sheet()
        elif char in ("x", "X"):
            app.sheet_draft = app.sheet_sha = None
            app.say("discarded")
        return

    if char in ("r", "R"):
        app.ask_path("photo of a sheet", lambda text: app.read_sheet(text))
        return
    if not app.sheets:
        return
    sheet = app.sheets[min(app.sheet_cursor, len(app.sheets) - 1)]
    lines = sheet["lines"]
    if code == curses.KEY_DOWN:
        app.line_cursor += 1
        if app.line_cursor >= len(lines):
            app.line_cursor = 0
            app.sheet_cursor = (app.sheet_cursor + 1) % len(app.sheets)
    elif code == curses.KEY_UP:
        app.line_cursor -= 1
        if app.line_cursor < 0:
            app.sheet_cursor = (app.sheet_cursor - 1) % len(app.sheets)
            app.line_cursor = max(0, len(app.sheets[app.sheet_cursor]["lines"]) - 1)
    elif char in ("s", "S", "o", "O", "c", "C") and lines:
        line = lines[min(app.line_cursor, len(lines) - 1)]
        value = {"s": True, "o": False, "c": None}[char.lower()]
        if db.set_in_stock(app.config.db_path, sheet["id"], line["line_no"], value):
            app.event("order.tick", f"list #{sheet['id']} line {line['line_no']}",
                      {True: "in stock", False: "to order", None: "not ticked"}[value])
            app.reload()
    elif char in ("d", "D"):
        def done(text):
            if text.strip().lower() in ("y", "yes"):
                db.delete_purchase_order(app.config.db_path, sheet["id"])
                app.event("order.delete", f"list #{sheet['id']}")
                app.reload()
                app.say(f"list #{sheet['id']} deleted")

        app.prompt = Prompt(f"delete list #{sheet['id']}? (y/n)", "", done)


def handle_settings(app, code, char):
    if char in ("b", "B"):
        app.run_backup()
    elif char in ("t", "T"):
        app.test_message()
    elif char in ("p", "P"):
        result = db.purge_photos(app.config.db_path, app.config.images_dir,
                                 app.config.photo_retention_days)
        app.say(f"{result['files']} photo(s) deleted, {result['bytes'] / 1e6:.1f} MB freed")
    elif char in ("e", "E"):
        def done(text):
            dataset = text.strip() or "inventory"
            if dataset not in db.EXPORTS:
                return app.say(f"unknown dataset: {dataset} ({', '.join(db.EXPORTS)})")
            app.export(dataset, "csv")

        app.prompt = Prompt(f"export which ({', '.join(db.EXPORTS)})", "inventory_lines", done)
    elif char in ("m", "M"):
        def second(alias):
            def done(article):
                app.merge_names(alias, article)

            app.prompt = Prompt(f"keep which name instead of {alias.upper()}", "", done)

        app.prompt = Prompt("merge which name", "", second)


HANDLE = (handle_new_note, handle_search, handle_sql, handle_orders, handle_settings)


def handle(app, code, char):
    if app.prompt:
        outcome = app.prompt.key(code, char)
        if outcome == "accept":
            prompt, app.prompt = app.prompt, None
            prompt.done(prompt.value)
        elif outcome == "cancel":
            app.prompt = None
        return

    if code == curses.KEY_F7:
        app.running = False
    elif code == curses.KEY_F1:
        app.tab = 2
    elif code == curses.KEY_F2:
        app.tab = 2
        app.prompt = Prompt("question", app.question, lambda text: app.run_question(text))
    elif code == curses.KEY_F3:
        app.tab = 1
    elif code == curses.KEY_F4:
        app.tab = 3
    elif code == curses.KEY_F5:
        app.export("inventory", "csv")
    elif code == curses.KEY_F6:
        app.export("inventory", "parquet")
    elif code == 9:
        app.tab = (app.tab + 1) % len(TABS)
    elif code == curses.KEY_BTAB:
        app.tab = (app.tab - 1) % len(TABS)
    elif char and char.isdigit() and 1 <= int(char) <= len(TABS):
        app.tab = int(char) - 1
    else:
        HANDLE[app.tab](app, code, char)


def paint(stdscr, app, colors):
    height, width = stdscr.getmaxyx()
    screen = render(app, width, max(10, height))
    stdscr.erase()
    for row, cells in enumerate(screen.cells[:height]):
        line = "".join(char for char, _ in cells)[:width - 1]
        attributes = [colors[style] for _, style in cells[:len(line)]]
        for col, char in enumerate(line):
            try:
                stdscr.addstr(row, col, char, attributes[col])
            except curses.error:
                pass
    stdscr.refresh()


def make_colors():
    curses.start_color()
    curses.use_default_colors()
    pairs = {NORMAL: (curses.COLOR_GREEN, -1), DIM: (curses.COLOR_GREEN, -1),
             BRIGHT: (curses.COLOR_GREEN, -1), INVERT: (curses.COLOR_BLACK, curses.COLOR_GREEN),
             TAB: (curses.COLOR_BLUE, -1), TAB_ON: (curses.COLOR_BLACK, curses.COLOR_BLUE),
             KEY: (curses.COLOR_WHITE, -1), WARN: (curses.COLOR_YELLOW, -1)}
    colors = {}
    for index, (style, (front, back)) in enumerate(pairs.items(), 1):
        curses.init_pair(index, front, back)
        attribute = curses.color_pair(index)
        if style in (BRIGHT, INVERT, TAB_ON, KEY):
            attribute |= curses.A_BOLD
        if style == DIM:
            attribute |= curses.A_DIM
        colors[style] = attribute
    return colors


def loop(stdscr, app):
    curses.curs_set(0)
    stdscr.keypad(True)
    colors = make_colors()
    app.running = True
    app.painter = lambda: paint(stdscr, app, colors)
    while app.running:
        paint(stdscr, app, colors)
        try:
            key = stdscr.get_wch()
        except curses.error:
            continue
        except KeyboardInterrupt:
            break
        if isinstance(key, str):
            code, char = ord(key), key
        else:
            code, char = key, None
        if app.prompt is None and char in ("\x11", "\x03"):
            break
        handle(app, code, char)


def run(config):
    app = App(config)
    app.start()
    curses.wrapper(loop, app)
    return 0
