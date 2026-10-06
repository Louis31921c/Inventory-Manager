
import subprocess
import sys
import tarfile
import time
from datetime import datetime
from pathlib import Path

import duckdb

TABLES = ("notes", "deliveries", "hardware_lists", "hardware_list_lines", "catalog",
          "article_names", "article_aliases", "readings", "users", "audit", "errors",
          "schema_migrations")


def export(source, target):
    with duckdb.connect(str(source), read_only=True) as con:
        con.execute(f"EXPORT DATABASE '{target}' (FORMAT parquet)")
        return _counts(con)


def restore(export_dir, target):
    if target.exists():
        raise SystemExit(f"{target} already exists: choose another file.")
    with duckdb.connect(str(target)) as con:
        con.execute(f"IMPORT DATABASE '{export_dir}'")
        return _counts(con)


def counts(database):
    with duckdb.connect(str(database), read_only=True) as con:
        return _counts(con)


def _counts(con):
    present = {row[0] for row in con.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
    ).fetchall()}
    return {t: con.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0]
            for t in TABLES if t in present}


def list_backups(data_dir):
    folder = data_dir / "backups"
    if not folder.exists():
        return []
    files = sorted(folder.glob("stock-*.tar.gz*"), key=lambda f: f.stat().st_mtime, reverse=True)
    return [{"name": f.name,
             "bytes": f.stat().st_size,
             "date": datetime.fromtimestamp(f.stat().st_mtime).isoformat(timespec="minutes"),
             "encrypted": f.suffix == ".enc"}
            for f in files]


def open_backup(archive, passphrase, into):
    
    into.mkdir(parents=True, exist_ok=True)
    source = archive
    if archive.suffix == ".enc":
        if not passphrase:
            raise ValueError("BACKUP_PASSPHRASE is not set: this backup cannot be decrypted.")
        source = into / "archive.tar.gz"
        result = subprocess.run(
            ["openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-iter", "200000",
             "-in", str(archive), "-out", str(source), "-pass", f"pass:{passphrase}"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise ValueError("Decryption failed: wrong backup passphrase?")
    with tarfile.open(source) as tar:
        tar.extractall(into, filter="data")
    return into / "db"


def table_to_csv(export_dir, table, target):
   
    scratch = target.parent / "read.duckdb"
    with duckdb.connect(str(scratch)) as con:
        con.execute(f"IMPORT DATABASE '{export_dir}'")
        con.execute(f"COPY (SELECT * FROM \"{table}\") TO '{target}' (HEADER, DELIMITER ';')")
    return target


def main():
    from pathlib import Path

    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    action = sys.argv[1]
    if action == "export":
        result = export(Path(sys.argv[2]), Path(sys.argv[3]))
    elif action == "import":
        result = restore(Path(sys.argv[2]), Path(sys.argv[3]))
    elif action == "counts":
        result = counts(Path(sys.argv[2]))
    else:
        raise SystemExit(__doc__)
    print("  " + ", ".join(f"{table} {n}" for table, n in sorted(result.items())))


if __name__ == "__main__":
    main()


def snapshot(config, keep_days=14, passphrase=None):
    """Write an encrypted snapshot into <data>/backups and prune the old ones."""
    import os
    import shutil
    import tempfile

    out_dir = config.data_dir / "backups"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M")
    archive = out_dir / f"stock-{stamp}.tar.gz"

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        export(config.db_path, work / "db")
        rules = config.data_dir / "private_rules.md"
        if rules.exists():
            shutil.copy2(rules, work / rules.name)
        (work / "INFO.txt").write_text(f"snapshot {stamp}\n")
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(work, arcname=".")

    passphrase = os.environ.get("BACKUP_PASSPHRASE", "") if passphrase is None else passphrase
    if passphrase:
        encrypted = archive.with_suffix(archive.suffix + ".enc")
        result = subprocess.run(
            ["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-salt",
             "-in", str(archive), "-out", str(encrypted), "-pass", f"pass:{passphrase}"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise RuntimeError("openssl could not encrypt the snapshot")
        archive.unlink()
        archive = encrypted
    archive.chmod(0o600)

    cutoff = time.time() - keep_days * 86400
    for old in out_dir.glob("stock-*.tar.gz*"):
        if old != archive and old.stat().st_mtime < cutoff:
            old.unlink()
    return archive
