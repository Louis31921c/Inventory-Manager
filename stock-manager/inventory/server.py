import argparse
import asyncio
import csv
import hashlib
import html
import io
import json
import logging
import subprocess
import time
import mimetypes
import os
import re
import secrets
import tempfile
import threading
from datetime import date
from pathlib import Path

import duckdb
import uvicorn
from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel

from dataclasses import replace

from . import (
    account, agents, alerts, archive, audit, autonomy,
    backorders, backup, bankdrop, baskets, bills, catalog,
    channels, charters, contacts, counting, dashboard, db,
    ddmrp, discord_bot, disputes, documents, feedback, forecast,
    gate, intake, judge, leadtimes, llm, mail_oauth,
    mailbox, matching, models_info, outbound, outbox, payments,
    pg, photos, policies, prices, pricing, purchasing,
    quotes, receiving, reorder, replenish, replies, requisitions,
    routing, sheets, shortcuts, simulation, slack_bot, stock,
    stockctl, units, verify, vocabulary, wallet, watch,
    workdays,
)
from . import access, health, integrity, licensing, vault
from .config import config_dir, load_config, set_setting
from .models import Note, parse_date

log = logging.getLogger("inventory.web")

WEB = Path(__file__).resolve().parent / "web_ui"
PAGE = WEB / "app.html"
SCRIPTS = WEB / "js"
SCRIPT_NAME = re.compile(r"^[0-9]{2}-[a-z0-9-]+\.js$")


def page_source():
    """The page and every script it loads, as one text (for the tests that read what the page wires up)."""
    return PAGE.read_text(encoding="utf-8") + "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(SCRIPTS.glob("*.js")))
LICENCE = WEB / "license.html"
UNLOCK = WEB / "unlock.html"
SUPPORTED = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
             "application/pdf": ".pdf"}
config = load_config()
app = FastAPI(title="Stock manager")
lock = account.Lock(config.data_dir)
backend = None
stopper = None
OPEN_PATHS = ("/unlock", "/robots.txt", "/sap.css", "/logo.png", "/icon.png", "/license-required",
              "/license/install")
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
    simulation.init(config.db_path)
    db.keep_open(config.db_path)
    config.images_dir.mkdir(parents=True, exist_ok=True)
    for path in vault.harden(config_dir(), config.data_dir):
        log.warning("permissions tightened: %s", path)
    if vault.seal_database(config.db_path):
        log.info("stored supplier tokens are now encrypted")
    log.info("database: %s", config.db_path)
    log.info("error reports: %s", alerts.destination(config.sms))
    asyncio.create_task(_purge_loop())
    asyncio.create_task(_mail_loop())
    asyncio.create_task(_queue_loop())
    asyncio.create_task(_watch_loop())
    if discord_bot.settings()["token"]:
        asyncio.create_task(discord_bot.run(config))
    if slack_bot.settings()["bot_token"] and slack_bot.settings()["app_token"]:
        asyncio.create_task(slack_bot.run(config))


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


async def _queue_loop():
    """Every hour: read what is ticked "to order", and order what has waited long enough."""
    while True:
        try:
            done = await asyncio.to_thread(reorder.run, config.db_path)
            if done["added"] or done["orders"]:
                log.info("order queue: %d read, %d order(s) placed",
                         done["added"], len(done["orders"]))
        except Exception as e:
            log.exception("order queue failed")
            await asyncio.to_thread(report, None, "queue", f"order queue failed: {e}",
                                    "order queue loop")
        await asyncio.sleep(3600)


# A bill is read as a bill, by a person: a file that says so is left for the queue.
NOT_A_NOTE = re.compile(r"factur|invoice|\bbill\b|avoir|devis|quote", re.IGNORECASE)


async def _read_mail_notes(intake_ids):
    """Read what the mailbox just brought, and store the delivery notes that read clean.

    A note with anything to fix, a spreadsheet, or something that calls itself a bill
    stays in the queue for a person, with the reason beside it. A stored note carries
    the name MAIL, so the audit says where it came from, and it can be deleted like
    any other.
    """
    stored, left = [], 0
    config.images_dir.mkdir(parents=True, exist_ok=True)
    known = vocabulary.known_articles(config.db_path)
    for intake_id in intake_ids:
        try:
            raw, media_type, item = intake.bytes_of(config, intake_id)
        except KeyError:
            continue
        if media_type not in SUPPORTED:
            left += 1
            continue
        if NOT_A_NOTE.search(f"{item['name']} {item['subject'] or ''}"):
            intake.mark(config, intake_id, intake.WAITING, "mail: looks like a bill or a quote")
            left += 1
            continue
        if db.find_by_image(config.db_path, item["sha256"]) is not None:
            intake.mark(config, intake_id, intake.DONE, "mail: already stored")
            continue
        try:
            note = catalog.apply(config.db_path,
                                 await get_backend().extract(raw, media_type, known))
        except Exception as e:
            intake.mark(config, intake_id, intake.WAITING, f"mail: not read ({str(e)[:120]})")
            left += 1
            continue
        problems = note.problems()
        if problems:
            intake.mark(config, intake_id, intake.WAITING, "mail: to fix, " + ", ".join(problems))
            left += 1
            continue
        path = config.images_dir / f"{item['sha256']}{SUPPORTED[media_type]}"
        photos.keep(path, raw)
        try:
            note_id = db.save_note(config.db_path, note, item["sha256"], str(path), "MAIL")
        except duckdb.ConstraintException:
            intake.mark(config, intake_id, intake.DONE, "mail: already stored")
            continue
        intake.mark(config, intake_id, intake.DONE, f"mail: note #{note_id}")
        audit.record(config.db_path, "MAIL", "note.save", f"note #{note_id}",
                     f"{note.supplier} · {len(note.lines)} line(s) · from {item['sender'] or 'mail'}")
        stored.append(note_id)
    return {"stored": len(stored), "left": left}


async def _watch_loop():
    """Every hour: look over the books for what needs a person, and push what is new."""
    while True:
        try:
            await asyncio.to_thread(feedback.learn, config.db_path)
            ordered = await asyncio.to_thread(replenish.run, config.db_path)
            for kind, list_id, lines in ordered.get("orders", []):
                log.info("Plutus wrote the %s order #%s: %d line(s) from the DDMRP", kind, list_id, lines)
            await asyncio.to_thread(judge.decide, config.db_path)
            await asyncio.to_thread(receiving.sync, config.db_path)
            counts = await asyncio.to_thread(counting.assign, config.db_path)
            if counts:
                await asyncio.to_thread(watch.deliver, config, "To count today: " + ", ".join(counts), "Count list")
            for finished in await asyncio.to_thread(requisitions.refresh, config.db_path):
                await asyncio.to_thread(watch.deliver, config, requisitions.text(finished),
                                        f"Request #{finished['req_id']} is complete")
            bought = await asyncio.to_thread(purchasing.run, config.db_path, config)
            if bought.get("done"):
                log.info("Plutus: %s", ", ".join(f"{k} {v}" for k, v in bought.items() if k != "done" and v))
            did = await asyncio.to_thread(policies.run, config.db_path, config)
            if did.get("acted"):
                log.info("policies acted %d time(s)", did["acted"])
            done = await asyncio.to_thread(watch.run, config.db_path, config)
            if done["new"] or done["pushed"]:
                log.info("notices: %d new, %d open, pushed by %s", done["new"], done["open"],
                         ", ".join(done["pushed"]) or "nothing")
        except Exception as e:
            log.exception("notice scan failed")
            await asyncio.to_thread(report, None, "watch", f"notice scan failed: {e}",
                                    "notice loop")
        await asyncio.sleep(3600)


last_mail = {"at": None, "queued": 0, "detail": "not run yet"}


async def _mail_loop():
    """Every so often, whatever the suppliers sent lands in the queue by itself."""
    while True:
        wait = max(config.mail.every_minutes, 1) * 60
        await asyncio.sleep(wait)
        if not config.mail.enabled:
            continue
        try:
            answer = await asyncio.to_thread(mailbox.fetch, config, config.mail)
            read = (await _read_mail_notes(answer["ids"])
                    if config.mail.auto_read and answer["ids"] else None)
            last_mail.update(at=time.strftime("%Y-%m-%d %H:%M"), queued=answer["queued"],
                             detail=f"{answer['messages']} message(s), {answer['queued']} queued"
                                    + (f", {read['stored']} note(s) stored" if read else ""))
            if answer["queued"]:
                log.info("mail: %d attachment(s) queued", answer["queued"])
        except Exception as e:
            last_mail.update(at=time.strftime("%Y-%m-%d %H:%M"), queued=0, detail=str(e)[:200])
            log.warning("mail fetch failed: %s", e)


# The pages carry their own script and style, so those two allow inline; nothing may come from another site.
CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data: blob:; font-src 'self'; connect-src 'self'; object-src 'none'; "
       "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


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
    request.state.role = lock.role(user)
    if not access.allowed(request.state.role, request.method, path):
        need = access.required_role(request.method, path)
        return JSONResponse({"detail": f"A {request.state.role} may not do this: it takes a {need}."}, 403)
    response = await call_next(request)
    response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    return response


# What a copy that may not run still answers: the page that says why, and the files that page needs.
HELD_OPEN = ("/license-required", "/license/install", "/license", "/api/license")


@app.middleware("http")
async def licence_gate(request: Request, call_next):
    """No licence, an ended one, or program files that no longer match the vendor's signature: nothing is served
    but the page that says so. The check is off for development (STOCK_LICENSE, STOCK_INTEGRITY)."""
    path = request.url.path
    if path in HELD_OPEN or path in OPEN_PATHS and path != "/unlock" or path.startswith(OPEN_PREFIXES):
        return await call_next(request)
    held = licensing.status()
    sealed = integrity.status()
    if held["valid"] and sealed["ok"]:
        return await call_next(request)
    why = held["reason"] if not held["valid"] else sealed["reason"]
    if path.startswith(("/api/", "/export")):
        return JSONResponse({"detail": f"This copy may not run: {why}.", "licence": held, "integrity": sealed}, 423)
    return RedirectResponse("/license-required", status_code=303)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """What the browser is told about the page: no framing, no sniffing, only this origin, never cached."""
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    # same-origin, not no-referrer: with no-referrer a browser sends "Origin: null" on a same-site POST,
    # which the same-origin check would refuse.
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    response.headers.setdefault("Content-Security-Policy", CSP)
    if request.url.path.startswith(("/api/", "/export")):
        response.headers["Cache-Control"] = "no-store"
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
    try:
        name = lock.authenticate(typed, password)
    except account.TooManyAttempts as e:
        return unlock_page(str(e))
    if name is None:
        audit.record(config.db_path, typed or lock.user, "unlock.refused", None, "wrong user or password", caller)
        return unlock_page("Wrong user or password.", user)
    audit.record(config.db_path, name, "unlock", None, None, caller)
    return _unlocked(name)


