import shutil
from dataclasses import dataclass
from pathlib import Path

import duckdb

from . import catalog, migrations, vocabulary
from .models import normalize_name

ROW_COLUMNS = ("article", "quantity", "supplier", "delivery_date", "order_date", "site",
               "work_item", "backorder", "designation")
EXPORT_COLUMNS = ("article", "quantity", "supplier", "delivery_date", "weekday", "order_date",
                  "site", "work_item", "backorder", "designation", "note_id")
LIST_COLUMNS = ("list_date", "site", "work_item", "drafter", "image_sha256", "image_path",
                "created_by")
LIST_LINE_COLUMNS = ("list_id", "line_no", "article", "designation", "reference", "quantity",
                     "stock", "in_stock")
COUNTED_TABLES = ("notes", "inventory_lines", "purchase_orders", "purchase_order_lines", "catalog",
                  "article_names", "audit")


class QueryError(Exception):
    pass


def read(db_path, sql, args=()):
    with duckdb.connect(str(db_path), read_only=True) as con:
        return con.execute(sql, list(args)).fetchall()


def write(db_path, sql, args=()):
    with duckdb.connect(str(db_path)) as con:
        return con.execute(sql, list(args)).fetchone()


@dataclass
class QueryResult:
    columns: list
    rows: list
    truncated: bool


def init(db_path):

    db_path.parent.mkdir(parents=True, exist_ok=True)
    with duckdb.connect(str(db_path)) as con:
        applied = migrations.apply(con)
        catalog.backfill(con)
        vocabulary.backfill(con)
    for path in (db_path, db_path.with_suffix(db_path.suffix + ".wal")):
        try:
            path.chmod(0o600)
        except OSError:
            pass
    return applied


def find_by_image(db_path, sha256):
    rows = read(db_path, "SELECT note_id FROM notes WHERE image_sha256 = ?", [sha256])
    return rows[0][0] if rows else None


def save_note(db_path, note, image_sha256, image_path, user=None):
 
    rows = note.rows()
    with duckdb.connect(str(db_path)) as con:
        con.begin()
        try:
         
            for row in rows:
                row["article"] = vocabulary.resolve(con, row["article"])

            note_id = con.execute(
                "INSERT INTO notes (image_sha256, image_path, confirmed_by) VALUES (?, ?, ?) "
                "RETURNING note_id", [image_sha256, image_path, user],
            ).fetchone()[0]
            placeholders = ", ".join("?" * (len(ROW_COLUMNS) + 2))
            con.executemany(
                f"INSERT INTO inventory_lines ({', '.join(ROW_COLUMNS)}, note_id, line_no) "
                f"VALUES ({placeholders})",
                [[r[c] for c in ROW_COLUMNS] + [note_id, n] for n, r in enumerate(rows, 1)],
            )
            catalog.learn(con, note)
            vocabulary.record(con, (r["article"] for r in rows), "delivery")
            con.commit()
        except Exception:
            con.rollback()
            raise
    return note_id


def describe_note(db_path, note_id):
   
    supplier, lines = read(db_path, "SELECT any_value(supplier), count(*) FROM inventory_lines "
                                    "WHERE note_id = ?", [note_id])[0]
    return (supplier, lines) if lines else None


def delete_note(db_path, note_id):
   
    with duckdb.connect(str(db_path)) as con:
        con.begin()
        row = con.execute("DELETE FROM notes WHERE note_id = ? RETURNING image_path",
                          [note_id]).fetchone()
        con.execute("DELETE FROM inventory_lines WHERE note_id = ?", [note_id])
        con.commit()
    return row[0] if row else None


def update_work_item(db_path, note_id, line_no, work_item):
   
    return write(db_path, "UPDATE inventory_lines SET work_item = ? WHERE note_id = ? AND line_no = ? "
                          "RETURNING line_no",
                 [normalize_name(work_item), note_id, line_no]) is not None


