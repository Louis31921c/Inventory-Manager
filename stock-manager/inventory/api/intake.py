"""Getting documents in: notes, photos, mail, imports, the SQL screen and the questions asked of the data."""

import asyncio
import duckdb
import time
from dataclasses import replace
from fastapi import HTTPException
from fastapi import Request
from fastapi import UploadFile
from fastapi.responses import FileResponse
from fastapi.responses import JSONResponse
from fastapi.responses import Response
from pathlib import Path
from pydantic import BaseModel

from .. import archive
from .. import bills
from .. import catalog
from .. import db
from .. import intake
from .. import llm
from .. import mail_oauth
from .. import mailbox
from .. import models_info
from .. import photos
from .. import sheets
from .. import stock
from .. import vocabulary
from ..config import load_config
from ..config import set_setting
from ..models import Note
from fastapi import APIRouter

from .. import server as s    # the server module, read at call time: its config and helpers

router = APIRouter()

@router.get("/api/data")
def data(request: Request):

    notes = db.read(s.config.db_path, """
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
    catalog_rows = db.read(s.config.db_path, "SELECT supplier, designation, reference, article, seen "
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
        "inventory": db.stock_by_article(s.config.db_path),
        "backend": s.config.backend,
        "user": s.who(request)[0],
    }


def _orders_with_cover():
    """Each order line carries what the delivery notes hold for it, so nobody has to
    tick a box: the paperwork says whether it is standing there or has to be ordered."""
    held = stock.by_place(s.config.db_path)
    sheets = db.purchase_orders(s.config.db_path)
    for sheet in sheets:
        for line in sheet.get("lines", []):
            line["have"] = stock.cover(held, line["article"], sheet.get("site"),
                                       sheet.get("work_item"), besides=sheet.get("id"))
    return sheets


@router.post("/api/extract")
async def extract(request: Request, photo: UploadFile):
    raw, media_type, sha = await s._photo_bytes(photo)
    already = db.find_by_image(s.config.db_path, sha)
    if already is not None:
        raise HTTPException(409, f"This note is already stored (note #{already}).")

    known = vocabulary.known_articles(s.config.db_path)
    try:
        note = await s.get_backend().extract(raw, media_type, known)
    except llm.ExtractionError as e:
        raise HTTPException(502, str(e)) from e
    except Exception as e:
        s.log.exception("extraction failed")
        reported = await asyncio.to_thread(s.report, request, "extract",
                                           f"note reading failed: {e}", "/api/extract")
        raise HTTPException(500, f"Reading failed (error #{reported['error_no']}): {e}") from e

    note = catalog.apply(s.config.db_path, note)
    photos.keep(s.config.images_dir / f"{sha}{s.SUPPORTED[media_type]}", raw)
    return JSONResponse({"sha256": sha, "note": note.model_dump()})


@router.get("/api/intake")
def read_intake():
    return {"waiting": intake.waiting(s.config), "counts": intake.counts(s.config),
            "mail": {"configured": s.config.mail.enabled, "folder": s.config.mail.folder,
                     "every_minutes": s.config.mail.every_minutes, **s.last_mail}}


@router.post("/api/intake")
async def add_to_intake(request: Request, files: list[UploadFile]):
    if len(files) > s.MAX_FILES:
        raise HTTPException(413, f"{s.MAX_FILES} files at a time, no more.")
    added, refused = [], []
    for item in files:
        raw = await item.read()
        if len(raw) > s.MAX_UPLOAD:
            refused.append({"name": item.filename, "why": "larger than 30 MB"})
            continue
        try:
            added.append(intake.add(s.config, raw, item.filename or "file", item.content_type))
        except ValueError as e:
            refused.append({"name": item.filename, "why": str(e)})
    s.event(request, "intake.add", f"{len(added)} file(s)")
    return {"added": added, "refused": refused, "waiting": intake.waiting(s.config)}


@router.post("/api/intake/{intake_id}/read")
async def read_queued(request: Request, intake_id: int):
    """Hand a queued scan to the model, exactly as a photo taken on the spot."""
    try:
        raw, media_type, item = intake.bytes_of(s.config, intake_id)
    except KeyError as e:
        raise HTTPException(404, "That file is no longer in the queue.") from e
    if media_type not in s.SUPPORTED:
        raise HTTPException(422, "A spreadsheet is imported, not read as a note.")

    already = db.find_by_image(s.config.db_path, item["sha256"])
    if already is not None:
        intake.mark(s.config, intake_id, intake.DONE, f"already note #{already}")
        raise HTTPException(409, f"This note is already stored (note #{already}).")

    known = vocabulary.known_articles(s.config.db_path)
    try:
        note = await s.get_backend().extract(raw, media_type, known)
    except llm.ExtractionError as e:
        raise HTTPException(502, str(e)) from e

    note = catalog.apply(s.config.db_path, note)
    photos.keep(s.config.images_dir / f"{item['sha256']}{s.SUPPORTED[media_type]}", raw)
    s.event(request, "intake.read", item["name"])
    return JSONResponse({"sha256": item["sha256"], "note": note.model_dump(),
                         "intake_id": intake_id})


@router.delete("/api/intake/{intake_id}")
def drop_queued(request: Request, intake_id: int):
    if not intake.drop(s.config, intake_id):
        raise HTTPException(404, "That file is no longer in the queue.")
    s.event(request, "intake.drop", str(intake_id))
    return {"dropped": intake_id}


class Mark(BaseModel):
    state: str = intake.DONE
    note: str | None = None


@router.patch("/api/intake/{intake_id}")
def mark_queued(request: Request, intake_id: int, body: Mark):
    if intake.one(s.config, intake_id) is None:
        raise HTTPException(404, "That file is no longer in the queue.")
    if body.state not in (intake.WAITING, intake.DONE, intake.DROPPED):
        raise HTTPException(422, f"Unknown state: {body.state}.")
    intake.mark(s.config, intake_id, body.state, body.note)
    return {"intake_id": intake_id, "state": body.state}


@router.post("/api/mail/fetch")
async def fetch_mail(request: Request):
    if not s.config.mail.enabled:
        raise HTTPException(422, "No mailbox configured: set IMAP_HOST, IMAP_USER and "
                                 "IMAP_PASSWORD in the settings file.")
    try:
        answer = await asyncio.to_thread(mailbox.fetch, s.config, s.config.mail)
    except mailbox.MailError as e:
        raise HTTPException(502, str(e)) from e
    read = (await s._read_mail_notes(answer["ids"])
            if s.config.mail.auto_read and answer["ids"] else None)
    s.last_mail.update(at=time.strftime("%Y-%m-%d %H:%M"), queued=answer["queued"],
                     detail=f"{answer['messages']} message(s), {answer['queued']} queued"
                            + (f", {read['stored']} note(s) stored" if read else ""))
    s.event(request, "mail.fetch", s.config.mail.folder, s.last_mail["detail"])
    return {**answer, "read": read, "waiting": intake.waiting(s.config)}


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
    mail = s.config.mail
    return {"provider": mail.provider, "host": mail.host, "port": mail.port, "user": mail.user,
            "folder": mail.folder, "security": mail.security, "mark_read": mail.mark_read,
            "auto_read": mail.auto_read,
            "every_minutes": mail.every_minutes, "configured": mail.enabled,
            "password_set": bool(mail.password), "oauth": mail.oauth,
            "client_id": mail.client_id, "tenant": mail.tenant}


@router.get("/api/mail/settings")
def mail_settings():
    """What the mailbox is set to. The password itself never leaves the machine."""
    return {"providers": mailbox.providers(), "current": _mail_state(), **s.last_mail}


@router.post("/api/mail/settings")
def save_mail_settings(request: Request, body: MailSettings):
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

    s.config = replace(s.config, mail=load_config(data_dir=s.config.data_dir, user=s.config.user).mail)
    s.event(request, "mail.settings", provider or "other", s.config.mail.host)
    return {"current": _mail_state(), "help": chosen["help"]}


@router.get("/api/mail/oauth")
def mail_oauth_state():
    """Where the Microsoft sign-in stands. No token is ever in this answer."""
    return mail_oauth.state()


@router.post("/api/mail/oauth/start")
def mail_oauth_start(request: Request):
    """Ask Microsoft for a code. The waiting is done on a thread, not on this request."""
    try:
        started = mail_oauth.start()
    except mail_oauth.OAuthError as e:
        raise HTTPException(409, str(e)) from e
    s.event(request, "mail.oauth.start", s.config.mail.user or "outlook")
    return {**started, "state": mail_oauth.state()}


@router.post("/api/mail/oauth/forget")
def mail_oauth_forget(request: Request):
    mail_oauth.forget()
    s.event(request, "mail.oauth.forget", s.config.mail.user or "outlook")
    return mail_oauth.state()


@router.post("/api/mail/check")
async def check_mail(request: Request):
    if not s.config.mail.enabled:
        return {"ok": False, "detail": "No mailbox configured."}
    ok, detail = await asyncio.to_thread(mailbox.check, s.config.mail)
    s.event(request, "mail.check", s.config.mail.host, detail[:200])
    return {"ok": ok, "detail": detail}


@router.get("/api/import/{intake_id}")
def preview_sheet(intake_id: int, target: str = "inventory"):
    if target not in ("inventory", "catalog"):
        raise HTTPException(422, f"Unknown target: {target}.")
    try:
        raw, media_type, item = intake.bytes_of(s.config, intake_id)
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


@router.post("/api/import/{intake_id}")
def import_sheet(request: Request, intake_id: int, body: SheetImport):
    if body.target not in ("inventory", "catalog"):
        raise HTTPException(422, f"Unknown target: {body.target}.")
    try:
        raw, media_type, item = intake.bytes_of(s.config, intake_id)
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
        answer = sheets.store_inventory(s.config.db_path, kept, s.who(request)[0])
    else:
        answer = sheets.store_catalog(s.config.db_path, kept)
    intake.mark(s.config, intake_id, intake.DONE, f"{len(kept)} line(s) into {body.target}")
    s.event(request, "import", item["name"], f"{len(kept)} line(s) into {body.target}")
    return {**answer, "dropped": dropped[:20], "target": body.target}


class SaveNote(BaseModel):
    sha256: str | None = None
    note: Note
    measurement: s.Measurement | None = None


@router.post("/api/notes")
def save_note(request: Request, body: SaveNote):
    problems = body.note.problems()
    if problems:
        raise HTTPException(422, "To fix: " + ", ".join(problems))

    user = s.who(request)[0]
    try:
        note_id = db.save_note(s.config.db_path, body.note, body.sha256 or None,
                               s._stored_photo(body.sha256), user)
    except duckdb.ConstraintException as e:
        raise HTTPException(409, "This note is already stored.") from e

    if body.measurement:
        db.record_reading(s.config.db_path, "note", body.measurement.fields,
                          body.measurement.corrected, body.measurement.lines)
    detail = f"{body.note.supplier} · {len(body.note.lines)} line(s)"
    if body.measurement:
        detail += f" · {body.measurement.corrected}/{body.measurement.fields} fields corrected"
    s.event(request, "note.save", f"note #{note_id}", detail)
    return {"note_id": note_id}


@router.delete("/api/notes/{note_id}")
def remove_note(request: Request, note_id: int):
    s._drop(db.delete_note(s.config.db_path, note_id), f"No note #{note_id}.")
    s.event(request, "note.delete", f"note #{note_id}")
    return {"deleted": note_id}


def _photo(path, sha=None):
    """A stored photo, or the packed page that replaced it once the photo was old."""
    if (not path or not Path(path).exists()) and sha:
        page = archive.get(s.config.db_path, sha)
        if page:
            return Response(page, media_type="image/png")
    if not path or not Path(path).exists():
        raise HTTPException(404, "No photo.")
    return FileResponse(path)


@router.get("/api/photo/{note_id}")
def photo(note_id: int):
    rows = db.read(s.config.db_path, "SELECT image_path, image_sha256 FROM notes WHERE note_id = ?",
                   [note_id])
    return _photo(*(rows[0] if rows else (None, None)))


@router.post("/api/intake/{intake_id}/bill")
async def read_queued_bill(request: Request, intake_id: int):
    """Read a queued scan or PDF as a bill, not as a delivery note."""
    try:
        raw, media_type, item = intake.bytes_of(s.config, intake_id)
    except KeyError as e:
        raise HTTPException(404, "That file is no longer in the queue.") from e
    if media_type not in s.SUPPORTED:
        raise HTTPException(422, "A spreadsheet is imported, not read as a bill.")

    already = bills.find_by_image(s.config.db_path, item["sha256"])
    if already is not None:
        intake.mark(s.config, intake_id, intake.DONE, f"already bill #{already}")
        raise HTTPException(409, f"This bill is already stored (bill #{already}).")

    read = await s._read_bill(raw, media_type)
    photos.keep(s.config.images_dir / f"{item['sha256']}{s.SUPPORTED[media_type]}", raw)
    s.event(request, "bill.read", item["name"])
    return {"sha256": item["sha256"], "bill": read, "intake_id": intake_id,
            "sender": item.get("sender")}


@router.get("/api/bill-photo/{bill_id}")
def bill_photo(bill_id: int):
    rows = db.read(s.config.db_path, "SELECT image_path, image_sha256 FROM bills WHERE bill_id = ?",
                   [bill_id])
    return _photo(*(rows[0] if rows else (None, None)))


@router.get("/api/tables")
def read_tables():
    """What a question can be asked about, with how many rows there are to ask about."""
    return {"tables": db.table_counts(s.config.db_path)}


class WorkItem(BaseModel):
    work_item: str | None = None


@router.patch("/api/inventory/{note_id}/{line_no}")
def edit_work_item(request: Request, note_id: int, line_no: int, body: WorkItem):
    value = (body.work_item or "").strip() or None
    if not db.update_work_item(s.config.db_path, note_id, line_no, value):
        raise HTTPException(404, "Line not found.")
    s.event(request, "delivery.edit", f"note #{note_id} line {line_no}",
          f"work_item = {value or 'empty'}")
    return {"note_id": note_id, "line_no": line_no, "work_item": value}


@router.get("/api/order-photo/{list_id}")
def list_photo(list_id: int):
    rows = db.read(s.config.db_path, "SELECT image_path, image_sha256 FROM purchase_orders "
                                  "WHERE list_id = ?", [list_id])
    return _photo(*(rows[0] if rows else (None, None)))


@router.get("/api/vocabulary")
def read_vocabulary():
    entries = vocabulary.vocabulary(s.config.db_path)
    return {
        "articles": entries[:400],
        "total": len(entries),
        "shared": sum(1 for e in entries if e["shared"]),
        "aliases": vocabulary.aliases(s.config.db_path),
    }


class Merge(BaseModel):
    alias: str
    article: str


@router.post("/api/vocabulary/merge")
def merge_vocabulary(request: Request, body: Merge):
    try:
        result = vocabulary.merge(s.config.db_path, body.alias, body.article, s.who(request)[0])
    except vocabulary.VocabularyError as e:
        raise HTTPException(422, str(e)) from e
    s.event(request, "vocabulary.merge", result["article"],
          f"{result['alias']} -> {result['article']} ({result['inventory']} delivery line(s), "
          f"{result['order_lines']} list line(s))")
    return result


class ModelChoice(BaseModel):
    id: str


@router.get("/api/models")
def models():
    return {"current": s.config.backend, "providers": models_info.status(s.config.backend)}


@router.post("/api/models/check")
def check_model(request: Request, body: ModelChoice):
    ok, detail = models_info.probe(body.id)
    s.event(request, "model.check", body.id, detail[:200])
    return {"id": body.id, "ok": ok, "detail": detail}


@router.post("/api/models/use")
def use_model(request: Request, body: ModelChoice):
    try:
        chosen = models_info.use(body.id)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    s.config = replace(s.config, backend=chosen)
    s.backend = None
    s.event(request, "model.use", chosen)
    return {"current": chosen, "providers": models_info.status(chosen)}


@router.get("/api/accuracy")
def accuracy():
    return db.accuracy(s.config.db_path)


@router.get("/api/storage")
def storage():
    info = db.storage(s.config.db_path, s.config.data_dir, s.config.images_dir)
    info["photo_retention_days"] = s.config.photo_retention_days
    return info


class Question(BaseModel):
    question: str


class SqlRequest(BaseModel):
    sql: str


def _json(value):
    if isinstance(value, bool):
        return value
    return value.isoformat() if hasattr(value, "isoformat") else value


def _result(sql, note):
    result = db.run_readonly(s.config.db_path, sql)
    return {"sql": sql, "note": note, "columns": result.columns,
            "rows": [[_json(v) for v in row] for row in result.rows],
            "truncated": result.truncated}


@router.post("/api/question")
async def question(request: Request, body: Question):
    if not body.question.strip():
        raise HTTPException(400, "Empty question.")

    previous = None
    for _ in range(2):
        try:
            sql, note = await s.get_backend().to_sql(body.question, previous)
        except llm.ExtractionError as e:
            raise HTTPException(502, str(e)) from e
        if not sql:
            return {"sql": None, "note": note or "This question cannot be answered from this data."}
        try:
            answer = _result(sql, note)
        except db.QueryError as e:
            previous = (sql, str(e))
            continue
        await asyncio.to_thread(s.event, request, "question", None, body.question[:200])
        return answer

    raise HTTPException(422, f"Query failed: {previous[1]}")


@router.post("/api/sql")
def run_sql(request: Request, body: SqlRequest):
    try:
        answer = _result(body.sql, None)
    except db.QueryError as e:
        raise HTTPException(422, str(e)) from e
    s.event(request, "sql", None, body.sql[:200])
    return answer
