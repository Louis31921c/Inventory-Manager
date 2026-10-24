from datetime import date, datetime

SCHEMA = "stock"
BATCH = 100

ARTICLES = f"""CREATE TABLE IF NOT EXISTS {SCHEMA}.articles (
    article text PRIMARY KEY
);"""


def literal(value):
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (date, datetime)):
        return "'" + value.isoformat(sep=" ") + "'" if isinstance(value, datetime) \
            else "'" + value.isoformat() + "'"
    text = str(value).replace("\\", "\\\\").replace("'", "''")
    return "E'" + text + "'" if "\\" in text else "'" + text + "'"


def _column(name, kind, key):
    line = f"    {name} {kind}"
    if name in key:
        line += " NOT NULL"
    if name == "article":
        line += f" REFERENCES {SCHEMA}.articles(article)"
    return line


def script(table, columns, rows, key, title=""):
    """One self contained file: schema, table, the shared articles, and the data."""
    names = [name for name, _ in columns]
    article_at = names.index("article") if "article" in names else None

    out = [f"-- {title}" if title else f"-- {table}",
           "-- Load it with: psql -d yourdatabase -f this-file.sql",
           "",
           "BEGIN;",
           "",
           f"CREATE SCHEMA IF NOT EXISTS {SCHEMA};",
           ARTICLES,
           "",
           f"CREATE TABLE IF NOT EXISTS {SCHEMA}.{table} ("]
    body = [_column(name, kind, key) for name, kind in columns]
    if key:
        body.append(f"    PRIMARY KEY ({', '.join(key)})")
    out.append(",\n".join(body))
    out.append(");")
    out.append("")
    out.append(f"DELETE FROM {SCHEMA}.{table};")
    out.append("")

    if article_at is not None:
        articles = sorted({row[article_at] for row in rows if row[article_at] is not None})
        for start in range(0, len(articles), BATCH):
            chunk = articles[start:start + BATCH]
            values = ", ".join(f"({literal(article)})" for article in chunk)
            out.append(f"INSERT INTO {SCHEMA}.articles (article) VALUES {values}"
                       " ON CONFLICT DO NOTHING;")
        out.append("")

    if rows:
        column_list = ", ".join(names)
        for start in range(0, len(rows), BATCH):
            chunk = rows[start:start + BATCH]
            values = ",\n       ".join(
                "(" + ", ".join(literal(value) for value in row) + ")" for row in chunk)
            out.append(f"INSERT INTO {SCHEMA}.{table} ({column_list}) VALUES\n       {values};")
        out.append("")

    out.append("COMMIT;")
    out.append("")
    return "\n".join(out)