def _unlocked(name):
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(account.COOKIE, lock.new_session(user=name),
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


def _licence_page(note=""):
    held, sealed = licensing.status(), integrity.status()
    changed = sealed["changed"] + sealed["missing"] + sealed["added"]
    body = ["<meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>",
            "<title>Licence - Stock Manager</title><link rel='icon' href='/icon.png'>",
            "<style>@import url('/sap.css');body{padding:24px;max-width:760px;margin:auto}"
            "textarea{width:100%;min-height:160px;font-family:monospace;font-size:12px}"
            ".bad{color:#b00020}code{background:#eee;padding:1px 4px}</style>",
            "<h1>Stock Manager</h1>"]
    if note:
        body.append(f"<p class='bad'>{html.escape(note)}</p>")
    if not sealed["ok"]:
        body.append(f"<h2>The program files do not match the vendor's signature</h2><p class='bad'>{html.escape(sealed['reason'])}</p>"
                    "<p>The program refuses to run until its files are put back as the vendor delivered them.</p>")
        if changed:
            body.append("<ul>" + "".join(f"<li><code>{html.escape(name)}</code></li>" for name in changed[:20]) + "</ul>")
    if not held["valid"]:
        body.append(f"<h2>This copy has no valid licence</h2><p class='bad'>{html.escape(held['reason'])}.</p>"
                    f"<p>Machine id: <code>{held['machine']}</code>. Give it to the vendor with your order, then paste the "
                    "licence file they send you.</p>"
                    "<form method='post' action='/license/install'><textarea name='text' "
                    "placeholder='{ \"payload\": ..., \"signature\": ... }'></textarea>"
                    "<p><button class='action' type='submit'>install the licence</button></p></form>")
    if held["valid"] and sealed["ok"]:
        body.append(f"<p>Licensed to <b>{html.escape(str(held['licensee']))}</b> until {held['expires']}.</p>"
                    "<p><a href='/'>Open the program</a></p>")
    return HTMLResponse("".join(body))


@app.get("/license-required", response_class=HTMLResponse)
def licence_required():
    return _licence_page()


@app.get("/api/license")
def licence_status():
    return {"licence": licensing.status(), "integrity": {k: v for k, v in integrity.status().items() if k != "added"}}


@app.post("/license/install")
async def licence_install(request: Request):
    form = await request.form()
    try:
        licensing.install(str(form.get("text") or ""))
    except ValueError as e:
        return _licence_page(str(e))
    audit.record(config.db_path, "licence", "licence.install", licensing.status()["licensee"], None,
                 request.client.host if request.client else "?")
    return RedirectResponse("/", status_code=303)


@app.get("/js/{name}")
def page_script(name: str):
    if not SCRIPT_NAME.match(name) or not (SCRIPTS / name).is_file():
        raise HTTPException(404, "No such script.")
    return FileResponse(SCRIPTS / name, media_type="text/javascript", headers={"Cache-Control": "no-cache"})


@app.get("/license", response_class=HTMLResponse)
def license_page():
    text = (WEB / "LICENSE.txt").read_text(encoding="utf-8")
    page = LICENCE.read_text(encoding="utf-8").replace("LICENCE_JSON", json.dumps(text))
    return HTMLResponse(page)


@app.get("/sap.css")
def stylesheet():
    return Response((WEB / "sap.css").read_text(encoding="utf-8"),
                    media_type="text/css; charset=utf-8",
                    headers={"Cache-Control": "no-cache"})


@app.get("/logo.png")
def logo():
    return FileResponse(WEB / "logo.png", media_type="image/png",
                        headers={"Cache-Control": "max-age=604800"})


@app.get("/icon.png")
def icon():
    return FileResponse(WEB / "icon.png", media_type="image/png",
                        headers={"Cache-Control": "max-age=604800"})


@app.get("/landing.png")
def landing():
    return FileResponse(WEB / "landing.png", media_type="image/png",
                        headers={"Cache-Control": "max-age=604800"})


@app.get("/fonts/{name}")
def font(name: str):

    path = (WEB / "fonts" / name).resolve()
    if path.parent != (WEB / "fonts").resolve() or not path.is_file() or path.suffix != ".woff2":
        raise HTTPException(404, "Font not found.")
    return FileResponse(path, media_type="font/woff2",
                        headers={"Cache-Control": "max-age=604800"})


@app.get("/doors/{name}")
def door_mark(name: str):
    """The brand marks on the landing page, the ones that are pictures rather than drawn."""
    path = (WEB / "doors" / name).resolve()
    if path.parent != (WEB / "doors").resolve() or not path.is_file() or path.suffix != ".png":
        raise HTTPException(404, "Mark not found.")
    # Revalidated every time: these are swapped by hand, and a week in the cache would
    # keep the old picture on the screen long after the file had changed.
    return FileResponse(path, media_type="image/png",
                        headers={"Cache-Control": "no-cache"})


@app.get("/vehicles/{name}")
def vehicle_picture(name: str):
    """The picture each truck card is drawn with, one per kind of vehicle."""
    path = (WEB / "vehicles" / name).resolve()
    if path.parent != (WEB / "vehicles").resolve() or not path.is_file() or path.suffix != ".png":
        raise HTTPException(404, "Picture not found.")
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "no-cache"})


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
        "lists": _orders_with_cover(),
        "inventory": db.stock_by_article(config.db_path),
        "backend": config.backend,
        "user": who(request)[0],
    }


def _orders_with_cover():
    """Each order line carries what the delivery notes hold for it, so nobody has to
    tick a box: the paperwork says whether it is standing there or has to be ordered."""
    held = stock.by_place(config.db_path)
    sheets = db.purchase_orders(config.db_path)
    for sheet in sheets:
        for line in sheet.get("lines", []):
            line["have"] = stock.cover(held, line["article"], sheet.get("site"),
                                       sheet.get("work_item"), besides=sheet.get("id"))
    return sheets


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
    photos.keep(config.images_dir / f"{sha}{SUPPORTED[media_type]}", raw)
    return JSONResponse({"sha256": sha, "note": note.model_dump()})


@app.get("/api/intake")
def read_intake():
    return {"waiting": intake.waiting(config), "counts": intake.counts(config),
            "mail": {"configured": config.mail.enabled, "folder": config.mail.folder,
                     "every_minutes": config.mail.every_minutes, **last_mail}}


MAX_FILES = 25


@app.post("/api/intake")
async def add_to_intake(request: Request, files: list[UploadFile]):
    if len(files) > MAX_FILES:
        raise HTTPException(413, f"{MAX_FILES} files at a time, no more.")
    added, refused = [], []
    for item in files:
        raw = await item.read()
        if len(raw) > MAX_UPLOAD:
            refused.append({"name": item.filename, "why": "larger than 30 MB"})
            continue
        try:
            added.append(intake.add(config, raw, item.filename or "file", item.content_type))
        except ValueError as e:
            refused.append({"name": item.filename, "why": str(e)})
    event(request, "intake.add", f"{len(added)} file(s)")
    return {"added": added, "refused": refused, "waiting": intake.waiting(config)}


@app.post("/api/intake/{intake_id}/read")
async def read_queued(request: Request, intake_id: int):
    """Hand a queued scan to the model, exactly as a photo taken on the spot."""
    try:
        raw, media_type, item = intake.bytes_of(config, intake_id)
    except KeyError as e:
        raise HTTPException(404, "That file is no longer in the queue.") from e
    if media_type not in SUPPORTED:
        raise HTTPException(422, "A spreadsheet is imported, not read as a note.")

    already = db.find_by_image(config.db_path, item["sha256"])
    if already is not None:
        intake.mark(config, intake_id, intake.DONE, f"already note #{already}")
        raise HTTPException(409, f"This note is already stored (note #{already}).")

    known = vocabulary.known_articles(config.db_path)
    try:
        note = await get_backend().extract(raw, media_type, known)
    except llm.ExtractionError as e:
        raise HTTPException(502, str(e)) from e

    note = catalog.apply(config.db_path, note)
    photos.keep(config.images_dir / f"{item['sha256']}{SUPPORTED[media_type]}", raw)
    event(request, "intake.read", item["name"])
    return JSONResponse({"sha256": item["sha256"], "note": note.model_dump(),
                         "intake_id": intake_id})


@app.delete("/api/intake/{intake_id}")
def drop_queued(request: Request, intake_id: int):
    if not intake.drop(config, intake_id):
        raise HTTPException(404, "That file is no longer in the queue.")
    event(request, "intake.drop", str(intake_id))
    return {"dropped": intake_id}


class Mark(BaseModel):
    state: str = intake.DONE
    note: str | None = None


@app.patch("/api/intake/{intake_id}")
def mark_queued(request: Request, intake_id: int, body: Mark):
    if intake.one(config, intake_id) is None:
        raise HTTPException(404, "That file is no longer in the queue.")
    if body.state not in (intake.WAITING, intake.DONE, intake.DROPPED):
        raise HTTPException(422, f"Unknown state: {body.state}.")
    intake.mark(config, intake_id, body.state, body.note)
    return {"intake_id": intake_id, "state": body.state}


@app.post("/api/mail/fetch")
async def fetch_mail(request: Request):
    if not config.mail.enabled:
        raise HTTPException(422, "No mailbox configured: set IMAP_HOST, IMAP_USER and "
                                 "IMAP_PASSWORD in the settings file.")
    try:
        answer = await asyncio.to_thread(mailbox.fetch, config, config.mail)
    except mailbox.MailError as e:
        raise HTTPException(502, str(e)) from e
    read = (await _read_mail_notes(answer["ids"])
            if config.mail.auto_read and answer["ids"] else None)
    last_mail.update(at=time.strftime("%Y-%m-%d %H:%M"), queued=answer["queued"],
                     detail=f"{answer['messages']} message(s), {answer['queued']} queued"
                            + (f", {read['stored']} note(s) stored" if read else ""))
    event(request, "mail.fetch", config.mail.folder, last_mail["detail"])
    return {**answer, "read": read, "waiting": intake.waiting(config)}


class MailSettings(BaseModel):
    provider: str = ""
    user: str = ""
    password: str | None = None
    folder: str = "INBOX"
    every_minutes: int = 15
    mark_read: bool = True
    auto_read: bool = True
    host: str = ""
    port: int | None = None
    security: str = ""
    oauth: bool = False
    client_id: str = ""
    tenant: str = ""


def _mail_state():
    mail = config.mail
    return {"provider": mail.provider, "host": mail.host, "port": mail.port, "user": mail.user,
            "folder": mail.folder, "security": mail.security, "mark_read": mail.mark_read,
            "auto_read": mail.auto_read,
            "every_minutes": mail.every_minutes, "configured": mail.enabled,
            "password_set": bool(mail.password), "oauth": mail.oauth,
            "client_id": mail.client_id, "tenant": mail.tenant}


@app.get("/api/mail/settings")
def mail_settings():
    """What the mailbox is set to. The password itself never leaves the machine."""
    return {"providers": mailbox.providers(), "current": _mail_state(), **last_mail}


@app.post("/api/mail/settings")
def save_mail_settings(request: Request, body: MailSettings):
    global config
    provider = body.provider.strip().lower()
    if provider and provider not in mailbox.PROVIDERS:
        raise HTTPException(422, f"Unknown provider: {provider}.")
    if body.security and body.security not in ("ssl", "starttls"):
        raise HTTPException(422, "Security is ssl or starttls.")

    chosen = mailbox.preset(provider)
    by_hand = provider in ("", "other")
    set_setting("MAIL_PROVIDER", provider)
    set_setting("IMAP_USER", body.user.strip())
    set_setting("IMAP_FOLDER", body.folder.strip() or "INBOX")
    set_setting("IMAP_MARK_READ", "1" if body.mark_read else "0")
    set_setting("IMAP_AUTO_READ", "1" if body.auto_read else "0")
    set_setting("MAIL_EVERY_MINUTES", str(min(max(body.every_minutes, 1), 1440)))
    set_setting("IMAP_HOST", body.host.strip() if by_hand else "")
    set_setting("IMAP_PORT", str(body.port) if (by_hand and body.port) else "")
    set_setting("IMAP_SECURITY", body.security if by_hand else "")
    if body.password:
        set_setting("IMAP_PASSWORD", body.password)
    elif not body.user.strip():
        set_setting("IMAP_PASSWORD", "")

    # Only Microsoft takes a token, so the switch means nothing anywhere else.
    set_setting(mail_oauth.OAUTH_KEY, "1" if (body.oauth and provider == "outlook") else "")
    set_setting(mail_oauth.CLIENT_ID_KEY, body.client_id.strip())
    set_setting(mail_oauth.TENANT_KEY, body.tenant.strip())

    config = replace(config, mail=load_config(data_dir=config.data_dir, user=config.user).mail)
    event(request, "mail.settings", provider or "other", config.mail.host)
    return {"current": _mail_state(), "help": chosen["help"]}


