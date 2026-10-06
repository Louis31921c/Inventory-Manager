import base64
import json
import logging
import urllib.error
import urllib.parse
import urllib.request

import duckdb

from .config import load_config

log = logging.getLogger("inventory.alerts")

PREFIX = "Stock manager"
MAX_DESCRIPTION = 400
MAX_SMS = 300
DUPLICATE_MINUTES = 15   
TIMEOUT = 10


def report(config, kind, description, context=None, user=None):
   
    text = " ".join(str(description).split())[:MAX_DESCRIPTION] or "unspecified error"
    try:
        with duckdb.connect(str(config.db_path)) as con:
            repeated = con.execute(
                f"SELECT count(*) FROM errors WHERE description = ? "
                f"AND happened_at > now() - INTERVAL {DUPLICATE_MINUTES} MINUTE", [text],
            ).fetchone()[0]
            sent_this_hour = con.execute(
                "SELECT count(*) FROM errors WHERE sms_status = 'sent' "
                "AND happened_at > now() - INTERVAL 1 HOUR"
            ).fetchone()[0]
            error_no = con.execute(
                "INSERT INTO errors (kind, description, context, user_name, sms_status) "
                "VALUES (?, ?, ?, ?, 'pending') RETURNING error_no",
                [kind, text, context, user],
            ).fetchone()[0]
    except duckdb.Error:
    
        log.exception("could not store the error report: %s", text)
        status = send(config.sms, f"{PREFIX} error (unnumbered): {text}")
        return {"error_no": None, "kind": kind, "description": text, "sms_status": status}

    if not config.sms.enabled:
        status = "disabled"
    elif repeated:
        status = f"muted: same error in the last {DUPLICATE_MINUTES} min"
    elif sent_this_hour >= config.sms.max_per_hour:
        status = f"muted: {config.sms.max_per_hour} messages already sent this hour"
    else:
        status = send(config.sms, f"{PREFIX} error #{error_no}: {text}")

    try:
        with duckdb.connect(str(config.db_path)) as con:
            con.execute("UPDATE errors SET sms_status = ? WHERE error_no = ?",
                        [status[:200], error_no])
    except duckdb.Error:
        log.exception("could not record the message status of error #%s", error_no)

    log.error("error #%s (%s): %s [sms: %s]", error_no, kind, text, status)
    return {"error_no": error_no, "kind": kind, "description": text, "sms_status": status}


def send(sms, text):
    
    if not sms.enabled:
        return "disabled"
    body = text[:MAX_SMS]
    try:
        if sms.provider == "twilio":
            _twilio(sms, body)
        elif sms.provider == "webhook":
            _webhook(sms, body)
        else:
            return "disabled"
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:120] if hasattr(e, "read") else ""
        return f"failed: http {e.code} {detail}".strip()
    except Exception as e:  
        return f"failed: {type(e).__name__} {e}"[:200]
    return "sent"


def _twilio(sms, text):
    url = (f"https://api.twilio.com/2010-04-01/Accounts/"
           f"{urllib.parse.quote(sms.account_sid)}/Messages.json")
    data = urllib.parse.urlencode({"To": sms.to, "From": sms.sender, "Body": text}).encode()
    token = base64.b64encode(f"{sms.account_sid}:{sms.auth_token}".encode()).decode()
    request = urllib.request.Request(url, data=data, method="POST", headers={
        "Authorization": f"Basic {token}",
        "Content-Type": "application/x-www-form-urlencoded",
    })
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        response.read()


def _webhook(sms, text):
    headers = {"Content-Type": "application/json"}
    if sms.webhook_token:
        headers["Authorization"] = f"Bearer {sms.webhook_token}"
    payload = json.dumps({"to": sms.to, "text": text}).encode()
    request = urllib.request.Request(sms.webhook_url, data=payload, method="POST", headers=headers)
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        response.read()


def recent(db_path, limit=50):
    with duckdb.connect(str(db_path), read_only=True) as con:
        rows = con.execute(
            "SELECT error_no, happened_at, kind, description, context, user_name, sms_status "
            "FROM errors ORDER BY error_no DESC LIMIT ?", [int(limit)],
        ).fetchall()
    return [{"error_no": no, "at": at.isoformat(timespec="seconds") if at else None,
             "kind": kind, "description": description, "context": context, "user": user,
             "sms_status": status}
            for no, at, kind, description, context, user, status in rows]


def destination(sms):
  
    number = sms.to
    masked = (number[:3] + "..." + number[-3:]) if len(number) > 6 else ("set" if number else "")
    return {"provider": sms.provider, "enabled": sms.enabled, "to": masked,
            "max_per_hour": sms.max_per_hour}


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Report an error: store it and text it.")
    parser.add_argument("description", help="what went wrong, in one line")
    parser.add_argument("--kind", default="script", help="monitor, backup, script...")
    parser.add_argument("--context", default=None, help="script name, path, host")
    args = parser.parse_args()
    logging.basicConfig(format="%(levelname)s %(message)s", level=logging.INFO)
    result = report(load_config(), args.kind, args.description, args.context)
    print(f"error #{result['error_no']}, sms: {result['sms_status']}")


if __name__ == "__main__":
    main()
