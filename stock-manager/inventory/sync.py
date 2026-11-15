"""Pull the data out of the older web version into this one.

The web app kept its tables in French: bons, livraisons, listes, liste_lignes, catalogue,
lectures. Everything here is the same data under the names this program uses. Notes and sheets are
matched on the photo hash, so running it twice adds nothing twice.
"""

import shutil
import subprocess
import tempfile
from pathlib import Path

import duckdb

from . import db, vocabulary

LEGACY_TABLES = ("bons", "livraisons", "listes", "liste_lignes", "catalogue", "lectures")
REMOTE_DB = "inventory/data/inventaire.duckdb"
REMOTE_IMAGES = "inventory/data/images"


class SyncError(Exception):
    pass


def fetch(server, into, key=None, remote=REMOTE_DB, photos=False, images=None):
    """Copy the database (and the photos if asked) off a server with scp."""
    scp = ["scp", "-q"]
    if key:
        scp += ["-i", str(key)]
    target = into / "legacy.duckdb"
    result = subprocess.run(scp + [f"{server}:{remote}", str(target)],
                            capture_output=True, text=True)
    if result.returncode != 0:
        raise SyncError(result.stderr.strip() or "scp could not copy the database")
    if photos and images is not None:
        images.mkdir(parents=True, exist_ok=True)
        subprocess.run(["rsync", "-a", "--ignore-existing"]
                       + (["-e", f"ssh -i {key}"] if key else [])
                       + [f"{server}:{REMOTE_IMAGES}/", str(images) + "/"],
                       capture_output=True, text=True)
    return target


def check(path):
    with duckdb.connect(str(path), read_only=True) as con:
        present = {row[0] for row in con.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
        ).fetchall()}
    missing = [name for name in ("bons", "livraisons") if name not in present]
    if missing:
        raise SyncError(f"that file is not from the web version (no {', '.join(missing)})")
    return present


def counts(path):
    present = check(path)
    with duckdb.connect(str(path), read_only=True) as con:
        return {name: con.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
                for name in LEGACY_TABLES if name in present}


def copy_in(db_path, legacy_path, images_dir=None, user=None):
    """Insert whatever is not already here. Returns what was added."""
    present = check(legacy_path)
    added = {"notes": 0, "inventory": 0, "orders": 0, "order_lines": 0, "catalog": 0,
             "readings": 0, "skipped_notes": 0, "skipped_orders": 0}

    with duckdb.connect(str(db_path)) as con:
        con.execute(f"ATTACH '{legacy_path}' AS legacy (READ_ONLY)")
        con.begin()
        try:
            for old_id, sha, image, confirmed in con.execute(
                    "SELECT bon_id, image_sha256, image_path, confirmed_at FROM legacy.bons "
                    "ORDER BY bon_id").fetchall():
                if sha and con.execute("SELECT 1 FROM notes WHERE image_sha256 = ?",
                                       [sha]).fetchone():
                    added["skipped_notes"] += 1
                    continue
                note_id = con.execute(
                    "INSERT INTO notes (image_sha256, image_path, confirmed_at, confirmed_by) "
                    "VALUES (?, ?, ?, ?) RETURNING note_id",
                    [sha, _photo(image, sha, images_dir), confirmed, user]).fetchone()[0]
                added["notes"] += 1
                added["inventory"] += len(con.execute(
                    """
                    INSERT INTO inventory_lines (article, quantity, supplier, delivery_date, order_date,
                                            site, work_item, backorder, designation, note_id,
                                            line_no)
                    SELECT article, quantite, fournisseur, date_livraison, date_commande,
                           chantier, ouvrage, reliquat, designation, ?, ligne_no
                    FROM legacy.livraisons WHERE bon_id = ? RETURNING 1
                    """, [note_id, old_id]).fetchall())

            if "listes" in present:
                for (old_id, day, site, work_item, drafter, sha, image,
                     created) in con.execute(
                        "SELECT liste_id, date_liste, chantier, ouvrage, dessinateur, "
                        "image_sha256, image_path, cree_le FROM legacy.listes "
                        "ORDER BY liste_id").fetchall():
                    if sha and con.execute("SELECT 1 FROM purchase_orders WHERE image_sha256 = ?",
                                           [sha]).fetchone():
                        added["skipped_orders"] += 1
                        continue
                    list_id = con.execute(
                        "INSERT INTO purchase_orders (list_date, site, work_item, drafter, "
                        "image_sha256, image_path, created_at, created_by) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) RETURNING list_id",
                        [day, site, work_item, drafter, sha, _photo(image, sha, images_dir),
                         created, user]).fetchone()[0]
                    added["orders"] += 1
                    added["order_lines"] += len(con.execute(
                        """
                        INSERT INTO purchase_order_lines (list_id, line_no, article, designation,
                                                         reference, quantity, stock, in_stock)
                        SELECT ?, ligne_no, article, designation, reference, quantite, stock,
                               en_stock
                        FROM legacy.liste_lignes WHERE liste_id = ? RETURNING 1
                        """, [list_id, old_id]).fetchall())

            if "catalogue" in present:
                added["catalog"] = len(con.execute(
                    """
                    INSERT INTO catalog (supplier, designation, reference, article, seen,
                                         updated_at)
                    SELECT fournisseur, designation, reference, article, vu, maj_le
                    FROM legacy.catalogue
                    ON CONFLICT (supplier, designation) DO NOTHING
                    RETURNING 1
                    """).fetchall())

            if "lectures" in present:
                added["readings"] = len(con.execute(
                    """
                    INSERT INTO readings (measured_at, document, fields, corrected, lines)
                    SELECT mesure_le, document, champs, corriges, lignes FROM legacy.lectures
                    RETURNING 1
                    """).fetchall())

            vocabulary.backfill(con)
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.execute("DETACH legacy")
    return added


def _photo(image_path, sha, images_dir):
    """Keep the photo only if it was copied across; otherwise the record stands on its own."""
    if not sha or images_dir is None:
        return ""
    for candidate in sorted(Path(images_dir).glob(f"{sha}.*")):
        return str(candidate)
    return ""


def from_server(config, server, key=None, photos=False):
    db.init(config.db_path)
    with tempfile.TemporaryDirectory() as tmp:
        legacy = fetch(server, Path(tmp), key, photos=photos, images=config.images_dir)
        before = counts(legacy)
        added = copy_in(config.db_path, legacy, config.images_dir, config.user)
    return before, added


def from_file(config, path, photos_dir=None):
    db.init(config.db_path)
    legacy = Path(path).expanduser()
    if not legacy.is_file():
        raise SyncError(f"no such file: {legacy}")
    if photos_dir:
        config.images_dir.mkdir(parents=True, exist_ok=True)
        for photo in Path(photos_dir).expanduser().glob("*"):
            target = config.images_dir / photo.name
            if photo.is_file() and not target.exists():
                shutil.copy2(photo, target)
    before = counts(legacy)
    added = copy_in(config.db_path, legacy, config.images_dir, config.user)
    return before, added
