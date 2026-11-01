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

from . import (account, agents, alerts, audit, backorders, backup, baskets, bills, catalog,
               contacts, db, ddmrp, discord_bot,
               forecast, gate, intake, llm,
               mail_oauth, mailbox, models_info, pg, prices, quotes, routing, sheets,
               slack_bot, stock, verify, vocabulary)
from .config import load_config, set_setting
from .models import Note, parse_date

log = logging.getLogger("inventory.web")

WEB = Path(__file__).resolve().parent / "web_ui"
PAGE = WEB / "app.html"
LICENCE = WEB / "license.html"
UNLOCK = WEB / "unlock.html"
SUPPORTED = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
             "application/pdf": ".pdf"}
config = load_config()
app = FastAPI(title="Stock manager")
lock = account.Lock(config.data_dir)
backend = None
stopper = None
OPEN_PATHS = ("/unlock", "/robots.txt", "/sap.css", "/logo.png", "/icon.png")
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
    asyncio.create_task(_mail_loop())
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
            last_mail.update(at=time.strftime("%Y-%m-%d %H:%M"), queued=answer["queued"],
                             detail=f"{answer['messages']} message(s), {answer['queued']} queued")
            if answer["queued"]:
                log.info("mail: %d attachment(s) queued", answer["queued"])
        except Exception as e:
            last_mail.update(at=time.strftime("%Y-%m-%d %H:%M"), queued=0, detail=str(e)[:200])
            log.warning("mail fetch failed: %s", e)


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
    (config.images_dir / f"{sha}{SUPPORTED[media_type]}").write_bytes(raw)
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
    (config.images_dir / f"{item['sha256']}{SUPPORTED[media_type]}").write_bytes(raw)
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
    last_mail.update(at=time.strftime("%Y-%m-%d %H:%M"), queued=answer["queued"],
                     detail=f"{answer['messages']} message(s), {answer['queued']} queued")
    event(request, "mail.fetch", config.mail.folder, last_mail["detail"])
    return {**answer, "waiting": intake.waiting(config)}


class MailSettings(BaseModel):
    provider: str = ""
    user: str = ""
    password: str | None = None
    folder: str = "INBOX"
    every_minutes: int = 15
    mark_read: bool = True
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
    return {
        "log": {"whole": whole, "broken_at": broken, "entries": entries,
                "last_seal": gate.last_seal(config.db_path)},
        "entries": list(reversed(rows)),
        "doings": gate.doings(config.db_path),
        "agents": agents.status(),
        "rules": {"ceiling": gate.CEILING, "dear": gate.DEAR, "drift": gate.DRIFT},
        "words": gate.REASON_WORDS,
        "steps": [{"number": number, "name": name, "detail": detail,
                   "built": number not in gate.UNBUILT}
                  for number, name, detail in gate.STEPS],
        "now": gate.where_we_are(config.db_path),
        "approval": gate.waiting(config.db_path),
        # A basket is emptied when it becomes an order, so whatever is still in one is
        # whatever nobody has bought.
        "baskets": baskets.open_baskets(config.db_path),
    }


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


def _photo(path):
    if not path or not Path(path).exists():
        raise HTTPException(404, "No photo.")
    return FileResponse(path)


@app.get("/api/photo/{note_id}")
def photo(note_id: int):
    rows = db.read(config.db_path, "SELECT image_path FROM notes WHERE note_id = ?", [note_id])
    return _photo(rows[0][0] if rows else None)


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
    (config.images_dir / f"{sha}{SUPPORTED[media_type]}").write_bytes(raw)
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
    (config.images_dir / f"{item['sha256']}{SUPPORTED[media_type]}").write_bytes(raw)
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
    rows = db.read(config.db_path, "SELECT image_path FROM bills WHERE bill_id = ?", [bill_id])
    return _photo(rows[0][0] if rows else None)


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


@app.get("/api/contacts")
def read_contacts():
    found = contacts.find_file()
    book = contacts.all_of(config.db_path)
    if not book and found:
        # Nobody has to press anything: an empty book fills itself from the file.
        contacts.load_file(config.db_path, found, "file")
        book = contacts.all_of(config.db_path)
    return {"contacts": book, "incoterms": contacts.terms(),
            "file": str(found) if found else None}


@app.get("/api/contacts/file")
def search_contact_file(name: str | None = None, company: str | None = None):
    """Straight from the csv on the disk, not from the book in the database."""
    try:
        return contacts.look_up(name, company)
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

    keep = ("article", "designation", "reference", "supplier", "per_week", "on_hand", "on_order",
            "lead_demand", "safety_stock", "reorder_point", "cover_days", "suggested", "state",
            "risk", "backorders")
    planner = {
        "settings": plan["settings"],
        "window": plan["window"],
        "counted_articles": plan["counts"]["counted"],
        "history_is_thin": plan["thin"],
        # Named by the person, which is a different question from "what should we order?"
        "asked_for_by_name": asked_for,
        "articles": [{key: row[key] for key in keep} for row in rows[:60]],
    }

    try:
        answer = await agents.draft_order(planner, model=config.model or None)
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
    # No line is an answer, not a failure: the agent is told to leave out what is
    # comfortable, so a shelf in good order is meant to come back empty.
    event(request, "order.draft", f"{len(lines)} line(s)", "by Plutus")
    return {"lines": lines, "note": str(answer.get("note") or ""),
            "model": config.model or "claude", "agent": agents.PLUTUS,
            "drafted_from": len(planner["articles"]),
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
