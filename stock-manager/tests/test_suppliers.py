"""Tests: suppliers."""

from datetime import date, datetime, timedelta

import pytest

from inventory import db, quotes
from inventory.models import Line
from helpers import _outbox_world, _ready_mail, _sent_acme_order, make_config, make_note, tmp_path_for


def test_a_quote_asks_in_the_right_unit(db_path):
    from inventory import quotes

    asked = quotes.parse_request("120 ml skirting board; 3 m3 gravel; 12 anchors")
    assert [(line["quantity"], line["unit"], line["article"]) for line in asked] == [
        (120.0, "ml", "SKIRTING BOARD"), (3.0, "m3", "GRAVEL"), (12.0, "piece", "ANCHORS")]

    quote_id = quotes.save(db_path, {"supplier": "foussier", "site": "villa"}, asked, "ALEX")
    stored = quotes.one(db_path, quote_id)
    assert stored["supplier"] == "FOUSSIER" and stored["state"] == "open"
    assert [row["unit_label"] for row in stored["rows"]] == ["linear metre", "cubic metre", "piece"]

    text = quotes.as_text(stored)
    assert "120 ml SKIRTING BOARD" in text and "price" in text
    assert "€" not in text and "EUR" not in text

    assert quotes.update(db_path, quote_id, {"state": "sent"})
    assert quotes.one(db_path, quote_id)["state"] == "sent"
    assert not quotes.update(db_path, quote_id, {"state": "nonsense"})
    assert quotes.drop(db_path, quote_id) and quotes.one(db_path, quote_id) is None


def test_the_contact_book_fills_itself_while_the_page_is_reading_everything_else(
        tmp_path, monkeypatch):
    import importlib
    import threading

    from fastapi.testclient import TestClient

    paper = tmp_path / "supplier-contacts.csv"
    paper.write_text("company,person,phone\n" + "".join(
        f"FIRM{n},Person {n},05 99 00 00 {n:02d}\n" for n in range(30)), encoding="utf-8")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("STOCK_CONTACTS_FILE", str(paper))

    from inventory import config as config_module
    from inventory import server as server_module

    importlib.reload(config_module)
    server = importlib.reload(server_module)
    mine = {"origin": "http://testserver"}
    with TestClient(server.app) as client:
        client.post("/unlock", data={"user": "alex", "password": "correct horse",
                                     "again": "correct horse"}, headers=mine)
        answers = []

        def read(path):
            answers.append((path, client.get(path).status_code))

        # The first screen's fan-out, with the contact page among it.
        paths = ["/api/audit", "/api/vocabulary", "/api/errors", "/api/accuracy",
                 "/api/storage", "/api/contacts", "/api/data", "/api/audit"] * 2
        threads = [threading.Thread(target=read, args=(path,)) for path in paths]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert [path for path, code in answers if code != 200] == []
        assert len(client.get("/api/contacts").json()["contacts"]) == 30


def test_topping_up_adds_quotes_part_deliveries_bills_and_the_round(db_path):
    from inventory import simulation

    done = simulation.top_up(db_path)
    assert done["quotes"] == 5
    assert db.read(db_path, "SELECT count(*) FROM quotes")[0][0] == 5
    assert db.read(db_path, "SELECT count(*) FROM notes")[0][0] == done["partly delivered"]
    assert db.read(db_path, "SELECT count(*) FROM bills")[0][0] == done["bills"] > 0
    assert db.read(db_path, "SELECT count(*) FROM gate_requests")[0][0] == done["blockchain"]
    # A second time does nothing, so the same notes are not made twice.
    assert simulation.top_up(db_path) == {}
    assert db.read(db_path, "SELECT count(*) FROM notes")[0][0] == done["partly delivered"]