@app.post("/api/slack/check")
def check_slack(request: Request):
    """Try the stored tokens against Slack. Neither of them is in the answer."""
    ok, said = slack_bot.check()
    event(request, "chat.check", "slack", "ok" if ok else said[:80])
    return {"ok": ok, "detail": said}


@app.get("/api/mail/oauth")
def mail_oauth_state():
    """Where the Microsoft sign-in stands. No token is ever in this answer."""
    return mail_oauth.state()


@app.post("/api/mail/oauth/start")
def mail_oauth_start(request: Request):
    """Ask Microsoft for a code. The waiting is done on a thread, not on this request."""
    try:
        started = mail_oauth.start()
    except mail_oauth.OAuthError as e:
        raise HTTPException(409, str(e)) from e
    event(request, "mail.oauth.start", config.mail.user or "outlook")
    return {**started, "state": mail_oauth.state()}


@app.post("/api/mail/oauth/forget")
def mail_oauth_forget(request: Request):
    mail_oauth.forget()
    event(request, "mail.oauth.forget", config.mail.user or "outlook")
    return mail_oauth.state()


@app.get("/api/baskets")
def read_baskets():
    return {"baskets": baskets.open_baskets(config.db_path), "chat": discord_bot.status(),
            "slack": slack_bot.status()}


@app.post("/api/baskets/{channel}/order")
def basket_to_order(request: Request, channel: str):
    """What the chat asked for becomes a purchase order, for a person to place."""
    answer = baskets.to_purchase_order(config.db_path, channel, who(request)[0])
    if answer is None:
        raise HTTPException(404, "That basket is empty.")
    event(request, "basket.order", f"list #{answer['list_id']}", f"{len(answer['lines'])} line(s)")
    return {"list_id": answer["list_id"], "lines": len(answer["lines"]), "shop": answer["shop"]}


# ---- the gate on what the bot buys ----------------------------------------

class GateAsk(BaseModel):
    channel: str | None = None
    shop: str | None = None
    site: str | None = None
    work_item: str | None = None
    supplier: str | None = None
    lines: list[dict]


class GateWord(BaseModel):
    why: str | None = None


@app.get("/api/chain")
def read_chain(limit: int = 200):
    """The sealed log, whether it still holds, and what the two agents have done."""
    whole, broken, entries = gate.verify_chain(config.db_path)
    rows = gate.chain(config.db_path)[-min(max(limit, 1), 500):]
    held = gate.waiting(config.db_path)
    in_flight = {str(row["channel"]) for row in held if row["channel"]}
    now = gate.where_we_are(config.db_path)
    standing = agents.status()

    faults = {}
    if not whole:
        # A seal that does not hold is a fault of the whole round, so it lands on the step
        # the round is standing on, which is where somebody is looking.
        faults[str(now["step"] or 1)] = f"the sealed log is broken at entry {broken}"
    if not standing["ready"]:
        faults["3"] = "Terminus cannot be reached: " + standing["detail"]
    return {
        "log": {"whole": whole, "broken_at": broken, "entries": entries,
                "last_seal": gate.last_seal(config.db_path)},
        "entries": list(reversed(rows)),
        "doings": gate.doings(config.db_path),
        "agents": standing,
        "rules": {"ceiling": gate.CEILING, "dear": gate.DEAR, "drift": gate.DRIFT},
        "words": gate.REASON_WORDS,
        "steps": [{"number": number, "name": name, "detail": detail,
                   "built": number not in gate.UNBUILT}
                  for number, name, detail in gate.STEPS],
        "now": now,
        # Red is for something being wrong, so it only ever comes from something that is.
        "faults": faults,
        "approval": held,
        # A basket is emptied when it becomes an order, so whatever is still in one is
        # whatever nobody has bought — except the ones already held, which are in flight
        # and are answered for on the line above rather than twice over.
        "baskets": [row for row in baskets.open_baskets(config.db_path)
                    if str(row["channel"]) not in in_flight],
    }


@app.get("/api/chain/step/{step}")
def read_chain_step(step: int, limit: int = 60):
    """What has happened in one wedge of the round: the audit behind a click on it."""
    if step not in {number for number, _, _ in gate.STEPS}:
        raise HTTPException(404, f"There is no step {step}.")
    return gate.step_history(config.db_path, step, limit=min(max(limit, 1), 200))


@app.get("/api/agents")
def read_agents():
    """The two agents and whether they can run. There is no key to show."""
    return agents.status()


@app.get("/api/gate")
def read_gate():
    """What is waiting on a person, what has been decided, and whether the log is whole."""
    whole, broken, entries = gate.verify_chain(config.db_path)
    return {
        "waiting": gate.waiting(config.db_path),
        "history": gate.history(config.db_path),
        "words": gate.REASON_WORDS,
        "rules": {"ceiling": gate.CEILING, "dear": gate.DEAR, "drift": gate.DRIFT},
        "log": {"whole": whole, "broken_at": broken, "entries": entries},
    }


@app.get("/api/gate/{request_id}")
def read_one_gate(request_id: int):
    found = gate.one(config.db_path, request_id)
    if found is None:
        raise HTTPException(404, f"No request #{request_id}.")
    return {"request": found, "log": gate.chain(config.db_path, request_id)}


@app.post("/api/gate")
def ask_gate(request: Request, body: GateAsk):
    """Hold a basket. Nothing is held that nothing holds back."""
    reading = gate.look(config.db_path, body.lines, supplier=body.supplier)
    if not reading["held"]:
        return {"held": False, "reading": reading}
    request_id = gate.ask(config.db_path, reading, channel=body.channel, shop=body.shop,
                          asked_by=who(request)[0], site=body.site, work_item=body.work_item)
    event(request, "gate.ask", f"request #{request_id}", ",".join(reading["reasons"]))
    return {"held": True, "request_id": request_id, "reading": reading}


@app.post("/api/gate/{request_id}/check")
async def check_gate(request: Request, request_id: int):
    """The first factor: Terminus reads the names, and nothing else."""
    found = gate.one(config.db_path, request_id)
    if found is None:
        raise HTTPException(404, f"No request #{request_id}.")
    answer = await verify.check(config.db_path, found["lines"])
    gate.record_check(config.db_path, request_id, answer["verdict"], answer["note"],
                      answer["lines"])
    event(request, "gate.check", f"request #{request_id}", answer["verdict"])
    await asyncio.to_thread(judge.decide, config.db_path, request_id)
    await asyncio.to_thread(policies.auto_decide, config.db_path, request_id)
    return {"request": gate.one(config.db_path, request_id), "agent": answer}


@app.post("/api/gate/{request_id}/approve")
def approve_gate(request: Request, request_id: int):
    """The second factor, and the only one that lets money out."""
    try:
        found = gate.approve(config.db_path, request_id, who(request)[0])
    except ValueError as e:
        raise HTTPException(409, str(e)) from e
    event(request, "gate.approve", f"request #{request_id}", f"{found['total']} EUR")
    return {"request": found}


@app.post("/api/gate/{request_id}/cancel")
def cancel_gate(request: Request, request_id: int, body: GateWord | None = None):
    try:
        found = gate.cancel(config.db_path, request_id, who(request)[0],
                            body.why if body else None)
    except ValueError as e:
        raise HTTPException(409, str(e)) from e
    event(request, "gate.cancel", f"request #{request_id}", body.why if body else None)
    return {"request": found}


@app.get("/api/prices/{article}")
def read_prices(article: str):
    return {"article": article, "usual": prices.usual(config.db_path, article),
            "seen": prices.sightings(config.db_path, article),
            "suppliers": sorted(prices.suppliers(config.db_path, article))}


@app.delete("/api/baskets/{channel}")
def drop_basket(request: Request, channel: str):
    held = baskets.clear(config.db_path, channel)
    if not held:
        raise HTTPException(404, "That basket is empty.")
    event(request, "basket.clear", channel, f"{held} line(s)")
    return {"dropped": held}


class ChatSettings(BaseModel):
    token: str | None = None
    channels: str = ""
    forget: bool = False


class SlackSettings(BaseModel):
    bot_token: str | None = None
    app_token: str | None = None
    channels: str = ""
    forget: bool = False


@app.get("/api/chat/settings")
def chat_settings():
    """Whether either bot can run. No token ever leaves the machine."""
    return {"discord": discord_bot.status(), "slack": slack_bot.status()}


@app.post("/api/chat/settings")
def save_chat_settings(request: Request, body: ChatSettings):
    channels = ",".join(piece.strip() for piece in body.channels.split(",") if piece.strip())
    set_setting(discord_bot.CHANNELS_KEY, channels)
    if body.forget:
        set_setting(discord_bot.TOKEN_KEY, "")
    elif body.token:
        set_setting(discord_bot.TOKEN_KEY, body.token.strip())
    event(request, "chat.settings", "discord", f"{len(channels.split(',')) if channels else 0} channel(s)")
    return {"discord": discord_bot.status(), "slack": slack_bot.status()}


@app.post("/api/slack/settings")
def save_slack_settings(request: Request, body: SlackSettings):
    """Two tokens: the bot says things with one and holds the socket open with the other."""
    channels = ",".join(piece.strip() for piece in body.channels.split(",") if piece.strip())
    set_setting(slack_bot.CHANNELS_KEY, channels)
    if body.forget:
        set_setting(slack_bot.BOT_TOKEN_KEY, "")
        set_setting(slack_bot.APP_TOKEN_KEY, "")
    else:
        if body.bot_token:
            set_setting(slack_bot.BOT_TOKEN_KEY, body.bot_token.strip())
        if body.app_token:
            set_setting(slack_bot.APP_TOKEN_KEY, body.app_token.strip())
    event(request, "chat.settings", "slack",
          f"{len(channels.split(',')) if channels else 0} channel(s)")
    return {"discord": discord_bot.status(), "slack": slack_bot.status()}


@app.post("/api/mail/check")
async def check_mail(request: Request):
    if not config.mail.enabled:
        return {"ok": False, "detail": "No mailbox configured."}
    ok, detail = await asyncio.to_thread(mailbox.check, config.mail)
    event(request, "mail.check", config.mail.host, detail[:200])
    return {"ok": ok, "detail": detail}


@app.get("/api/import/{intake_id}")
def preview_sheet(intake_id: int, target: str = "inventory"):
    if target not in ("inventory", "catalog"):
        raise HTTPException(422, f"Unknown target: {target}.")
    try:
        raw, media_type, item = intake.bytes_of(config, intake_id)
    except KeyError as e:
        raise HTTPException(404, "That file is no longer in the queue.") from e
    try:
        header, body = sheets.read_table(raw, media_type)
    except Exception as e:
        raise HTTPException(422, f"Unreadable spreadsheet: {e}") from e
    return {"intake_id": intake_id, "name": item["name"], "header": header,
            "rows": len(body), "sample": body[:sheets.PREVIEW_ROWS],
            "mapping": sheets.guess_mapping(header, target),
            "fields": sheets.INVENTORY_FIELDS if target == "inventory" else sheets.CATALOG_FIELDS}


class SheetImport(BaseModel):
    target: str = "inventory"
    mapping: dict[str, int] = {}
    dry_run: bool = False


