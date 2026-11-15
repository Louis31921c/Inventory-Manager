"""The bank file goes to the bank by SFTP, and the bank's answer comes back the same way.

payments.py writes a batch of payments as a SEPA credit transfer file (pain.001). Many banks and
ERP gateways take that file from a folder on an SFTP server, and put a status report (pain.002)
in another. This module is that hand-over, nothing more: it uploads the file and reads the report.

It uses the `sftp` program of the machine, with a key file and BatchMode, so nothing here ever
holds or types a password. The host must already be in the user's known_hosts (connect once by
hand): the check is strict, so a different machine answering is a failure, not a surprise.

The status report is read for its group status: ACCP, ACTC, ACSP, ACSC, ACWC are accepted;
RJCT is rejected; PDNG and anything else is still pending. A rejected batch cancels its
payments and gives the wallet its money back (payments.cancel).
"""

import os
import re
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from . import db

KEYS = ("host", "port", "user", "key", "dir", "status_dir")
ACCEPTED = {"ACCP", "ACTC", "ACSP", "ACSC", "ACWC"}
HOST = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$")
USER = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")
SAFE_PATH = re.compile(r'^[^\x00-\x1f"\\]+$')
TIMEOUT = 60


def settings(db_path):
    rows = dict(db.read(db_path, "SELECT key, value FROM policy_settings WHERE key LIKE 'bank_sftp_%'"))
    out = {key: rows.get(f"bank_sftp_{key}", "") for key in KEYS}
    out["port"] = out["port"] or "22"
    return out


def save(db_path, host, port, user, key, directory, status_dir):
    host, user = (host or "").strip(), (user or "").strip()
    key, directory, status_dir = (key or "").strip(), (directory or "").strip(), (status_dir or "").strip()
    if host and not HOST.match(host):
        raise ValueError("That is not a host name.")
    if user and not USER.match(user):
        raise ValueError("That is not a user name.")
    try:
        number = int(port or 22)
    except (TypeError, ValueError):
        raise ValueError("The port is a number.") from None
    if not 0 < number < 65536:
        raise ValueError("The port is a number from 1 to 65535.")
    for path in (key, directory, status_dir):
        if path and not SAFE_PATH.match(path):
            raise ValueError("A path may not hold quotes, backslashes or control characters.")
    if key:
        found = Path(key).expanduser()
        if not found.is_file():
            raise ValueError("The key file is not there.")
        if found.stat().st_mode & 0o077:
            raise ValueError("The key file must be private: chmod 600 on it.")
    for name, value in (("host", host), ("port", str(number)), ("user", user), ("key", key),
                        ("dir", directory), ("status_dir", status_dir)):
        db.write(db_path, "INSERT INTO policy_settings (key, value) VALUES (?, ?) "
                          "ON CONFLICT (key) DO UPDATE SET value = excluded.value RETURNING key",
                 [f"bank_sftp_{name}", value])
    return settings(db_path)


def ready(db_path):
    """(whether a file can be handed over, the reason when not)."""
    s = settings(db_path)
    missing = [name for name in ("host", "user", "key", "dir") if not s[name]]
    if missing:
        return False, "the bank drop lacks: " + ", ".join(missing)
    return True, ""


def _command(s, binary="sftp"):
    return [binary, "-b", "-", "-i", str(Path(s["key"]).expanduser()), "-P", s["port"],
            "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=15",
            f"{s['user']}@{s['host']}"]


def _run(db_path, script, run):
    s = settings(db_path)
    done = run(_command(s), input=script, capture_output=True, text=True, timeout=TIMEOUT)
    if done.returncode != 0:
        raise ValueError("The bank drop refused: " + ((done.stderr or done.stdout or "").strip()[-300:]
                                                       or f"sftp exit {done.returncode}"))
    return done.stdout


def check(db_path, run=subprocess.run):
    """Log in and list the folder: the test the Settings card offers."""
    ok, why = ready(db_path)
    if not ok:
        raise ValueError(why)
    s = settings(db_path)
    _run(db_path, f'ls -1 "{s["dir"]}"\n', run)
    return True


def upload(db_path, local_path, remote_name, run=subprocess.run):
    ok, why = ready(db_path)
    if not ok:
        raise ValueError(why)
    if not re.fullmatch(r"[A-Za-z0-9._-]+", remote_name):
        raise ValueError("Odd file name.")
    s = settings(db_path)
    _run(db_path, f'put "{local_path}" "{s["dir"].rstrip("/")}/{remote_name}"\n', run)
    return True


def parse_status(text):
    """'accepted', 'rejected' or 'pending' from the group status of a pain.002 report."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return "pending"
    for node in root.iter():
        if node.tag.split("}")[-1] == "GrpSts" and (node.text or "").strip():
            code = node.text.strip().upper()
            return "accepted" if code in ACCEPTED else "rejected" if code == "RJCT" else "pending"
    return "pending"


def fetch_status(db_path, msg_id, run=subprocess.run):
    """The report for a message id, if the bank has put one down: 'accepted', 'rejected' or 'pending'."""
    s = settings(db_path)
    where = (s["status_dir"] or s["dir"]).rstrip("/")
    names = [line.strip() for line in _run(db_path, f'ls -1 "{where}"\n', run).splitlines()]
    found = [name.split("/")[-1] for name in names if msg_id in name and name.lower().endswith(".xml")
             and not name.split("/")[-1].startswith(msg_id + ".xml")]
    if not found:
        return "pending"
    with tempfile.TemporaryDirectory() as folder:
        target = os.path.join(folder, "status.xml")
        _run(db_path, f'get "{where}/{found[0]}" "{target}"\n', run)
        return parse_status(Path(target).read_text(encoding="utf-8", errors="replace"))