def test_a_bill_is_checked_against_the_deliveries_it_pays_for(db_path):
    from inventory import bills as bills_module
    from inventory import matching, prices

    today = date(2026, 11, 3)
    prices.remember(db_path, "BOLT", 2.0, supplier="ACME", seen_on=date(2026, 9, 1))
    for number, day in enumerate((date(2026, 10, 2), date(2026, 10, 9)), 1):
        db.save_note(db_path, make_note(supplier="ACME", delivery_date=day.isoformat(),
                                        order_date="2026-09-20",
                                        lines=[Line(article="BOLT", quantity=10)]),
                     f"s{number}", "", "T")

    def bill(net, when="2026-10-31"):
        return bills_module.save(db_path, bills_module.shape(
            {"supplier": "ACME", "number": f"B{net}", "bill_date": when, "net": net,
             "total": net}, source="t"))

    bill(40.0)                      # 2 deliveries of 10 at 2.00
    found = matching.bills(db_path, today)
    assert found["bills"][0]["verdict"] == "matched"
    assert found["bills"][0]["expected"] == 40.0 and found["bills"][0]["deliveries"] == 2

    bill(55.0, "2026-11-02")        # nothing delivered since the last bill
    found = matching.bills(db_path, today)
    verdicts = {row["number"]: row["verdict"] for row in found["bills"]}
    assert verdicts == {"B40.0": "matched", "B55.0": "nothing delivered"}

    db.save_note(db_path, make_note(supplier="ACME", delivery_date="2026-11-03",
                                    order_date="2026-10-20",
                                    lines=[Line(article="BOLT", quantity=5)]), "s3", "", "T")
    bill(30.0, "2026-11-03")        # 5 bolts are worth 10, the bill says 30
    found = matching.bills(db_path, today)
    last = next(row for row in found["bills"] if row["number"] == "B30.0")
    assert last["verdict"] == "over" and last["gap"] == 20.0


def test_an_order_becomes_one_draft_a_supplier_and_is_sent_only_when_told(db_path):
    from inventory import outbox

    list_id = _outbox_world(db_path)
    made = outbox.draft_order(db_path, list_id, "T")
    by_supplier = {row["supplier"]: row for row in made["drafts"]}
    assert set(by_supplier) == {"ACME", "TUBES SA"} and made["unassigned"] == ["ODDITY"]
    acme = by_supplier["ACME"]
    assert acme["to_addr"] == "sales@acme.example" and acme["state"] == "draft"
    assert "10 x BOLT" in acme["body"] and "4 x NUT" in acme["body"]
    assert "SHELF" not in acme["body"] and "PIPE" not in acme["body"]
    assert by_supplier["TUBES SA"]["to_addr"] is None                # no contact on file

    # Asking again hands back the same drafts instead of piling up new ones.
    again = outbox.draft_order(db_path, list_id, "T")
    assert {row["mail_id"] for row in again["drafts"]} == {row["mail_id"] for row in made["drafts"]}

    mail = make_config(tmp_path_for(db_path)).mail
    with pytest.raises(outbox.SendError):                            # no mailbox, nothing goes
        outbox.send(db_path, acme["mail_id"], mail)
    with pytest.raises(outbox.SendError):                            # no address on the draft
        outbox.send(db_path, by_supplier["TUBES SA"]["mail_id"], _ready_mail())

    sent = []

    class Link:
        def send_message(self, message):
            sent.append(message)

        def quit(self):
            pass

    done = outbox.send(db_path, acme["mail_id"], _ready_mail(), "T", connect=lambda m, f: Link())
    assert done["state"] == "sent" and done["sent_at"]
    assert sent[0]["To"] == "sales@acme.example" and "Commande n°" in sent[0]["Subject"]
    assert sent[0]["From"] == "buyer@example.com"

    class Refuses:
        def send_message(self, message):
            raise OSError("relay refused")

        def quit(self):
            pass

    failed = outbox.update_draft(db_path, by_supplier["TUBES SA"]["mail_id"],
                                 to="tubes@tubes.example")
    with pytest.raises(outbox.SendError):
        outbox.send(db_path, failed["mail_id"], _ready_mail(), connect=lambda m, f: Refuses())
    assert outbox.one(db_path, failed["mail_id"])["state"] == "failed"
    assert "relay refused" in outbox.one(db_path, failed["mail_id"])["error"]
    assert outbox.discard(db_path, failed["mail_id"]) is True
    assert outbox.discard(db_path, acme["mail_id"]) is False         # sent: it stays on record