@app.post("/api/import/{intake_id}")
def import_sheet(request: Request, intake_id: int, body: SheetImport):
    if body.target not in ("inventory", "catalog"):
        raise HTTPException(422, f"Unknown target: {body.target}.")
    try:
        raw, media_type, item = intake.bytes_of(config, intake_id)
    except KeyError as e:
        raise HTTPException(404, "That file is no longer in the queue.") from e
    try:
        header, rows = sheets.read_table(raw, media_type)
        kept, dropped = sheets.shape(header, rows, body.mapping, body.target)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e

    if body.dry_run:
        return {"kept": len(kept), "dropped": dropped[:20], "sample": kept[:5]}
    if not kept:
        raise HTTPException(422, "Nothing in that file could be read as a line.")

    if body.target == "inventory":
        answer = sheets.store_inventory(config.db_path, kept, who(request)[0])
    else:
        answer = sheets.store_catalog(config.db_path, kept)
    intake.mark(config, intake_id, intake.DONE, f"{len(kept)} line(s) into {body.target}")
    event(request, "import", item["name"], f"{len(kept)} line(s) into {body.target}")
    return {**answer, "dropped": dropped[:20], "target": body.target}


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


def _photo(path, sha=None):
    """A stored photo, or the packed page that replaced it once the photo was old."""
    if (not path or not Path(path).exists()) and sha:
        page = archive.get(config.db_path, sha)
        if page:
            return Response(page, media_type="image/png")
    if not path or not Path(path).exists():
        raise HTTPException(404, "No photo.")
    return FileResponse(path)


@app.get("/api/photo/{note_id}")
def photo(note_id: int):
    rows = db.read(config.db_path, "SELECT image_path, image_sha256 FROM notes WHERE note_id = ?",
                   [note_id])
    return _photo(*(rows[0] if rows else (None, None)))


class BillBody(BaseModel):
    bill: dict
    sha256: str | None = None
    intake_id: int | None = None
    sender: str | None = None


@app.get("/api/bills")
def read_bills():
    """What the months cost. The bills themselves are not listed on the screen."""
    return {"months": bills.by_month(config.db_path),
            "summary": bills.summary(config.db_path)}


async def _read_bill(raw, media_type):
    try:
        return await get_backend().extract_bill(raw, media_type)
    except llm.ExtractionError as e:
        raise HTTPException(502, str(e)) from e


@app.post("/api/bills/read")
async def read_bill_photo(request: Request, photo: UploadFile):
    """A bill dropped straight on the screen, same road as a delivery note."""
    raw, media_type, sha = await _photo_bytes(photo)
    already = bills.find_by_image(config.db_path, sha)
    if already is not None:
        raise HTTPException(409, f"This bill is already stored (bill #{already}).")
    read = await _read_bill(raw, media_type)
    photos.keep(config.images_dir / f"{sha}{SUPPORTED[media_type]}", raw)
    event(request, "bill.read", photo.filename or "upload")
    return {"sha256": sha, "bill": read}


@app.post("/api/intake/{intake_id}/bill")
async def read_queued_bill(request: Request, intake_id: int):
    """Read a queued scan or PDF as a bill, not as a delivery note."""
    try:
        raw, media_type, item = intake.bytes_of(config, intake_id)
    except KeyError as e:
        raise HTTPException(404, "That file is no longer in the queue.") from e
    if media_type not in SUPPORTED:
        raise HTTPException(422, "A spreadsheet is imported, not read as a bill.")

    already = bills.find_by_image(config.db_path, item["sha256"])
    if already is not None:
        intake.mark(config, intake_id, intake.DONE, f"already bill #{already}")
        raise HTTPException(409, f"This bill is already stored (bill #{already}).")

    read = await _read_bill(raw, media_type)
    photos.keep(config.images_dir / f"{item['sha256']}{SUPPORTED[media_type]}", raw)
    event(request, "bill.read", item["name"])
    return {"sha256": item["sha256"], "bill": read, "intake_id": intake_id,
            "sender": item.get("sender")}


@app.post("/api/bills")
def save_bill(request: Request, body: BillBody):
    source = "mail" if body.sender else ("file" if body.sha256 else "hand")
    row = bills.shape(body.bill, source=source, user=who(request)[0], sender=body.sender,
                      sha256=body.sha256 or None, path=_stored_photo(body.sha256) or None)
    wrong = bills.problems(row)
    if wrong:
        raise HTTPException(422, "To fix: " + ", ".join(wrong))

    twin = bills.find_same(config.db_path, row["supplier"], row["number"])
    if twin is not None:
        raise HTTPException(409, f"{row['supplier']} {row['number']} is already stored "
                                 f"(bill #{twin}).")
    try:
        bill_id = bills.save(config.db_path, row)
    except duckdb.ConstraintException as e:
        raise HTTPException(409, "This bill is already stored.") from e

    if body.intake_id is not None:
        intake.mark(config, body.intake_id, intake.DONE, f"bill #{bill_id}")
    event(request, "bill.save", f"bill #{bill_id}",
          f"{row['supplier']} · {row['total']} {row['currency']}")
    return {"bill_id": bill_id, "bill": bills.one(config.db_path, bill_id)}


class MailRef(BaseModel):
    list_id: int | None = None
    quote_id: int | None = None
    mail_id: int | None = None
    supplier: str | None = None


class DraftEdit(BaseModel):
    to: str | None = None
    subject: str | None = None
    body: str | None = None


def _outbox_work(request, action, target, work):
    """The mails to suppliers: a refusal is in words the person can act on."""
    try:
        done = work()
    except (ValueError, outbox.SendError) as e:
        raise HTTPException(422, str(e)) from e
    event(request, f"supplier.{action}", target, None)
    return {**(done if isinstance(done, dict) else {}),
            "outbox": outbox.overview(config.db_path, config.mail)}



# ---- dashboard, notices, autonomy ------------------------------------------

_autonomy_cache = {"at": 0.0, "value": None}


@app.get("/api/dashboard")
def read_dashboard():
    return dashboard.build(config.db_path)


@app.get("/api/planned-orders")
def read_planned_orders():
    return dashboard.planned_orders(config.db_path)


@app.get("/api/notices")
def read_notices():
    return {"notices": watch.open_notices(config.db_path), "notify_via": watch.channels()}


@app.post("/api/notices/scan")
def scan_notices(request: Request):
    done = watch.run(config.db_path, config)
    event(request, "notices.scan", f"{done['new']} new", None)
    return {**done, "notices": watch.open_notices(config.db_path)}


@app.get("/api/autonomy")
def read_autonomy():
    """The figure on the landing page. Worked out at most every few seconds."""
    now = time.monotonic()
    if _autonomy_cache["value"] is None or now - _autonomy_cache["at"] > 4:
        _autonomy_cache.update(at=now, value=autonomy.measure(config.db_path, config=config))
    return _autonomy_cache["value"]


def _do(request, action, target, work):
    """Writes that can be refused in words: the refusal is the person's to read."""
    try:
        done = work()
    except (ValueError, documents.Unavailable) as e:
        raise HTTPException(422, str(e)) from e
    event(request, action, target, None)
    return done


# ---- calendar, supplier terms, exchange rates ----------------------------------

class ClosedDay(BaseModel):
    day: str
    label: str | None = None


class Terms(BaseModel):
    supplier: str
    cutoff: str | None = None
    closed_days: str | None = None
    payment_days: int | None = None
    currency: str | None = None
    note: str | None = None


class Rate(BaseModel):
    day: str
    currency: str
    per_eur: float


@app.get("/api/calendar")
def read_calendar(year: int | None = None):
    year = year or date.today().year
    return {"year": year, "days": workdays.calendar_view(config.db_path, year),
            "terms": workdays.all_terms(config.db_path),
            "rates": [{"day": d.isoformat(), "currency": c, "per_eur": r} for d, c, r in db.read(
                config.db_path, "SELECT day, currency, per_eur FROM fx_rates ORDER BY day DESC, "
                                "currency LIMIT 40")]}


@app.post("/api/calendar/close")
def close_a_day(request: Request, body: ClosedDay):
    day = parse_date(body.day)
    if day is None:
        raise HTTPException(422, "That day is not a date.")
    workdays.close_day(config.db_path, day, body.label)
    event(request, "calendar.close", day.isoformat(), body.label)
    return read_calendar(day.year)


@app.delete("/api/calendar/{day}")
def reopen_a_day(request: Request, day: str):
    when = parse_date(day)
    if when is None or not workdays.reopen_day(config.db_path, when):
        raise HTTPException(404, "That day is not one you closed.")
    event(request, "calendar.open", when.isoformat())
    return read_calendar(when.year)


@app.put("/api/terms")
def put_terms(request: Request, body: Terms):
    name = _do(request, "terms.set", body.supplier, lambda: workdays.set_terms(
        config.db_path, body.supplier, body.cutoff, body.closed_days, body.payment_days,
        body.currency, body.note))
    return {"supplier": name, "terms": workdays.all_terms(config.db_path)}


@app.post("/api/rates")
def put_rate(request: Request, body: Rate):
    day = parse_date(body.day)
    if day is None:
        raise HTTPException(422, "That day is not a date.")
    _do(request, "rate.set", body.currency, lambda: workdays.set_rate(
        config.db_path, day, body.currency, body.per_eur))
    return read_calendar(day.year)


@app.post("/api/rates/fetch")
def fetch_rates(request: Request):
    try:
        got = workdays.fetch_rates(config.db_path)
    except Exception as e:
        raise HTTPException(502, f"The rates could not be fetched: {e}") from e
    event(request, "rate.fetch", got["day"], f"{got['stored']} currencies")
    return {**got, **read_calendar()}


# ---- pack sizes ---------------------------------------------------------------

class Pack(BaseModel):
    article: str
    pack_qty: float
    supplier: str | None = ""
    base_unit: str | None = "piece"
    pack_label: str | None = None
    moq_packs: int = 1


@app.get("/api/units")
def read_units():
    return {"packs": units.listing(config.db_path)}


@app.put("/api/units")
def put_pack(request: Request, body: Pack):
    _do(request, "units.set", body.article, lambda: units.set_pack(
        config.db_path, body.article, body.pack_qty, body.supplier or "", body.base_unit or "piece",
        body.pack_label, body.moq_packs))
    return read_units()


@app.delete("/api/units")
def delete_pack(request: Request, article: str, supplier: str = ""):
    if not units.remove(config.db_path, article, supplier):
        raise HTTPException(404, "No such pack size.")
    event(request, "units.delete", article)
    return read_units()


# ---- quote answers, comparison, awards --------------------------------------------

class Offer(BaseModel):
    quote_id: int
    line_no: int
    unit_price: float
    lead_days: int | None = None
    valid_until: str | None = None
    note: str | None = None


class Award(BaseModel):
    quote_id: int
    line_no: int
    valid_to: str | None = None


class Others(BaseModel):
    suppliers: list[str]


@app.get("/api/offers/{quote_id}")
def read_offers(quote_id: int):
    return {"offers": pricing.offers(config.db_path, quote_id)}


@app.put("/api/offers")
def put_offer(request: Request, body: Offer):
    _do(request, "offer.set", f"quote #{body.quote_id} line {body.line_no}", lambda: pricing.set_offer(
        config.db_path, body.quote_id, body.line_no, body.unit_price, body.lead_days,
        body.valid_until, body.note, who(request)[0]))
    return {"offers": pricing.offers(config.db_path, body.quote_id), **pricing.compare(config.db_path)}


@app.post("/api/quotes/{quote_id}/ask-others")
def ask_other_suppliers(request: Request, quote_id: int, body: Others):
    made = _do(request, "quote.copy", f"quote #{quote_id}", lambda: pricing.ask_others(
        config.db_path, quote_id, body.suppliers, who(request)[0]))
    return {"made": made, "quotes": quotes.recent(config.db_path)}


@app.get("/api/compare")
def compare_offers():
    return pricing.compare(config.db_path)


@app.post("/api/award")
def award_offer(request: Request, body: Award):
    done = _do(request, "offer.award", f"quote #{body.quote_id} line {body.line_no}",
               lambda: pricing.award(config.db_path, body.quote_id, body.line_no, body.valid_to,
                                     who(request)[0]))
    return {**done, **pricing.compare(config.db_path)}


