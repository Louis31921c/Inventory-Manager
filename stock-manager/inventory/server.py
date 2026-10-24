import argparse
import asyncio
import csv
import hashlib
import html
import io
import json
import logging
import time
import mimetypes
import os
import secrets
import socket
import subprocess
import tempfile
from datetime import date
from pathlib import Path

import duckdb
import uvicorn
from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel

from dataclasses import replace

from . import (account, alerts, audit, backup, catalog, db, ddmrp, forecast, llm, models_info,
               pg, vocabulary)
from .config import load_config
from .models import Note, parse_date

log = logging.getLogger("inventory.web")

WEB = Path(__file__).resolve().parent / "web_ui"
PAGE = WEB / "app.html"
PREDICTIONS = WEB / "predictions.html"
LICENCE = WEB / "license.html"
UNLOCK = WEB / "unlock.html"
SUPPORTED = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
             "application/pdf": ".pdf"}
config = load_config()
app = FastAPI(title="Stock manager")
lock = account.Lock(config.data_dir)
backend = None
stopper = None
OPEN_PATHS = ("/unlock", "/robots.txt")
OPEN_PREFIXES = ("/fonts/",)
WRITING = ("POST", "PUT", "PATCH", "DELETE")
MAX_UPLOAD = 30 * 1024 * 1024

NOT_FOUND = HTMLResponse("<h1>404 Not Found</h1>", status_code=404)


def get_backend():
    global backend
    if backend is None:
        backend = llm.make_backend(config)
    return backend


def who(request):
    user = getattr(request.state, "user", None)
    address = request.client.host if request.client else "?"
    return user, address


def event(request, action, target=None, detail=None):
    user, address = who(request)
    audit.record(config.db_path, user, action, target, detail, address)


def report(request, kind, description, context=None):
    user = who(request)[0] if request is not None else None
    return alerts.report(config, kind, description, context, user)


@app.on_event("startup")
async def startup():
    db.init(config.db_path)
    config.images_dir.mkdir(parents=True, exist_ok=True)
    log.info("database: %s", config.db_path)
    log.info("error reports: %s", alerts.destination(config.sms))
    asyncio.create_task(_purge_loop())


async def _purge_loop():
    while True:
        try:
            result = await asyncio.to_thread(db.purge_photos, config.db_path, config.images_dir,
                                             config.photo_retention_days)
            if result["files"]:
                log.info("photo purge: %d file(s), %.1f MB freed (%d notes, %d lists)",
                         result["files"], result["bytes"] / 1e6, result["notes"], result["lists"])
        except Exception as e:
            log.exception("photo purge failed")
            await asyncio.to_thread(report, None, "purge", f"photo purge failed: {e}", "purge loop")
        await asyncio.sleep(24 * 3600)


@app.middleware("http")
async def require_unlock(request: Request, call_next):
    """Nothing is served until the password has been typed on this run."""
    path = request.url.path
    request.state.user = None
    if path in OPEN_PATHS or path.startswith(OPEN_PREFIXES):
        return await call_next(request)

    user = lock.read_session(request.cookies.get(account.COOKIE))
    if user is None:
        if path.startswith("/api/") or path.startswith("/export"):
            return JSONResponse({"detail": "Wrong ID or password"}, 401)
        return RedirectResponse("/unlock", status_code=303)

    request.state.user = user
    response = await call_next(request)
    response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    return response


def unlock_page(error="", user=""):
    setting_up = not lock.ready
    page = UNLOCK.read_text(encoding="utf-8")
    replacements = {
        "FIRST_LINE": "first run" if setting_up else "SIGN IN",
        "USER_VALUE": html.escape(user or lock.user or ""),
        "USER_STATE": "required autofocus" if setting_up else "required",
        "PASSWORD_MODE": "new-password" if setting_up else "current-password",
        "CONFIRM_ROW": ('<div class="row"><label for="again">again    : </label>'
                        '<input type="password" id="again" name="again" '
                        'autocomplete="new-password" required></div>') if setting_up else "",
        "BUTTON_LABEL": "create" if setting_up else "ENTER",
        "HINT": ("choose the name your changes are signed with, and a password of at least "
                 f"{account.MIN_LENGTH} characters" if setting_up else "awaiting"),
    }
    for key, value in replacements.items():
        page = page.replace(key, value)
    return HTMLResponse(page)


@app.get("/unlock", response_class=HTMLResponse)
def unlock_form():
    return unlock_page()