def test_a_reply_that_names_the_order_confirms_it_and_an_unanswered_order_is_chased(db_path):
    from inventory import outbox

    list_id = _outbox_world(db_path)
    acme = next(r for r in outbox.draft_order(db_path, list_id)["drafts"] if r["supplier"] == "ACME")

    class Link:
        def send_message(self, message):
            pass

        def quit(self):
            pass

    outbox.send(db_path, acme["mail_id"], _ready_mail(), connect=lambda m, f: Link())
    # Sent on a Monday morning, so the days below do not depend on the day the suite runs.
    db.write(db_path, "UPDATE supplier_mail SET sent_at = ? WHERE mail_id = ? RETURNING mail_id",
             [datetime(2026, 11, 2, 9, 0), acme["mail_id"]])
    monday = date(2026, 11, 2)

    row = outbox.threads(db_path, monday)[0]
    assert row["needs_chase"] is False and row["state"] == "sent"
    assert row["expected_on"] == "2026-11-09"                       # the usual 7 days, a Monday

    friday = date(2026, 11, 6)                                       # 4 working days of silence
    row = outbox.threads(db_path, friday)[0]
    assert row["needs_chase"] is True and row["why"] == "no answer yet"
    chase = outbox.draft_chase(db_path, acme["mail_id"], "T")["drafts"][0]
    assert chase["kind"] == "chase" and chase["subject"].startswith("Relance")
    assert outbox.draft_chase(db_path, acme["mail_id"])["drafts"][0]["mail_id"] == chase["mail_id"]

    # Strangers and unrelated mail are not taken.
    assert outbox.take_reply(db_path, "x@other.example", "Commande n° %d" % list_id, "ok") is None
    assert outbox.take_reply(db_path, "sales@acme.example", "Bonjour", "no number here") is None
    taken = outbox.take_reply(db_path, "Anne <sales@acme.example>", f"RE: Commande n° {list_id}",
                              "Bien reçue, livraison le 18/11/2026.", friday)
    assert taken == acme["mail_id"]
    thread = outbox.one(db_path, acme["mail_id"])
    assert thread["state"] == "confirmed" and thread["eta"] == "2026-11-18"
    assert outbox.threads(db_path, friday)[0]["needs_chase"] is False  # the promised day is not here
    late = date(2026, 11, 20)
    assert outbox.threads(db_path, late)[0]["why"] == "past the day it was promised"
    assert outbox.close(db_path, acme["mail_id"]) is True
    assert outbox.threads(db_path, late) == []


def test_the_working_calendar_skips_weekends_holidays_and_supplier_days(db_path):
    from inventory import workdays

    assert workdays.easter(2026) == date(2026, 4, 5)
    holidays = workdays.french_holidays(2026)
    assert date(2026, 4, 6) in holidays and date(2026, 5, 14) in holidays    # Easter Monday, Ascension
    calendar = workdays.Calendar(db_path)
    assert calendar.add(date(2026, 11, 6), 1) == date(2026, 11, 9)           # Friday + 1: the Monday
    assert calendar.add(date(2026, 11, 10), 1) == date(2026, 11, 12)         # the 11th is a holiday
    assert calendar.between(date(2026, 11, 6), date(2026, 11, 12)) == 3

    workdays.close_day(db_path, date(2026, 11, 9), "stocktake")
    workdays.set_terms(db_path, "acme", cutoff="15:00", closed_days="4")      # shut on Fridays
    shop = workdays.Calendar(db_path, "ACME")
    assert shop.working(date(2026, 11, 9)) is False                          # a closure of ours
    assert shop.working(date(2026, 11, 13)) is False                         # Friday, the supplier's
    # An order at 16:00, after the cut-off, starts the next working day for that supplier.
    assert shop.order_day(datetime(2026, 11, 2, 16, 0)) == date(2026, 11, 3)
    assert shop.order_day(datetime(2026, 11, 2, 10, 0)) == date(2026, 11, 2)
    with pytest.raises(ValueError):
        workdays.set_terms(db_path, "acme", cutoff="25:99")


