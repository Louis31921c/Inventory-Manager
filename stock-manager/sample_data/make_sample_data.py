"""Build a year of made-up paperwork to run the app against.

    python sample_data/make_sample_data.py                 # write the csv files
    python sample_data/make_sample_data.py --db demo       # and a database to open

The csv files are the year itself, in plain text, so they can be read, edited or fed to
the import screen. --db builds a DuckDB out of them through the app's own code, which is
what a simulation runs on:

    DATA_DIR=sample_data/demo "$HOME/Desktop/Stock Manager/venv/bin/stock-manager"

The same seed gives the same year every time, so two runs can be compared.
"""
import argparse
import csv
import random
import sys
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from catalogue import ARTICLES, DEPOT, DRAFTERS, QUOTE_LINES, SITES, STOPS  # noqa: E402

SEED = 2026
LAST_DAY = date(2026, 10, 30)
DAYS = 364

# What the app knows: a bill is to pay, paid, or disputed. Late is worked out from
# the day it falls due, so some of the ones left to pay are deliberately old.
BILL_STATES = ("paid", "to pay", "disputed")
VAT = 0.20


def working_days(first, last):
    day = first
    while day <= last:
        if day.weekday() < 5:
            yield day
        day += timedelta(days=1)


def pick_place(rng):
    site, items = rng.choice(SITES)
    return site, rng.choice(items + [None])


