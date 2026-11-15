
import re

import duckdb

from .models import normalize_name


_REFERENCE = re.compile(r"^\s*([A-Z0-9][A-Z0-9./_-]*\d[A-Z0-9./_-]*)\s+-\s+", re.I)


def _norm(text):
    return " ".join((text or "").upper().split())


def reference_of(designation):
    match = _REFERENCE.match(designation or "")
    return match.group(1).upper() if match else None


def learn(con, note):

    supplier = _norm(note.supplier)
    for line in note.lines:
        if not line.designation or not line.article:
            continue
        con.execute(
            """
            INSERT INTO catalog (supplier, designation, reference, article) VALUES (?, ?, ?, ?)
            ON CONFLICT (supplier, designation) DO UPDATE SET
                article = excluded.article, reference = excluded.reference,
                seen = catalog.seen + 1, updated_at = now()
            """,
            [supplier, _norm(line.designation), reference_of(line.designation),
             normalize_name(line.article)],
        )


def backfill(con):
    rows = con.execute(
        "SELECT DISTINCT supplier, designation, article FROM inventory_lines WHERE designation IS NOT NULL"
    ).fetchall()        # distinct: a long history repeats the same few names millions of times
    if not rows:
        return
    seen = {}
    for supplier, designation, article in rows:
        key = (_norm(supplier), _norm(designation))
        seen.setdefault(key, (key[0], key[1], reference_of(designation), normalize_name(article)))
    con.execute("CREATE OR REPLACE TEMP TABLE catalog_fresh (supplier VARCHAR, designation VARCHAR, "
                "reference VARCHAR, article VARCHAR)")
    con.executemany("INSERT INTO catalog_fresh VALUES (?, ?, ?, ?)", list(seen.values()))
    con.execute(                    # one statement into the real table, not a round trip per name
        "INSERT INTO catalog (supplier, designation, reference, article) "
        "SELECT supplier, designation, reference, article FROM catalog_fresh ON CONFLICT DO NOTHING")
    con.execute("DROP TABLE catalog_fresh")


def apply(db_path, note):
    
    note = note.model_copy(deep=True)
    supplier = _norm(note.supplier)
    with duckdb.connect(str(db_path)) as con:
        for line in note.lines:
            if not line.designation:
                continue
            row = con.execute(
                "SELECT article FROM catalog WHERE supplier = ? AND designation = ?",
                [supplier, _norm(line.designation)],
            ).fetchone()
            reference = reference_of(line.designation)
            if row is None and reference:
            
                row = con.execute(
                    "SELECT article FROM catalog WHERE supplier = ? AND reference = ? "
                    "ORDER BY updated_at DESC LIMIT 1",
                    [supplier, reference],
                ).fetchone()
            if row:
                line.article, line.known = row[0], True
    return note
