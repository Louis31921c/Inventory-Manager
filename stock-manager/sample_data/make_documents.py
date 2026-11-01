"""The paperwork behind the year: the documents a simulation can be asked to show.

A database full of rows is enough to drive the screens, but not enough to rehearse the job
with. Somebody asks where the delivery note is, or what the bill said, or what exactly was
approved the day that order went out. These are those: a PDF a delivery note, a PDF a bill,
a PDF a purchase order, and for every basket the gate let through, a proof carrying the
sealed entries and their hashes so the approval can be read on paper.

    python sample_data/make_sample_data.py --db demo

writes them into the database folder, under `documents/`. They are made up, like the rest
of the year, and the letterhead belongs to no one.
"""

import sys
from datetime import date
from pathlib import Path

import duckdb

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

FIRM = "ATELIER METAL & VITRAGE"
ADDRESS = "12 rue des Ateliers, 33520 BRUGES"
TRADE = "SIRET 000 000 000 00000 — made up, for a simulation"

# How many of each to write. Every one of a year would be a thousand files and no more
# useful than a dozen; the proofs are all written, because there are few and each is the
# only record of a decision.
NOTES = 12
BILLS = 8
ORDERS = 10

INK = (0.12, 0.14, 0.16)
SOFT = (0.42, 0.45, 0.48)
RULE = (0.78, 0.80, 0.82)
RED = (0.69, 0.13, 0.16)
GREEN = (0.04, 0.48, 0.18)


def _money(value):
    return f"{float(value or 0):,.2f}".replace(",", " ")


def _number(value):
    if value is None:
        return "?"
    value = float(value)
    return str(int(value)) if value.is_integer() else f"{value:g}"


class Sheet:
    """One page of letterhead, and the few things that get drawn on it."""

    def __init__(self, canvas, title, reference, when):
        self.canvas = canvas
        self.width, self.height = canvas._pagesize
        self.y = self.height - 50
        self._letterhead(title, reference, when)

    def _letterhead(self, title, reference, when):
        page = self.canvas
        page.setFillColorRGB(*INK)
        page.setFont("Helvetica-Bold", 15)
        page.drawString(50, self.y, FIRM)
        page.setFont("Helvetica", 8.5)
        page.setFillColorRGB(*SOFT)
        page.drawString(50, self.y - 13, ADDRESS)
        page.drawString(50, self.y - 24, TRADE)

        page.setFillColorRGB(*INK)
        page.setFont("Helvetica-Bold", 13)
        page.drawRightString(self.width - 50, self.y, title.upper())
        page.setFont("Helvetica", 9)
        page.setFillColorRGB(*SOFT)
        page.drawRightString(self.width - 50, self.y - 14, reference)
        page.drawRightString(self.width - 50, self.y - 26, str(when))

        self.y -= 42
        self.rule()

    def rule(self, gap=10):
        self.canvas.setStrokeColorRGB(*RULE)
        self.canvas.setLineWidth(0.6)
        self.canvas.line(50, self.y, self.width - 50, self.y)
        self.y -= gap

    def facts(self, pairs):
        """The head of the document: who, where, when, in two columns."""
        page = self.canvas
        page.setFont("Helvetica", 9)
        for index, (label, value) in enumerate(pairs):
            x = 50 + (index % 2) * ((self.width - 100) / 2)
            if index % 2 == 0 and index:
                self.y -= 14
            page.setFillColorRGB(*SOFT)
            page.drawString(x, self.y, f"{label}")
            page.setFillColorRGB(*INK)
            page.drawString(x + 92, self.y, str(value))
        self.y -= 18
        self.rule()

    def table(self, columns, rows):
        """columns is (heading, width, align); a row is a tuple the same length."""
        page = self.canvas
        page.setFont("Helvetica-Bold", 8)
        page.setFillColorRGB(*SOFT)
        x = 50
        for heading, wide, align in columns:
            if align == "right":
                page.drawRightString(x + wide, self.y, heading.upper())
            else:
                page.drawString(x, self.y, heading.upper())
            x += wide
        self.y -= 6
        self.rule(gap=12)

        page.setFont("Helvetica", 9)
        for row in rows:
            if self.y < 90:
                self.page_break()
            x = 50
            for (_, wide, align), cell in zip(columns, row):
                page.setFillColorRGB(*INK)
                if align == "right":
                    page.drawRightString(x + wide, self.y, str(cell))
                else:
                    page.drawString(x, self.y, str(cell)[:int(wide / 4.6)])
                x += wide
            self.y -= 13
        self.y -= 4
        self.rule()

    def lines(self, texts, size=9, colour=INK, gap=12):
        self.canvas.setFont("Helvetica", size)
        self.canvas.setFillColorRGB(*colour)
        for text in texts:
            if self.y < 70:
                self.page_break()
            self.canvas.drawString(50, self.y, text)
            self.y -= gap
        self.y -= 4

    def heading(self, text):
        self.y -= 4
        self.canvas.setFont("Helvetica-Bold", 9.5)
        self.canvas.setFillColorRGB(*INK)
        self.canvas.drawString(50, self.y, text.upper())
        self.y -= 14

    def total(self, label, amount):
        page = self.canvas
        page.setFont("Helvetica-Bold", 11)
        page.setFillColorRGB(*INK)
        page.drawRightString(self.width - 120, self.y, label)
        page.drawRightString(self.width - 50, self.y, amount)
        self.y -= 16

    def sign(self, words):
        self.y = max(self.y, 100)
        self.rule()
        self.canvas.setFont("Helvetica", 8)
        self.canvas.setFillColorRGB(*SOFT)
        self.canvas.drawString(50, 60, words)
        self.canvas.drawRightString(self.width - 50, 60, "Signature")
        self.canvas.setStrokeColorRGB(*RULE)
        self.canvas.line(self.width - 190, 75, self.width - 50, 75)

    def page_break(self):
        self.canvas.showPage()
        self.y = self.height - 60