@app.delete("/api/agreements/{agreement_id}")
def end_agreement(request: Request, agreement_id: int):
    if not pricing.end(config.db_path, agreement_id):
        raise HTTPException(404, "That agreement is not active.")
    event(request, "agreement.end", f"agreement #{agreement_id}")
    return pricing.compare(config.db_path)


# ---- disputes -------------------------------------------------------------------------

class Dispute(BaseModel):
    bill_id: int


class Credit(BaseModel):
    ref: str | None = None
    amount: float


@app.get("/api/disputes")
def read_disputes():
    return {"disputes": disputes.listing(config.db_path)}


@app.post("/api/disputes")
def open_dispute(request: Request, body: Dispute):
    made = _do(request, "dispute.open", f"bill #{body.bill_id}", lambda: disputes.open_for_bill(
        config.db_path, body.bill_id, who(request)[0]))
    return {"dispute": made, "disputes": disputes.listing(config.db_path),
            "outbox": outbox.overview(config.db_path, config.mail)}


@app.post("/api/disputes/{dispute_id}/credit")
def credit_dispute(request: Request, dispute_id: int, body: Credit):
    _do(request, "dispute.credit", f"dispute #{dispute_id}", lambda: disputes.record_credit(
        config.db_path, dispute_id, body.ref, body.amount))
    return {"disputes": disputes.listing(config.db_path)}


@app.post("/api/disputes/{dispute_id}/close")
def close_dispute(request: Request, dispute_id: int):
    if not disputes.close(config.db_path, dispute_id):
        raise HTTPException(404, "That dispute is not open.")
    event(request, "dispute.close", f"dispute #{dispute_id}")
    return {"disputes": disputes.listing(config.db_path)}


# ---- deliveries to sites ---------------------------------------------------------------

class NewDispatch(BaseModel):
    list_id: int
    vehicle_id: int | None = None
    planned_on: str | None = None
    note: str | None = None


class Handover(BaseModel):
    received_by: str
    delivered: dict[int, float] | None = None
    note: str | None = None


class Failure(BaseModel):
    note: str = ""


@app.get("/api/dispatches")
def read_dispatches():
    return {"dispatches": outbound.listing(config.db_path),
            "vehicles": [{"vehicle_id": i, "name": n} for i, n in db.read(
                config.db_path, "SELECT vehicle_id, name FROM vehicles ORDER BY vehicle_id")]}


@app.get("/api/dispatches/{dispatch_id}")
def read_dispatch(dispatch_id: int):
    held = outbound.one(config.db_path, dispatch_id)
    if held is None:
        raise HTTPException(404, "No such dispatch.")
    return held


@app.post("/api/dispatches")
def new_dispatch(request: Request, body: NewDispatch):
    made = _do(request, "dispatch.create", f"order #{body.list_id}", lambda: outbound.create_from_order(
        config.db_path, body.list_id, body.vehicle_id, body.planned_on, who(request)[0], body.note))
    return {"dispatch": made, **read_dispatches()}


@app.post("/api/dispatches/{dispatch_id}/load")
def load_dispatch(request: Request, dispatch_id: int):
    _do(request, "dispatch.load", f"dispatch #{dispatch_id}", lambda: outbound.load(
        config.db_path, dispatch_id))
    return read_dispatches()


@app.post("/api/dispatches/{dispatch_id}/deliver")
def deliver_dispatch(request: Request, dispatch_id: int, body: Handover):
    _do(request, "dispatch.deliver", f"dispatch #{dispatch_id}", lambda: outbound.deliver(
        config.db_path, dispatch_id, body.received_by, body.delivered, body.note))
    return read_dispatches()


@app.post("/api/dispatches/{dispatch_id}/fail")
def fail_dispatch(request: Request, dispatch_id: int, body: Failure):
    _do(request, "dispatch.fail", f"dispatch #{dispatch_id}", lambda: outbound.fail(
        config.db_path, dispatch_id, body.note))
    return read_dispatches()