@app.post("/unlock")
def unlock(request: Request, password: str = Form(""), user: str = Form(""),
           again: str = Form("")):
    caller = request.client.host if request.client else "?"
    if not lock.ready:
        if password != again:
            return unlock_page("The two passwords don't match.", user)
        try:
            name = account.write_account(user, password)
        except ValueError as e:
            return unlock_page(str(e), user)
        audit.record(config.db_path, name, "account.create", None, "sign up", caller)
        return _unlocked(name)

    typed = (user or "").strip()
    if typed and typed.upper() != (lock.user or "").upper():
        audit.record(config.db_path, lock.user, "unlock.refused", None, "wrong user", caller)
        return unlock_page("Wrong user or password.", user)

    try:
        ok = lock.unlock(password)
    except account.TooManyAttempts as e:
        return unlock_page(str(e))
    if not ok:
        audit.record(config.db_path, lock.user, "unlock.refused", None, "wrong password", caller)
        return unlock_page("Wrong password.")
    audit.record(config.db_path, lock.user, "unlock", None, None, caller)
    return _unlocked(lock.user)


def _unlocked(name):
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(account.COOKIE, lock.new_session(),
                        max_age=account.SESSION_HOURS * 3600, httponly=True, samesite="strict",
                        path="/")
    return response


@app.post("/lock")
def lock_again(request: Request):
    event(request, "lock")
    response = RedirectResponse("/unlock", status_code=303)
    response.delete_cookie(account.COOKIE, path="/")
    return response