def _sheet(folder, name, title, reference, when):
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas as canvas_module

    folder.mkdir(parents=True, exist_ok=True)
    page = canvas_module.Canvas(str(folder / name), pagesize=A4)
    page.setTitle(f"{title} {reference}")
    page.setAuthor(FIRM)
    return page, Sheet(page, title, reference, when)


# ---- the three kinds of paperwork the year already has --------------------

def delivery_notes(con, where, how_many=NOTES):
    rows = con.execute("""
            SELECT note_id, any_value(supplier), any_value(delivery_date),
                   any_value(order_date), any_value(site), any_value(work_item)
            FROM inventory_lines GROUP BY note_id ORDER BY any_value(delivery_date) DESC
            LIMIT ?
            """, [how_many]).fetchall()
    written = []
    for note_id, supplier, delivered, ordered, site, work_item in rows:
        lines = con.execute("""
                SELECT article, designation, quantity, backorder, expected_on
                FROM inventory_lines WHERE note_id = ? ORDER BY line_no
                """, [note_id]).fetchall()
        page, sheet = _sheet(where / "delivery-notes", f"note-{note_id:04d}.pdf",
                             "Delivery note", f"BL-{note_id:04d}", delivered)
        sheet.facts([("Supplier", supplier), ("Delivered", delivered),
                     ("Site", site or "—"), ("Ordered", ordered or "—"),
                     ("Work item", work_item or "—"), ("Note", f"#{note_id}")])
        sheet.table(
            [("Article", 150, "left"), ("Designation", 220, "left"),
             ("Qty", 50, "right"), ("Backorder", 75, "right")],
            [(article, designation or "—", _number(quantity),
              (f"yes, {expected}" if expected else "yes") if backorder else "")
             for article, designation, quantity, backorder, expected in lines])
        short = sum(1 for row in lines if row[3])
        if short:
            sheet.lines([f"{short} line(s) on backorder. The rest is to follow."],
                        colour=RED)
        sheet.sign("Received in good order, subject to the backorders noted above.")
        page.save()
        written.append(note_id)
    return written