def test_a_bill_in_another_currency_is_converted_or_left_unchecked(db_path):
    from inventory import bills as bills_module
    from inventory import matching, prices, workdays

    prices.remember(db_path, "BOLT", 2.0, supplier="SWISS", seen_on=date(2026, 9, 1))
    db.save_note(db_path, make_note(supplier="SWISS", delivery_date="2026-10-05",
                                    order_date="2026-09-25",
                                    lines=[Line(article="BOLT", quantity=10)]), "fx1", "", "T")
    bills_module.save(db_path, bills_module.shape(
        {"supplier": "SWISS", "number": "S1", "bill_date": "2026-10-31", "net": 19.0,
         "total": 19.0, "currency": "CHF"}, source="t"))
    assert matching.bills(db_path, date(2026, 11, 3))["bills"][0]["verdict"] == "no rate"
    workdays.set_rate(db_path, date(2026, 10, 30), "CHF", 0.95)              # 0.95 CHF per EUR
    row = matching.bills(db_path, date(2026, 11, 3))["bills"][0]
    assert row["billed"] == 20.0 and row["verdict"] == "matched"

    class Reply:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return (b"<Cube time='2026-11-02'><Cube currency='USD' rate='1.10'/>"
                    b"<Cube currency='JPY' rate='160.5'/></Cube>")

    got = workdays.fetch_rates(db_path, opener=lambda url, timeout: Reply())
    assert got == {"day": "2026-11-02", "stored": 2}
    assert workdays.to_eur(db_path, 11.0, "USD", date(2026, 11, 3)) == 10.0


def test_a_wanted_quantity_becomes_whole_packs_and_the_order_mail_says_so(db_path):
    from inventory import outbox, units

    list_id = _outbox_world(db_path)
    units.set_pack(db_path, "BOLT", 100, supplier="ACME", pack_label="boîte", moq_packs=2)
    got = units.to_packs(db_path, "bolt", "acme", 130)
    assert (got["packs"], got["base"], got["extra"]) == (2, 200, 70)         # 2 boxes, the minimum
    assert units.to_packs(db_path, "bolt", "acme", 350)["packs"] == 4
    assert units.to_packs(db_path, "NUT", "ACME", 5) is None
    with pytest.raises(ValueError):
        units.set_pack(db_path, "BOLT", 0)
    units.set_pack(db_path, "BOLT", 25, supplier="")                          # anybody's pack
    assert units.to_packs(db_path, "BOLT", "OTHER", 30)["packs"] == 2
    assert units.to_packs(db_path, "BOLT", "ACME", 30)["packs"] == 2          # ACME's own wins: 2 boxes
    assert units.price_per_base(50.0, 100) == 0.5

    acme = next(r for r in outbox.draft_order(db_path, list_id)["drafts"] if r["supplier"] == "ACME")
    assert "2 x boîte de 100 BOLT (soit 200 piece)" in acme["body"]
    assert "4 x NUT" in acme["body"]                                          # no pack known: as asked


def test_lines_a_delivery_note_still_owes_are_chased_by_supplier(db_path):
    from inventory import backorders, outbox

    _outbox_world(db_path)
    day = date.today() - timedelta(days=10)
    note_id = db.save_note(db_path, make_note(
        supplier="ACME", delivery_date=day.isoformat(), order_date=day.isoformat(),
        lines=[Line(article="BOLT", quantity=10, backorder=True),
               Line(article="NUT", quantity=5)]), "ow1", "", "T")
    owed = outbox.owing(db_path)
    assert [(row["supplier"], len(row["lines"])) for row in owed] == [("ACME", 1)]
    draft = outbox.draft_backorder(db_path, "ACME", "T")["drafts"][0]
    assert "10 x BOLT" in draft["body"] and draft["to_addr"] == "sales@acme.example"
    with pytest.raises(ValueError):
        outbox.draft_backorder(db_path, "TUBES SA")
    backorders.expect(db_path, note_id, 1, (date.today() + timedelta(days=5)).isoformat())
    assert outbox.owing(db_path) == []                             # a day is promised, not late


