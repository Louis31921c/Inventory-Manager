"""One look at whether this copy is in good order: `stock doctor`, and /api/health on the screen.

Every check says ok or not, with a line of detail. Nothing here changes anything.
"""

import stat
import time
from datetime import datetime

from . import __version__, audit, db, integrity, licensing, migrations, vault
from .config import config_dir

BACKUP_MAX_AGE_DAYS = 3


def _check(name, ok, detail):
    return {"name": name, "ok": bool(ok), "detail": detail}


def report(config):
    out = [_check("version", True, __version__)]

    held = licensing.status()
    out.append(_check("licence", held["valid"], held["reason"] or
                      f"{held['licensee']} until {held['expires']}" if not held.get("off") else "check is off"))
    sealed = integrity.status()
    out.append(_check("program files", sealed["ok"], sealed["reason"] or "match the vendor's signature"))

    try:
        version = db.read(config.db_path, "SELECT max(version) FROM schema_migrations")[0][0]
        newest = migrations.MIGRATIONS[-1][0]
        out.append(_check("database", version == newest, f"schema {version} of {newest}"))
    except Exception as e:                       # noqa: BLE001
        out.append(_check("database", False, f"cannot be read: {e}"))

    chain = audit.verify(config.db_path)
    out.append(_check("audit log", chain["ok"], f"{chain['checked']} events chained" if chain["ok"]
                      else f"broken at event {chain['broken_at']}: {chain['why']}"))

    loose = []
    for path in (config_dir(), config.data_dir, config_dir() / "settings.env", config_dir() / "account.json",
                 vault.key_file(), config.data_dir / "secret.key"):
        try:
            if path.exists() and stat.S_IMODE(path.stat().st_mode) & 0o077:
                loose.append(path.name)
        except OSError:
            continue
    out.append(_check("permissions", not loose, "owner only" if not loose else "open to others: " + ", ".join(loose)))

    plain = []
    try:
        text = (config_dir() / "settings.env").read_text(encoding="utf-8")
        plain = [line.split("=")[0] for line in text.splitlines()
                 if line.split("=")[0] in vault.SECRET_KEYS and "=" in line and line.split("=", 1)[1]
                 and not vault.is_sealed(line.split("=", 1)[1])]
    except OSError:
        pass
    out.append(_check("secrets", not plain, "encrypted on disk" if not plain else "in the clear: " + ", ".join(plain)))

    folder = config.data_dir / "backups"
    newest_backup = max((p.stat().st_mtime for p in folder.glob("*")), default=None) if folder.is_dir() else None
    age = (time.time() - newest_backup) / 86400 if newest_backup else None
    out.append(_check("backup", age is not None and age <= BACKUP_MAX_AGE_DAYS,
                      "none yet" if age is None else f"latest {age:.1f} day(s) old"))
    return {"ok": all(c["ok"] for c in out), "checked_at": datetime.now().isoformat(timespec="seconds"), "checks": out}
