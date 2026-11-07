if __name__ == "__main__":      # `python -m inventory.server`: run the one canonical module, so the api modules see its names
    from inventory.server import main

    raise SystemExit(main())

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
import re
import tempfile
from datetime import date
from pathlib import Path

import duckdb
import uvicorn
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel


from . import (
    account, alerts, audit, backup, bankdrop, catalog,
    channels, charters, counting, db,
    ddmrp, discord_bot, documents, feedback, intake, judge, llm, mailbox, payments,
    pg, photos, policies, purchasing,
    receiving, reorder, replenish, requisitions,
    simulation, slack_bot, vocabulary, wallet, watch,
)
from . import access, integrity, licensing, vault
from .config import config_dir, load_config

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






MAX_FILES = 25


































# ---- the gate on what the bot buys ----------------------------------------













































class Measurement(BaseModel):
    fields: int = 0
    corrected: int = 0
    lines: int = 0




def _stored_photo(sha256):
    if not sha256:
        return ""
    matches = list(config.images_dir.glob(f"{sha256}.*"))
    return str(matches[0]) if matches else ""




def _drop(image_path, missing):

    if image_path is None:
        raise HTTPException(404, missing)
    if image_path:
        Path(image_path).unlink(missing_ok=True)












async def _read_bill(raw, media_type):
    try:
        return await get_backend().extract_bill(raw, media_type)
    except llm.ExtractionError as e:
        raise HTTPException(502, str(e)) from e















# ---- dashboard, notices, autonomy ------------------------------------------













def _do(request, action, target, work):
    """Writes that can be refused in words: the refusal is the person's to read."""
    try:
        done = work()
    except (ValueError, documents.Unavailable) as e:
        raise HTTPException(422, str(e)) from e
    event(request, action, target, None)
    return done


# ---- calendar, supplier terms, exchange rates ----------------------------------



















# ---- pack sizes ---------------------------------------------------------------









# ---- quote answers, comparison, awards --------------------------------------------



















# ---- disputes -------------------------------------------------------------------------













# ---- deliveries to sites ---------------------------------------------------------------



























# ---- archive ---------------------------------------------------------------------------






















# ---- policies and the bots' charters ---------------------------------------------





class Switch(BaseModel):
    on: bool






















# ---- Plutus orders from the DDMRP, Terminus decides baskets ------------------------















# ---- the wallet, Plutus's purchasing, and the payments --------------------------------







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






















# ---- site requests ----------------------------------------------------------------









# ---- how orders reach a supplier, and the bank drop ------------------------------------





def _connections():
    return {"channels": channels.listing(config.db_path), "bank": bankdrop.settings(config.db_path),
            "bank_ready": bankdrop.ready(config.db_path)[0]}














# ---- users and roles --------------------------------------------------------------























# ---- receiving and counting -----------------------------

class PutAwayBody(BaseModel):
    location: str | None = None


















# ---- what suppliers asked ------------------------------------------------------------

























# ---- quote requests -------------------------------------------------------













# ---- what the shelf holds -------------------------------------------------





# ---- who the order goes to ------------------------------------------------























# ---- what a delivery note still owes --------------------------------------







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


































def _named(rows, asked_for):
    """The planner's rows for the articles a person typed, exact names first."""
    wanted = [name.upper() for name in asked_for]
    exact = [row for row in rows if row["article"].upper() in wanted]
    if exact:
        return exact
    return [row for row in rows
            if any(name in row["article"].upper() for name in wanted)]
















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














@app.post("/exit")
def quit_app(request: Request):
    """The window's exit button: stop the server, which closes the app."""
    event(request, "quit")
    if stopper is not None:
        stopper()
    return Response(status_code=204)


# The routes of each area, in their own modules (inventory/api). They read this module's names at call time.
from .api import admin as _api_admin  # noqa: E402
from .api import intake as _api_intake  # noqa: E402
from .api import buying as _api_buying  # noqa: E402
from .api import stock as _api_stock  # noqa: E402
from .api import money as _api_money  # noqa: E402
from .api import delivery as _api_delivery  # noqa: E402
app.include_router(_api_admin.router)
app.include_router(_api_intake.router)
app.include_router(_api_buying.router)
app.include_router(_api_stock.router)
app.include_router(_api_money.router)
app.include_router(_api_delivery.router)


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