def bills(con, where, how_many=BILLS):
    rows = con.execute("""
            SELECT bill_id, supplier, number, bill_date, due_date, net, tax, total, state,
                   site, order_ref, month
            FROM bills ORDER BY bill_date DESC LIMIT ?
            """, [how_many]).fetchall()
    for (bill_id, supplier, number, billed, due, net, tax, total, state, site, order_ref,
         month) in rows:
        page, sheet = _sheet(where / "bills", f"bill-{bill_id:04d}.pdf",
                             "Supplier bill", number or f"F-{bill_id:04d}", billed)
        sheet.facts([("Supplier", supplier), ("Billed", billed),
                     ("Site", site or "—"), ("Due", due or "—"),
                     ("Our ref", order_ref or "—"), ("Month", month or "—")])
        sheet.table(
            [("What", 300, "left"), ("Amount", 120, "right")],
            [(f"Goods supplied in {month}", _money(net)),
             ("VAT", _money(tax))])
        sheet.total("TOTAL EUR", _money(total))
        say = {"paid": ("Paid.", GREEN), "disputed": ("Disputed — do not pay yet.", RED)}
        words, colour = say.get(state, ("To pay.", INK))
        sheet.lines([words], colour=colour)
        sheet.sign("Priced off the delivery notes of the month. Query within 30 days.")
        page.save()


def purchase_orders(con, where, how_many=ORDERS):
    rows = con.execute("""
            SELECT list_id, list_date, site, work_item, drafter, incoterm, ship_by
            FROM purchase_orders ORDER BY list_date DESC LIMIT ?
            """, [how_many]).fetchall()
    for list_id, when, site, work_item, drafter, incoterm, ship_by in rows:
        lines = con.execute("""
                SELECT article, designation, reference, quantity
                FROM purchase_order_lines WHERE list_id = ? ORDER BY line_no
                """, [list_id]).fetchall()
        page, sheet = _sheet(where / "purchase-orders", f"order-{list_id:04d}.pdf",
                             "Purchase order", f"CDE-{list_id:04d}", when)
        sheet.facts([("Site", site or "—"), ("Drawn", when),
                     ("Work item", work_item or "—"), ("Drafted by", drafter or "—"),
                     ("Incoterm", incoterm or "—"), ("Ship by", ship_by or "—")])
        sheet.table(
            [("Article", 170, "left"), ("Designation", 190, "left"),
             ("Reference", 85, "left"), ("Qty", 50, "right")],
            [(article, designation or "—", reference or "—", _number(quantity))
             for article, designation, reference, quantity in lines])
        sheet.sign("Nothing on this sheet is paid for until it is received and checked.")
        page.save()


# ---- what the gate decided, on paper --------------------------------------

