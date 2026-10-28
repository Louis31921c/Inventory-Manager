import email
import imaplib
import ssl
from email.header import decode_header, make_header

from . import intake

PROVIDERS = {
    "proton": {
        "label": "Proton Mail (through the Bridge)",
        "host": "127.0.0.1", "port": 1143, "security": "starttls", "insecure": True,
        "help": "Proton Bridge has to be installed and running on this machine. Open it, copy the "
                "Bridge's own IMAP user name and password (not your Proton password) and paste "
                "them here.",
    },
    "gmail": {
        "label": "Gmail",
        "host": "imap.gmail.com", "port": 993, "security": "ssl", "insecure": False,
        "help": "Switch two-step verification on, then make an app password "
                "(Google account, Security, App passwords) and paste that here. Your usual "
                "password will be refused.",
    },
    "outlook": {
        "label": "Outlook / Microsoft 365",
        "host": "outlook.office365.com", "port": 993, "security": "ssl", "insecure": False,
        "help": "Microsoft no longer lets a password alone open an IMAP mailbox. Ask whoever "
                "runs your tenant for an app password, or have the supplier mail forwarded to a "
                "mailbox that still allows it.",
    },
    "yahoo": {
        "label": "Yahoo Mail",
        "host": "imap.mail.yahoo.com", "port": 993, "security": "ssl", "insecure": False,
        "help": "Make an app password in Yahoo account security and paste that here.",
    },
    "icloud": {
        "label": "iCloud Mail",
        "host": "imap.mail.me.com", "port": 993, "security": "ssl", "insecure": False,
        "help": "Make an app-specific password on appleid.apple.com and paste that here.",
    },
    "other": {
        "label": "Another mailbox (IMAP)",
        "host": "", "port": 993, "security": "ssl", "insecure": False,
        "help": "Type the IMAP server your provider gives you, usually on port 993 over SSL.",
    },
}

DEFAULT_PROVIDER = "other"


def preset(name):
    return PROVIDERS.get((name or "").strip().lower(), PROVIDERS[DEFAULT_PROVIDER])


def providers():
    """What the Settings screen shows, in the order it shows it."""
    return [{"id": key, **{name: value for name, value in fields.items() if name != "insecure"}}
            for key, fields in PROVIDERS.items()]


MAX_ATTACHMENT = 30 * 1024 * 1024
MAX_PER_RUN = 20


class MailError(Exception):
    pass


def _text(raw):
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw)))
    except Exception:
        return str(raw)


LOCAL = ("127.0.0.1", "::1", "localhost")


def _context(settings):
    """Certificates are checked everywhere; the Bridge on this machine is the one exception."""
    context = ssl.create_default_context()
    if settings.insecure and settings.host in LOCAL:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


def connect(settings):
    """Proton Bridge speaks STARTTLS on 127.0.0.1, everyone else plain IMAPS."""
    if not settings.host or not settings.user:
        raise MailError("No mailbox configured.")
    try:
        if settings.security == "starttls":
            box = imaplib.IMAP4(settings.host, settings.port, timeout=30)
            box.starttls(_context(settings))
        else:
            box = imaplib.IMAP4_SSL(settings.host, settings.port,
                                    ssl_context=_context(settings), timeout=30)
        box.login(settings.user, settings.password)
    except (imaplib.IMAP4.error, OSError, ssl.SSLError) as e:
        raise MailError(f"{settings.host}: {e}") from e
    return box


def check(settings):
    try:
        box = connect(settings)
    except MailError as e:
        return False, str(e)
    try:
        state, data = box.select(settings.folder, readonly=True)
        if state != "OK":
            return False, f"No folder named {settings.folder}"
        return True, f"{settings.user} is reachable, {int(data[0] or 0)} message(s) in {settings.folder}"
    finally:
        _close(box)


def _close(box):
    try:
        box.close()
    except Exception:
        pass
    try:
        box.logout()
    except Exception:
        pass


def _attachments(message):
    for part in message.walk():
        if part.get_content_maintype() == "multipart":
            continue
        name = _text(part.get_filename())
        if not name:
            continue
        media_type = intake.media_type_of(name, part.get_content_type())
        if media_type not in intake.READABLE and media_type not in intake.SHEETS:
            continue
        raw = part.get_payload(decode=True) or b""
        if not raw or len(raw) > MAX_ATTACHMENT:
            continue
        yield name, media_type, raw


def fetch(config, settings):
    """Unread mail in one folder, attachments into the queue, message left read."""
    box = connect(settings)
    picked, skipped, seen = [], 0, 0
    try:
        state, _ = box.select(settings.folder)
        if state != "OK":
            raise MailError(f"No folder named {settings.folder}")
        state, found = box.search(None, "UNSEEN")
        if state != "OK":
            raise MailError("The mailbox refused the search.")

        ids = (found[0] or b"").split()[:MAX_PER_RUN]
        for number in ids:
            state, data = box.fetch(number, "(RFC822)")
            if state != "OK" or not data or not isinstance(data[0], tuple):
                continue
            seen += 1
            message = email.message_from_bytes(data[0][1])
            sender = _text(message.get("From"))
            subject = _text(message.get("Subject"))
            for name, media_type, raw in _attachments(message):
                answer = intake.add(config, raw, name, media_type, source="mail",
                                    sender=sender, subject=subject)
                if answer["duplicate"]:
                    skipped += 1
                else:
                    picked.append(answer["name"])
            if settings.mark_read:
                box.store(number, "+FLAGS", "\\Seen")
    finally:
        _close(box)

    return {"messages": seen, "queued": len(picked), "duplicates": skipped, "names": picked}
