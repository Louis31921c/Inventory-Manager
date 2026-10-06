"""Command line entry point. With no arguments it opens the full screen program."""

import argparse
import asyncio
import csv
import hashlib
import sys
from pathlib import Path

from dataclasses import replace

from . import account, alerts, audit, backup, catalog, db, llm, sync, vocabulary
from .config import load_config, settings_file
from .tui import read_image

DATASETS = ", ".join(db.EXPORTS)


def table(columns, rows, limit=None):
    rows = [[("-" if value is None else str(value)) for value in row] for row in rows]
    if limit:
        rows = rows[:limit]
    widths = [max(len(str(columns[i])), *(len(row[i]) for row in rows)) if rows
              else len(str(columns[i])) for i in range(len(columns))]
    print("  ".join(str(name).upper().ljust(width) for name, width in zip(columns, widths)))
    print("  ".join("-" * width for width in widths))
    for row in rows:
        print("  ".join(value.ljust(width) for value, width in zip(row, widths)))


def ready(config, chosen_user=None):
    """The account name signs the changes unless --user says otherwise."""
    stored = account.read_account()
    if stored and not chosen_user:
        config = replace(config, user=stored["user"])
    db.init(config.db_path)
    config.images_dir.mkdir(parents=True, exist_ok=True)
    return config


def cmd_read(config, args):
    data, media = read_image(args.file)
    sha = hashlib.sha256(data).hexdigest()
    existing = db.find_by_image(config.db_path, sha)
    if existing is not None:
        print(f"already stored as note #{existing}")
        return 1
    known = vocabulary.known_articles(config.db_path)
    note = asyncio.run(llm.make_backend(config).extract(data, media, known))
    note = catalog.apply(config.db_path, note)
    problems = note.problems()

    print(note.model_dump_json(indent=2))
    for warning in note.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    for problem in problems:
        print(f"check: {problem}", file=sys.stderr)

    if not args.save:
        print("\nnothing stored: add --save, or use the full screen program to correct it first",
              file=sys.stderr)
        return 0
    if problems:
        print("not stored: fix it in the full screen program", file=sys.stderr)
        return 1
    path = config.images_dir / f"{sha}{Path(args.file).suffix.lower()}"
    path.write_bytes(data)
    note_id = db.save_note(config.db_path, note, sha, str(path), config.user)
    audit.record(config.db_path, config.user, "note.save", f"note #{note_id}",
                 f"{note.supplier} - {len(note.lines)} line(s)", "cli")
    print(f"note #{note_id} stored", file=sys.stderr)
    return 0


def cmd_search(config, args):
    rows = db.inventory_rows(config.db_path)
    needle = (args.text or "").upper()
    keep = []
    for row in rows:
        if args.backorders and not row["backorder"]:
            continue
        hay = " ".join(str(row.get(k) or "") for k in
                       ("article", "designation", "supplier", "site", "work_item")).upper()
        if needle and needle not in hay:
            continue
        keep.append([row["article"], row["quantity"], row["supplier"], row["delivery_date"],
                     row["site"], row["work_item"], "BO" if row["backorder"] else ""])
    table(["article", "qty", "supplier", "delivered", "site", "work item", ""], keep, args.limit)
    print(f"\n{len(keep)} line(s)", file=sys.stderr)
    return 0


def cmd_ask(config, args):
    question = " ".join(args.question)
    sql, note = asyncio.run(llm.make_backend(config).to_sql(question))
    if not sql:
        print(note or "that cannot be answered from this data", file=sys.stderr)
        return 1
    print(sql, file=sys.stderr)
    result = db.run_readonly(config.db_path, sql)
    table(result.columns, result.rows)
    audit.record(config.db_path, config.user, "question", None, question[:200], "cli")
    return 0


def cmd_sql(config, args):
    result = db.run_readonly(config.db_path, " ".join(args.query))
    table(result.columns, result.rows)
    audit.record(config.db_path, config.user, "sql", None, " ".join(args.query)[:200], "cli")
    return 0


def cmd_export(config, args):
    if args.dataset not in db.EXPORTS:
        print(f"unknown dataset: {args.dataset} ({DATASETS})", file=sys.stderr)
        return 1
    out = Path(args.out) if args.out else config.data_dir / "exports" / f"{args.dataset}.{args.format}"
    out.parent.mkdir(parents=True, exist_ok=True)
    if args.format == "xlsx":
        write_xlsx(config, args.dataset, out)
    else:
        db.export_to_file(config.db_path, args.dataset, out)
    audit.record(config.db_path, config.user, "export", out.name, None, "cli")
    print(out)
    return 0