def proofs(con, where):
    """One a basket the gate let through, or turned down. The seals are on the page.

    The point of a sealed log is that it can be checked by somebody who was not there.
    That is hard to do from inside the app that keeps it, so the proof carries the hashes:
    each entry's own, and the one it was sealed against. Anybody with the file and the
    rule can walk the chain again.
    """
    import json

    rows = con.execute("""
            SELECT request_id, shop, site, work_item, asked_by, reasons, basket, total,
                   state, agent_verdict, agent_said, decided_by, decided_at, asked_at
            FROM gate_requests WHERE state IN ('approved', 'ordered', 'cancelled')
            ORDER BY request_id
            """).fetchall()
    written = 0
    for (request_id, shop, site, work_item, asked_by, reasons, basket, total, state,
         verdict, said, decided_by, decided_at, asked_at) in rows:
        entries = con.execute("""
                SELECT entry_id, event, who, made_at, prev_seal, seal
                FROM gate_log WHERE request_id = ? ORDER BY entry_id
                """, [request_id]).fetchall()

        page, sheet = _sheet(where / "proofs", f"proof-{request_id:04d}.pdf",
                             "Purchase gate proof", f"GATE-{request_id:04d}",
                             (decided_at or asked_at).date() if decided_at or asked_at
                             else date.today())
        sheet.facts([("Shop", shop or "—"), ("Asked", (asked_at or "—")),
                     ("Site", site or "—"), ("Asked by", asked_by or "—"),
                     ("Work item", work_item or "—"), ("Outcome", state.upper())])

        sheet.heading("What was asked for")
        sheet.table(
            [("Article", 190, "left"), ("Supplier", 130, "left"),
             ("Qty", 50, "right"), ("Unit", 60, "right"), ("Line", 65, "right")],
            [(line.get("article", "?"), line.get("supplier") or "—",
              _number(line.get("quantity")),
              _money(line["unit_price"]) if line.get("unit_price") is not None else "no price",
              _money((line.get("unit_price") or 0) * (line.get("quantity") or 0))
              if line.get("unit_price") is not None else "—")
             for line in json.loads(basket)])
        sheet.total("BASKET EUR", _money(total))

        sheet.heading("Why it was held")
        sheet.lines([f"· {word}" for word in (reasons or "").split(",") if word])

        sheet.heading("First check — Terminus")
        sheet.lines([f"Verdict: {verdict or 'not read'}", said or ""],
                    colour=GREEN if verdict == "same" else INK)

        sheet.heading("Second check — a person")
        sheet.lines([f"{state.upper()} by {decided_by or '—'}"
                     + (f" on {decided_at}" if decided_at else "")],
                    colour=RED if state == "cancelled" else GREEN)

        sheet.heading("The sealed entries")
        sheet.table(
            [("#", 30, "left"), ("Event", 90, "left"), ("Who", 70, "left"),
             ("When", 110, "left"), ("Sealed against", 85, "left"), ("Seal", 95, "left")],
            [(entry, event, who or "—", str(made_at)[:19],
              f"{prev[:10]}…", f"{seal[:10]}…")
             for entry, event, who, made_at, prev, seal in entries])
        sheet.lines([
            "Each seal is the SHA-256 of the entry before it, the request, the event, who,",
            "when, and the body — in that order, joined by a bar. Change any one of them and",
            "every seal from there on stops matching.",
        ], size=8, colour=SOFT)
        sheet.sign("This sheet is a copy of what the log held. The log itself is the record.")
        page.save()
        written += 1
    return written


# ---- the year as a workbook -----------------------------------------------

def workbook(con, where):
    """One sheet a table, for whoever would rather have it in a spreadsheet."""
    from openpyxl import Workbook

    book = Workbook()
    book.remove(book.active)
    tables = ("inventory_lines", "purchase_orders", "purchase_order_lines", "bills",
              "quotes", "quote_lines", "stock_counts", "baskets", "article_prices",
              "gate_requests", "gate_log")
    for table in tables:
        try:
            rows = con.execute(f"SELECT * FROM {table}").fetchall()
            names = [d[0] for d in con.execute(f"SELECT * FROM {table} LIMIT 0").description]
        except duckdb.Error:
            continue
        sheet = book.create_sheet(table[:31])
        sheet.append(names)
        for row in rows:
            sheet.append([value if not isinstance(value, (dict, list)) else str(value)
                          for value in row])
    where.mkdir(parents=True, exist_ok=True)
    book.save(where / "the-year.xlsx")


def build(database, folder=None):
    """Write the lot. Gives back what was written, for the generator to print."""
    where = Path(folder or Path(database).parent / "documents")
    with duckdb.connect(str(database), read_only=True) as con:
        notes = delivery_notes(con, where)
        bills(con, where)
        purchase_orders(con, where)
        sealed = proofs(con, where)
        workbook(con, where)
    return {"folder": where, "notes": len(notes), "bills": BILLS, "orders": ORDERS,
            "proofs": sealed}