def stock_by_article(db_path):
    
    rows = read(db_path, """
            SELECT article,
                   sum(quantity)                     AS quantity,
                   max(delivery_date)                AS last_delivery,
                   arg_max(supplier, delivery_date)  AS supplier,
                   arg_max(site, delivery_date)      AS site,
                   arg_max(work_item, delivery_date) AS work_item,
                   count(*)                          AS lines,
                   count(*) FILTER (backorder)       AS backorders
            FROM inventory_lines
            GROUP BY article
            ORDER BY article
            """)
    return [{"article": article, "quantity": quantity,
             "delivery_date": last.isoformat() if last else None,
             "supplier": supplier, "site": site, "work_item": work_item,
             "lines": lines, "backorders": backorders}
            for article, quantity, last, supplier, site, work_item, lines, backorders in rows]


EXPORTS = {
    "inventory": "SELECT " + ", ".join(EXPORT_COLUMNS)
                  + " FROM inventory_lines ORDER BY delivery_date, note_id",
    "stock": """SELECT article, sum(quantity) AS quantity, max(delivery_date) AS last_delivery,
                           arg_max(supplier, delivery_date) AS supplier,
                           arg_max(site, delivery_date) AS site,
                           arg_max(work_item, delivery_date) AS work_item,
                           count(*) AS lines, count(*) FILTER (backorder) AS backorders
                    FROM inventory_lines GROUP BY article ORDER BY article""",
    "purchase_orders": """SELECT h.list_id, h.list_date, h.site, h.work_item, h.drafter,
                          l.line_no, l.article, l.designation, l.quantity, l.stock, l.in_stock
                   FROM purchase_orders h JOIN purchase_order_lines l USING (list_id)
                   ORDER BY h.list_date DESC NULLS LAST, h.list_id, l.line_no""",
    "catalog": "SELECT article, supplier, reference, designation, seen FROM catalog "
               "ORDER BY article",
    "vocabulary": """SELECT n.article, n.seen, n.source, n.first_seen, n.last_seen,
                            coalesce(string_agg(a.alias, ', ' ORDER BY a.alias), '') AS aliases
                     FROM article_names n LEFT JOIN article_aliases a ON a.article = n.article
                     GROUP BY n.article, n.seen, n.source, n.first_seen, n.last_seen
                     ORDER BY n.seen DESC, n.article""",
    "audit": """SELECT event_id, happened_at, user_name, action, target, detail, address
                FROM audit ORDER BY event_id""",
}


def export_query(db_path, dataset):
    with duckdb.connect(str(db_path), read_only=True) as con:
        con.execute(EXPORTS[dataset])
        return [column[0] for column in con.description], con.fetchall()


def export_to_file(db_path, dataset, out):
    
    options = "(FORMAT parquet)" if out.suffix == ".parquet" else "(HEADER, DELIMITER ';')"
    with duckdb.connect(str(db_path), read_only=True) as con:
        con.execute(f"COPY ({EXPORTS[dataset]}) TO '{out}' {options}")


def export(db_path, out_dir):
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = [out_dir / "inventory.csv", out_dir / "inventory.parquet"]
    for path in paths:
        export_to_file(db_path, "inventory", path)
    return paths


def purge_photos(db_path, images_dir, days=42):
  
    removed, freed = 0, 0
    if not images_dir.exists():
        return {"files": 0, "bytes": 0, "notes": 0, "lists": 0}

    def unlink(path_str):
        nonlocal removed, freed
        if not path_str:
            return
        path = Path(path_str)
        if not path.is_file():
            return
        freed += path.stat().st_size
        path.unlink(missing_ok=True)
        removed += 1

    with duckdb.connect(str(db_path)) as con:
        old_notes = con.execute(
            "SELECT note_id, image_path FROM notes WHERE image_path IS NOT NULL "
            f"AND confirmed_at < now() - INTERVAL {int(days)} DAY"
        ).fetchall()
        for note_id, path_str in old_notes:
            unlink(path_str)
            con.execute("UPDATE notes SET image_path = NULL WHERE note_id = ?", [note_id])

        old_lists = con.execute(
            "SELECT list_id, image_path FROM purchase_orders WHERE image_path IS NOT NULL "
            f"AND created_at < now() - INTERVAL {int(days)} DAY"
        ).fetchall()
        for list_id, path_str in old_lists:
            unlink(path_str)
            con.execute("UPDATE purchase_orders SET image_path = NULL WHERE list_id = ?", [list_id])

        kept = {row[0] for row in con.execute(
            "SELECT image_path FROM notes WHERE image_path IS NOT NULL "
            "UNION SELECT image_path FROM purchase_orders WHERE image_path IS NOT NULL"
        ).fetchall()}

    for orphan in images_dir.glob("*"):
        if orphan.is_file() and str(orphan) not in kept:
            unlink(str(orphan))

    return {"files": removed, "bytes": freed, "notes": len(old_notes), "lists": len(old_lists)}