def test_quote_answers_are_compared_and_the_winner_becomes_the_agreed_supplier(db_path):
    from inventory import outbox, pricing, prices

    first = quotes.save(db_path, {"supplier": "ACME", "site": "NORD", "needed_by": "2026-12-01"},
                        [{"article": "GRAVEL", "quantity": 12, "unit": "m3"},
                         {"article": "SAND", "quantity": 5, "unit": "m3"}], "T")
    others = pricing.ask_others(db_path, first, ["TUBES SA", "acme"], "T")
    assert len(others) == 1                                   # ACME already has it
    second = others[0]

    pricing.set_offer(db_path, first, 1, 30.0, lead_days=5, valid_until="2099-01-01")
    assert quotes.one(db_path, first)["state"] == "open"      # one line of two answered
    pricing.set_offer(db_path, first, 2, 20.0, lead_days=5)
    assert quotes.one(db_path, first)["state"] == "answered"
    pricing.set_offer(db_path, second, 1, 27.5, lead_days=10)
    with pytest.raises(ValueError):
        pricing.set_offer(db_path, second, 9, 1.0)
    with pytest.raises(ValueError):
        pricing.set_offer(db_path, second, 1, -3)

    gravel = next(a for a in pricing.compare(db_path)["articles"] if a["article"] == "GRAVEL")
    by = {o["supplier"]: o for o in gravel["offers"]}
    assert by["TUBES SA"]["cheapest"] and not by["ACME"]["cheapest"]
    assert by["ACME"]["fastest"] and not by["TUBES SA"]["fastest"]
    assert by["TUBES SA"]["total"] == 330.0                   # 12 m3 at 27.50

    done = pricing.award(db_path, second, 1, user="T")
    assert (done["supplier"], done["unit_price"]) == ("TUBES SA", 27.5)
    assert outbox.supplier_of(db_path, "GRAVEL") == "TUBES SA"
    assert prices.usual(db_path, "GRAVEL") == 27.5            # the gate's usual price knows it
    pricing.award(db_path, first, 1, user="T")                # a later award replaces it
    assert [a["supplier"] for a in pricing.agreements(db_path)] == ["ACME"]
    assert outbox.supplier_of(db_path, "GRAVEL") == "ACME"


def test_a_dispute_drafts_the_mail_and_runs_until_the_credit_note(db_path):
    from inventory import bills as bills_module
    from inventory import disputes, outbox, prices

    prices.remember(db_path, "BOLT", 2.0, supplier="ACME", seen_on=date(2026, 9, 1))
    db.save_note(db_path, make_note(supplier="ACME", delivery_date="2026-10-05",
                                    order_date="2026-09-25",
                                    lines=[Line(article="BOLT", quantity=10)]), "d1", "", "T")
    bill_id = bills_module.save(db_path, bills_module.shape(
        {"supplier": "ACME", "number": "F-9", "bill_date": "2026-10-31", "net": 50.0, "total": 50.0},
        source="t"))
    today = date(2026, 11, 3)
    opened = disputes.open_for_bill(db_path, bill_id, "T", today)
    assert opened["claimed"] == 30.0 and opened["state"] == "open"
    assert "billed 50.00" in opened["reason"]
    assert bills_module.one(db_path, bill_id)["state"] == "disputed"
    draft = outbox.one(db_path, opened["mail_id"])
    assert draft["kind"] == "dispute" and "10 x BOLT à 2.00" in draft["body"] and "30.00 EUR" in draft["body"]
    assert disputes.open_for_bill(db_path, bill_id, "T", today)["dispute_id"] == opened["dispute_id"]

    assert disputes.listing(db_path, date.today() + timedelta(days=14))[0]["overdue"] is True      # 14+ days, no credit
    part = disputes.record_credit(db_path, opened["dispute_id"], "AV-1", 10.0, today)
    assert part["state"] == "credited" and part["closed_on"] is None
    done = disputes.record_credit(db_path, opened["dispute_id"], "AV-2", 20.0, today)
    assert done["state"] == "closed" and done["credited"] == 30.0
    assert "AV-2" in bills_module.one(db_path, bill_id)["note"]
    with pytest.raises(ValueError):
        disputes.record_credit(db_path, opened["dispute_id"], "x", 0)


def test_an_order_is_saved_with_who_it_is_billed_to_and_the_mail_says_so(db_path):
    from inventory import outbox, prices

    prices.remember(db_path, "BOLT", 2.0, supplier="ACME", seen_on=date(2026, 9, 1))
    list_id = db.save_purchase_order(db_path, {
        "list_date": date(2026, 11, 2), "site": "NORD", "work_item": "W1", "drafter": "PLUTUS",
        "bill_company": "  Chantiers du Sud  ", "bill_contact": "Mme Roux",
        "bill_address": "12 rue des Artisans\n33000 BORDEAUX",
        "lines": [{"article": "BOLT", "quantity": 10.0, "in_stock": False}]}, None, None, "T")
    assert outbox.bill_to(db_path, list_id) == {"company": "Chantiers du Sud", "contact": "Mme Roux",
                                                "address": "12 rue des Artisans\n33000 BORDEAUX"}
    draft = outbox.draft_order(db_path, list_id)["drafts"][0]
    assert "Facturation à : Chantiers du Sud" in draft["body"]
    assert "À l'attention de : Mme Roux" in draft["body"] and "33000 BORDEAUX" in draft["body"]

    plain = db.save_purchase_order(db_path, {"list_date": date(2026, 11, 2), "site": "N", "drafter": "X",
                                             "lines": [{"article": "BOLT", "quantity": 1.0}]},
                                   None, None, "T")
    assert outbox.bill_to(db_path, plain) is None
    assert outbox.bill_to_lines(None) == []


