import duckdb

from .models import normalize_name

PROMPT_LIMIT = 400


class VocabularyError(Exception):
    pass


def record(con, articles, source):
  
    for raw in articles:
        article = normalize_name(raw)
        if not article:
            continue
        con.execute(
            """
            INSERT INTO article_names (article, seen, source) VALUES (?, 1, ?)
            ON CONFLICT (article) DO UPDATE SET seen = article_names.seen + 1, last_seen = now()
            """,
            [article, source],
        )


def resolve(con, name):
    """The name to actually store: an alias target if there is one, otherwise the name itself."""
    article = normalize_name(name)
    if not article:
        return article
    row = con.execute("SELECT article FROM article_aliases WHERE alias = ?", [article]).fetchone()
    return row[0] if row else article


def known_articles(db_path, limit=PROMPT_LIMIT):
   
    with duckdb.connect(str(db_path), read_only=True) as con:
        rows = con.execute(
            "SELECT article FROM article_names ORDER BY seen DESC, article LIMIT ?", [limit]
        ).fetchall()
        if not rows:
            rows = con.execute(
                "SELECT article FROM catalog GROUP BY article ORDER BY sum(seen) DESC, article "
                "LIMIT ?", [limit],
            ).fetchall()
    return [r[0] for r in rows]


def backfill(con):
    for table, source in (("deliveries", "delivery"), ("hardware_list_lines", "list")):
        con.execute(
            f"""
            INSERT INTO article_names (article, seen, source)
            SELECT article, count(*), '{source}' FROM {table} GROUP BY article
            ON CONFLICT (article) DO NOTHING
            """
        )


def vocabulary(db_path):

    with duckdb.connect(str(db_path), read_only=True) as con:
        rows = con.execute(
            """
            WITH on_notes AS (SELECT article, count(*) AS n FROM deliveries GROUP BY article),
                 on_lists AS (SELECT article, count(*) AS n FROM hardware_list_lines GROUP BY article),
                 alias_of AS (SELECT article, string_agg(alias, ', ' ORDER BY alias) AS aliases,
                                     count(*) AS alias_count
                              FROM article_aliases GROUP BY article)
            SELECT n.article, n.seen, n.source,
                   coalesce(d.n, 0), coalesce(l.n, 0),
                   coalesce(a.aliases, ''), coalesce(a.alias_count, 0), n.last_seen
            FROM article_names n
            LEFT JOIN on_notes d USING (article)
            LEFT JOIN on_lists l USING (article)
            LEFT JOIN alias_of a USING (article)
            ORDER BY (coalesce(d.n, 0) + coalesce(l.n, 0)) DESC, n.article
            """
        ).fetchall()
    return [
        {
            "article": article, "seen": seen, "source": source,
            "deliveries": notes, "lists": lists,
            "aliases": [a for a in aliases.split(", ") if a], "alias_count": alias_count,
            "last_seen": last_seen.isoformat(timespec="minutes") if last_seen else None,
            "shared": bool(notes and lists),
        }
        for article, seen, source, notes, lists, aliases, alias_count, last_seen in rows
    ]


def aliases(db_path):
    with duckdb.connect(str(db_path), read_only=True) as con:
        rows = con.execute(
            "SELECT alias, article, created_at, created_by FROM article_aliases "
            "ORDER BY created_at DESC"
        ).fetchall()
    return [{"alias": a, "article": b, "created_by": by,
             "created_at": at.isoformat(timespec="minutes") if at else None}
            for a, b, at, by in rows]


def _rewrite(con, alias_name, target, user):
    notes = con.execute("UPDATE deliveries SET article = ? WHERE article = ? RETURNING 1",
                        [target, alias_name]).fetchall()
    lines = con.execute("UPDATE hardware_list_lines SET article = ? WHERE article = ? RETURNING 1",
                        [target, alias_name]).fetchall()
    con.execute("UPDATE catalog SET article = ? WHERE article = ?", [target, alias_name])
    con.execute("UPDATE article_aliases SET article = ? WHERE article = ?", [target, alias_name])

    seen = con.execute("SELECT coalesce(seen, 0) FROM article_names WHERE article = ?",
                       [alias_name]).fetchone()
    con.execute("DELETE FROM article_names WHERE article = ?", [alias_name])
    con.execute(
        """
        INSERT INTO article_names (article, seen, source) VALUES (?, ?, 'merge')
        ON CONFLICT (article) DO UPDATE SET seen = article_names.seen + excluded.seen,
                                            last_seen = now()
        """,
        [target, seen[0] if seen else 0],
    )
    con.execute(
        """
        INSERT INTO article_aliases (alias, article, created_by) VALUES (?, ?, ?)
        ON CONFLICT (alias) DO UPDATE SET article = excluded.article, created_at = now(),
                                          created_by = excluded.created_by
        """,
        [alias_name, target, user],
    )
    return {"deliveries": len(notes), "list_lines": len(lines)}


def merge(db_path, alias, article, user=None):
   
    alias_name, target = normalize_name(alias), normalize_name(article)
    if not alias_name or not target:
        raise VocabularyError("Both names are required.")
    if alias_name == target:
        raise VocabularyError("A name cannot be an alias of itself.")

    with duckdb.connect(str(db_path)) as con:
        target = resolve(con, target) 
        if target == alias_name:
            raise VocabularyError(f"{alias!r} is already the canonical name of {article!r}.")
        con.begin()
        try:
            moved = _rewrite(con, alias_name, target, user)
            con.commit()
        except Exception:
            con.rollback()
            raise

    return {"alias": alias_name, "article": target, **moved}
