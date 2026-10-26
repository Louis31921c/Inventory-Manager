import hashlib
import re
from pathlib import Path

from . import db

READABLE = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
            "application/pdf": ".pdf"}
SHEETS = {"text/csv": ".csv", "application/vnd.ms-excel": ".csv",
          "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx"}
BY_SUFFIX = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
             ".webp": "image/webp", ".pdf": "application/pdf", ".csv": "text/csv",
             ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}

WAITING = "waiting"
DONE = "read"
DROPPED = "dropped"


def folder(config):
    path = Path(config.data_dir) / "intake"
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def media_type_of(name, offered=None):
    if offered in READABLE or offered in SHEETS:
        return offered
    return BY_SUFFIX.get(Path(name).suffix.lower())


def safe_name(name):
    cleaned = re.sub(r"[^A-Za-z0-9._ -]", "_", Path(name).name).strip() or "file"
    return cleaned[:120]


def add(config, raw, name, media_type, source="file", sender=None, subject=None):
    """Park a file in the queue. The same bytes twice stay one row."""
    media_type = media_type_of(name, media_type)
    if media_type is None:
        raise ValueError(f"{name}: not a kind of file this reads")

    sha = hashlib.sha256(raw).hexdigest()
    seen = db.read(config.db_path, "SELECT intake_id FROM intake WHERE sha256 = ?", (sha,))
    if seen:
        return {"intake_id": seen[0][0], "name": name, "duplicate": True}

    suffix = READABLE.get(media_type) or SHEETS.get(media_type) or ".bin"
    path = folder(config) / f"{sha[:16]}{suffix}"
    path.write_bytes(raw)
    path.chmod(0o600)

    row = db.write(config.db_path, """
            INSERT INTO intake (source, name, media_type, sha256, path, bytes, sender, subject)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            RETURNING intake_id
            """, (source, safe_name(name), media_type, sha, str(path), len(raw), sender, subject))
    return {"intake_id": row[0], "name": safe_name(name), "duplicate": False}


def waiting(config, limit=100):
    rows = db.read(config.db_path, """
            SELECT intake_id, source, name, media_type, bytes, sender, subject, arrived_at,
                   state, note
            FROM intake
            WHERE state = ?
            ORDER BY arrived_at DESC, intake_id DESC
            LIMIT ?
            """, (WAITING, limit))
    return [{"intake_id": i, "source": source, "name": name, "media_type": media_type,
             "bytes": size, "sender": sender, "subject": subject,
             "arrived_at": arrived.isoformat(timespec="minutes") if arrived else None,
             "state": state, "note": note,
             "kind": "sheet" if media_type in SHEETS else "scan"}
            for i, source, name, media_type, size, sender, subject, arrived, state, note in rows]


def counts(config):
    rows = db.read(config.db_path, "SELECT state, count(*) FROM intake GROUP BY state")
    return {state: number for state, number in rows}


def one(config, intake_id):
    rows = db.read(config.db_path, """
            SELECT intake_id, name, media_type, path, sha256, state FROM intake
            WHERE intake_id = ?
            """, (intake_id,))
    if not rows:
        return None
    intake_id, name, media_type, path, sha, state = rows[0]
    return {"intake_id": intake_id, "name": name, "media_type": media_type,
            "path": Path(path), "sha256": sha, "state": state}


def bytes_of(config, intake_id):
    item = one(config, intake_id)
    if item is None:
        raise KeyError(intake_id)
    return item["path"].read_bytes(), item["media_type"], item


def mark(config, intake_id, state, note=None):
    db.write(config.db_path, "UPDATE intake SET state = ?, note = ? WHERE intake_id = ?",
             (state, note, intake_id))


def drop(config, intake_id):
    item = one(config, intake_id)
    if item is None:
        return False
    try:
        item["path"].unlink(missing_ok=True)
    except OSError:
        pass
    db.write(config.db_path, "DELETE FROM intake WHERE intake_id = ?", (intake_id,))
    return True
