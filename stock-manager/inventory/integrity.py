"""Has the program been changed since the vendor signed it?

A release carries MANIFEST.json: the SHA-256 of every file of the package (code, pages, prompts, rule
defaults, fonts) and the vendor's signature over that list. At start, and every few minutes, the files on
disk are hashed again. A file that differs, one that is missing, and one that is not in the list (a new
`.py` dropped into the package) all fail the check, and the program refuses to serve until the vendor's
files are put back. What a licensee may change lives outside the package: the database and the settings
file. The rule files read from BOT_RULES_DIR are theirs too, and the code enforces the numbers in them.

STOCK_INTEGRITY=off switches the check off, for development and the test suite. A source checkout has no
manifest, and that is reported as "unsigned build", which only the check being off allows.
"""

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

from . import licensing

PACKAGE = Path(__file__).resolve().parent
MANIFEST = PACKAGE / "MANIFEST.json"
SUFFIXES = {".py", ".html", ".css", ".js", ".md", ".txt", ".png", ".woff2", ".json"}
SKIP_DIRS = {"__pycache__"}
SKIP_FILES = {"MANIFEST.json"}
RECHECK_SECONDS = 300

_last: dict[str, Any] = {"at": 0.0, "result": None}


def enforced():
    return os.environ.get("STOCK_INTEGRITY", "on").strip().lower() != "off"


def hash_files(root=None):
    root = Path(root or PACKAGE)
    out = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name in SKIP_FILES or path.suffix not in SUFFIXES:
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        out[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def build(private_key_b64, root=None):
    """The signed manifest for the package as it is now (tools/sign_manifest.py)."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    import base64

    files = hash_files(root)
    key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(private_key_b64))
    return {"files": files, "signature": base64.b64encode(key.sign(licensing.canonical(files))).decode()}


def verify(root=None, manifest=None, public_key=None):
    """{ok, reason, changed, missing, added}."""
    root = Path(root or PACKAGE)
    path = Path(manifest or (root / "MANIFEST.json"))
    out = {"ok": False, "reason": "", "changed": [], "missing": [], "added": []}
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        out["reason"] = "unsigned build: there is no manifest"
        return out
    files = document.get("files", {})
    if not licensing.verify_signature(files, document.get("signature", ""), public_key):
        out["reason"] = "the manifest is not signed by the vendor"
        return out
    now = hash_files(root)
    out["changed"] = sorted(name for name, digest in files.items() if name in now and now[name] != digest)
    out["missing"] = sorted(name for name in files if name not in now)
    out["added"] = sorted(name for name in now if name not in files)
    out["ok"] = not (out["changed"] or out["missing"] or out["added"])
    if not out["ok"]:
        out["reason"] = "program files were changed, removed or added"
    return out


def status(force=False):
    """The cached verdict, refreshed every few minutes. With the check off it is always fine."""
    if not enforced():
        return {"ok": True, "reason": "integrity check is off", "changed": [], "missing": [], "added": [], "off": True}
    now = time.time()
    if force or _last["result"] is None or now - _last["at"] > RECHECK_SECONDS:
        _last.update(at=now, result=verify())
    return _last["result"]
