"""The licence file, and whether this copy may run.

A licence is a small JSON file signed by the vendor's private key (which never leaves the vendor). The
program holds only the public key, so it can check a licence but cannot make one. The file says who the
copy is licensed to, until when, and for which machine ("any" or the id this machine shows on the
licence page). It sits in the settings folder as `license.json`, or wherever STOCK_LICENSE_FILE points.

STOCK_LICENSE=off switches the check off, for development and for the test suite. Nothing else does.

Be clear about what this is: it keeps an honest licensee honest and makes copying a deliberate act. The
program is Python, so a person who can edit it can remove the check; integrity.py notices that, and a
compiled build raises the effort again. The licence terms are what finally bind.
"""

import base64
import hashlib
import json
import os
import socket
import uuid
from datetime import date
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .config import config_dir

# The vendor's public key. The matching private key is in tools/ only as a script that reads it from the vendor's machine.
PUBLIC_KEY = "eS5VbmgQ0KUJ6vU8lc2lEs9mWcMLrDrs6URpBjLd4c0="
PRODUCT = "stock-manager"
WARN_DAYS = 30


def enforced():
    return os.environ.get("STOCK_LICENSE", "on").strip().lower() != "off"


def licence_file():
    return Path(os.environ.get("STOCK_LICENSE_FILE") or (config_dir() / "license.json"))


def machine_id():
    """A stable id for this machine, a hash of its own id so the raw one is not shown."""
    raw = ""
    for candidate in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            raw = Path(candidate).read_text().strip()
            break
        except OSError:
            continue
    raw = raw or f"{uuid.getnode()}-{socket.gethostname()}"
    return hashlib.sha256(f"{PRODUCT}:{raw}".encode()).hexdigest()[:16]


def canonical(payload):
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def verify_signature(payload, signature, public_key=None):
    try:
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key or PUBLIC_KEY))
        key.verify(base64.b64decode(signature), canonical(payload))
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def check(document, today=None, public_key=None, machine=None):
    """Judge a licence document. Gives back {valid, reason, licensee, expires, days_left, machine}."""
    today = today or date.today()
    out = {"valid": False, "reason": "", "licensee": None, "expires": None, "days_left": None,
           "machine": machine or machine_id()}
    if not isinstance(document, dict) or "payload" not in document or "signature" not in document:
        out["reason"] = "the licence file is not a licence"
        return out
    payload = document["payload"]
    if not verify_signature(payload, document["signature"], public_key):
        out["reason"] = "the licence is not signed by the vendor, or was changed"
        return out
    if payload.get("product") != PRODUCT:
        out["reason"] = "the licence is for another product"
        return out
    out["licensee"] = payload.get("licensee")
    try:
        expires = date.fromisoformat(payload["expires"])
    except (KeyError, ValueError):
        out["reason"] = "the licence has no valid end date"
        return out
    out["expires"], out["days_left"] = expires.isoformat(), (expires - today).days
    if payload.get("machine", "any") not in ("any", out["machine"]):
        out["reason"] = "the licence is for another machine"
        return out
    if expires < today:
        out["reason"] = f"the licence ended on {expires.isoformat()}"
        return out
    out["valid"] = True
    out["warn"] = out["days_left"] <= WARN_DAYS
    return out


def status(today=None):
    """The state of this copy now. With the check switched off it is always valid, and says so."""
    if not enforced():
        return {"valid": True, "reason": "licence check is off", "licensee": None, "expires": None,
                "days_left": None, "machine": machine_id(), "off": True}
    try:
        document = json.loads(licence_file().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {**check({}, today), "reason": "there is no licence file yet"}
    except (OSError, json.JSONDecodeError):
        return check(None, today)
    return check(document, today)


def install(text, today=None):
    """Keep a licence a person pasted or uploaded, if it is a good one. Gives back its status."""
    try:
        document = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        raise ValueError("That is not a licence file.") from None
    found = check(document, today)
    if not found["valid"]:
        raise ValueError(f"That licence cannot be used: {found['reason']}.")
    path = licence_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=1), encoding="utf-8")
    path.chmod(0o600)
    return found


def issue(private_key_b64, licensee, days=365, machine="any", today=None):
    """Make a signed licence. Only the vendor, who holds the private key, can do this (tools/issue_license.py)."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    today = today or date.today()
    payload = {"product": PRODUCT, "licensee": licensee, "issued": today.isoformat(),
               "expires": date.fromordinal(today.toordinal() + int(days)).isoformat(), "machine": machine}
    key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(private_key_b64))
    return {"payload": payload, "signature": base64.b64encode(key.sign(canonical(payload))).decode()}