def storage(db_path, data_dir, images_dir):
  
    def size(path):
        if path.is_file():
            return path.stat().st_size
        return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())

    usage = shutil.disk_usage(data_dir if data_dir.exists() else Path("/"))
    photos = list(images_dir.glob("*")) if images_dir.exists() else []
    counts = {table: read(db_path, f"SELECT count(*) FROM {table}")[0][0]
              for table in COUNTED_TABLES}
    return {
        "database_bytes": size(db_path) if db_path.exists() else 0,
        "photo_bytes": size(images_dir) if images_dir.exists() else 0,
        "photo_count": len(photos),
        "data_bytes": size(data_dir) if data_dir.exists() else 0,
        "disk_total_bytes": usage.total,
        "disk_used_bytes": usage.used,
        "disk_free_bytes": usage.free,
        "rows": counts,
    }


def record_reading(db_path, document, fields, corrected, lines):
   
    if fields <= 0:
        return
    write(db_path, "INSERT INTO readings (document, fields, corrected, lines) VALUES (?, ?, ?, ?)",
          [document, int(fields), max(0, int(corrected)), int(lines)])


def accuracy(db_path, days=90):
   
    rows = read(db_path, f"""
            SELECT CAST(measured_at AS DATE) AS day, count(*), sum(fields), sum(corrected)
            FROM readings
            WHERE measured_at >= now() - INTERVAL {int(days)} DAY
            GROUP BY day ORDER BY day
            """)
    documents, fields, corrected = read(
        db_path, "SELECT count(*), coalesce(sum(fields), 0), coalesce(sum(corrected), 0) "
                 "FROM readings")[0]

    series = [{"day": day.isoformat(), "documents": docs, "fields": seen, "corrected": fixed,
               "accuracy": round(100 * (seen - fixed) / seen, 1) if seen else None}
              for day, docs, seen, fixed in rows]
    return {
        "series": series,
        "documents": documents,
        "fields": fields,
        "corrected": corrected,
        "accuracy": round(100 * (fields - corrected) / fields, 1) if fields else None,
    }


def find_order_by_image(db_path, sha256):
    rows = read(db_path, "SELECT list_id FROM purchase_orders WHERE image_sha256 = ?", [sha256])
    return rows[0][0] if rows else None


def save_purchase_order(db_path, data, image_sha256, image_path, user=None):
    
    with duckdb.connect(str(db_path)) as con:
        con.begin()
        try:
            list_id = con.execute(
                f"INSERT INTO purchase_orders ({', '.join(LIST_COLUMNS)}) "
                f"VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING list_id",
                [data.get("list_date"), normalize_name(data.get("site")),
                 normalize_name(data.get("work_item")), normalize_name(data.get("drafter")),
                 image_sha256, image_path, user],
            ).fetchone()[0]

            lines = data.get("lines", [])
            articles = [vocabulary.resolve(con, line["article"]) for line in lines]
            con.executemany(
                f"INSERT INTO purchase_order_lines ({', '.join(LIST_LINE_COLUMNS)}) "
                f"VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [[list_id, n, article, line.get("designation"), line.get("reference"),
                  line.get("quantity"), line.get("stock"), line.get("in_stock")]
                 for n, (line, article) in enumerate(zip(lines, articles), 1)],
            )
            vocabulary.record(con, articles, "list")
            con.commit()
        except Exception:
            con.rollback()
            raise
    return list_id


