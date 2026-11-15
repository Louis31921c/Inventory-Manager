"""Second half of make_big_data.py: turn the demand and the deliveries into the app's tables.

Everything here is SQL over the two working tables (t_out: what left, t_in: what came) so the
numbers in one table are sums of the numbers in another: a bill is the month's deliveries at the
price on the day, a payment is a paid bill, a wallet entry is an order or a payment.
"""
import hashlib
import hmac
import json
import time
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

from make_big_data import (DRAFTERS, SLOW_SUPPLIER, WHO_RECEIVES, mix64, say)

NOTE_LINES = 24
ORDER_LINES = 14
PLUTUS_PILOT = ("LINKOFFICE", "MARTEL", "EMBALLAGES DU LITTORAL", "EPI PRO 33", "OUTILLAGE GIRONDIN")
PLUTUS_DAYS = 730
FIRM = ("ATELIERS DE LA GARONNE", "Service achats", "8 rue des Forges, 33520 BRUGES")


def sql(con, text, what=None):
    t = time.time()
    con.execute(text)
    if what:
        say(f"{what} ({time.time() - t:.0f}s)")


def bump(con, seq, n):
    """Move a sequence past ids that were written explicitly."""
    if n > 0:
        con.execute(f"SELECT count(nextval('{seq}')) FROM range({int(n)})")


