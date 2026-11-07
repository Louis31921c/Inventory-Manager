"""Running the program: users, audit, health, backups, errors, notices, chat settings."""

import asyncio
import json
import os
import secrets
from fastapi import HTTPException
from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .. import account
from .. import alerts
from .. import archive
from .. import audit
from .. import backup
from .. import db
from .. import discord_bot
from .. import health as doctor
from .. import slack_bot
from .. import watch
from ..config import set_setting
from fastapi import APIRouter

from .. import server as s    # the server module, read at call time: its config and helpers

router = APIRouter()

NOT_FOUND = HTMLResponse("<h1>404 Not Found</h1>", status_code=404)


@router.post("/api/slack/check")
def check_slack(request: Request):
    """Try the stored tokens against Slack. Neither of them is in the answer."""
    ok, said = slack_bot.check()
    s.event(request, "chat.check", "slack", "ok" if ok else said[:80])
    return {"ok": ok, "detail": said}


class ChatSettings(BaseModel):
    token: str | None = None
    channels: str = ""
    forget: bool = False


class SlackSettings(BaseModel):
    bot_token: str | None = None
    app_token: str | None = None
    channels: str = ""
    forget: bool = False


@router.get("/api/chat/settings")
def chat_settings():
    """Whether either bot can run. No token ever leaves the machine."""
    return {"discord": discord_bot.status(), "slack": slack_bot.status()}


@router.post("/api/chat/settings")
def save_chat_settings(request: Request, body: ChatSettings):
    channels = ",".join(piece.strip() for piece in body.channels.split(",") if piece.strip())
    set_setting(discord_bot.CHANNELS_KEY, channels)
    if body.forget:
        set_setting(discord_bot.TOKEN_KEY, "")
    elif body.token:
        set_setting(discord_bot.TOKEN_KEY, body.token.strip())
    s.event(request, "chat.settings", "discord", f"{len(channels.split(',')) if channels else 0} channel(s)")
    return {"discord": discord_bot.status(), "slack": slack_bot.status()}


@router.post("/api/slack/settings")
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
    s.event(request, "chat.settings", "slack",
          f"{len(channels.split(',')) if channels else 0} channel(s)")
    return {"discord": discord_bot.status(), "slack": slack_bot.status()}


@router.get("/api/notices")
def read_notices():
    return {"notices": watch.open_notices(s.config.db_path), "notify_via": watch.channels()}


@router.post("/api/notices/scan")
def scan_notices(request: Request):
    done = watch.run(s.config.db_path, s.config)
    s.event(request, "notices.scan", f"{done['new']} new", None)
    return {**done, "notices": watch.open_notices(s.config.db_path)}


@router.get("/api/archive")
def read_archive():
    return archive.summary(s.config.db_path)


@router.post("/api/archive/run")
def run_archive(request: Request):
    done = archive.sweep(s.config.db_path, s.config.images_dir, 0)
    s.event(request, "archive.run", f"{done['pages']} page(s)", f"{done['before']} -> {done['after']} bytes")
    return {**done, **archive.summary(s.config.db_path)}


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


@router.get("/api/health")
def read_health():
    return doctor.report(s.config)


@router.get("/api/audit/verify")
def verify_audit():
    return audit.verify(s.config.db_path)


@router.get("/api/users")
def read_users(request: Request):
    return {"users": account.users(), "roles": list(account.ROLES), "me": request.state.user}


@router.post("/api/users")
def add_user(request: Request, body: UserBody):
    s._do(request, "user.add", body.user, lambda: account.add_user(body.user, body.role, body.password))
    return read_users(request)


@router.put("/api/users/{name}/role")
def set_user_role(request: Request, name: str, body: RoleBody):
    s._do(request, "user.role", name, lambda: account.set_role(name, body.role))
    return read_users(request)


@router.put("/api/users/{name}/password")
def reset_user_password(request: Request, name: str, body: PasswordBody):
    s._do(request, "user.reset", name, lambda: account.reset_password(name, body.password))
    return read_users(request)


@router.delete("/api/users/{name}")
def delete_user(request: Request, name: str):
    if name.strip().upper() == (request.state.user or "").upper():
        raise HTTPException(422, "You cannot remove the account you are using.")
    s._do(request, "user.remove", name, lambda: account.remove_user(name))
    return read_users(request)


@router.post("/api/account/password")
def change_my_password(request: Request, body: PasswordBody):
    s._do(request, "user.password", request.state.user, lambda: account.change_own_password(
        request.state.user, body.old, body.new))
    return {"ok": True}


@router.get("/api/audit")
def read_audit(limit: int = 100, user: str | None = None):
    return {
        "users": audit.users(s.config.db_path),
        "events": audit.events(s.config.db_path, min(max(limit, 1), 500), user),
    }


@router.get("/api/errors")
def read_errors(limit: int = 50):
    return {
        "errors": alerts.recent(s.config.db_path, min(max(limit, 1), 200)),
        "alerting": alerts.destination(s.config.sms),
    }


@router.post("/api/errors/test")
async def test_error(request: Request):
    user = s.who(request)[0] or "?"
    result = await asyncio.to_thread(s.report, request, "test",
                                     f"test message requested by {user}", "Settings tab")
    s.event(request, "error.test", f"error #{result['error_no']}", result["sms_status"])
    return result


@router.get("/api/health/{token}")
def health(token: str):
    expected = os.environ.get("HEALTH_TOKEN", "")
    if not expected or not secrets.compare_digest(token, expected):
        return NOT_FOUND
    try:
        db.storage(s.config.db_path, s.config.data_dir, s.config.images_dir)
    except Exception as e:
        s.log.exception("health check: database unreadable")
        s.report(None, "health", f"health check failed: {e}", "/api/health")
        return JSONResponse({"status": "ko"}, 503)
    return JSONResponse({"status": "ok"})


@router.get("/api/checks")
def checks():
    try:
        return json.loads((s.config.data_dir / "monitor.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {"status": "unknown", "problems": ["no check has run yet"]}


@router.get("/api/backups")
def list_backups():
    return {"backups": backup.list_backups(s.config.data_dir)[:20]}


@router.post("/api/backups")
async def force_backup(request: Request):
    try:
        await asyncio.to_thread(backup.snapshot, s.config)
    except Exception as e:
        s.log.exception("backup failed")
        reported = await asyncio.to_thread(s.report, request, "backup", f"backup failed: {e}",
                                           "backup")
        raise HTTPException(500, f"Backup failed (error #{reported['error_no']}): {e}") from e
    files = backup.list_backups(s.config.data_dir)
    s.event(request, "backup.run", files[0]["name"] if files else None)
    return {"backups": files[:20], "latest": files[0] if files else None}