def write_xlsx(config, dataset, out):
    from openpyxl import Workbook

    columns, rows = db.export_query(config.db_path, dataset)
    book = Workbook()
    sheet = book.active
    sheet.title = dataset[:31]
    sheet.append(columns)
    for row in rows:
        sheet.append([("yes" if value else "no") if isinstance(value, bool) else value
                      for value in row])
    sheet.freeze_panes = "A2"
    book.save(out)


def cmd_backup(config, args):
    print(backup.snapshot(config, args.keep))
    audit.record(config.db_path, config.user, "backup.run", None, None, "cli")
    return 0


def cmd_restore(config, args):
    import os
    import tempfile

    archive = Path(args.archive)
    target = Path(args.into) if args.into else config.data_dir / f"restored-{archive.stem}.duckdb"
    with tempfile.TemporaryDirectory() as tmp:
        folder = backup.open_backup(archive, os.environ.get("BACKUP_PASSPHRASE", ""), Path(tmp))
        counts = backup.restore(folder, target)
    table(["table", "rows"], sorted(counts.items()))
    print(f"\nrestored beside the live data: {target}", file=sys.stderr)
    print("swap it in yourself once you have looked at it", file=sys.stderr)
    return 0


def cmd_vocab(config, args):
    if args.merge:
        alias, article = args.merge
        result = vocabulary.merge(config.db_path, alias, article, config.user)
        audit.record(config.db_path, config.user, "vocabulary.merge", result["article"],
                     f"{result['alias']} -> {result['article']}", "cli")
        print(f"{result['alias']} is now {result['article']}: "
              f"{result['inventory']} delivery line(s), {result['order_lines']} sheet line(s)")
        return 0
    rows = [[entry["article"], entry["inventory"], entry["orders"], entry["seen"],
             ", ".join(entry["aliases"])] for entry in vocabulary.vocabulary(config.db_path)]
    table(["article", "lines", "orders", "seen", "aliases"], rows, args.limit)
    return 0


def cmd_audit(config, args):
    if args.users:
        rows = [[u["user"], u["notes"], u["orders"], u["corrections"], u["events"], u["last_seen"]]
                for u in audit.users(config.db_path)]
        table(["user", "notes", "orders", "corrections", "events", "last seen"], rows)
        return 0
    rows = [[e["at"], e["user"], e["action"], e["target"], e["detail"]]
            for e in audit.events(config.db_path, args.limit)]
    table(["when", "user", "action", "target", "detail"], rows)
    return 0


def cmd_errors(config, args):
    if args.test:
        result = alerts.report(config, "test", f"test message requested by {config.user}", "cli",
                               config.user)
        print(f"error #{result['error_no']}, message: {result['sms_status']}")
        return 0
    rows = [[e["error_no"], e["at"], e["kind"], e["description"], e["sms_status"]]
            for e in alerts.recent(config.db_path, args.limit)]
    table(["#", "when", "kind", "description", "message"], rows)
    return 0


def cmd_sync(config, args):
    """Bring the data over from the older web version, from a server or from a file."""
    if args.server:
        before, added = sync.from_server(config, args.server, args.key, args.photos)
    elif args.file:
        before, added = sync.from_file(config, args.file, args.photos_from)
    else:
        print("give --server user@host or --file legacy.duckdb", file=sys.stderr)
        return 1
    table(["table", "found there"], sorted(before.items()))
    print()
    table(["added here", "rows"], sorted(added.items()))
    audit.record(config.db_path, config.user, "sync",
                 args.server or args.file, f"{added['notes']} note(s)", "cli")
    return 0


def cmd_password(config, args):
    import getpass

    stored = account.read_account()
    if stored is None:
        name = input("name for your changes: ").strip()
        first = getpass.getpass("new password: ")
        if first != getpass.getpass("again: "):
            print("the two passwords are different", file=sys.stderr)
            return 1
        account.write_account(name, first)
        print(f"password set for {account.read_account()['user']}")
        return 0

    old = getpass.getpass("current password: ")
    first = getpass.getpass("new password: ")
    if first != getpass.getpass("again: "):
        print("the two passwords are different", file=sys.stderr)
        return 1
    account.change_password(old, first)
    print("password changed")
    return 0