def finish(S, args):
    con, today, base_ord, today_off = S["con"], S["today"], S["base_ord"], S["today_off"]
    base = date.fromordinal(base_ord)
    D = f"DATE '{base}'"                       # a day offset is days after this date
    TODAY = f"DATE '{today}'"
    rng, nrng = S["rng"], S["nrng"]

    # ---- the dimensions -------------------------------------------------------------------------
    art = S["art"]
    sup = S["supplier_list"]
    t_art = pd.DataFrame({
        "article_id": art["article_id"], "name": art["name"], "designation": art["designation"],
        "reference": art["reference"], "supplier1": [sup[i] for i in art["supplier1"]],
        "supplier2": [sup[i] if i >= 0 else None for i in art["supplier2"]],
        "usual": art["usual"].astype(float)})
    con.execute("CREATE TABLE t_art AS SELECT * FROM t_art")
    con.execute("CREATE TABLE t_sup AS SELECT * FROM (SELECT * FROM (VALUES %s) v(supplier_id, name))" %
                ",".join(f"({i}, '{n.replace(chr(39), chr(39) * 2)}')" for i, n in enumerate(sup)))
    con.execute("CREATE TABLE t_site (site_id INTEGER, name VARCHAR, first_week INTEGER, last_week INTEGER)")
    con.execute("CREATE TABLE t_item (site_id INTEGER, item_id INTEGER, name VARCHAR)")
    sites = S["sites"]
    con.executemany("INSERT INTO t_site VALUES (?, ?, ?, ?)", [(i, s[0], s[2], s[3]) for i, s in enumerate(sites)])
    con.executemany("INSERT INTO t_item VALUES (?, ?, ?)",
                    [(i, j, item) for i, s in enumerate(sites) for j, item in enumerate(s[1])])

    # ---- delivery notes -------------------------------------------------------------------------
    sql(con, f"""
        CREATE TABLE t_nl AS
        WITH r AS (
            SELECT i.*, row_number() OVER (PARTITION BY supplier_id, day ORDER BY article_id) rn
            FROM t_in i WHERE NOT future)
        SELECT r.*, dense_rank() OVER (ORDER BY day, supplier_id, (rn - 1) // {NOTE_LINES}) AS note_id,
               ((rn - 1) % {NOTE_LINES}) + 1 AS line_no,
               (day > {today_off - 100}) AND (hash(day * 1000 + article_id) % 1000 < 40) AS bo
        FROM r""", "delivery note lines grouped into notes")
    n_notes = con.execute("SELECT max(note_id) FROM t_nl").fetchone()[0] or 0
    sql(con, f"""
        INSERT INTO inventory_lines (article, quantity, supplier, delivery_date, order_date, site, work_item,
                                     backorder, designation, note_id, line_no, expected_on, settled_on)
        SELECT a.name, n.qty, s.name, {D} + n.day, {D} + n.day - n.lead, st.name, it.name,
               n.bo, a.designation, n.note_id, n.line_no,
               CASE WHEN n.bo THEN {D} + n.day + CAST(hash(n.note_id * 31 + n.line_no) % 45 AS INTEGER) - 8 END,
               CASE WHEN n.bo AND hash(n.note_id * 17 + n.line_no) % 100 < 45
                         AND {D} + n.day + CAST(hash(n.note_id * 31 + n.line_no) % 45 AS INTEGER) - 8 < {TODAY}
                    THEN {D} + n.day + CAST(hash(n.note_id * 31 + n.line_no) % 45 AS INTEGER) - 8 END
        FROM t_nl n JOIN t_art a USING (article_id) JOIN t_sup s USING (supplier_id)
             JOIN t_site st USING (site_id) JOIN t_item it ON it.site_id = n.site_id AND it.item_id = n.item_id
        ORDER BY n.note_id, n.line_no""", "inventory_lines")
    who = "[" + ",".join(f"'{w}'" for w in WHO_RECEIVES) + "]"
    sql(con, f"""
        INSERT INTO notes (note_id, image_sha256, image_path, confirmed_at, confirmed_by)
        SELECT note_id, 'big-note-' || note_id, '', CAST({D} + min(day) AS TIMESTAMP) + INTERVAL 16 HOUR,
               {who}[1 + CAST(hash(note_id) % {len(WHO_RECEIVES)} AS INTEGER)]
        FROM t_nl GROUP BY note_id""", "notes")
    bump(con, "note_seq", n_notes)

    # ---- customer orders: what left ----------------------------------------------------------------
    sql(con, f"""
        CREATE TABLE t_ol AS
        WITH r AS (SELECT *, row_number() OVER (PARTITION BY day, site_id, item_id ORDER BY article_id) rn FROM t_out)
        SELECT r.*, dense_rank() OVER (ORDER BY day, site_id, item_id, (rn - 1) // {ORDER_LINES}) AS list_id,
               ((rn - 1) % {ORDER_LINES}) + 1 AS line_no
        FROM r""", "order lines grouped into orders")
    n_lists = con.execute("SELECT max(list_id) FROM t_ol").fetchone()[0] or 0
    drafters = "[" + ",".join(f"'{d}'" for d in DRAFTERS) + "]"
    sql(con, f"""
        INSERT INTO purchase_orders (list_id, list_date, site, work_item, drafter, image_sha256, image_path,
                                     created_at, created_by)
        SELECT o.list_id, {D} + min(o.day), min(st.name), min(it.name),
               {drafters}[1 + CAST(hash(o.list_id) % {len(DRAFTERS)} AS INTEGER)], 'big-order-' || o.list_id, '',
               CAST({D} + min(o.day) AS TIMESTAMP) + INTERVAL 1 MINUTE * CAST(420 + hash(o.list_id) % 540 AS INTEGER),
               {drafters}[1 + CAST(hash(o.list_id) % {len(DRAFTERS)} AS INTEGER)]
        FROM t_ol o JOIN t_site st USING (site_id) JOIN t_item it ON it.site_id = o.site_id AND it.item_id = o.item_id
        GROUP BY o.list_id ORDER BY o.list_id""", "purchase_orders")
    sql(con, """
        INSERT INTO purchase_order_lines (list_id, line_no, article, designation, reference, quantity, stock, in_stock)
        SELECT o.list_id, o.line_no, a.name, a.designation, NULL, o.qty, NULL, NULL
        FROM t_ol o JOIN t_art a USING (article_id) ORDER BY o.list_id, o.line_no""", "purchase_order_lines")

    # ---- suppliers: contacts, terms, bank ------------------------------------------------------------
    from make_big_data import COMPANIES

    contact_rows = []
    for i, name in enumerate(sup):
        slug = "".join(ch for ch in name.lower() if ch.isalnum())
        person = COMPANIES[i % len(COMPANIES)]
        contact_rows.append((i + 1, name, person, f"05 56 {rng.randint(10, 99)} {rng.randint(10, 99)} {rng.randint(10, 99)}",
                             f"commandes@{slug}.example.com", f"{rng.randint(2, 180)} avenue de l'Industrie, 33{rng.randint(100, 700)} "
                             f"{rng.choice(['BORDEAUX', 'MERIGNAC', 'PESSAC', 'LORMONT', 'BEGLES'])}",
                             rng.choice(["EXW", "FCA", "DAP", "DDP"]), None, "made up", datetime.now(), "SAMPLE"))
    con.executemany("INSERT INTO contacts VALUES (?,?,?,?,?,?,?,?,?,?,?)", contact_rows)
    bump(con, "contact_seq", len(sup))
    terms = [(name, "17:00", "SATURDAY,SUNDAY", rng.choice([30, 30, 45, 60]), "EUR", None) for name in sup]
    con.executemany("INSERT INTO supplier_terms VALUES (?,?,?,?,?,?)", terms)
    con.executemany("INSERT INTO supplier_bank VALUES (?,?,?,?)",
                    [(name, "FR76" + "".join(str(rng.randint(0, 9)) for _ in range(23)),
                      "".join(rng.choice("ABCDEFGHJKLMNPRSTUVWXYZ") for _ in range(8)), name) for name in sup])

    # ---- prices ----------------------------------------------------------------------------------------
    chg = S["chg"]
    chg = chg[chg["day"] <= today_off].copy()
    chg["article_id"] = S["pair_art"][chg["pair"].to_numpy()]
    chg["supplier_id"] = S["pair_sup"][chg["pair"].to_numpy()]
    chg["source"] = nrng.choice(["order", "bill", "quote", "basket"], len(chg), p=[0.5, 0.3, 0.15, 0.05])
    con.execute("CREATE TABLE t_chg AS SELECT pair, CAST(day AS INTEGER) AS day, price, article_id, supplier_id, source FROM chg")
    sql(con, f"""
        INSERT INTO article_prices (price_id, article, supplier, unit_price, source, seen_on, added_at)
        SELECT row_number() OVER (ORDER BY c.day, c.pair), a.name, s.name, c.price,
               CASE WHEN c.source = 'basket' THEN 'order' ELSE c.source END,
               {D} + c.day, CAST({D} + c.day AS TIMESTAMP) + INTERVAL 10 HOUR
        FROM t_chg c JOIN t_art a USING (article_id) JOIN t_sup s USING (supplier_id)""", "article_prices")
    bump(con, "price_seq", len(chg))

    # ---- bills: one a month per supplier, priced as delivered --------------------------------------------
    sql(con, f"""
        CREATE TABLE t_bill AS
        WITH m AS (
            SELECT supplier_id, date_trunc('month', {D} + day) AS month, sum(qty * price) AS goods,
                   count(DISTINCT note_id) AS notes
            FROM t_nl WHERE NOT bo AND {D} + day < date_trunc('month', {TODAY}) GROUP BY ALL)
        SELECT row_number() OVER (ORDER BY month, supplier_id) AS bill_id, m.*,
               CASE WHEN hash(CAST(supplier_id AS BIGINT) * 1000 + month_n) % 100 < 6
                    THEN 1 + (CAST(hash(supplier_id + month_n) % 17 AS DOUBLE) - 8) / 100 ELSE 1.0 END AS skew
        FROM (SELECT *, year(month) * 12 + month(month) AS month_n FROM m) m""", "monthly bills")
    n_bills = con.execute("SELECT count(*) FROM t_bill").fetchone()[0]
    terms_days = {name: t[3] for name, t in zip(sup, terms)}
    con.execute("CREATE TABLE t_terms (name VARCHAR, days INTEGER)")
    con.executemany("INSERT INTO t_terms VALUES (?, ?)", list(terms_days.items()))
    sql(con, f"""
        INSERT INTO bills (bill_id, supplier, number, bill_date, due_date, currency, net, tax, total, site,
                           order_ref, state, paid_on, source, sender, image_sha256, image_path, added_at, added_by, note)
        SELECT b.bill_id, s.name,
               substr(regexp_replace(s.name, '[^A-Z]', '', 'g'), 1, 3) || '-' || strftime(b.month, '%Y%m') || '-'
                    || lpad(CAST(b.bill_id AS VARCHAR), 6, '0'),
               last_day(b.month), last_day(b.month) + t.days, 'EUR',
               round(b.goods * b.skew, 2), round(b.goods * b.skew * 0.2, 2), round(b.goods * b.skew * 1.2, 2),
               NULL, NULL,
               CASE WHEN last_day(b.month) > {TODAY} - 35 THEN 'to pay'
                    WHEN last_day(b.month) > {TODAY} - 75 AND hash(b.bill_id) % 100 < 14 THEN 'to pay'
                    WHEN hash(b.bill_id * 7) % 1000 < 12 AND last_day(b.month) > {TODAY} - 400 THEN 'disputed'
                    ELSE 'paid' END,
               NULL, CASE WHEN hash(b.bill_id) % 3 = 0 THEN 'mail' ELSE 'scan' END, 'compta@' ||
                    lower(regexp_replace(s.name, '[^A-Za-z]', '', 'g')) || '.example.com', 'big-bill-' || b.bill_id, '',
               CAST(last_day(b.month) + 1 AS TIMESTAMP), 'MARIE', NULL
        FROM t_bill b JOIN t_sup s USING (supplier_id) JOIN t_terms t ON t.name = s.name
        ORDER BY b.bill_id""", "bills")
    sql(con, f"""
        UPDATE bills SET paid_on = due_date - CAST(hash(bill_id) % 9 AS INTEGER) WHERE state = 'paid'""")
    # One bill entered twice, and one with a number reused: what the anomaly engine is for.
    con.execute("""
        INSERT INTO bills (bill_id, supplier, number, bill_date, due_date, currency, net, tax, total, state,
                           source, sender, added_at, added_by)
        SELECT (SELECT max(bill_id) FROM bills) + 1, supplier, number, bill_date + 3, due_date + 3, currency, net, tax,
               total, 'to pay', 'mail', sender, added_at + INTERVAL 3 DAY, 'MARIE'
        FROM bills WHERE state = 'to pay' ORDER BY bill_date DESC, total DESC LIMIT 1""")
    bump(con, "bill_seq", n_bills + 1)

    sql(con, f"""
        CREATE TABLE t_batch AS
        SELECT row_number() OVER (ORDER BY wk) AS batch_id, wk FROM
            (SELECT DISTINCT date_trunc('week', paid_on) AS wk FROM bills
             WHERE state = 'paid' AND paid_on >= {TODAY} - 1100 AND paid_on <= {TODAY})""")
    sql(con, """
        INSERT INTO payment_batches (batch_id, created_at, state, exported_at, executed_at, msg_id, uploaded_at, bank_status)
        SELECT batch_id, CAST(wk AS TIMESTAMP) + INTERVAL 9 HOUR, 'executed', CAST(wk AS TIMESTAMP) + INTERVAL 11 HOUR,
               CAST(wk AS TIMESTAMP) + INTERVAL 2 DAY, 'MSG' || lpad(CAST(batch_id AS VARCHAR), 6, '0'),
               CAST(wk AS TIMESTAMP) + INTERVAL 12 HOUR, 'ACCP' FROM t_batch""")
    bump(con, "batch_seq", con.execute("SELECT count(*) FROM t_batch").fetchone()[0])
    sql(con, f"""
        INSERT INTO payments (payment_id, bill_id, supplier, amount, currency, iban, batch_id, state, reference,
                              instructed_at, executed_at, done_by)
        SELECT row_number() OVER (ORDER BY b.paid_on, b.bill_id), b.bill_id, b.supplier, b.total, 'EUR', k.iban, t.batch_id,
               'executed', 'BILL ' || b.number, CAST(b.paid_on AS TIMESTAMP) - INTERVAL 1 DAY + INTERVAL 15 HOUR,
               CAST(b.paid_on AS TIMESTAMP) + INTERVAL 10 HOUR,
               CASE WHEN b.supplier IN {PLUTUS_PILOT} AND b.paid_on > {TODAY} - {PLUTUS_DAYS} THEN 'PLUTUS' ELSE 'MARIE' END
        FROM bills b JOIN supplier_bank k ON k.supplier = b.supplier
             JOIN t_batch t ON t.wk = date_trunc('week', b.paid_on)
        WHERE b.state = 'paid' AND b.paid_on >= {TODAY} - 1100 AND b.paid_on <= {TODAY}""", "payments")
    bump(con, "payment_seq", con.execute("SELECT count(*) FROM payments").fetchone()[0])
    sql(con, f"""
        INSERT INTO disputes (dispute_id, bill_id, supplier, reason, claimed, state, opened_on, credit_ref, credited,
                              closed_on, mail_id, opened_by)
        SELECT row_number() OVER (ORDER BY bill_id), bill_id, supplier,
               ['prices above the agreed list', 'quantity billed not delivered', 'delivery note missing',
                'damaged goods returned', 'billed twice'][1 + CAST(hash(bill_id) % 5 AS INTEGER)],
               round(total * (0.03 + (hash(bill_id * 3) % 20) / 100.0), 2),
               CASE WHEN bill_date < {TODAY} - 120 THEN 'credited' ELSE 'open' END, bill_date + 20,
               CASE WHEN bill_date < {TODAY} - 120 THEN 'CR-' || bill_id END,
               CASE WHEN bill_date < {TODAY} - 120 THEN round(total * (0.03 + (hash(bill_id * 3) % 20) / 100.0), 2) END,
               CASE WHEN bill_date < {TODAY} - 120 THEN bill_date + 50 END, NULL, 'MARIE'
        FROM bills WHERE state = 'disputed' ORDER BY bill_id""", "disputes")
    bump(con, "dispute_seq", con.execute("SELECT count(*) FROM disputes").fetchone()[0])

    # ---- Plutus's pilot, and what is on its way --------------------------------------------------------------
    pilot = ",".join(f"'{p.replace(chr(39), chr(39) * 2)}'" for p in PLUTUS_PILOT)
    sql(con, f"""
        CREATE TABLE t_po AS
        WITH done AS (
            SELECT supplier_id, day, note_id AS n, min(day - lead) AS ordered, sum(qty * price) AS total, FALSE AS open
            FROM t_nl WHERE day > {today_off - PLUTUS_DAYS} AND supplier_id IN (SELECT supplier_id FROM t_sup WHERE name IN ({pilot}))
            GROUP BY supplier_id, day, note_id),
        coming AS (
            SELECT supplier_id, day, 0 AS n, min(day - lead) AS ordered, sum(qty * price) AS total, TRUE AS open
            FROM t_in WHERE future GROUP BY supplier_id, day)
        SELECT {n_lists} + row_number() OVER (ORDER BY ordered, supplier_id, day, n) AS list_id, * FROM
            (SELECT * FROM done UNION ALL SELECT * FROM coming)""", "Plutus orders")
    n_po = con.execute("SELECT count(*) FROM t_po").fetchone()[0]
    sql(con, f"""
        INSERT INTO purchase_orders (list_id, list_date, site, work_item, drafter, image_sha256, image_path, created_at,
                                     created_by, contact_id, incoterm, ship_by, bill_company, bill_contact, bill_address)
        SELECT p.list_id, {D} + p.ordered, NULL, NULL, 'PLUTUS', NULL, NULL,
               CAST({D} + p.ordered AS TIMESTAMP) + INTERVAL 8 HOUR + INTERVAL 1 MINUTE * CAST(hash(p.list_id) % 120 AS INTEGER),
               'PLUTUS', p.supplier_id + 1, 'DAP', {D} + p.day, '{FIRM[0]}', '{FIRM[1]}', '{FIRM[2]}'
        FROM t_po p""")
    sql(con, f"""
        INSERT INTO purchase_order_lines (list_id, line_no, article, designation, reference, quantity, stock, in_stock)
        SELECT p.list_id, row_number() OVER (PARTITION BY p.list_id ORDER BY i.article_id), a.name, a.designation, NULL, i.qty, NULL, NULL
        FROM t_po p JOIN (SELECT n.*, note_id AS nid FROM t_nl n) i
             ON NOT p.open AND i.note_id = p.n
        JOIN t_art a ON a.article_id = i.article_id
        UNION ALL
        SELECT p.list_id, row_number() OVER (PARTITION BY p.list_id ORDER BY i.article_id), a.name, a.designation, NULL, i.qty, NULL, NULL
        FROM t_po p JOIN t_in i ON p.open AND i.future AND i.supplier_id = p.supplier_id AND i.day = p.day
        JOIN t_art a ON a.article_id = i.article_id""", "Plutus order lines")
    bump(con, "list_seq", n_lists + n_po)
    return state_two(S, args, locals())


def state_two(S, args, scope):
    from make_big_more import more

    return more(S, args, scope)