def quantity(rng, usual):
    """Round numbers, the way a note is written: a box, a bundle, a few lengths."""
    step = max(1, int(usual / 10))
    return float(max(step, int(rng.triangular(usual * 0.3, usual * 1.6, usual)) // step * step))


def build(rng, days=DAYS):
    """The whole year at once: what came in, what went out, and the paper around it."""
    first = LAST_DAY - timedelta(days=days)
    diary = list(working_days(first, LAST_DAY))
    by_supplier = {}
    for article, designation, supplier, usual, price in ARTICLES:
        by_supplier.setdefault(supplier, []).append((article, designation, usual, price))

    notes, note_lines = [], []
    orders, order_lines = [], []
    quotes, quote_lines = [], []
    bills = []
    # What the notes have left standing at each site, so an order can be answered.
    shelf = {}

    note_id = order_id = quote_id = 0
    for day in diary:
        # A delivery or two a day, a little more in spring when the sites are busy.
        deliveries = rng.choices([1, 2, 3], [4, 4, 2])[0]
        if 3 <= day.month <= 7 and rng.random() < 0.3:
            deliveries += 1
        for _ in range(deliveries):
            note_id += 1
            # The big merchants deliver more often than the one-line specialists.
            supplier = rng.choices(list(by_supplier),
                                   [len(kinds) for kinds in by_supplier.values()])[0]
            site, work_item = pick_place(rng)
            ordered = day - timedelta(days=rng.randint(2, 21))
            notes.append({"note_id": note_id, "supplier": supplier,
                          "delivery_date": day.isoformat(), "order_date": ordered.isoformat(),
                          "site": site, "work_item": work_item or ""})
            wanted_lines = min(rng.randint(2, 7), len(by_supplier[supplier]))
            for line_no, (article, designation, usual, _price) in enumerate(
                    rng.sample(by_supplier[supplier], wanted_lines), 1):
                amount = quantity(rng, usual)
                late = rng.random() < 0.08
                # Two in three of the missing lines have a day promised for the rest;
                # some of those days have already gone by, which is what turns them red.
                expected = ""
                if late and rng.random() < 0.66:
                    expected = (day + timedelta(days=rng.randint(-20, 45))).isoformat()
                note_lines.append({"note_id": note_id, "line_no": line_no, "article": article,
                                   "designation": designation, "quantity": amount,
                                   "backorder": "yes" if late else "", "expected_on": expected,
                                   "supplier": supplier, "delivery_date": day.isoformat(),
                                   "order_date": ordered.isoformat(), "site": site,
                                   "work_item": work_item or ""})
                if not late:
                    shelf.setdefault((site, work_item, article), 0.0)
                    shelf[(site, work_item, article)] += amount

        # Two orders or so a day from a customer, shipped inside the day after. Over a
        # year they have to carry off most of what the notes brought, or the shelf only
        # ever grows and every buffer ends up over the top of green.
        for _ in range(rng.choices([1, 2, 3], [3, 4, 2])[0]):
            site, work_item = pick_place(rng)
            standing = [key for key in shelf if key[0] == site and shelf[key] > 0]
            wanted = rng.randint(6, 18)
            lines = []
            # Most of what goes out was delivered for that site; some of it was never
            # scanned in, which is what the "to order" word on a line is there to say.
            for key in rng.sample(standing, min(len(standing), wanted)):
                if rng.random() < 0.05:
                    continue
                article = key[2]
                amount = float(max(1, int(shelf[key] * rng.uniform(0.8, 1.0))))
                shelf[key] = max(0.0, shelf[key] - amount)
                lines.append((article, _designation(article), amount))
            # Now and then a line for something that was never scanned in at that site.
            while len(lines) < wanted and rng.random() < 0.2:
                article, designation, _supplier, usual, _price = rng.choice(ARTICLES)
                lines.append((article, designation, quantity(rng, usual) / 2))
            # A site with nothing standing on it and no stray line orders nothing: an
            # order with no line at all is not a thing the app can store.
            if not lines:
                continue
            order_id += 1
            orders.append({"list_id": order_id, "list_date": day.isoformat(), "site": site,
                           "work_item": work_item or "", "drafter": rng.choice(DRAFTERS)})
            for line_no, (article, designation, amount) in enumerate(lines, 1):
                order_lines.append({"list_id": order_id, "line_no": line_no, "article": article,
                                    "designation": designation, "quantity": amount})

        # A quote request about once a week, for what is not sold by the piece.
        if rng.random() < 0.2:
            quote_id += 1
            site, work_item = pick_place(rng)
            quotes.append({"quote_id": quote_id, "supplier": rng.choice(list(by_supplier)),
                           "site": site, "work_item": work_item or "",
                           "needed_by": (day + timedelta(days=rng.randint(7, 40))).isoformat(),
                           "state": rng.choices(["open", "sent", "answered", "closed"],
                                                [3, 4, 2, 2])[0],
                           "note": "" if rng.random() < 0.7 else "Ask for the lead time as well."})
            for line_no, (article, amount, unit) in enumerate(
                    rng.sample(QUOTE_LINES, rng.randint(1, 3)), 1):
                quote_lines.append({"quote_id": quote_id, "line_no": line_no, "article": article,
                                    "quantity": amount, "unit": unit, "designation": ""})

    bills = _bills(rng, note_lines, by_supplier)
    counts = _counts(rng, shelf)
    return {"notes": notes, "note_lines": note_lines, "orders": orders,
            "order_lines": order_lines, "quotes": quotes, "quote_lines": quote_lines,
            "bills": bills, "counts": counts}


_DESIGNATIONS = {article: designation for article, designation, *_ in ARTICLES}


def _designation(article):
    return _DESIGNATIONS.get(article, "")


def _bills(rng, note_lines, by_supplier):
    """One bill a month per supplier, priced off what that month's notes carried."""
    price = {article: money for article, _d, _s, _u, money in ARTICLES}
    supplier_of = {article: supplier for article, _d, supplier, _u, _m in ARTICLES}
    months = {}
    for line in note_lines:
        month = line["delivery_date"][:7]
        supplier = supplier_of[line["article"]]
        months.setdefault((supplier, month), []).append(
            price[line["article"]] * float(line["quantity"]))

    bills, number = [], 0
    for (supplier, month), amounts in sorted(months.items(), key=lambda pair: pair[0][1]):
        number += 1
        year, mon = (int(part) for part in month.split("-"))
        billed = date(year, mon, 28)
        net = round(sum(amounts) * rng.uniform(0.9, 1.1), 2)
        tax = round(net * VAT, 2)
        state = rng.choices(BILL_STATES, [7, 4, 1])[0]
        due = billed + timedelta(days=30)
        bills.append({
            "supplier": supplier, "number": f"{year}-{mon:02d}-{number:04d}",
            "bill_date": billed.isoformat(), "due_date": due.isoformat(), "currency": "EUR",
            "net": net, "tax": tax, "total": round(net + tax, 2), "site": "",
            "order_ref": "", "state": state,
            "paid_on": (due - timedelta(days=rng.randint(0, 8))).isoformat()
                       if state == "paid" else "",
            "note": "",
        })
    return bills


def _counts(rng, shelf):
    """The shelf counted by hand now and then, a little beside what the paper says."""
    held = {}
    for (_site, _work, article), amount in shelf.items():
        held[article] = held.get(article, 0.0) + amount
    counted = []
    for article in rng.sample(sorted(held), min(25, len(held))):
        out = held[article] * rng.uniform(0.85, 1.15)
        counted.append({"article": article, "quantity": round(out, 1),
                        "counted_on": (LAST_DAY - timedelta(days=rng.randint(0, 30))).isoformat(),
                        "counted_by": rng.choice(DRAFTERS)})
    return counted


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
    """Pour the same year into a database, through the code the app itself uses."""
    sys.path.insert(0, str(HERE.parent))
    from inventory import backorders, bills as bills_module
    from inventory import contacts, db, quotes as quotes_module
    from inventory.models import Line, Note

    where.mkdir(parents=True, exist_ok=True)
    database = where / "inventory.duckdb"
    if database.exists():
        database.unlink()
    for leftover in where.glob("inventory.duckdb.wal"):
        leftover.unlink()
    db.init(database)

    lines_of = {}
    for line in year["note_lines"]:
        lines_of.setdefault(line["note_id"], []).append(line)
    for head in year["notes"]:
        note = Note(supplier=head["supplier"], delivery_date=head["delivery_date"],
                    order_date=head["order_date"], site=head["site"],
                    work_item=head["work_item"] or None,
                    lines=[Line(article=line["article"], quantity=line["quantity"],
                                designation=line["designation"],
                                backorder=bool(line["backorder"]))
                           for line in lines_of.get(head["note_id"], [])])
        saved = db.save_note(database, note, f"sample-note-{head['note_id']}", "", "SAMPLE")
        for line_no, line in enumerate(lines_of.get(head["note_id"], []), 1):
            if line["expected_on"]:
                backorders.expect(database, saved, line_no, line["expected_on"])

    order_lines = {}
    for line in year["order_lines"]:
        order_lines.setdefault(line["list_id"], []).append(line)
    for head in year["orders"]:
        db.save_purchase_order(database, {
            "list_date": date.fromisoformat(head["list_date"]), "site": head["site"],
            "work_item": head["work_item"] or None, "drafter": head["drafter"],
            "lines": [{"article": line["article"], "designation": line["designation"],
                       "quantity": line["quantity"]}
                      for line in order_lines.get(head["list_id"], [])],
        }, f"sample-order-{head['list_id']}", "", "SAMPLE")

    quote_lines = {}
    for line in year["quote_lines"]:
        quote_lines.setdefault(line["quote_id"], []).append(line)
    for head in year["quotes"]:
        quote_id = quotes_module.save(database, head, [
            {"article": line["article"], "quantity": line["quantity"], "unit": line["unit"]}
            for line in quote_lines.get(head["quote_id"], [])], "SAMPLE")
        quotes_module.update(database, quote_id, {"state": head["state"]})

    for bill in year["bills"]:
        bills_module.save(database, bills_module.shape(bill, source="sample", user="SAMPLE"))

    for count in year["counts"]:
        db.save_stock_count(database, count["article"], count["quantity"], count["counted_by"])
        db.write(database, "UPDATE stock_counts SET counted_on = ? WHERE article = ?",
                 (date.fromisoformat(count["counted_on"]), count["article"]))

    book = folder.parent.parent / "supplier-contacts.csv"
    if book.is_file():
        contacts.load_file(database, book, "SAMPLE")

    db.write(database, "DELETE FROM places")
    db.write(database, """
            INSERT INTO places (kind, label, address, lat, lon, added_by)
            VALUES (?, ?, ?, ?, ?, 'SAMPLE')
            """, ("depot", DEPOT[0], DEPOT[1], DEPOT[2], DEPOT[3]))
    for label, address, lat, lon in STOPS:
        db.write(database, """
                INSERT INTO places (kind, label, address, lat, lon, added_by)
                VALUES (?, ?, ?, ?, ?, 'SAMPLE')
                """, ("stop", label, address, lat, lon))
    return database


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", nargs="?", const="demo", default=None,
                        help="also build a database here (default: sample_data/demo)")
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--days", type=int, default=DAYS,
                        help="how far back to start (default: a year)")
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
        print(f'DATA_DIR="{where}" "$HOME/Desktop/Stock Manager/venv/bin/stock-manager"')


if __name__ == "__main__":
    main()