def cmd_where(config, args):
    info = db.storage(config.db_path, config.data_dir, config.images_dir)
    stored = account.read_account()
    rows = [["data", config.data_dir], ["database", config.db_path], ["photos", config.images_dir],
            ["settings", settings_file()], ["account", account.account_file()],
            ["password set", "yes" if stored else "no, the app asks on first run"],
            ["user", config.user], ["model", config.backend],
            ["database size", f"{info['database_bytes'] / 1e6:.1f} MB"],
            ["photos size", f"{info['photo_bytes'] / 1e6:.1f} MB ({info['photo_count']} files)"]]
    table(["item", "value"], rows)
    return 0


def cmd_tui(config, args):
    from . import tui

    return tui.run(config)


def build_parser():
    parser = argparse.ArgumentParser(prog="stock-manager", description=__doc__)
    parser.add_argument("--data", help="data folder to use instead of the default")
    parser.add_argument("--user", help="name recorded against your changes")
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="open the full screen program (default)")
    run.set_defaults(func=cmd_tui)

    read = sub.add_parser("read", help="read one photo and print what came back")
    read.add_argument("file")
    read.add_argument("--save", action="store_true", help="store it if nothing needs fixing")
    read.set_defaults(func=cmd_read)

    search = sub.add_parser("search", help="search the stored lines")
    search.add_argument("text", nargs="?", default="")
    search.add_argument("--backorders", action="store_true")
    search.add_argument("--limit", type=int, default=40)
    search.set_defaults(func=cmd_search)

    ask = sub.add_parser("ask", help="ask a question in plain words")
    ask.add_argument("question", nargs="+")
    ask.set_defaults(func=cmd_ask)

    sql = sub.add_parser("sql", help="run one SELECT")
    sql.add_argument("query", nargs="+")
    sql.set_defaults(func=cmd_sql)

    export = sub.add_parser("export", help=f"write a dataset to a file ({DATASETS})")
    export.add_argument("dataset")
    export.add_argument("--format", choices=("csv", "parquet", "xlsx"), default="csv")
    export.add_argument("--out")
    export.set_defaults(func=cmd_export)

    snapshot = sub.add_parser("backup", help="write an encrypted snapshot")
    snapshot.add_argument("--keep", type=int, default=14, help="days of snapshots to keep")
    snapshot.set_defaults(func=cmd_backup)

    restore = sub.add_parser("restore", help="rebuild a database from a snapshot")
    restore.add_argument("archive")
    restore.add_argument("--into")
    restore.set_defaults(func=cmd_restore)

    vocab = sub.add_parser("vocab", help="the shared article names")
    vocab.add_argument("--merge", nargs=2, metavar=("ALIAS", "ARTICLE"))
    vocab.add_argument("--limit", type=int, default=40)
    vocab.set_defaults(func=cmd_vocab)

    trail = sub.add_parser("audit", help="who did what")
    trail.add_argument("--users", action="store_true")
    trail.add_argument("--limit", type=int, default=20)
    trail.set_defaults(func=cmd_audit)

    failures = sub.add_parser("errors", help="reported failures")
    failures.add_argument("--test", action="store_true", help="send one test message")
    failures.add_argument("--limit", type=int, default=20)
    failures.set_defaults(func=cmd_errors)

    bring = sub.add_parser("sync", help="import the data from the older web version")
    bring.add_argument("--server", help="user@host that runs the web version")
    bring.add_argument("--key", help="ssh key to use")
    bring.add_argument("--file", help="a copy of its inventaire.duckdb instead")
    bring.add_argument("--photos", action="store_true", help="bring the photos too (slower)")
    bring.add_argument("--photos-from", help="folder of photos to copy in with --file")
    bring.set_defaults(func=cmd_sync)

    password = sub.add_parser("password", help="set or change the password the app asks for")
    password.set_defaults(func=cmd_password)

    where = sub.add_parser("where", help="where the data and settings live")
    where.set_defaults(func=cmd_where)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    config = ready(load_config(args.data, args.user), args.user)
    func = getattr(args, "func", cmd_tui)
    try:
        return func(config, args)
    except (llm.ExtractionError, vocabulary.VocabularyError, db.QueryError, ValueError) as e:
        print(e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
