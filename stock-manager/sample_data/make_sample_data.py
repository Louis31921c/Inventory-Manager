"""Build a year of made-up paperwork to run the app against.

    python sample_data/make_sample_data.py                 # write the csv files
    python sample_data/make_sample_data.py --db demo       # and a database to open

The csv files are the year itself, in plain text, so they can be read, edited or fed to
the import screen. --db builds a DuckDB out of them through the app's own code, which is
what a simulation runs on:

    DATA_DIR=sample_data/demo "$HOME/Desktop/Stock Manager/venv/bin/stock-manager"

The year itself is made in inventory/simulation.py, which is also what fills a database
the app builds for the first time. The same seed gives the same year every time, so two
runs can be compared.
"""
import argparse
import csv
import random
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from inventory import contacts, db, simulation  # noqa: E402
from inventory.simulation import (BILL_STATES, DAYS, LAST_DAY, SEED, build,  # noqa: E402,F401
                                  fill_gate)
from inventory.simulation_catalogue import DEPOT, STOPS  # noqa: E402


def write_csv(folder, name, rows, columns):
    path = folder / f"{name}.csv"
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    return path, len(rows)


SHEETS = {
    "delivery_notes": ("notes", ["note_id", "supplier", "delivery_date", "order_date",
                                 "site", "work_item"]),
    "delivery_note_lines": ("note_lines", ["note_id", "line_no", "article", "designation",
                                           "quantity", "backorder", "expected_on", "supplier",
                                           "delivery_date", "order_date", "site", "work_item"]),
    "purchase_orders": ("orders", ["list_id", "list_date", "site", "work_item", "drafter"]),
    "purchase_order_lines": ("order_lines", ["list_id", "line_no", "article", "designation",
                                             "quantity"]),
    "quotes": ("quotes", ["quote_id", "supplier", "site", "work_item", "needed_by", "state",
                          "note"]),
    "quote_lines": ("quote_lines", ["quote_id", "line_no", "article", "quantity", "unit",
                                    "designation"]),
    "bills": ("bills", ["supplier", "number", "bill_date", "due_date", "currency", "net", "tax",
                        "total", "site", "order_ref", "state", "paid_on", "note"]),
    "stock_counts": ("counts", ["article", "quantity", "counted_on", "counted_by"]),
}


def write_places(folder):
    rows = [{"kind": "depot", "label": DEPOT[0], "address": DEPOT[1], "lat": DEPOT[2],
             "lon": DEPOT[3]}]
    rows += [{"kind": "stop", "label": label, "address": address, "lat": lat, "lon": lon}
             for label, address, lat, lon in STOPS]
    return write_csv(folder, "places", rows, ["kind", "label", "address", "lat", "lon"])


def fill_database(folder, year, where):
    """Pour the same year into a database in `where`, starting it over if it is there."""
    where.mkdir(parents=True, exist_ok=True)
    database = where / "inventory.duckdb"
    if database.exists():
        database.unlink()
    for leftover in where.glob("inventory.duckdb.wal"):
        leftover.unlink()
    db.init(database)
    # The firm's own contact sheet, when it is there, rather than the made-up one.
    simulation.pour(database, year, LAST_DAY, book=False)
    book = folder.parent.parent / "supplier-contacts.csv"
    if book.is_file():
        contacts.load_file(database, book, "SAMPLE")
    else:
        simulation._contact_book(database)
    return database


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", nargs="?", const="demo", default=None,
                        help="also build a database here (default: sample_data/demo)")
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--days", type=int, default=DAYS,
                        help="how far back to start (default: a year)")
    parser.add_argument("--no-documents", action="store_true",
                        help="skip the PDFs and the workbook, which take the longest")
    chosen = parser.parse_args()

    rng = random.Random(chosen.seed)
    year = build(rng, chosen.days)

    for name, (key, columns) in SHEETS.items():
        path, count = write_csv(HERE, name, year[key], columns)
        print(f"{path.name:<24} {count:>6} row(s)")
    path, count = write_places(HERE)
    print(f"{path.name:<24} {count:>6} row(s)")

    if chosen.db:
        where = Path(chosen.db)
        if not where.is_absolute():
            where = HERE / where
        database = fill_database(HERE, year, where)
        print(f"\ndatabase {database}")
        if not chosen.no_documents:
            try:
                import make_documents

                made = make_documents.build(database)
                print(f"documents {made['folder']}"
                      f"  {made['notes']} note(s), {made['bills']} bill(s), "
                      f"{made['orders']} order(s), {made['proofs']} gate proof(s), "
                      f"the-year.xlsx")
            except ImportError:
                print("documents skipped: pip install 'stock-manager[documents]'")
        print(f'DATA_DIR="{where}" "$HOME/Desktop/Stock Manager/venv/bin/stock-manager"')


if __name__ == "__main__":
    main()