def _pdf(make, name):
    return Response(make(), media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{name}"'})


@app.get("/api/dispatches/{dispatch_id}/note.pdf")
def dispatch_note_pdf(dispatch_id: int):
    return _pdf_or_422(lambda: documents.delivery_note_pdf(config.db_path, dispatch_id),
                       f"bl-{dispatch_id}.pdf")


@app.get("/api/dispatches/{dispatch_id}/labels.pdf")
def dispatch_labels_pdf(dispatch_id: int):
    return _pdf_or_422(lambda: documents.labels_pdf(config.db_path, dispatch_id),
                       f"etiquettes-{dispatch_id}.pdf")


def _pdf_or_422(make, name):
    try:
        return _pdf(make, name)
    except (ValueError, documents.Unavailable) as e:
        raise HTTPException(422, str(e)) from e


# ---- archive ---------------------------------------------------------------------------

@app.get("/api/archive")
def read_archive():
    return archive.summary(config.db_path)


@app.post("/api/archive/run")
def run_archive(request: Request):
    done = archive.sweep(config.db_path, config.images_dir, 0)
    event(request, "archive.run", f"{done['pages']} page(s)", f"{done['before']} -> {done['after']} bytes")
    return {**done, **archive.summary(config.db_path)}

@app.get("/api/outbox")
def read_outbox():
    return outbox.overview(config.db_path, config.mail)


@app.post("/api/outbox/draft-order")
def outbox_draft_order(request: Request, body: MailRef):
    return _outbox_work(request, "draft", f"order #{body.list_id}", lambda: outbox.draft_order(
        config.db_path, body.list_id, who(request)[0]))


@app.post("/api/outbox/draft-quote")
def outbox_draft_quote(request: Request, body: MailRef):
    return _outbox_work(request, "draft", f"quote #{body.quote_id}", lambda: outbox.draft_quote(
        config.db_path, body.quote_id, who(request)[0]))


@app.post("/api/outbox/draft-chase")
def outbox_draft_chase(request: Request, body: MailRef):
    return _outbox_work(request, "draft", f"chase #{body.mail_id}", lambda: outbox.draft_chase(
        config.db_path, body.mail_id, who(request)[0]))


@app.post("/api/outbox/draft-backorder")
def outbox_draft_backorder(request: Request, body: MailRef):
    return _outbox_work(request, "draft", body.supplier or "", lambda: outbox.draft_backorder(
        config.db_path, body.supplier or "", who(request)[0]))


@app.patch("/api/outbox/{mail_id}")
def outbox_edit(request: Request, mail_id: int, body: DraftEdit):
    return _outbox_work(request, "edit", f"mail #{mail_id}", lambda: {
        "draft": outbox.update_draft(config.db_path, mail_id, body.to, body.subject, body.body)})


@app.delete("/api/outbox/{mail_id}")
def outbox_discard(request: Request, mail_id: int):
    if not outbox.discard(config.db_path, mail_id):
        raise HTTPException(404, "That draft is not there, or it has already been sent.")
    event(request, "supplier.discard", f"mail #{mail_id}")
    return {"outbox": outbox.overview(config.db_path, config.mail)}


@app.post("/api/outbox/{mail_id}/send")
def outbox_send(request: Request, mail_id: int):
    """The one place a mail leaves: only on this call, for a draft a person has seen."""
    return _outbox_work(request, "send", f"mail #{mail_id}", lambda: {
        "sent": outbox.send(config.db_path, mail_id, config.mail, who(request)[0])})


@app.post("/api/outbox/{mail_id}/close")
def outbox_close(request: Request, mail_id: int):
    if not outbox.close(config.db_path, mail_id):
        raise HTTPException(404, "That order is not waiting.")
    event(request, "supplier.close", f"mail #{mail_id}")
    return {"outbox": outbox.overview(config.db_path, config.mail)}


# ---- policies and the bots' charters ---------------------------------------------

class PolicyBody(BaseModel):
    name: str = ""
    action: str = ""
    supplier: str | None = None
    max_total: float | None = None
    per_day: float | None = None
    per_month: float | None = None
    price_band: float = 0.10
    min_deliveries: int = 3
    allowed_reasons: str = ""
    terminus_same: bool = True
    hours_from: int = 8
    hours_to: int = 17
    per_supplier_day: int = 2
    max_chases: int = 2


class PolicyChange(BaseModel):
    active: bool | None = None
    mode: str | None = None
    max_total: float | None = None
    price_band: float | None = None
    per_day: float | None = None
    per_month: float | None = None


class Switch(BaseModel):
    on: bool


class LimitValue(BaseModel):
    value: float


@app.get("/api/policies")
def read_policies():
    return policies.overview(config.db_path)


@app.post("/api/policies")
def create_policy(request: Request, body: PolicyBody):
    made = _do(request, "policy.create", body.name, lambda: policies.create(
        config.db_path, body.model_dump(), who(request)[0]))
    return {"policy": made, **policies.overview(config.db_path)}


@app.patch("/api/policies/{policy_id}")
def change_policy(request: Request, policy_id: int, body: PolicyChange):
    _do(request, "policy.update", f"policy #{policy_id}", lambda: policies.update(
        config.db_path, policy_id, body.model_dump(exclude_none=True), who(request)[0]))
    return policies.overview(config.db_path)


@app.delete("/api/policies/{policy_id}")
def delete_policy(request: Request, policy_id: int):
    if not policies.remove(config.db_path, policy_id):
        raise HTTPException(404, "No such policy.")
    event(request, "policy.delete", f"policy #{policy_id}")
    return policies.overview(config.db_path)


@app.post("/api/policies/switch")
def switch_policies(request: Request, body: Switch):
    policies.set_enabled(config.db_path, body.on, who(request)[0])
    return policies.overview(config.db_path)


@app.post("/api/policies/run")
def run_policies(request: Request):
    """Offer what is waiting to the policies now, rather than at the next hour."""
    done = policies.run(config.db_path, config)
    event(request, "policy.run", str(done.get("acted", 0)), done.get("skipped"))
    return {**done, **policies.overview(config.db_path)}


@app.get("/api/charters")
def read_charters():
    return charters.overview(config.db_path)


@app.put("/api/charters/{bot}/{key}")
def put_limit(request: Request, bot: str, key: str, body: LimitValue):
    _do(request, "charter.set", f"{bot} {key}", lambda: charters.set_limit(
        config.db_path, bot, key, body.value))
    return charters.overview(config.db_path)


@app.delete("/api/charters/{bot}/{key}")
def reset_limit(request: Request, bot: str, key: str):
    charters.reset(config.db_path, bot, key)
    event(request, "charter.reset", f"{bot} {key}")
    return charters.overview(config.db_path)


# ---- Plutus orders from the DDMRP, Terminus decides baskets ------------------------

class Mode(BaseModel):
    mode: str


@app.get("/api/replenish")
def read_replenish():
    """What the DDMRP buffers ask for now, and what is left alone because it is already on its way."""
    return replenish.overview(config.db_path)


@app.post("/api/replenish/run")
def run_replenish(request: Request, kind: str = "cycle"):
    if kind not in ("cycle", "urgent"):
        raise HTTPException(422, "The order is a cycle order or an urgent one.")
    done = replenish.place(config.db_path, who(request)[0] or "PLUTUS", kind=kind)
    event(request, "replenish.run", f"order #{done['list_id']}" if done["list_id"] else "nothing to order",
          f"{kind}, {done['lines']} line(s)")
    return {**done, **replenish.overview(config.db_path)}


@app.post("/api/replenish/auto")
def switch_replenish(request: Request, body: Switch):
    replenish.set_auto(config.db_path, body.on, who(request)[0])
    return replenish.overview(config.db_path)


@app.get("/api/judge")
def read_judge():
    return judge.status(config.db_path)


@app.post("/api/judge/mode")
def set_judge_mode(request: Request, body: Mode):
    _do(request, "terminus.mode", body.mode, lambda: judge.set_mode(config.db_path, body.mode, who(request)[0]))
    return judge.status(config.db_path)


@app.post("/api/judge/run")
def run_judge(request: Request):
    done = judge.decide(config.db_path)
    event(request, "terminus.run", f"{len(done)} basket(s)")
    return {"decided": [{"request_id": i, "does": what} for i, what in done], **judge.status(config.db_path)}


# ---- the wallet, Plutus's purchasing, and the payments --------------------------------

class Money(BaseModel):
    amount: float
    note: str | None = None


class Bank(BaseModel):
    supplier: str
    iban: str
    bic: str | None = None
    holder: str | None = None


class Payer(BaseModel):
    name: str = ""
    iban: str = ""
    bic: str = ""


def _payer():
    rows = dict(db.read(config.db_path, "SELECT key, value FROM policy_settings WHERE key LIKE 'payer_%'"))
    return {"name": rows.get("payer_name", ""), "iban": rows.get("payer_iban", ""),
            "bic": rows.get("payer_bic", "")}


def _wallet_overview():
    rules = charters.limits(config.db_path, "plutus")
    return {**wallet.overview(config.db_path, rules["wallet_reserve"]),
            "plutus": purchasing.status(config.db_path), "payer": _payer(),
            "payments": payments.listing(config.db_path),
            "log": [r for r in policies.log_recent(config.db_path, 80) if r["policy"] == "PLUTUS"][:40]}


@app.get("/api/wallet")
def read_wallet():
    return _wallet_overview()


@app.post("/api/wallet/deposit")
def wallet_deposit(request: Request, body: Money):
    _do(request, "wallet.deposit", f"{body.amount} EUR", lambda: wallet.deposit(
        config.db_path, body.amount, body.note, who(request)[0]))
    return _wallet_overview()


@app.post("/api/wallet/withdraw")
def wallet_withdraw(request: Request, body: Money):
    _do(request, "wallet.withdraw", f"{body.amount} EUR", lambda: wallet.withdraw(
        config.db_path, body.amount, body.note, who(request)[0]))
    return _wallet_overview()


@app.post("/api/plutus/mode")
def set_plutus_mode(request: Request, body: Mode):
    _do(request, "plutus.mode", body.mode, lambda: purchasing.set_mode(config.db_path, body.mode, who(request)[0]))
    return _wallet_overview()


@app.post("/api/plutus/run")
def run_plutus(request: Request):
    done = purchasing.run(config.db_path, config)
    watch.scan(config.db_path)
    event(request, "plutus.run", str(done.get("done", 0)), done.get("skipped"))
    return {**done, **_wallet_overview()}


@app.put("/api/bank")
def put_bank(request: Request, body: Bank):
    _do(request, "bank.set", body.supplier, lambda: payments.set_bank(
        config.db_path, body.supplier, body.iban, body.bic, body.holder))
    return _wallet_overview()


@app.put("/api/payer")
def put_payer(request: Request, body: Payer):
    if body.iban.strip() and payments.valid_iban(body.iban) is None:
        raise HTTPException(422, "That is not a valid IBAN.")
    for key, value in (("payer_name", body.name.strip()), ("payer_iban", body.iban.replace(" ", "").upper()),
                       ("payer_bic", body.bic.strip())):
        db.write(config.db_path, "INSERT INTO policy_settings (key, value) VALUES (?, ?) "
                                 "ON CONFLICT (key) DO UPDATE SET value = excluded.value RETURNING key", [key, value])
    event(request, "payer.set", body.name.strip() or "empty")
    return _wallet_overview()


@app.get("/api/payments/batch/{batch_id}.xml")
def payment_file(request: Request, batch_id: int):
    payer = _payer()
    try:
        xml = payments.batch_file(config.db_path, batch_id, payer["name"], payer["iban"], payer["bic"])
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    event(request, "payment.export", f"batch #{batch_id}")
    return Response(xml, media_type="application/xml",
                    headers={"Content-Disposition": f'attachment; filename="sepa-batch-{batch_id}.xml"'})


@app.post("/api/payments/batch/{batch_id}/executed")
def payments_executed(request: Request, batch_id: int):
    _do(request, "payment.confirm", f"batch #{batch_id}", lambda: payments.confirm(
        config.db_path, batch_id, who(request)[0]))
    return _wallet_overview()


@app.post("/api/payments/{payment_id}/cancel")
def cancel_payment(request: Request, payment_id: int):
    _do(request, "payment.cancel", f"payment #{payment_id}", lambda: payments.cancel(
        config.db_path, payment_id, who(request)[0]))
    return _wallet_overview()


# ---- site requests ----------------------------------------------------------------

class RequestBody(BaseModel):
    site: str
    work_item: str
    needed_by: str | None = None
    text: str = ""
    note: str | None = None


@app.get("/api/requisitions")
def read_requisitions(all: bool = False):
    return {"requests": requisitions.listing(config.db_path, include_done=all)}


@app.post("/api/requisitions")
def open_requisition(request: Request, body: RequestBody):
    items = baskets.parse_items(body.text)
    user = who(request)[0] or "?"
    try:
        made = requisitions.submit(config.db_path, user, body.site, body.work_item, body.needed_by, items, body.note)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    event(request, "request.open", f"request #{made['req_id']}", f"{made['site']} / {made['work_item']}")
    return {"request": made, "requests": requisitions.listing(config.db_path)}


@app.post("/api/requisitions/{req_id}/cancel")
def cancel_requisition(request: Request, req_id: int):
    _do(request, "request.cancel", f"request #{req_id}", lambda: requisitions.cancel(
        config.db_path, req_id, who(request)[0]))
    return {"requests": requisitions.listing(config.db_path)}


# ---- how orders reach a supplier, and the bank drop ------------------------------------

class ChannelBody(BaseModel):
    supplier: str
    url: str
    token: str = ""


class BankDropBody(BaseModel):
    host: str = ""
    port: str = "22"
    user: str = ""
    key: str = ""
    dir: str = ""
    status_dir: str = ""


def _connections():
    return {"channels": channels.listing(config.db_path), "bank": bankdrop.settings(config.db_path),
            "bank_ready": bankdrop.ready(config.db_path)[0]}


@app.get("/api/connections")
def read_connections():
    return _connections()


@app.put("/api/channels")
def put_channel(request: Request, body: ChannelBody):
    _do(request, "channel.set", body.supplier, lambda: channels.save(
        config.db_path, body.supplier, body.url, body.token))
    return _connections()


@app.delete("/api/channels/{supplier}")
def delete_channel(request: Request, supplier: str):
    _do(request, "channel.remove", supplier, lambda: channels.remove(config.db_path, supplier))
    return _connections()


@app.post("/api/channels/{supplier}/test")
def test_channel(request: Request, supplier: str):
    try:
        done = channels.test(config.db_path, supplier)
    except (ValueError, outbox.SendError) as e:
        raise HTTPException(422, str(e)) from e
    event(request, "channel.test", supplier)
    return {"status": done["status"]}


@app.put("/api/bank-drop")
def put_bank_drop(request: Request, body: BankDropBody):
    _do(request, "bankdrop.set", body.host or "empty", lambda: bankdrop.save(
        config.db_path, body.host, body.port, body.user, body.key, body.dir, body.status_dir))
    return _connections()


@app.post("/api/bank-drop/test")
def test_bank_drop(request: Request):
    try:
        bankdrop.check(config.db_path)
    except (ValueError, OSError, subprocess.SubprocessError) as e:
        raise HTTPException(422, str(e)) from e
    event(request, "bankdrop.test", None)
    return {"ok": True}


# ---- users and roles --------------------------------------------------------------

class UserBody(BaseModel):
    user: str
    role: str = "buyer"
    password: str = ""


class RoleBody(BaseModel):
    role: str


class PasswordBody(BaseModel):
    password: str = ""
    old: str = ""
    new: str = ""


@app.get("/api/health")
def read_health():
    return health.report(config)


@app.get("/api/audit/verify")
def verify_audit():
    return audit.verify(config.db_path)


@app.get("/api/users")
def read_users(request: Request):
    return {"users": account.users(), "roles": list(account.ROLES), "me": request.state.user}


@app.post("/api/users")
def add_user(request: Request, body: UserBody):
    _do(request, "user.add", body.user, lambda: account.add_user(body.user, body.role, body.password))
    return read_users(request)


@app.put("/api/users/{name}/role")
def set_user_role(request: Request, name: str, body: RoleBody):
    _do(request, "user.role", name, lambda: account.set_role(name, body.role))
    return read_users(request)


@app.put("/api/users/{name}/password")
def reset_user_password(request: Request, name: str, body: PasswordBody):
    _do(request, "user.reset", name, lambda: account.reset_password(name, body.password))
    return read_users(request)


@app.delete("/api/users/{name}")
def delete_user(request: Request, name: str):
    if name.strip().upper() == (request.state.user or "").upper():
        raise HTTPException(422, "You cannot remove the account you are using.")
    _do(request, "user.remove", name, lambda: account.remove_user(name))
    return read_users(request)


@app.post("/api/account/password")
def change_my_password(request: Request, body: PasswordBody):
    _do(request, "user.password", request.state.user, lambda: account.change_own_password(
        request.state.user, body.old, body.new))
    return {"ok": True}


# ---- receiving and counting -----------------------------

class PlaceBody(BaseModel):
    location: str | None = None


class CountBody(BaseModel):
    article: str
    quantity: float


class QuantityBody(BaseModel):
    quantity: float


@app.get("/api/receiving")
def read_receiving():
    return receiving.overview(config.db_path)


@app.post("/api/receiving/confirm-suggested")
def receiving_confirm(request: Request):
    done = receiving.confirm_suggested(config.db_path, who(request)[0])
    event(request, "receiving.confirm", None, f"{done} line(s)")
    return receiving.overview(config.db_path)


@app.post("/api/receiving/{putaway_id}/put-away")
def receiving_put_away(request: Request, putaway_id: int, body: PlaceBody):
    _do(request, "receiving.putaway", f"line #{putaway_id}", lambda: receiving.put_away(
        config.db_path, putaway_id, body.location, who(request)[0]))
    return receiving.overview(config.db_path)


@app.get("/api/counting")
def read_counting():
    return counting.overview(config.db_path)


@app.post("/api/counting/count")
def count_article(request: Request, body: CountBody):
    try:
        done = counting.submit(config.db_path, body.article, body.quantity, who(request)[0])
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    event(request, "count", done["article"], done["state"])
    return {"result": done, **counting.overview(config.db_path)}


@app.post("/api/counting/{check_id}/settle")
def settle_count(request: Request, check_id: int, body: QuantityBody):
    _do(request, "count.settle", f"count #{check_id}", lambda: counting.settle(
        config.db_path, check_id, body.quantity, who(request)[0]))
    return counting.overview(config.db_path)


# ---- what suppliers asked ------------------------------------------------------------

@app.get("/api/supplier-questions")
def supplier_questions():
    return {"questions": replies.open_questions(config.db_path)}


@app.post("/api/supplier-questions/{mail_id}/answered")
def supplier_question_answered(request: Request, mail_id: int):
    replies.answered(config.db_path, mail_id)
    event(request, "reply.answered", f"mail #{mail_id}")
    return {"questions": replies.open_questions(config.db_path)}


@app.get("/api/tuning")
def read_tuning():
    """What the app has learned from corrections, refusals, stock-outs and short counts."""
    return {"tuning": feedback.listing(config.db_path), "recent": feedback.recent(config.db_path)}


@app.delete("/api/tuning/{article}")
def forget_tuning(request: Request, article: str):
    if not feedback.forget(config.db_path, article):
        raise HTTPException(404, "Nothing is learned for that article.")
    event(request, "tuning.forget", article)
    return read_tuning()


class BillTo(BaseModel):
    company: str = ""
    contact: str = ""
    address: str = ""


@app.get("/api/bill-to")
def read_bill_to():
    """Who orders are billed to unless said otherwise."""
    rows = db.read(config.db_path, "SELECT company, contact, address FROM bill_to_default WHERE id = 1")
    company, contact, address = rows[0] if rows else (None, None, None)
    return {"company": company or "", "contact": contact or "", "address": address or ""}


@app.put("/api/bill-to")
def put_bill_to(request: Request, body: BillTo):
    db.write(config.db_path, """
            INSERT INTO bill_to_default (id, company, contact, address) VALUES (1, ?, ?, ?)
            ON CONFLICT (id) DO UPDATE SET company = excluded.company, contact = excluded.contact,
                address = excluded.address RETURNING id""",
             [body.company.strip(), body.contact.strip(), body.address.strip()])
    event(request, "billto.set", body.company.strip() or "empty")
    return read_bill_to()


@app.get("/api/leadtimes")
def read_lead_times():
    """How long each supplier really takes, from the delivery notes."""
    return leadtimes.overview(config.db_path)


@app.get("/api/match")
def three_way_match():
    """Bills against the deliveries they pay for, and orders against what has come in."""
    return matching.report(config.db_path)


@app.patch("/api/bills/{bill_id}")
def edit_bill(request: Request, bill_id: int, body: dict):
    if bills.one(config.db_path, bill_id) is None:
        raise HTTPException(404, f"No bill #{bill_id}.")
    if not bills.update(config.db_path, bill_id, body):
        raise HTTPException(422, "Nothing to change.")
    event(request, "bill.edit", f"bill #{bill_id}", ", ".join(sorted(body)))
    return {"bill": bills.one(config.db_path, bill_id)}


@app.delete("/api/bills/{bill_id}")
def remove_bill(request: Request, bill_id: int):
    stored = bills.one(config.db_path, bill_id)
    if stored is None:
        raise HTTPException(404, f"No bill #{bill_id}.")
    if not bills.drop(config.db_path, bill_id):
        raise HTTPException(404, f"No bill #{bill_id}.")
    event(request, "bill.delete", f"bill #{bill_id}")
    return {"deleted": bill_id}


@app.get("/api/bill-photo/{bill_id}")
def bill_photo(bill_id: int):
    rows = db.read(config.db_path, "SELECT image_path, image_sha256 FROM bills WHERE bill_id = ?",
                   [bill_id])
    return _photo(*(rows[0] if rows else (None, None)))


# ---- quote requests -------------------------------------------------------

class QuoteBody(BaseModel):
    supplier: str | None = None
    site: str | None = None
    work_item: str | None = None
    needed_by: str | None = None
    note: str | None = None
    lines: list[dict] = []
    text: str | None = None


@app.get("/api/quotes")
def read_quotes():
    return {"quotes": quotes.recent(config.db_path), "units": quotes.units(),
            "summary": quotes.summary(config.db_path)}


@app.get("/api/quotes/{quote_id}")
def read_quote(quote_id: int):
    found = quotes.one(config.db_path, quote_id)
    if found is None:
        raise HTTPException(404, f"No quote #{quote_id}.")
    found["text"] = quotes.as_text(found)
    return found


@app.post("/api/quotes")
def save_quote(request: Request, body: QuoteBody):
    lines = body.lines or quotes.parse_request(body.text)
    if not lines:
        raise HTTPException(422, "Name at least one thing to be quoted.")
    head = body.model_dump(exclude={"lines", "text"})
    try:
        quote_id = quotes.save(config.db_path, head, lines, who(request)[0])
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    event(request, "quote.save", f"quote #{quote_id}",
          f"{head.get('supplier') or 'no supplier'} · {len(lines)} line(s)")
    return {"quote_id": quote_id, "quote": quotes.one(config.db_path, quote_id)}


@app.patch("/api/quotes/{quote_id}")
def edit_quote(request: Request, quote_id: int, body: dict):
    if quotes.one(config.db_path, quote_id) is None:
        raise HTTPException(404, f"No quote #{quote_id}.")
    if not quotes.update(config.db_path, quote_id, body):
        raise HTTPException(422, "Nothing to change.")
    event(request, "quote.edit", f"quote #{quote_id}", ", ".join(sorted(body)))
    return {"quote": quotes.one(config.db_path, quote_id)}


@app.delete("/api/quotes/{quote_id}")
def remove_quote(request: Request, quote_id: int):
    if not quotes.drop(config.db_path, quote_id):
        raise HTTPException(404, f"No quote #{quote_id}.")
    event(request, "quote.delete", f"quote #{quote_id}")
    return {"deleted": quote_id}


# ---- what the shelf holds -------------------------------------------------

@app.get("/api/stock")
def read_stock():
    return stock.summary(config.db_path)


@app.get("/api/tables")
def read_tables():
    """What a question can be asked about, with how many rows there are to ask about."""
    return {"tables": db.table_counts(config.db_path)}


# ---- who the order goes to ------------------------------------------------

class ContactBody(BaseModel):
    company: str
    person: str | None = None
    phone: str | None = None
    email: str | None = None
    address: str | None = None
    incoterm: str | None = None
    site: str | None = None
    note: str | None = None


class OrderTerms(BaseModel):
    contact_id: int | None = None
    incoterm: str | None = None
    ship_by: str | None = None


# Two requests for the contact book can arrive together, and each would find it empty and
# fill it. One fills, the other waits and then reads what the first wrote.
_filling_contacts = threading.Lock()


@app.get("/api/contacts")
def read_contacts():
    found = contacts.find_file()
    book = contacts.all_of(config.db_path)
    if not book and found:
        # Nobody has to press anything: an empty book fills itself from the file.
        with _filling_contacts:
            book = contacts.all_of(config.db_path)
            if not book:
                contacts.load_file(config.db_path, found, "file")
                book = contacts.all_of(config.db_path)
    return {"contacts": _with_lead_times(book), "incoterms": contacts.terms(),
            "file": str(found) if found else None}


def _with_lead_times(rows):
    """Each contact with how long that supplier really takes, learned from the delivery notes."""
    leads = leadtimes.per_supplier(config.db_path)
    for row in rows:
        known = leads.get(str(row.get("company") or "").strip().upper())
        row["lead_days"] = known["plan_days"] if known and known["n"] >= leadtimes.MIN_SAMPLES else None
        row["lead_usual"] = known["days"] if known else None
        row["lead_n"] = known["n"] if known else 0
    return rows


@app.get("/api/contacts/file")
def search_contact_file(name: str | None = None, company: str | None = None,
                        all: bool = False):
    """Straight from the csv on the disk, not from the book in the database."""
    try:
        found = contacts.look_up(name, company, limit=None if all else 50)
        _with_lead_times(found["rows"])
        return found
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e
    except (OSError, UnicodeDecodeError) as e:
        raise HTTPException(422, f"That file could not be read ({e}).") from e


@app.post("/api/contacts/load")
def load_contact_file(request: Request, path: str | None = None):
    """Read the supplier contact file on the disk into the contact book."""
    try:
        done = contacts.load_file(config.db_path, path, who(request)[0])
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e
    except (OSError, UnicodeDecodeError) as e:
        raise HTTPException(422, f"That file could not be read ({e}).") from e
    event(request, "contact.load", done["file"],
          f"{done['added']} added, {done['updated']} updated")
    return {**done, "contacts": contacts.all_of(config.db_path)}


@app.post("/api/contacts")
def save_contact(request: Request, body: ContactBody):
    try:
        contact_id = contacts.save(config.db_path, body.model_dump(), who(request)[0])
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    event(request, "contact.save", f"contact #{contact_id}", body.company)
    return {"contact_id": contact_id, "contacts": contacts.all_of(config.db_path)}


@app.patch("/api/contacts/{contact_id}")
def edit_contact(request: Request, contact_id: int, body: dict):
    if contacts.one(config.db_path, contact_id) is None:
        raise HTTPException(404, f"No contact #{contact_id}.")
    try:
        contacts.update(config.db_path, contact_id, body)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    event(request, "contact.edit", f"contact #{contact_id}", ", ".join(sorted(body)))
    return {"contacts": contacts.all_of(config.db_path)}


@app.delete("/api/contacts/{contact_id}")
def remove_contact(request: Request, contact_id: int):
    if not contacts.drop(config.db_path, contact_id):
        raise HTTPException(404, f"No contact #{contact_id}.")
    event(request, "contact.delete", f"contact #{contact_id}")
    return {"deleted": contact_id}


@app.patch("/api/orders/{list_id}/terms")
def set_order_terms(request: Request, list_id: int, body: OrderTerms):
    if not db.read(config.db_path, "SELECT count(*) FROM purchase_orders WHERE list_id = ?",
                   [list_id])[0][0]:
        raise HTTPException(404, f"No purchase order #{list_id}.")
    contacts.put_on_order(config.db_path, list_id, body.contact_id, body.incoterm, body.ship_by)
    event(request, "order.terms", f"order #{list_id}",
          f"contact {body.contact_id or 'none'} · {body.incoterm or 'no incoterm'}")
    return {"list_id": list_id}


# ---- what a delivery note still owes --------------------------------------

class Waiting(BaseModel):
    expected_on: str | None = None
    settled: bool | None = None


@app.get("/api/backorders")
def read_backorders():
    return {"notes": backorders.notes(config.db_path),
            "lines": backorders.rows(config.db_path),
            "summary": backorders.summary(config.db_path)}


@app.patch("/api/backorders/{note_id}/{line_no}")
def edit_backorder(request: Request, note_id: int, line_no: int, body: Waiting):
    if body.expected_on is not None:
        backorders.expect(config.db_path, note_id, line_no, body.expected_on)
    if body.settled is not None:
        backorders.settle(config.db_path, note_id, line_no, body.settled)
    event(request, "backorder.edit", f"note #{note_id} line {line_no}",
          body.expected_on or ("arrived" if body.settled else "still waiting"))
    return {"summary": backorders.summary(config.db_path),
            "notes": backorders.notes(config.db_path)}


# ---- route wrangler -------------------------------------------------------

class PlaceBody(BaseModel):
    address: str
    label: str | None = None
    kind: str = "stop"
    note: str | None = None
    vehicle_id: int | None = None
    cubic: float | None = None
    kilos: float | None = None
    open_from: str | None = None
    open_to: str | None = None


def _route_screen():
    stops = routing.places(config.db_path, "stop")
    vans = routing.vehicles(config.db_path)
    for van in vans:
        mine = [stop for stop in stops
                if stop["vehicle_id"] == van["vehicle_id"] and not stop["done"]]
        van["stops"] = len(mine)
        van["load"] = routing.load_of(mine)
        van["trouble"] = routing.trouble(van, mine)
    return {"depot": routing.depot(config.db_path), "stops": stops, "vehicles": vans}


@app.get("/api/route")
def read_route_places():
    return _route_screen()


@app.post("/api/route/places")
def add_route_place(request: Request, body: PlaceBody):
    kind = "depot" if body.kind == "depot" else "stop"
    try:
        place_id = routing.add(config.db_path, body.address, body.label, kind,
                               body.note, who(request)[0], vehicle_id=body.vehicle_id,
                               cubic=body.cubic, kilos=body.kilos,
                               open_from=body.open_from, open_to=body.open_to)
    except routing.MapError as e:
        raise HTTPException(422, str(e)) from e
    event(request, "route.place", f"{kind} #{place_id}", body.address)
    return {"place_id": place_id, **_route_screen()}


@app.patch("/api/route/places/{place_id}")
def mark_route_place(place_id: int, body: dict):
    if "done" in body:
        routing.mark(config.db_path, place_id, bool(body.get("done")))
    if "vehicle_id" in body:
        routing.set_vehicle(config.db_path, place_id, body.get("vehicle_id"))
    return _route_screen()


@app.delete("/api/route/places/{place_id}")
def remove_route_place(request: Request, place_id: int):
    if not routing.drop(config.db_path, place_id):
        raise HTTPException(404, f"No place #{place_id}.")
    event(request, "route.delete", f"place #{place_id}")
    return {"deleted": place_id}


@app.delete("/api/route/stops")
def clear_route_stops(request: Request):
    held = routing.clear_stops(config.db_path)
    event(request, "route.clear", f"{held} stop(s)")
    return {"deleted": held}


@app.post("/api/route/plan")
async def plan_route(round_trip: bool = True, vehicle_id: int | None = None):
    """Asks OpenStreetMap for road distances; the only call that leaves this machine."""
    try:
        return await asyncio.to_thread(routing.route, config.db_path, round_trip, None, vehicle_id)
    except routing.MapError as e:
        raise HTTPException(422, str(e)) from e


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

    photos.keep(config.images_dir / f"{sha}{SUPPORTED[media_type]}", raw)
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
    bill_company: str | None = None
    bill_contact: str | None = None
    bill_address: str | None = None
    lines: list[ListLine] = []


class SaveList(BaseModel):
    sha256: str | None = None
    list: HardwareList
    measurement: Measurement | None = None
    # What Plutus showed and what was kept, line by line, so the app can learn from the edit.
    proposal: list[dict] | None = None


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

    if body.proposal:
        feedback.review_order(config.db_path, body.proposal, who(request)[0])

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


@app.get("/api/order-queue")
def read_queue():
    return reorder.listing(config.db_path)


@app.post("/api/order-queue/collect")
def collect_queue(request: Request):
    """Read every line ticked "to order" now, rather than at the next hour."""
    done = reorder.collect(config.db_path)
    event(request, "order.queue.read", f"{done['added']} line(s)",
          f"{done['dropped']} left the queue")
    return {**done, **reorder.listing(config.db_path)}


@app.post("/api/order-queue/place")
def place_queue(request: Request):
    """Order what has waited its fifteen days, now."""
    done = reorder.place_due(config.db_path)
    event(request, "order.queue.place", f"{len(done['orders'])} order(s)", "by Plutus")
    return {**done, **reorder.listing(config.db_path)}


@app.delete("/api/orders/{list_id}")
def remove_list(request: Request, list_id: int):
    _drop(db.delete_purchase_order(config.db_path, list_id), f"No list #{list_id}.")
    event(request, "order.delete", f"order #{list_id}")
    return {"deleted": list_id}


@app.get("/api/order-photo/{list_id}")
def list_photo(list_id: int):
    rows = db.read(config.db_path, "SELECT image_path, image_sha256 FROM purchase_orders "
                                  "WHERE list_id = ?", [list_id])
    return _photo(*(rows[0] if rows else (None, None)))


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


@app.get("/api/shortcuts")
def read_shortcuts():
    """The five doors on the landing page: Slack, Discord, the mailboxes, the contact sheet."""
    return {"shortcuts": shortcuts.listing(config.data_dir)}


@app.post("/api/shortcuts/{key}")
def open_shortcut(request: Request, key: str):
    """Open one of them outside this window. The page sends a name, never a path."""
    try:
        opened = shortcuts.open_it(key, config.data_dir)
    except ValueError as e:
        raise HTTPException(404, str(e)) from e
    except FileNotFoundError as e:
        raise HTTPException(422, str(e)) from e
    except RuntimeError as e:
        raise HTTPException(501, str(e)) from e
    event(request, "shortcut.open", key, opened["where"])
    return opened


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


def _stock_control(request, action, target, work):
    """One door for the stock-control writes: a refusal is the person's to read, not an error."""
    try:
        done = work()
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    event(request, f"stock.{action}", target, None)
    return {**done, "control": stockctl.overview(config.db_path)}


class Transfer(BaseModel):
    article: str
    from_site: str
    to_site: str
    quantity: float
    note: str | None = None
    force: bool = False


class Adjustment(BaseModel):
    article: str
    site: str | None = None
    quantity: float
    reason: str = ""


class Reservation(BaseModel):
    article: str
    site: str | None = None
    work_item: str | None = None
    quantity: float
    needed_by: str | None = None
    note: str | None = None
    force: bool = False


class CycleCount(BaseModel):
    article: str
    quantity: float


@app.get("/api/stockctl")
def read_stock_control():
    return stockctl.overview(config.db_path)


@app.post("/api/stockctl/transfer")
def stock_transfer(request: Request, body: Transfer):
    return _stock_control(request, "transfer", body.article, lambda: stockctl.transfer(
        config.db_path, body.article, body.from_site, body.to_site, body.quantity,
        who(request)[0], body.note, body.force))


@app.post("/api/stockctl/adjust")
def stock_adjust(request: Request, body: Adjustment):
    return _stock_control(request, "adjust", body.article, lambda: stockctl.adjust(
        config.db_path, body.article, body.site, body.quantity, body.reason, who(request)[0]))


@app.post("/api/stockctl/reserve")
def stock_reserve(request: Request, body: Reservation):
    return _stock_control(request, "reserve", body.article, lambda: stockctl.reserve(
        config.db_path, body.article, body.site, body.work_item, body.quantity,
        body.needed_by, who(request)[0], body.note, body.force))


@app.delete("/api/stockctl/reservations/{reservation_id}")
def stock_release(request: Request, reservation_id: int):
    if not stockctl.release(config.db_path, reservation_id):
        raise HTTPException(404, "That reservation is not held any more.")
    event(request, "stock.release", f"reservation #{reservation_id}")
    return {"control": stockctl.overview(config.db_path)}


@app.post("/api/stockctl/count")
def stock_cycle_count(request: Request, body: CycleCount):
    return _stock_control(request, "count", body.article, lambda: stockctl.count(
        config.db_path, body.article, body.quantity, who(request)[0]))


class OrderDraft(BaseModel):
    lead: int = forecast.DEFAULT_LEAD
    service: int = forecast.DEFAULT_SERVICE
    horizon: int = forecast.DEFAULT_HORIZON
    site: str | None = None
    work_item: str | None = None
    only: list[str] | None = None


def _named(rows, asked_for):
    """The planner's rows for the articles a person typed, exact names first."""
    wanted = [name.upper() for name in asked_for]
    exact = [row for row in rows if row["article"].upper() in wanted]
    if exact:
        return exact
    return [row for row in rows
            if any(name in row["article"].upper() for name in wanted)]


@app.post("/api/forecast/draft")
async def draft_order(request: Request, body: OrderDraft):
    """Plutus turns the planner's numbers into an order, for a person to sign."""
    lead, service, horizon = _planner_settings(body.lead, body.service, body.horizon)
    plan = forecast.summary(config.db_path, lead, service, horizon)
    rows = plan["articles"]
    asked_for = [name.strip() for name in (body.only or []) if name.strip()]
    if asked_for:
        # Typed by hand, so the whole name is rarely typed: "rondelle m8" finds the one
        # the shelf calls RONDELLE M8, and the exact name still wins over a part of it.
        rows = _named(rows, asked_for)
        if not rows:
            raise HTTPException(422, "No article on the shelf matches "
                                     + ", ".join(asked_for) + ".")
    else:
        rows = plan["shortlist"]
        if not rows:
            raise HTTPException(422,
                                "Nothing worth ordering: no article is under its reorder point.")

    refused = feedback.refusals(config.db_path)
    # The DDMRP is the rule for what to order: its zone and its number go beside the forecast's.
    buffers, _chain, _span = ddmrp.buffers(config.db_path)
    zones = {b["article"]: b for b in buffers}
    for row in rows:
        b = zones.get(row["article"])
        if b is not None:
            row["ddmrp"] = {"zone": b["zone"], "net_flow": b["net_flow"], "top_of_red": b["top_of_red"],
                            "top_of_yellow": b["top_of_yellow"], "top_of_green": b["top_of_green"],
                            "to_order": b["to_order"] if b["zone"] in ("red", "yellow") else 0}
    keep = ("article", "ddmrp", "designation", "reference", "supplier", "per_week", "on_hand", "on_order",
            "lead_days", "lead_source", "qty_factor", "safety_factor", "lead_demand", "safety_stock",
            "reorder_point", "cover_days", "suggested", "state",
            "risk", "backorders")
    planner = {
        "settings": plan["settings"],
        "window": plan["window"],
        "counted_articles": plan["counts"]["counted"],
        "history_is_thin": plan["thin"],
        # Named by the person, which is a different question from "what should we order?"
        "asked_for_by_name": asked_for,
        "articles": [dict({key: row[key] for key in keep},
                          refused_90_days=refused.get(row["article"], 0)) for row in rows[:60]],
        "note_on_factors": "qty_factor and safety_factor are what people's corrections have taught "
                           "the app about each article: the numbers above already include them.",
    }

    try:
        answer = await agents.draft_order(planner, model=config.model or None,
                                          charter=charters.prompt_block(config.db_path, "plutus"))
    except llm.ExtractionError as e:
        report(request, "draft", str(e), "Predictions window")
        raise HTTPException(502, str(e)) from e

    lines = []
    taught = {row["article"].upper(): row for row in feedback.listing(config.db_path)
              if row["qty_factor"] != 1.0 or row["safety_factor"] != 1.0}
    for line in answer.get("lines", []):
        article = str(line.get("article") or "").strip()
        quantity = line.get("quantity")
        if not article or quantity is None:
            continue
        learned = taught.get(article.upper())
        lines.append({"article": article, "quantity": float(quantity),
                      "unit": line.get("unit"), "supplier": line.get("supplier"),
                      "reference": line.get("reference"),
                      "reason": str(line.get("reason") or ""),
                      "learned": learned})
    # The charter's limits are applied again here, to whatever the model wrote.
    lines, cuts, changed = charters.enforce_plutus(config.db_path, lines, rows, asked_for, refused)
    # No line is an answer, not a failure: the agent is told to leave out what is
    # comfortable, so a shelf in good order is meant to come back empty.
    event(request, "order.draft", f"{len(lines)} line(s)", "by Plutus")
    return {"lines": lines, "note": str(answer.get("note") or ""),
            "model": config.model or "claude", "agent": agents.PLUTUS,
            "drafted_from": len(planner["articles"]),
            "cuts": cuts, "adjusted": changed,
            "asked_for": asked_for,
            "read": [row["article"] for row in rows[:60]],
            "site": body.site, "work_item": body.work_item}


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


RISKY_START = ("=", "+", "-", "@", "\t", "\r")


def _cell(value):
    """A cell that starts like a formula is quoted: a spreadsheet must not run our data."""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, str) and value.startswith(RISKY_START):
        return "'" + value
    return value


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
    "bills": ("bills", "Supplier bills", ("bill_id",), (
        ("bill_id", "integer"), ("supplier", "text"), ("number", "text"), ("bill_date", "date"),
        ("month", "text"), ("due_date", "date"), ("currency", "text"),
        ("net", "double precision"), ("tax", "double precision"), ("total", "double precision"),
        ("site", "text"), ("order_ref", "text"), ("state", "text"), ("paid_on", "date"),
        ("source", "text"), ("sender", "text"))),
    "quotes": ("quote_lines", "Quote requests", ("quote_id", "line_no"), (
        ("quote_id", "integer"), ("line_no", "integer"), ("article", "text"),
        ("quantity", "double precision"), ("unit", "text"), ("designation", "text"),
        ("supplier", "text"), ("site", "text"), ("work_item", "text"),
        ("needed_by", "date"), ("state", "text"))),
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
    elif dataset in ("bills", "quotes"):
        names, fetched = db.export_query(config.db_path, dataset)
        order = [names.index(name) for name, _ in columns]
        data = [[row[at] for at in order] for row in fetched]
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
    simulation.init(config.db_path)
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