def purchase_orders(db_path):
    
    rows = read(db_path, """
            SELECT h.list_id, h.list_date, h.site, h.work_item, h.drafter, h.image_path,
                   h.created_by,
                   list({'line_no': l.line_no, 'article': l.article, 'designation': l.designation,
                         'reference': l.reference, 'quantity': l.quantity, 'stock': l.stock,
                         'in_stock': l.in_stock} ORDER BY l.line_no) AS lines
            FROM purchase_orders h LEFT JOIN purchase_order_lines l USING (list_id)
            GROUP BY h.list_id, h.list_date, h.site, h.work_item, h.drafter, h.image_path,
                     h.created_by
            ORDER BY h.list_date DESC NULLS LAST, h.list_id DESC
            """)
    return [{"id": list_id, "list_date": day.isoformat() if day else None,
             "site": site, "work_item": work_item, "drafter": drafter,
             "photo": bool(image_path), "created_by": created_by,
             "lines": [l for l in (lines or []) if l.get("article") is not None]}
            for list_id, day, site, work_item, drafter, image_path, created_by, lines in rows]


def set_in_stock(db_path, list_id, line_no, in_stock):
   
    return write(db_path, "UPDATE purchase_order_lines SET in_stock = ? "
                          "WHERE list_id = ? AND line_no = ? RETURNING line_no",
                 [in_stock, list_id, line_no]) is not None


def delete_purchase_order(db_path, list_id):
    with duckdb.connect(str(db_path)) as con:
        con.begin()
        row = con.execute("DELETE FROM purchase_orders WHERE list_id = ? RETURNING image_path",
                          [list_id]).fetchone()
        con.execute("DELETE FROM purchase_order_lines WHERE list_id = ?", [list_id])
        con.commit()
    return row[0] if row else None


def order_photo_path(db_path, list_id):
    rows = read(db_path, "SELECT image_path FROM purchase_orders WHERE list_id = ?", [list_id])
    return rows[0][0] if rows else None


def run_readonly(db_path, sql, max_rows=500):
    
    try:
        with duckdb.connect(str(db_path), read_only=True,
                            config={"enable_external_access": False}) as con:
            statements = con.extract_statements(sql)
            if len(statements) != 1:
                raise QueryError("One query at a time.")
            if statements[0].type != duckdb.StatementType.SELECT:
                raise QueryError("Only read queries (SELECT) are allowed.")
            con.execute(sql)
            columns = [d[0] for d in con.description]
            rows = con.fetchmany(max_rows + 1)  
    except duckdb.Error as e:
        raise QueryError(str(e)) from e
    return QueryResult(columns, rows[:max_rows], len(rows) > max_rows)


def notes_with_lines(db_path):
    """Every stored note with its lines, newest first."""
    rows = read(db_path, """
        SELECT n.note_id, n.image_path, n.confirmed_by, n.confirmed_at,
               any_value(d.supplier), any_value(d.delivery_date), any_value(d.order_date),
               any_value(d.site), any_value(d.work_item),
               list({'line_no': d.line_no, 'article': d.article, 'quantity': d.quantity,
                     'backorder': d.backorder, 'designation': d.designation,
                     'work_item': d.work_item} ORDER BY d.line_no)
        FROM notes n JOIN inventory_lines d USING (note_id)
        GROUP BY n.note_id, n.image_path, n.confirmed_by, n.confirmed_at
        ORDER BY any_value(d.delivery_date) DESC, n.note_id DESC
        """)
    return [{"id": note_id, "photo": bool(image), "confirmed_by": by,
             "confirmed_at": at.isoformat(timespec="minutes") if at else None,
             "supplier": supplier,
             "delivery_date": delivered.isoformat() if delivered else None,
             "order_date": ordered.isoformat() if ordered else None,
             "site": site, "work_item": work_item, "lines": lines}
            for note_id, image, by, at, supplier, delivered, ordered, site, work_item, lines in rows]


def inventory_rows(db_path):
    """One flat row per article line, for the search screen."""
    rows = read(db_path, "SELECT " + ", ".join(EXPORT_COLUMNS) + ", line_no "
                         "FROM inventory_lines ORDER BY delivery_date DESC, note_id DESC, line_no")
    keys = list(EXPORT_COLUMNS) + ["line_no"]
    out = []
    for row in rows:
        item = dict(zip(keys, row))
        item["delivery_date"] = item["delivery_date"].isoformat() if item["delivery_date"] else None
        item["order_date"] = item["order_date"].isoformat() if item["order_date"] else None
        out.append(item)
    return out