def _activity_logger():
    from logging.handlers import RotatingFileHandler
    config.data_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("inventory.activity")
    if not logger.handlers:
        handler = RotatingFileHandler(config.data_dir / "activity.log", maxBytes=2_000_000,
                                      backupCount=5, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


activity = _activity_logger()


@app.middleware("http")
async def log_activity(request: Request, call_next):
    """One line per request in data/activity.log: ip, user, method, path, status, ms.
    No query string, no body, and invitation tokens are cut from the path."""
    started = time.monotonic()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        path = request.url.path
        if path.startswith("/fonts/"):
            pass
        else:
            if path.startswith("/api/health/"):
                path = "/api/health/<token>"
            user, address = who(request)
            activity.info("%s user=%s %s %s -> %d %dms", address, user or "-", request.method,
                          path, status, (time.monotonic() - started) * 1000)


@app.middleware("http")
async def same_origin(request: Request, call_next):
    """Only this window may change anything.

    The server listens on loopback, but any page in any browser on this machine could still post a
    form to it. Anything that writes must therefore come from our own origin.
    """
    if request.method in WRITING:
        origin = request.headers.get("origin") or request.headers.get("referer") or ""
        mine = str(request.base_url).rstrip("/")
        if not origin.startswith(mine):
            return JSONResponse({"detail": "Refused: that request came from somewhere else."}, 403)
    return await call_next(request)


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    description = f"{type(exc).__name__}: {exc}"
    result = await asyncio.to_thread(report, request, "request", description, request.url.path)
    number = result["error_no"]
    detail = (f"Internal error #{number}. Quote that number when you report it."
              if number else f"Internal error: {description}")
    return JSONResponse({"detail": detail}, status_code=500)


@app.get("/robots.txt")
def robots():
    return Response("User-agent: *\nDisallow: /\n", media_type="text/plain")


@app.get("/", response_class=HTMLResponse)
def page():
    return HTMLResponse(PAGE.read_text(encoding="utf-8"))


@app.get("/predictions", response_class=HTMLResponse)
def predictions_page():
    return HTMLResponse(PREDICTIONS.read_text(encoding="utf-8"))


@app.get("/license", response_class=HTMLResponse)
def license_page():
    text = (WEB / "LICENSE.txt").read_text(encoding="utf-8")
    page = LICENCE.read_text(encoding="utf-8").replace("LICENCE_JSON", json.dumps(text))
    return HTMLResponse(page)


@app.get("/fonts/{name}")
def font(name: str):
 
    path = (WEB / "fonts" / name).resolve()
    if path.parent != (WEB / "fonts").resolve() or not path.is_file() or path.suffix != ".woff2":
        raise HTTPException(404, "Font not found.")
    return FileResponse(path, media_type="font/woff2",
                        headers={"Cache-Control": "max-age=604800"})


@app.get("/api/data")
def data(request: Request):
   
    notes = db.read(config.db_path, """
            SELECT n.note_id, n.image_path, n.confirmed_by,
                   any_value(d.supplier), any_value(d.delivery_date), any_value(d.order_date),
                   any_value(d.site), any_value(d.work_item),
                   list({'line_no': d.line_no, 'article': d.article, 'quantity': d.quantity,
                         'backorder': d.backorder, 'designation': d.designation,
                         'work_item': d.work_item} ORDER BY d.line_no) AS lines
            FROM notes n JOIN inventory_lines d USING (note_id)
            GROUP BY n.note_id, n.image_path, n.confirmed_by
            ORDER BY any_value(d.delivery_date) DESC, n.note_id DESC
            """)
    catalog_rows = db.read(config.db_path, "SELECT supplier, designation, reference, article, seen "
                                           "FROM catalog ORDER BY article")

    return {
        "notes": [{"id": note_id, "photo": bool(image), "confirmed_by": by, "supplier": supplier,
                   "delivery_date": delivered.isoformat() if delivered else None,
                   "order_date": ordered.isoformat() if ordered else None,
                   "site": site, "work_item": work_item, "lines": lines}
                  for note_id, image, by, supplier, delivered, ordered, site, work_item, lines
                  in notes],
        "catalog": [{"supplier": s, "designation": d, "reference": r, "article": a, "seen": n}
                    for s, d, r, a, n in catalog_rows],
        "lists": db.purchase_orders(config.db_path),
        "inventory": db.stock_by_article(config.db_path),
        "backend": config.backend,
        "user": who(request)[0],
    }


async def _photo_bytes(photo):
    media_type = photo.content_type or mimetypes.guess_type(photo.filename or "")[0] or ""
    if media_type not in SUPPORTED:
        raise HTTPException(415, f"Unsupported format ({media_type or 'unknown'}): "
                                 f"JPEG, PNG or PDF.")
    raw = await photo.read()
    if not raw:
        raise HTTPException(400, "Empty file.")
    if len(raw) > MAX_UPLOAD:
        raise HTTPException(413, f"That file is larger than {MAX_UPLOAD // 1024 // 1024} MB.")
    return raw, media_type, hashlib.sha256(raw).hexdigest()


@app.post("/api/extract")
async def extract(request: Request, photo: UploadFile):
    raw, media_type, sha = await _photo_bytes(photo)
    already = db.find_by_image(config.db_path, sha)
    if already is not None:
        raise HTTPException(409, f"This note is already stored (note #{already}).")

    known = vocabulary.known_articles(config.db_path)
    try:
        note = await get_backend().extract(raw, media_type, known)
    except llm.ExtractionError as e:
        raise HTTPException(502, str(e)) from e
    except Exception as e: 
        log.exception("extraction failed")
        reported = await asyncio.to_thread(report, request, "extract",
                                           f"note reading failed: {e}", "/api/extract")
        raise HTTPException(500, f"Reading failed (error #{reported['error_no']}): {e}") from e

    note = catalog.apply(config.db_path, note)
    (config.images_dir / f"{sha}{SUPPORTED[media_type]}").write_bytes(raw)
    return JSONResponse({"sha256": sha, "note": note.model_dump()})


class Measurement(BaseModel):
    fields: int = 0
    corrected: int = 0
    lines: int = 0


class SaveNote(BaseModel):
    sha256: str | None = None
    note: Note
    measurement: Measurement | None = None


def _stored_photo(sha256):
    if not sha256:
        return ""
    matches = list(config.images_dir.glob(f"{sha256}.*"))
    return str(matches[0]) if matches else ""


@app.post("/api/notes")
def save_note(request: Request, body: SaveNote):
    problems = body.note.problems()
    if problems:
        raise HTTPException(422, "To fix: " + ", ".join(problems))

    user = who(request)[0]
    try:
        note_id = db.save_note(config.db_path, body.note, body.sha256 or None,
                               _stored_photo(body.sha256), user)
    except duckdb.ConstraintException as e:
        raise HTTPException(409, "This note is already stored.") from e

    if body.measurement:
        db.record_reading(config.db_path, "note", body.measurement.fields,
                          body.measurement.corrected, body.measurement.lines)
    detail = f"{body.note.supplier} · {len(body.note.lines)} line(s)"
    if body.measurement:
        detail += f" · {body.measurement.corrected}/{body.measurement.fields} fields corrected"
    event(request, "note.save", f"note #{note_id}", detail)
    return {"note_id": note_id}


def _drop(image_path, missing):

    if image_path is None:
        raise HTTPException(404, missing)
    if image_path:
        Path(image_path).unlink(missing_ok=True)


@app.delete("/api/notes/{note_id}")
def remove_note(request: Request, note_id: int):
    _drop(db.delete_note(config.db_path, note_id), f"No note #{note_id}.")
    event(request, "note.delete", f"note #{note_id}")
    return {"deleted": note_id}


def _photo(path):
    if not path or not Path(path).exists():
        raise HTTPException(404, "No photo.")
    return FileResponse(path)


@app.get("/api/photo/{note_id}")
def photo(note_id: int):
    rows = db.read(config.db_path, "SELECT image_path FROM notes WHERE note_id = ?", [note_id])
    return _photo(rows[0][0] if rows else None)


class WorkItem(BaseModel):
    work_item: str | None = None


@app.patch("/api/inventory/{note_id}/{line_no}")
def edit_work_item(request: Request, note_id: int, line_no: int, body: WorkItem):
    value = (body.work_item or "").strip() or None
    if not db.update_work_item(config.db_path, note_id, line_no, value):
        raise HTTPException(404, "Line not found.")
    event(request, "delivery.edit", f"note #{note_id} line {line_no}",
          f"work_item = {value or 'empty'}")
    return {"note_id": note_id, "line_no": line_no, "work_item": value}


@app.post("/api/orders/extract")
async def extract_list(request: Request, photo: UploadFile):
    raw, media_type, sha = await _photo_bytes(photo)
    already = db.find_order_by_image(config.db_path, sha)
    if already is not None:
        raise HTTPException(409, f"This list is already stored (list #{already}).")

    known = vocabulary.known_articles(config.db_path)
    try:
        sheet = await get_backend().extract_list(raw, media_type, known)
    except llm.ExtractionError as e:
        raise HTTPException(502, str(e)) from e
    except Exception as e:
        log.exception("list reading failed")
        reported = await asyncio.to_thread(report, request, "extract",
                                           f"list reading failed: {e}", "/api/orders/extract")
        raise HTTPException(500, f"Reading failed (error #{reported['error_no']}): {e}") from e

    (config.images_dir / f"{sha}{SUPPORTED[media_type]}").write_bytes(raw)
    return JSONResponse({"sha256": sha, "list": sheet})

class ListLine(BaseModel):
    article: str
    designation: str | None = None
    reference: str | None = None
    quantity: float | None = None
    stock: float | None = None
    in_stock: bool | None = None


class HardwareList(BaseModel):
    list_date: str | None = None
    site: str | None = None
    work_item: str | None = None
    drafter: str | None = None
    lines: list[ListLine] = []


class SaveList(BaseModel):
    sha256: str | None = None
    list: HardwareList
    measurement: Measurement | None = None


@app.post("/api/orders")
def save_list(request: Request, body: SaveList):
    sheet = body.list
    if not sheet.lines:
        raise HTTPException(422, "To fix: no line.")
    for i, line in enumerate(sheet.lines, 1):
        if not line.article.strip():
            raise HTTPException(422, f"To fix: line {i} has no article.")
    if sheet.list_date and parse_date(sheet.list_date) is None:
        raise HTTPException(422, f"Unreadable date: {sheet.list_date!r}")

    payload = sheet.model_dump()
    payload["list_date"] = parse_date(sheet.list_date) if sheet.list_date else None
    payload["lines"] = [dict(l, article=l["article"].strip()) for l in payload["lines"]]

    try:
        list_id = db.save_purchase_order(config.db_path, payload, body.sha256 or None,
                                        _stored_photo(body.sha256), who(request)[0])
    except duckdb.ConstraintException as e:
        raise HTTPException(409, "This list is already stored.") from e

    if body.measurement:
        db.record_reading(config.db_path, "list", body.measurement.fields,
                          body.measurement.corrected, body.measurement.lines)
    event(request, "order.save", f"order #{list_id}",
          f"{sheet.site or 'no site'} · {len(sheet.lines)} line(s)")
    return {"list_id": list_id}


class Stock(BaseModel):
    in_stock: bool | None = None


@app.patch("/api/orders/{list_id}/lines/{line_no}")
def set_in_stock(request: Request, list_id: int, line_no: int, body: Stock):
    if not db.set_in_stock(config.db_path, list_id, line_no, body.in_stock):
        raise HTTPException(404, "Line not found.")
    state = {True: "in stock", False: "to order", None: "not ticked"}[body.in_stock]
    event(request, "order.tick", f"order #{list_id} line {line_no}", state)
    return {"list_id": list_id, "line_no": line_no, "in_stock": body.in_stock}


@app.delete("/api/orders/{list_id}")
def remove_list(request: Request, list_id: int):
    _drop(db.delete_purchase_order(config.db_path, list_id), f"No list #{list_id}.")
    event(request, "order.delete", f"order #{list_id}")
    return {"deleted": list_id}


@app.get("/api/order-photo/{list_id}")
def list_photo(list_id: int):
    return _photo(db.order_photo_path(config.db_path, list_id))


@app.get("/api/vocabulary")
def read_vocabulary():
    entries = vocabulary.vocabulary(config.db_path)
    return {
        "articles": entries[:400],
        "total": len(entries),
        "shared": sum(1 for e in entries if e["shared"]),
        "aliases": vocabulary.aliases(config.db_path),
    }


class Merge(BaseModel):
    alias: str
    article: str


@app.post("/api/vocabulary/merge")
def merge_vocabulary(request: Request, body: Merge):
    try:
        result = vocabulary.merge(config.db_path, body.alias, body.article, who(request)[0])
    except vocabulary.VocabularyError as e:
        raise HTTPException(422, str(e)) from e
    event(request, "vocabulary.merge", result["article"],
          f"{result['alias']} -> {result['article']} ({result['inventory']} delivery line(s), "
          f"{result['order_lines']} list line(s))")
    return result


@app.get("/api/audit")
def read_audit(limit: int = 100, user: str | None = None):
    return {
        "users": audit.users(config.db_path),
        "events": audit.events(config.db_path, min(max(limit, 1), 500), user),
    }


class ModelChoice(BaseModel):
    id: str


@app.get("/api/models")
def models():
    return {"current": config.backend, "providers": models_info.status(config.backend)}


@app.post("/api/models/check")
def check_model(request: Request, body: ModelChoice):
    ok, detail = models_info.probe(body.id)
    event(request, "model.check", body.id, detail[:200])
    return {"id": body.id, "ok": ok, "detail": detail}


@app.post("/api/models/use")
def use_model(request: Request, body: ModelChoice):
    global config, backend
    try:
        chosen = models_info.use(body.id)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    config = replace(config, backend=chosen)
    backend = None
    event(request, "model.use", chosen)
    return {"current": chosen, "providers": models_info.status(chosen)}


def _planner_settings(lead, service, horizon):
    lead = min(max(int(lead), 1), 120)
    service = min(max(int(service), 50), 99)
    horizon = min(max(int(horizon), 1), 180)
    return lead, service, horizon


@app.get("/api/movements")
def read_movements(period: str = "week"):
    if period not in forecast.PERIODS:
        raise HTTPException(422, f"Unknown period: {period}.")
    rows = forecast.movements(config.db_path, period)
    return {"period": period, "rows": rows,
            "totals": {
                "in_quantity": round(sum(row["in_quantity"] for row in rows), 2),
                "out_quantity": round(sum(row["out_quantity"] for row in rows), 2),
                "net": round(sum(row["net"] for row in rows), 2),
            }}


def _sheet(request, columns, rows, stem, fmt):
    """One table, out as a file, the same way the other exports go."""
    if fmt not in ("csv", "xlsx"):
        raise HTTPException(404, f"Unknown format: {fmt}.")
    headers = [title for _, title in columns]
    table = [[_cell(row.get(key)) for key, _ in columns] for row in rows]
    name = f"{stem}.{fmt}"

    if fmt == "xlsx":
        from openpyxl import Workbook

        book = Workbook()
        sheet = book.active
        sheet.title = stem[:31]
        sheet.append(headers)
        for line in table:
            sheet.append(line)
        sheet.freeze_panes = "A2"
        for index, title in enumerate(headers, 1):
            width = max([len(title)] + [len(str(line[index - 1])) for line in table[:200]] or [10])
            letter = sheet.cell(row=1, column=index).column_letter
            sheet.column_dimensions[letter].width = min(width + 2, 60)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / name
            book.save(out)
            body = out.read_bytes()
        media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    else:
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(headers)
        writer.writerows(table)
        body = "\ufeff".encode() + buffer.getvalue().encode()
        media = "text/csv; charset=utf-8"

    event(request, "export", name)
    return Response(body, media_type=media,
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


def _buffer_settings(cycle, horizon):
    return min(max(int(cycle), 1), 90), min(max(int(horizon), 1), 26)


@app.get("/api/ddmrp")
def read_ddmrp(cycle: int = ddmrp.DEFAULT_CYCLE, horizon: int = ddmrp.DEFAULT_HORIZON):
    cycle, horizon = _buffer_settings(cycle, horizon)
    return ddmrp.summary(config.db_path, cycle, horizon)


@app.get("/api/forecast")
def read_forecast(lead: int = forecast.DEFAULT_LEAD, service: int = forecast.DEFAULT_SERVICE,
                  horizon: int = forecast.DEFAULT_HORIZON):
    lead, service, horizon = _planner_settings(lead, service, horizon)
    answer = forecast.summary(config.db_path, lead, service, horizon)
    answer["counts_source"] = len(db.stock_counts(config.db_path))
    return answer


class StockCount(BaseModel):
    article: str
    quantity: float | None = None


@app.put("/api/stock-count")
def put_stock_count(request: Request, body: StockCount):
    try:
        article = db.save_stock_count(config.db_path, body.article, body.quantity,
                                      who(request)[0])
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    event(request, "stock.count", body.article,
          "cleared" if body.quantity is None else str(body.quantity))
    return {"article": article or body.article, "quantity": body.quantity}


class OrderDraft(BaseModel):
    lead: int = forecast.DEFAULT_LEAD
    service: int = forecast.DEFAULT_SERVICE
    horizon: int = forecast.DEFAULT_HORIZON
    site: str | None = None
    work_item: str | None = None
    only: list[str] | None = None


@app.post("/api/forecast/draft")
async def draft_order(request: Request, body: OrderDraft):
    """Dots turns the planner's numbers into a purchase order for someone to approve."""
    lead, service, horizon = _planner_settings(body.lead, body.service, body.horizon)
    plan = forecast.summary(config.db_path, lead, service, horizon)
    rows = plan["articles"]
    if body.only:
        wanted = {name.strip().upper() for name in body.only}
        rows = [row for row in rows if row["article"].upper() in wanted]
    else:
        rows = plan["shortlist"]
    if not rows:
        raise HTTPException(422, "Nothing worth ordering: no article is under its reorder point.")

    keep = ("article", "designation", "reference", "supplier", "per_week", "on_hand", "on_order",
            "lead_demand", "safety_stock", "reorder_point", "cover_days", "suggested", "state",
            "risk", "backorders")
    planner = {
        "settings": plan["settings"],
        "window": plan["window"],
        "counted_articles": plan["counts"]["counted"],
        "history_is_thin": plan["thin"],
        "articles": [{key: row[key] for key in keep} for row in rows[:60]],
    }

    dots = llm.planner_backend(config)
    try:
        answer = await dots.draft_order(planner)
    except llm.ExtractionError as e:
        report(request, "draft", str(e), "Predictions window")
        raise HTTPException(502, str(e)) from e

    lines = []
    for line in answer.get("lines", []):
        article = str(line.get("article") or "").strip()
        quantity = line.get("quantity")
        if not article or quantity is None:
            continue
        lines.append({"article": article, "quantity": float(quantity),
                      "unit": line.get("unit"), "supplier": line.get("supplier"),
                      "reference": line.get("reference"),
                      "reason": str(line.get("reason") or "")})
    if not lines:
        raise HTTPException(502, "Dots came back with no line.")

    event(request, "order.draft", f"{len(lines)} line(s)", f"by Dots, {dots.model}")
    return {"lines": lines, "note": str(answer.get("note") or ""), "model": dots.model,
            "drafted_from": len(planner["articles"]), "site": body.site,
            "work_item": body.work_item}


@app.get("/api/errors")
def read_errors(limit: int = 50):
    return {
        "errors": alerts.recent(config.db_path, min(max(limit, 1), 200)),
        "alerting": alerts.destination(config.sms),
    }


@app.post("/api/errors/test")
async def test_error(request: Request):
    user = who(request)[0] or "?"
    result = await asyncio.to_thread(report, request, "test",
                                     f"test message requested by {user}", "Settings tab")
    event(request, "error.test", f"error #{result['error_no']}", result["sms_status"])
    return result


@app.get("/api/health/{token}")
def health(token: str):
    expected = os.environ.get("HEALTH_TOKEN", "")
    if not expected or not secrets.compare_digest(token, expected):
        return NOT_FOUND
    try:
        db.storage(config.db_path, config.data_dir, config.images_dir)
    except Exception as e:
        log.exception("health check: database unreadable")
        report(None, "health", f"health check failed: {e}", "/api/health")
        return JSONResponse({"status": "ko"}, 503)
    return JSONResponse({"status": "ok"})


@app.get("/api/checks")
def checks():
    try:
        return json.loads((config.data_dir / "monitor.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {"status": "unknown", "problems": ["no check has run yet"]}


@app.get("/api/backups")
def list_backups():
    return {"backups": backup.list_backups(config.data_dir)[:20]}


@app.post("/api/backups")
async def force_backup(request: Request):
    try:
        await asyncio.to_thread(backup.snapshot, config)
    except Exception as e:
        log.exception("backup failed")
        reported = await asyncio.to_thread(report, request, "backup", f"backup failed: {e}",
                                           "backup")
        raise HTTPException(500, f"Backup failed (error #{reported['error_no']}): {e}") from e
    files = backup.list_backups(config.data_dir)
    event(request, "backup.run", files[0]["name"] if files else None)
    return {"backups": files[:20], "latest": files[0] if files else None}


@app.get("/export/backup/{name}.csv")
def backup_csv(name: str, table: str = "inventory_lines"):
 
    archive = (config.data_dir / "backups" / name).resolve()
    if archive.parent != (config.data_dir / "backups").resolve() or not archive.is_file():
        raise HTTPException(404, "Backup not found.")
    if table not in backup.TABLES:
        raise HTTPException(404, f"Unknown table: {table}.")

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        try:
            export_dir = backup.open_backup(archive, os.environ.get("BACKUP_PASSPHRASE", ""),
                                            work / "x")
            csv_path = backup.table_to_csv(export_dir, table, work / f"{table}.csv")
        except ValueError as e:
            raise HTTPException(500, str(e)) from e
        body = "﻿".encode() + csv_path.read_bytes()

    stem = name.replace(".tar.gz.enc", "").replace(".tar.gz", "")
    return Response(body, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{stem}-{table}.csv"'})


@app.get("/api/accuracy")
def accuracy():
    return db.accuracy(config.db_path)


@app.get("/api/storage")
def storage():
    info = db.storage(config.db_path, config.data_dir, config.images_dir)
    info["photo_retention_days"] = config.photo_retention_days
    return info


def _cell(value):
    
    return ("yes" if value else "no") if isinstance(value, bool) else value


def _to_xlsx(dataset, name):
    from openpyxl import Workbook

    columns, rows = db.export_query(config.db_path, dataset)
    book = Workbook()
    sheet = book.active
    sheet.title = dataset[:31]
    sheet.append(columns)
    for row in rows:
        sheet.append([_cell(v) for v in row])
    sheet.freeze_panes = "A2"
    for i, column in enumerate(columns, 1):
      
        width = max([len(column)] + [len(str(_cell(r[i - 1]))) for r in rows[:200]] or [10])
        sheet.column_dimensions[sheet.cell(row=1, column=i).column_letter].width = min(width + 2, 60)
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / name
        book.save(out)
        return out.read_bytes()


PG_TABLES = {
    "inventory": ("inventory_lines", "Inventory lines", ("id",), (
        ("id", "integer"), ("note_id", "integer"), ("line_no", "integer"), ("article", "text"),
        ("quantity", "double precision"), ("supplier", "text"), ("delivery_date", "date"),
        ("weekday", "text"), ("order_date", "date"), ("site", "text"), ("work_item", "text"),
        ("backorder", "boolean"), ("designation", "text"))),
    "purchase_orders": ("purchase_order_lines", "Purchase order lines", ("id",), (
        ("id", "integer"), ("list_id", "integer"), ("line_no", "integer"), ("article", "text"),
        ("list_date", "date"), ("site", "text"), ("work_item", "text"), ("drafter", "text"),
        ("designation", "text"), ("reference", "text"), ("quantity", "double precision"),
        ("stock", "double precision"), ("in_stock", "boolean"))),
    "safety-stock": ("safety_stock", "Safety stock watch", ("article",), (
        ("article", "text"), ("supplier", "text"), ("per_week", "double precision"),
        ("adu", "double precision"), ("lead_days", "double precision"), ("lead_class", "text"),
        ("cv", "double precision"), ("variability", "text"), ("red", "double precision"),
        ("yellow", "double precision"), ("green", "double precision"),
        ("top_of_red", "double precision"), ("top_of_yellow", "double precision"),
        ("top_of_green", "double precision"), ("on_hand", "double precision"),
        ("open_demand", "double precision"), ("net_flow", "double precision"),
        ("buffer_share", "double precision"), ("zone", "text"), ("to_order", "integer"),
        ("state", "text"), ("spike_risk", "double precision"))),
}

FORMATS = ("csv", "xlsx", "sql")


def _rows_for(dataset, cycle, horizon):
    """The three downloads, every one of them keyed so they can be joined again."""
    table, title, key, columns = PG_TABLES[dataset]
    if dataset == "safety-stock":
        rows, _, _ = ddmrp.buffers(config.db_path, cycle, horizon)
        data = [[row.get(name) for name, _ in columns] for row in rows]
    else:
        names, fetched = db.export_query(config.db_path, dataset)
        order = [names.index(name) for name, _ in columns if name != "id"]
        data = [[number] + [row[at] for at in order]
                for number, row in enumerate(fetched, 1)]
    return table, title, key, columns, data


@app.get("/export/{dataset}.{fmt}")
def export_dataset(request: Request, dataset: str, fmt: str,
                   cycle: int = ddmrp.DEFAULT_CYCLE, horizon: int = ddmrp.DEFAULT_HORIZON):
    if dataset not in PG_TABLES:
        raise HTTPException(404, f"Unknown dataset: {dataset}.")
    if fmt not in FORMATS:
        raise HTTPException(404, f"Unknown format: {fmt}.")

    cycle, horizon = _buffer_settings(cycle, horizon)
    table, title, key, columns, rows = _rows_for(dataset, cycle, horizon)
    name = f"{table}.{fmt}"

    if fmt == "sql":
        body = pg.script(table, columns, rows, key, f"{title}, {date.today().isoformat()}")
        event(request, "export", name)
        return Response(body.encode(), media_type="application/sql",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    names = [name for name, _ in columns]
    headers = tuple((name, name.replace("_", " ")) for name in names)
    return _sheet(request, headers, [dict(zip(names, row)) for row in rows], table, fmt)


class Question(BaseModel):
    question: str


class SqlRequest(BaseModel):
    sql: str


def _json(value):
    if isinstance(value, bool):
        return value
    return value.isoformat() if hasattr(value, "isoformat") else value


def _result(sql, note):
    result = db.run_readonly(config.db_path, sql)
    return {"sql": sql, "note": note, "columns": result.columns,
            "rows": [[_json(v) for v in row] for row in result.rows],
            "truncated": result.truncated}


@app.post("/api/question")
async def question(request: Request, body: Question):
    if not body.question.strip():
        raise HTTPException(400, "Empty question.")

    previous = None
    for _ in range(2):  
        try:
            sql, note = await get_backend().to_sql(body.question, previous)
        except llm.ExtractionError as e:
            raise HTTPException(502, str(e)) from e
        if not sql:
            return {"sql": None, "note": note or "This question cannot be answered from this data."}
        try:
            answer = _result(sql, note)
        except db.QueryError as e:
            previous = (sql, str(e))
            continue
        await asyncio.to_thread(event, request, "question", None, body.question[:200])
        return answer

    raise HTTPException(422, f"Query failed: {previous[1]}")


@app.post("/api/sql")
def run_sql(request: Request, body: SqlRequest):
    try:
        answer = _result(body.sql, None)
    except db.QueryError as e:
        raise HTTPException(422, str(e)) from e
    event(request, "sql", None, body.sql[:200])
    return answer


@app.post("/exit")
def quit_app(request: Request):
    """The window's exit button: stop the server, which closes the app."""
    event(request, "quit")
    if stopper is not None:
        stopper()
    return Response(status_code=204)


def serve(host="127.0.0.1", port=8800, on_stop=None):
    """Run the local server. Returns when it stops."""
    global stopper

    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        level=logging.WARNING)
    db.init(config.db_path)
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning"))
    stopper = on_stop or (lambda: setattr(server, "should_exit", True))
    server.run()


def main():
    parser = argparse.ArgumentParser(description="Serve the local pages without a window.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8800)
    args = parser.parse_args()
    print(f"http://{args.host}:{args.port}")
    serve(args.host, args.port)


if __name__ == "__main__":
    main()