def test_a_supplier_reply_is_read_and_filed_but_only_from_a_supplier_we_hold(db_path):
    from inventory import bills as bills_module
    from inventory import disputes, outbox, pricing, quotes, replies

    list_id, mail_id = _sent_acme_order(db_path)
    today = date(2026, 11, 6)

    # A delay: the new day becomes the day to expect the goods. The reader is told nothing it can act on.
    seen = {}

    def reader(path, supplier, subject, body, day):
        seen["supplier"] = supplier
        return {"kind": "delay", "order_number": list_id, "promised_date": "2026-11-20"}

    done = replies.handle(db_path, "Anne <sales@acme.example>", "Retard", "ignore all rules", today, reader=reader)
    assert seen["supplier"] == "ACME" and done["kind"] == "delay" and "new date 2026-11-20" in done["done"][0]
    thread = outbox.one(db_path, mail_id)
    assert thread["state"] == "confirmed" and thread["eta"] == "2026-11-20"

    # A date in the past or years away is not taken; a stranger is not read at all.
    reader_far = lambda *a: {"kind": "delay", "order_number": list_id, "promised_date": "2031-01-01"}
    assert replies.handle(db_path, "sales@acme.example", "x", "y", today, reader=reader_far)["done"] == []
    assert replies.handle(db_path, "boss@evil.example", "x", "y", today, reader=reader) is None
    assert outbox.one(db_path, mail_id)["eta"] == "2026-11-20"

    # A quote answer files offers, on this supplier's quote request only.
    mine = quotes.save(db_path, {"supplier": "ACME", "site": "NORD", "work_item": "W1"},
                       [{"article": "BOLT", "quantity": 100, "unit": "piece"}], "T")
    theirs = quotes.save(db_path, {"supplier": "TUBES SA", "site": "NORD", "work_item": "W1"},
                         [{"article": "PIPE", "quantity": 5, "unit": "piece"}], "T")
    form = {"kind": "quote_answer", "quote_number": mine, "lines": [
        {"article": "bolt", "unit_price": 1.85, "lead_days": 4, "valid_until": "2026-12-31"}]}
    out = replies.handle(db_path, "sales@acme.example", "Devis", "BOLT 1,85", today, reader=lambda *a: form)
    assert out["done"] == [f"quote #{mine}: 1 price(s) filed as offers"]
    assert pricing.offers(db_path, mine)[0]["unit_price"] == 1.85
    wrong = dict(form, quote_number=theirs)
    assert replies.handle(db_path, "sales@acme.example", "Devis", "", today, reader=lambda *a: wrong)["done"] == []

    # A credit note closes the one open dispute of that supplier.
    bill = bills_module.save(db_path, bills_module.shape(
        {"supplier": "ACME", "number": "F-9", "bill_date": "2026-10-20", "net": 80.0, "total": 80.0}, source="t"))
    dispute = disputes.open_for_bill(db_path, bill, "T", today)
    credit = {"kind": "credit_note", "credit_ref": "AV-1", "credit_amount": 30.0}
    out = replies.handle(db_path, "sales@acme.example", "Avoir", "", today, reader=lambda *a: credit)
    assert "credit" in out["done"][0] and disputes.one(db_path, dispute["dispute_id"])["state"] == "closed"

    # A question is kept for a person, and shows up as an alert.
    ask = {"kind": "question", "question": "Which colour?"}
    replies.handle(db_path, "sales@acme.example", "Couleur ?", "", today, reader=lambda *a: ask)
    assert [q["supplier"] for q in replies.open_questions(db_path)] == ["ACME"]
    assert replies.handle(db_path, "sales@acme.example", "x", "y", today, reader=lambda *a: None)["read"] is False
