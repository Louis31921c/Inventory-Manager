"""The rest of the tables for make_big_data.py: mail, wallet, counts, dispatches, the sealed logs."""
import hashlib
import hmac
import json
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

from make_big_data import DRAFTERS, SLOW_SUPPLIER, WHO_RECEIVES, say
from make_big_tables import FIRM, PLUTUS_DAYS, PLUTUS_PILOT, bump, sql

ACTORS = ["ADMIN", "ALEX", "MARIE", "SAM", "LEO", "INES", "THOMAS", "CAMILLE", "KARIM", "PLUTUS", "TERMINUS"]


def q(text):
    return text.replace("'", "''")


def more(S, args, scope):
    con, today, today_off, base_ord = S["con"], S["today"], S["today_off"], S["base_ord"]
    base = date.fromordinal(base_ord)
    D, TODAY = f"DATE '{base}'", f"DATE '{today}'"
    rng, nrng = S["rng"], S["nrng"]
    sup = S["supplier_list"]
    n_lists, n_po, n_notes = scope["n_lists"], scope["n_po"], scope["n_notes"]
    pilot = ",".join(f"'{q(p)}'" for p in PLUTUS_PILOT)
    sites = S["sites"]

    # ---- the shelf today: what the paper says is on it ------------------------------------------------------
    sql(con, f"""
        CREATE TABLE t_oh AS
        SELECT a.article_id, a.name, coalesce(i.q, 0) - coalesce(o.q, 0) AS oh, coalesce(o.q, 0) AS shipped
        FROM t_art a
        LEFT JOIN (SELECT article_id, sum(qty) q FROM t_in WHERE NOT future GROUP BY 1) i USING (article_id)
        LEFT JOIN (SELECT article_id, sum(qty) q FROM t_out WHERE day <= {today_off - 1} GROUP BY 1) o USING (article_id)""",
        "shelf today")

    # ---- names, catalog, units, homes ------------------------------------------------------------------------
    sql(con, f"""
        INSERT INTO article_names (article, first_seen, last_seen, seen, source)
        SELECT a.name, CAST({D} + f.d AS TIMESTAMP), CAST({D} + f.l AS TIMESTAMP), f.n, 'delivery'
        FROM t_art a JOIN (SELECT article_id, min(day) d, max(day) l, count(*) n FROM t_in WHERE NOT future GROUP BY 1) f
             USING (article_id)""", "article_names")
    sql(con, f"""
        INSERT INTO catalog (supplier, designation, reference, article, seen, updated_at)
        SELECT s.name, a.designation, a.reference, a.name, f.n, CAST({D} + f.l AS TIMESTAMP)
        FROM t_art a JOIN t_sup s ON s.name = a.supplier1
             JOIN (SELECT article_id, count(*) n, max(day) l FROM t_in WHERE NOT future GROUP BY 1) f USING (article_id)""",
        "catalog")
    sql(con, """
        INSERT INTO article_units (article, supplier, base_unit, pack_qty, pack_label, moq_packs)
        SELECT name, supplier1, CASE WHEN usual >= 40 THEN 'piece' ELSE 'unit' END,
               CASE WHEN usual >= 400 THEN 100 WHEN usual >= 100 THEN 50 WHEN usual >= 40 THEN 25 ELSE 1 END,
               CASE WHEN usual >= 40 THEN 'box' ELSE 'unit' END, 1
        FROM t_art WHERE hash(article_id) % 10 < 3""", "article_units")
    sql(con, f"""
        INSERT INTO article_homes (article, site, location, last_used)
        SELECT a.name, '', chr(65 + CAST(hash(a.article_id) % 12 AS INTEGER)) || '-' ||
               lpad(CAST(1 + hash(a.article_id * 3) % 40 AS VARCHAR), 2, '0') || '-' || CAST(1 + hash(a.article_id * 5) % 5 AS VARCHAR),
               CAST({TODAY} - CAST(hash(a.article_id) % 60 AS INTEGER) AS TIMESTAMP)
        FROM t_art a JOIN t_oh USING (article_id) WHERE oh > 0 AND hash(a.article_id) % 10 < 7""", "article_homes")
    sql(con, """
        INSERT INTO article_aliases (alias, article, created_at, created_by)
        SELECT replace(name, ' ', '-'), name, now(), 'TERMINUS' FROM t_art WHERE hash(article_id) % 200 = 0""")

    # ---- supplier mail: an order out and a confirmation back for every note ----------------------------------
    sql(con, f"""
        CREATE TABLE t_note AS
        SELECT note_id, supplier_id, min(day) AS day, min(day - lead) AS ordered,
               string_agg(a.name || ' x ' || CAST(CAST(qty AS BIGINT) AS VARCHAR), chr(10) ORDER BY line_no) AS body,
               count(*) AS n, sum(qty * price) AS total
        FROM t_nl JOIN t_art a USING (article_id) GROUP BY note_id, supplier_id""", "notes summarised")
    sql(con, f"""
        CREATE TABLE t_mail AS
        SELECT n.*, s.name AS supplier, c.email, po.list_id AS po_id,
               2 * n.note_id - 1 AS out_id, 2 * n.note_id AS in_id
        FROM t_note n JOIN t_sup s USING (supplier_id)
             JOIN contacts c ON c.company = s.name
             LEFT JOIN t_po po ON po.n = n.note_id AND NOT po.open""")
    sql(con, f"""
        INSERT INTO supplier_mail (mail_id, direction, kind, supplier, to_addr, from_addr, subject, body, ref_kind, ref_id,
                                   thread_id, state, error, message_id, created_at, created_by, sent_at, confirmed_at, eta,
                                   chase_count, last_chased_at, orig_body, read_json)
        SELECT out_id, 'out', 'order', supplier, email, 'achats@ateliers-garonne.example.com',
               'Commande ' || coalesce(po_id, note_id) || ' - ' || supplier,
               'Bonjour,' || chr(10) || chr(10) || 'Veuillez trouver ci-dessous notre commande pour livraison le ' ||
               strftime({D} + day, '%d/%m/%Y') || ' :' || chr(10) || chr(10) || body || chr(10) || chr(10) ||
               'Merci de nous confirmer la date de livraison.' || chr(10) || 'Cordialement,' || chr(10) || '{FIRM[1]}',
               CASE WHEN po_id IS NULL THEN 'note' ELSE 'po' END, coalesce(po_id, note_id), out_id, 'closed', NULL,
               '<' || md5('o' || note_id) || '@ateliers-garonne.example.com>',
               CAST({D} + ordered AS TIMESTAMP) + INTERVAL 9 HOUR,
               CASE WHEN po_id IS NULL THEN ['ALEX','MARIE','SAM','LEO'][1 + CAST(hash(note_id) % 4 AS INTEGER)] ELSE 'PLUTUS' END,
               CAST({D} + ordered AS TIMESTAMP) + INTERVAL 9 HOUR + INTERVAL 1 MINUTE,
               CAST({D} + ordered AS TIMESTAMP) + INTERVAL 13 HOUR + INTERVAL 1 MINUTE * CAST(hash(note_id) % 200 AS INTEGER),
               {D} + day, CASE WHEN hash(note_id * 5) % 100 < 6 THEN 1 ELSE 0 END,
               CASE WHEN hash(note_id * 5) % 100 < 6 THEN CAST({D} + ordered AS TIMESTAMP) + INTERVAL 3 DAY END, NULL, NULL
        FROM t_mail
        UNION ALL
        SELECT in_id, 'in', 'confirmation', supplier, 'achats@ateliers-garonne.example.com', email,
               'RE: Commande ' || coalesce(po_id, note_id) || ' - ' || supplier,
               'Bonjour, commande bien recue, livraison prevue le ' || strftime({D} + day, '%d/%m/%Y') || ' (' || n || ' lignes).' ||
               chr(10) || 'Cordialement,' || chr(10) || 'ADV ' || supplier, CASE WHEN po_id IS NULL THEN 'note' ELSE 'po' END,
               coalesce(po_id, note_id), out_id, 'received', NULL, '<' || md5('i' || note_id) || '@' ||
               lower(regexp_replace(supplier, '[^A-Za-z]', '', 'g')) || '.example.com>',
               CAST({D} + ordered AS TIMESTAMP) + INTERVAL 13 HOUR + INTERVAL 1 MINUTE * CAST(hash(note_id) % 200 AS INTEGER),
               NULL, NULL, NULL, {D} + day, 0, NULL, NULL, NULL
        FROM t_mail""", "supplier_mail (orders and confirmations)")
    top = 2 * n_notes
    # What is on its way: orders sent, not yet delivered.
    sql(con, f"""
        INSERT INTO supplier_mail (mail_id, direction, kind, supplier, to_addr, from_addr, subject, body, ref_kind, ref_id,
                                   thread_id, state, message_id, created_at, created_by, sent_at, confirmed_at, eta, chase_count)
        SELECT {top} + row_number() OVER (ORDER BY po.list_id), 'out', 'order', s.name, c.email,
               'achats@ateliers-garonne.example.com', 'Commande ' || po.list_id || ' - ' || s.name,
               'Bonjour,' || chr(10) || chr(10) || 'Veuillez trouver notre commande (' || cast(CAST(po.total AS BIGINT) AS VARCHAR) ||
               ' EUR HT) pour livraison le ' || strftime({D} + po.day, '%d/%m/%Y') || '.' || chr(10) || 'Cordialement,',
               'po', po.list_id, {top} + row_number() OVER (ORDER BY po.list_id),
               CASE WHEN hash(po.list_id) % 3 = 0 THEN 'sent' ELSE 'confirmed' END,
               '<' || md5('c' || po.list_id) || '@ateliers-garonne.example.com>',
               CAST({D} + po.ordered AS TIMESTAMP) + INTERVAL 9 HOUR, 'PLUTUS',
               CAST({D} + po.ordered AS TIMESTAMP) + INTERVAL 9 HOUR + INTERVAL 1 MINUTE,
               CASE WHEN hash(po.list_id) % 3 = 0 THEN NULL ELSE CAST({D} + po.ordered AS TIMESTAMP) + INTERVAL 14 HOUR END,
               {D} + po.day, 0
        FROM t_po po JOIN t_sup s USING (supplier_id) JOIN contacts c ON c.company = s.name WHERE po.open""",
        "supplier_mail (on its way)")
    bump(con, "supplier_mail_seq", con.execute("SELECT max(mail_id) FROM supplier_mail").fetchone()[0])

    # ---- Plutus: policies, the wallet and its commitments ---------------------------------------------------
    con.execute("""INSERT INTO policies (policy_id, name, action, active, mode, supplier, max_total, per_day, per_month,
                       price_band, min_deliveries, allowed_reasons, terminus_same, hours_from, hours_to, per_supplier_day,
                       max_chases, created_by) VALUES
        (1, 'Consumables orders', 'send_order', TRUE, 'dry_run', '*', 2500, 15000, 200000, 0.10, 5, '', TRUE, 7, 18, 3, 2, 'ADMIN'),
        (2, 'Chase late orders', 'chase', TRUE, 'dry_run', '*', 2500, NULL, NULL, 0.10, 3, '', TRUE, 8, 17, 2, 2, 'ADMIN'),
        (3, 'Approve small baskets', 'approve_basket', TRUE, 'dry_run', '*', 500, 1000, 15000, 0.10, 3, 'first-article', TRUE, 8, 17, 2, 2, 'ADMIN')""")
    bump(con, "policy_seq", 3)
    con.execute("INSERT INTO policy_settings VALUES ('policies_enabled', '1')")
    for key, val in (("send_max_total", "2500"), ("send_per_day", "15000"), ("approve_max_total", "500"),
                     ("pay_max_total", "2500"), ("pay_per_day", "10000")):
        con.execute("INSERT INTO bot_limits VALUES ('plutus', ?, ?)", [key, val])
    sql(con, f"""
        INSERT INTO policy_log (log_id, happened_at, policy_id, policy_name, action, subject, supplier, amount, decision,
                                reasons, mode, acted)
        SELECT row_number() OVER (ORDER BY po.list_id), CAST({D} + po.ordered AS TIMESTAMP) + INTERVAL 9 HOUR + INTERVAL 1 SECOND,
               1, 'Consumables orders', 'send_order', 'PO ' || po.list_id, s.name, round(po.total, 2),
               CASE WHEN po.total > 2500 THEN 'denied' ELSE 'allowed' END,
               CASE WHEN po.total > 2500 THEN 'over the limit for one order' END, 'live', po.total <= 2500
        FROM t_po po JOIN t_sup s USING (supplier_id)""", "policy_log")
    bump(con, "policy_log_seq", con.execute("SELECT count(*) FROM policy_log").fetchone()[0])
    # Money in and out, month by month. A deposit tops the wallet up so it never goes below a cushion.
    ev = con.execute(f"""
        SELECT CAST({D} + po.ordered AS TIMESTAMP) + INTERVAL 9 HOUR + INTERVAL 1 SECOND AS at, 'order' AS kind,
               -round(po.total, 2) AS amount, 'po' AS ref_kind, po.list_id AS ref_id, s.name AS supplier
        FROM t_po po JOIN t_sup s USING (supplier_id) WHERE po.total <= 2500
        UNION ALL
        SELECT CAST(p.executed_at AS TIMESTAMP), 'payment', -p.amount, 'bill', p.bill_id, p.supplier
        FROM payments p WHERE p.done_by = 'PLUTUS' AND p.executed_at > {TODAY} - {PLUTUS_DAYS}
        ORDER BY 1""").fetchdf()
    wallet = []
    if len(ev):
        bal = committed = 0.0
        month = None
        open_commit = {}
        pay_by_month = ev[ev["kind"] == "payment"].copy()
        pay_by_month["m"] = pay_by_month["at"].dt.to_period("M")
        need = pay_by_month.groupby("m")["amount"].sum().abs()
        for _, r in ev.iterrows():
            m = r["at"].to_period("M")
            if m != month:
                month = m
                want = float(need.get(m, 0.0)) * 1.1 + 5000
                dep = max(0.0, np.ceil((want - bal) / 1000) * 1000)
                if dep:
                    bal += dep
                    wallet.append((r["at"].replace(day=1, hour=8, minute=0, second=0), "deposit", dep, bal, committed,
                                   "bank", None, None, "monthly top-up", "ADMIN"))
            if r["kind"] == "order":
                committed += -r["amount"]
                open_commit[r["ref_id"]] = -r["amount"]
            else:
                bal += r["amount"]
                released = open_commit.pop(r["ref_id"], 0.0)
                committed -= released
            wallet.append((r["at"], r["kind"], r["amount"], bal, round(committed, 2), r["ref_kind"], int(r["ref_id"]),
                           r["supplier"], "committed to an order" if r["kind"] == "order" else "bill paid", "PLUTUS"))
    wdf = pd.DataFrame(wallet, columns=["at", "kind", "amount", "bal", "committed", "ref_kind", "ref_id", "supplier",
                                        "note", "by"])
    if len(wdf):
        wdf = wdf.sort_values("at", kind="stable").reset_index(drop=True)
        wdf["entry_id"] = np.arange(1, len(wdf) + 1)
        con.execute("""INSERT INTO wallet_entries (entry_id, happened_at, kind, amount, balance_after, committed_after,
                       ref_kind, ref_id, supplier, note, done_by)
                       SELECT entry_id, "at", kind, amount, bal, committed, ref_kind, ref_id, supplier, note, "by" FROM wdf""")
        bump(con, "wallet_seq", len(wdf))
    sql(con, f"""
        INSERT INTO commitments (commit_id, supplier, amount, ref_kind, ref_id, made_on, state, released_on)
        SELECT row_number() OVER (ORDER BY po.list_id), s.name, round(po.total, 2), 'po', po.list_id, {D} + po.ordered,
               CASE WHEN po.day > {today_off} THEN 'open' ELSE 'released' END,
               CASE WHEN po.day <= {today_off} THEN {D} + po.day END
        FROM t_po po JOIN t_sup s USING (supplier_id) WHERE po.total <= 2500""", "commitments")
    bump(con, "commitment_seq", con.execute("SELECT count(*) FROM commitments").fetchone()[0])

    # ---- quotes and agreements ---------------------------------------------------------------------------------
    from inventory.simulation_catalogue import QUOTE_LINES

    nq = int(args.years * 260)
    qd = pd.DataFrame({
        "quote_id": np.arange(1, nq + 1),
        "supplier": [sup[i] for i in nrng.integers(0, len(sup), nq)],
        "site": [sites[min(i, len(sites) - 1)][0] for i in nrng.integers(0, len(sites), nq)],
        "day": np.sort(nrng.integers(0, today_off, nq)),
        "state": nrng.choice(["open", "sent", "answered", "closed"], nq, p=[0.05, 0.1, 0.25, 0.6])})
    recent = qd["day"] > today_off - 60
    qd.loc[recent, "state"] = nrng.choice(["open", "sent", "answered", "closed"], int(recent.sum()), p=[0.25, 0.3, 0.3, 0.15])
    qd["needed"] = qd["day"] + nrng.integers(7, 45, nq)
    qd["asked"] = [datetime.combine(date.fromordinal(base_ord + int(d)), datetime.min.time()) + timedelta(hours=9) for d in qd["day"]]
    con.execute("""INSERT INTO quotes (quote_id, supplier, site, work_item, needed_by, state, note, asked_at, asked_by)
                   SELECT quote_id, supplier, site, NULL, DATE '%s' + CAST(needed AS INTEGER), state, NULL, asked,
                          ['ALEX','MARIE','SAM','LEO'][1 + quote_id %% 4] FROM qd""" % base)
    bump(con, "quote_seq", nq)
    ql = []
    for qid in qd["quote_id"]:
        for ln in range(1, int(nrng.integers(1, 4)) + 1):
            art, qty, unit = QUOTE_LINES[int(nrng.integers(0, len(QUOTE_LINES)))]
            ql.append((int(qid), ln, art, float(qty) * float(nrng.uniform(0.5, 3)), unit, ""))
    qdf = pd.DataFrame(ql, columns=["quote_id", "line_no", "article", "quantity", "unit", "designation"])
    con.execute("INSERT INTO quote_lines SELECT quote_id, line_no, article, round(quantity, 1), unit, designation FROM qdf")
    con.execute(f"""
        INSERT INTO quote_offers (offer_id, quote_id, line_no, supplier, unit_price, lead_days, valid_until, note, received_on, created_by)
        SELECT row_number() OVER (ORDER BY l.quote_id, l.line_no), l.quote_id, l.line_no, q.supplier,
               round(5 + (hash(l.quote_id * 13 + l.line_no) % 4000) / 10.0, 2), 3 + CAST(hash(l.quote_id) % 25 AS INTEGER),
               q.needed_by + 30, NULL, CAST(q.asked_at + INTERVAL 2 DAY AS DATE), 'MARIE'
        FROM quote_lines l JOIN quotes q USING (quote_id) WHERE q.state IN ('answered', 'closed')""")
    bump(con, "offer_seq", con.execute("SELECT count(*) FROM quote_offers").fetchone()[0])
    sql(con, f"""
        INSERT INTO agreements (agreement_id, article, supplier, unit_price, lead_days, valid_from, valid_to, quote_id, state,
                                created_by, created_at)
        SELECT row_number() OVER (ORDER BY t.article_id), a.name, a.supplier1, round(p.price * 0.97, 2),
               3 + CAST(hash(a.article_id) % 10 AS INTEGER), {TODAY} - 120, {TODAY} + 245, NULL, 'active', 'ADMIN', now()
        FROM (SELECT article_id, sum(qty) q FROM t_out WHERE day > {today_off - 365} GROUP BY 1 ORDER BY q DESC
              LIMIT {max(50, args.articles // 25)}) t
             JOIN t_art a USING (article_id)
             JOIN (SELECT article, supplier, arg_max(unit_price, seen_on) price FROM article_prices GROUP BY 1, 2) p
                  ON p.article = a.name AND p.supplier = a.supplier1""", "agreements")
    bump(con, "agreement_seq", con.execute("SELECT count(*) FROM agreements").fetchone()[0])

    # ---- counts, moves, reservations, requisitions, queue --------------------------------------------------------
    sql(con, f"""
        INSERT INTO stock_counts (article, quantity, counted_on, counted_by, noted_at)
        SELECT name, round(greatest(0, oh * (1 + (CAST(hash(article_id * 3) % 61 AS INTEGER) - 30) / 1000.0)), 1),
               {TODAY} - CAST(hash(article_id * 7) % 80 AS INTEGER), ['MARIE','LEO','INES','KARIM'][1 + CAST(hash(article_id) % 4 AS INTEGER)],
               CAST({TODAY} - CAST(hash(article_id * 7) % 80 AS INTEGER) AS TIMESTAMP) + INTERVAL 11 HOUR
        FROM t_oh WHERE oh > 0 AND hash(article_id * 11) % 100 < 35""", "stock_counts")
    sql(con, f"""
        INSERT INTO stock_moves (move_id, article, site, quantity, kind, reason, ref, moved_on, created_at, created_by)
        SELECT row_number() OVER (ORDER BY c.article), c.article, NULL, round(c.quantity - o.oh, 1), 'adjust', 'cycle count', NULL,
               c.counted_on, CAST(c.counted_on AS TIMESTAMP) + INTERVAL 11 HOUR, c.counted_by
        FROM stock_counts c JOIN t_oh o ON o.name = c.article WHERE abs(c.quantity - o.oh) > 0.05""")
    sql(con, f"""
        INSERT INTO stock_moves (move_id, article, site, quantity, kind, reason, ref, moved_on, created_at, created_by)
        SELECT (SELECT coalesce(max(move_id), 0) FROM stock_moves) + row_number() OVER (ORDER BY o.article_id), o.name, NULL,
               -round(o.oh * 0.02, 1), 'adjust', ['breakage', 'damaged in handling', 'scrap', 'lost'][1 + CAST(hash(o.article_id) % 4 AS INTEGER)],
               NULL, {TODAY} - 100 - CAST(hash(o.article_id) % 600 AS INTEGER), CAST({TODAY} - 100 AS TIMESTAMP), 'LEO'
        FROM t_oh o WHERE o.oh > 100 AND hash(o.article_id * 17) % 100 < 6""")
    nm = con.execute("SELECT max(move_id) FROM stock_moves").fetchone()[0] or 0
    bump(con, "stock_move_seq", nm)
    sql(con, f"""
        INSERT INTO count_tasks (article, due_on, state)
        SELECT name, {TODAY}, 'open' FROM t_oh WHERE oh > 0 AND shipped > 0 ORDER BY shipped DESC LIMIT {max(20, args.articles // 400)}""")
    sql(con, f"""
        INSERT INTO count_checks (check_id, article, expected, first_count, counted_by, asked_at, state, second_count, explanation, closed_at)
        SELECT row_number() OVER (ORDER BY c.article), c.article, o.oh, round(o.oh * 0.8, 1), 'LEO',
               CAST({TODAY} - CAST(hash(o.article_id) % 200 AS INTEGER) AS TIMESTAMP),
               CASE WHEN hash(o.article_id) % 3 = 0 THEN 'recount' ELSE 'settled' END,
               CASE WHEN hash(o.article_id) % 3 = 0 THEN NULL ELSE round(o.oh * 0.8, 1) END, 'a pallet was moved to the site',
               CASE WHEN hash(o.article_id) % 3 = 0 THEN NULL ELSE CAST({TODAY} - CAST(hash(o.article_id) % 200 AS INTEGER) AS TIMESTAMP) + INTERVAL 1 DAY END
        FROM t_oh o JOIN stock_counts c ON c.article = o.name WHERE o.oh > 20 AND hash(o.article_id * 5) % 100 < 2""")
    bump(con, "count_check_seq", con.execute("SELECT count(*) FROM count_checks").fetchone()[0])
    sql(con, f"""
        INSERT INTO reservations (reservation_id, article, site, work_item, quantity, needed_by, note, state, created_on, created_at, created_by)
        SELECT row_number() OVER (ORDER BY o.article_id), o.name, st.name, NULL, greatest(1, round(o.oh * 0.2)), {TODAY} + 3 + CAST(hash(o.article_id) % 30 AS INTEGER),
               NULL, 'active', {TODAY} - CAST(hash(o.article_id) % 10 AS INTEGER), now(), 'ALEX'
        FROM t_oh o JOIN t_site st ON st.site_id = hash(o.article_id) % 14
        WHERE o.oh > 10 AND hash(o.article_id * 13) % 1000 < {max(2, 300000 // max(1, args.articles) * 0 + 8)}""", "reservations")
    bump(con, "reservation_seq", con.execute("SELECT count(*) FROM reservations").fetchone()[0])
    sql(con, f"""
        INSERT INTO requisitions (req_id, requester, site, work_item, needed_by, state, note, gate_request_id, created_at, closed_at)
        SELECT row_number() OVER (ORDER BY list_id), drafter, site, work_item, list_date + 5,
               CASE WHEN list_date > {TODAY} - 14 THEN 'open' ELSE 'closed' END, NULL, NULL, created_at,
               CASE WHEN list_date <= {TODAY} - 14 THEN created_at + INTERVAL 2 DAY END
        FROM purchase_orders WHERE drafter <> 'PLUTUS' AND list_date > {TODAY} - 365 AND list_id % 40 = 0""", "requisitions")
    sql(con, """
        INSERT INTO requisition_lines (req_id, line_no, article, quantity, reserved, reservation_id, to_order)
        SELECT r.rn, l.line_no, l.article, l.quantity, CASE WHEN l.line_no % 3 = 0 THEN 0 ELSE l.quantity END, NULL,
               CASE WHEN l.line_no % 3 = 0 THEN l.quantity ELSE 0 END
        FROM (SELECT req_id, row_number() OVER (ORDER BY req_id) rn FROM requisitions) r
             JOIN (SELECT list_id, row_number() OVER (ORDER BY list_id) rn FROM purchase_orders WHERE drafter <> 'PLUTUS'
                   AND list_date > CURRENT_DATE - 365 AND list_id % 40 = 0) x ON x.rn = r.rn
             JOIN purchase_order_lines l ON l.list_id = x.list_id""")
    bump(con, "requisition_seq", con.execute("SELECT count(*) FROM requisitions").fetchone()[0])

    # ---- sites on the map, vans, dispatches --------------------------------------------------------------------------
    con.execute("DELETE FROM places")
    con.execute("INSERT INTO places (kind, label, address, lat, lon, added_by) VALUES "
                "('depot', 'THE YARD', '8 rue des Forges, 33520 BRUGES', 44.8801, -0.6102, 'SAMPLE')")
    from inventory.simulation_catalogue import STOPS

    known = {s[0]: s for s in STOPS}
    for si, s in enumerate(sites):
        if s[3] < S["weeks_hist"] - 12 and s[0] not in known:
            continue
        lat, lon = (known[s[0]][2], known[s[0]][3]) if s[0] in known else (
            44.84 + rng.uniform(-0.09, 0.09), -0.58 + rng.uniform(-0.12, 0.10))
        con.execute("INSERT INTO places (kind, label, address, lat, lon, added_by) VALUES ('stop', ?, ?, ?, ?, 'SAMPLE')",
                    [s[0], f"{rng.randint(1, 120)} rue de {s[0].title()}, 33000 BORDEAUX", lat, lon])
    for k in range(4, 9):
        con.execute("INSERT INTO vehicles (name, shape, plate, cubic, kilos, note) VALUES (?, ?, ?, ?, ?, NULL)",
                    [f"Renault Master {k - 3}", "van", f"GH-{rng.randint(100, 999)}-{rng.choice(['AB', 'CD', 'EF'])}", 12.0, 1200.0])
    nv = con.execute("SELECT count(*) FROM vehicles").fetchone()[0]
    bump(con, "vehicle_seq", nv)
    sql(con, f"""
        INSERT INTO dispatches (dispatch_id, list_id, site, work_item, vehicle_id, planned_on, state, loaded_at, delivered_at,
                                received_by, note, created_at, created_by)
        SELECT row_number() OVER (ORDER BY list_id), list_id, site, work_item, 1 + CAST(hash(list_id) % {nv} AS INTEGER),
               list_date + 1 + CASE WHEN dayofweek(list_date) = 5 THEN 2 WHEN dayofweek(list_date) = 6 THEN 1 ELSE 0 END,
               CASE WHEN list_date >= {TODAY} - 1 THEN 'planned' WHEN list_date >= {TODAY} - 2 THEN 'loaded'
                    WHEN hash(list_id) % 100 < 2 THEN 'part delivered' ELSE 'delivered' END,
               CASE WHEN list_date < {TODAY} - 1 THEN CAST(list_date + 1 AS TIMESTAMP) + INTERVAL 7 HOUR END,
               CASE WHEN list_date < {TODAY} - 2 THEN CAST(list_date + 1 AS TIMESTAMP) + INTERVAL 10 HOUR + INTERVAL 1 MINUTE * CAST(hash(list_id) % 240 AS INTEGER) END,
               CASE WHEN list_date < {TODAY} - 2 THEN ['JEAN','PAUL','MEHDI','SARAH','OLIVIER'][1 + CAST(hash(list_id) % 5 AS INTEGER)] END,
               NULL, created_at, 'AUTO'
        FROM purchase_orders WHERE drafter <> 'PLUTUS' AND site IS NOT NULL AND list_date > {TODAY} - {args.dispatch_days}
              AND hash(list_id) % 100 < 60""", "dispatches")
    sql(con, """
        INSERT INTO dispatch_lines (dispatch_id, line_no, article, designation, quantity, delivered)
        SELECT d.dispatch_id, l.line_no, l.article, l.designation, l.quantity,
               CASE WHEN d.state IN ('delivered', 'part delivered') THEN l.quantity END
        FROM dispatches d JOIN purchase_order_lines l USING (list_id)""", "dispatch_lines")
    bump(con, "dispatch_seq", con.execute("SELECT count(*) FROM dispatches").fetchone()[0])

    # ---- the receiving dock: what came in the last days -------------------------------------------------------------
    sql(con, f"""
        INSERT INTO putaways (putaway_id, note_id, line_no, article, site, supplier, quantity, suggested, location, state,
                              check_word, check_detail, created_at, done_at, done_by)
        SELECT row_number() OVER (ORDER BY n.note_id, n.line_no), n.note_id, n.line_no, a.name, st.name, s.name, n.qty,
               coalesce(h.location, 'DOCK'), CASE WHEN n.day < {today_off - 1} THEN coalesce(h.location, 'DOCK') END,
               CASE WHEN n.day < {today_off - 1} THEN 'put away' ELSE 'to put away' END,
               'as ordered', NULL, CAST({D} + n.day AS TIMESTAMP) + INTERVAL 8 HOUR,
               CASE WHEN n.day < {today_off - 1} THEN CAST({D} + n.day AS TIMESTAMP) + INTERVAL 15 HOUR END,
               CASE WHEN n.day < {today_off - 1} THEN 'JEAN' END
        FROM t_nl n JOIN t_art a USING (article_id) JOIN t_sup s USING (supplier_id) JOIN t_site st USING (site_id)
             LEFT JOIN article_homes h ON h.article = a.name AND h.site = ''
        WHERE n.day > {today_off - 30} AND NOT n.bo""", "putaways")
    bump(con, "putaway_seq", con.execute("SELECT count(*) FROM putaways").fetchone()[0])
    sql(con, f"""
        INSERT INTO order_queue (queue_id, list_id, line_no, article, designation, reference, quantity, site, work_item, queued_on,
                                 due_on, state, ordered_on, ordered_list)
        SELECT row_number() OVER (ORDER BY l.list_id, l.line_no), l.list_id, l.line_no, l.article, l.designation, NULL, l.quantity,
               h.site, h.work_item, h.list_date, h.list_date + 15, 'waiting', NULL, NULL
        FROM purchase_order_lines l JOIN purchase_orders h USING (list_id)
        WHERE h.drafter <> 'PLUTUS' AND h.list_date > {TODAY} - 12 AND l.line_no = 1 AND h.list_id % 7 = 0""")
    bump(con, "order_queue_seq", con.execute("SELECT count(*) FROM order_queue").fetchone()[0])

    # ---- calendar, exchange rates, users -----------------------------------------------------------------------------
    hol = S["hol"]
    con.executemany("INSERT INTO calendar_days VALUES (?, 'closed', ?)", [(d, n) for d, n in hol.items()
                                                                          if date.fromordinal(base_ord) <= d <= today + timedelta(days=120)])
    rows, rate = [], {"USD": 1.1, "GBP": 0.85, "CHF": 0.97}
    d = date.fromordinal(base_ord)
    while d <= today:
        if d.weekday() < 5:
            for cur in rate:
                rate[cur] = max(0.5, rate[cur] * (1 + nrng.normal(0, 0.003)))
                rows.append((d, cur, round(rate[cur], 4)))
        d += timedelta(days=1)
    con.executemany("INSERT INTO fx_rates VALUES (?, ?, ?)", rows)
    for who in ACTORS:
        con.execute("INSERT INTO users (user_name, created_at, last_seen_at) VALUES (?, now(), now()) ON CONFLICT DO NOTHING", [who])
    con.execute("INSERT INTO bill_to_default VALUES (1, ?, ?, ?)", list(FIRM))
    sql(con, f"""
        INSERT INTO feedback (feedback_id, article, kind, proposed, final, detail, happened_at, done_by)
        SELECT row_number() OVER (ORDER BY article_id), name, ['stockout', 'count_short', 'refused'][1 + CAST(hash(article_id) % 3 AS INTEGER)],
               round(shipped / 50, 1), round(shipped / 40, 1), 'from the shelf', {TODAY} - CAST(hash(article_id) % 300 AS INTEGER),
               'LEO' FROM t_oh WHERE shipped > 0 AND hash(article_id * 3) % 100 < 1""", "feedback")
    bump(con, "feedback_seq", con.execute("SELECT count(*) FROM feedback").fetchone()[0])
    sql(con, """
        INSERT INTO tuning (article, qty_factor, safety_factor, samples, last_reason, updated_at)
        SELECT article, 1.1, 1.25, count(*), 'stockout', now() FROM feedback WHERE kind = 'stockout' GROUP BY article""")

    return sealed(S, args, scope, con, D, TODAY, base, today_off)


def sealed(S, args, scope, con, D, TODAY, base, today_off):
    """The gate log, the audit chain, the documents read, and what went wrong."""
    from inventory import audit, gate

    rng, nrng = S["rng"], S["nrng"]
    sup = S["supplier_list"]
    # -- the gate: baskets held and decided, sealed one after the other
    n_req = int(args.years * 200)
    reasons_pool = ["first-article", "new-supplier", "over-ceiling", "dear-line", "no-price"]
    ar = con.execute("SELECT name, supplier1 FROM t_art USING SAMPLE 3000").fetchall()
    price = dict(con.execute("SELECT article, arg_max(unit_price, seen_on) FROM article_prices GROUP BY article").fetchall())
    req_rows, log_rows, prev = [], [], gate.GENESIS
    stamp0 = datetime.combine(date.fromordinal(S["base_ord"]), datetime.min.time())
    times = np.sort(nrng.integers(0, today_off * 86400 // 1, n_req))
    for rid in range(1, n_req + 1):
        asked = stamp0 + timedelta(seconds=int(times[rid - 1])) + timedelta(hours=8)
        site = S["sites"][int(nrng.integers(0, len(S["sites"])))]
        who = DRAFTERS[int(nrng.integers(0, len(DRAFTERS)))]
        pick = [ar[int(i)] for i in nrng.integers(0, len(ar), int(nrng.integers(1, 5)))]
        lines = []
        for name, supplier in pick:
            p = float(price.get(name) or 5.0)
            qty = float(nrng.choice([1, 2, 4, 6, 12, 24, 100]))
            lines.append({"article": name, "quantity": qty, "unit_price": round(p, 2), "supplier": supplier,
                          "found": [], "usual": round(p, 2), "site": site[0], "work_item": site[1][0], "shop": "westfix.fr",
                          "added_by": who, "added_at": asked.isoformat(timespec="minutes")})
        total = round(sum(l["quantity"] * l["unit_price"] for l in lines), 2)
        reasons = [r for r in reasons_pool if nrng.random() < 0.2] or ["over-ceiling"]
        age = (today_off * 86400 - int(times[rid - 1])) / 86400
        state = "cancelled" if nrng.random() < 0.15 else "ordered" if age > 3 else ("waiting" if nrng.random() < 0.5 else "checked")
        verdict = "same" if "first-article" not in reasons else "new"
        body = {"channel": f"C0{rid}", "lines": lines, "reasons": reasons, "shop": "westfix.fr", "site": site[0],
                "total": total, "work_item": site[1][0]}
        t_req = asked.isoformat(timespec="seconds")
        seal = gate._digest(prev, rid, "requested", who, t_req, body)
        log_rows.append((rid, "requested", gate._canon(body), who, asked, prev, seal))
        prev = seal
        agent_at = None
        if state != "waiting":
            agent_at = asked + timedelta(seconds=40)
            cb = {"matches": [{"asked": l["article"], "verdict": verdict} for l in lines], "said": "the lines match the shelf.",
                  "verdict": verdict}
            seal = gate._digest(prev, rid, "checked", "agent", agent_at.isoformat(timespec="seconds"), cb)
            log_rows.append((rid, "checked", gate._canon(cb), "agent", agent_at, prev, seal))
            prev = seal
        decided = None
        if state in ("ordered", "cancelled"):
            decided = asked + timedelta(hours=3)
            ev = "approved" if state == "ordered" else "cancelled"
            db_body = {"by": "ADMIN"} if state == "cancelled" else {"by": "ADMIN"}
            seal = gate._digest(prev, rid, ev, "ADMIN", decided.isoformat(timespec="seconds"), db_body)
            log_rows.append((rid, ev, gate._canon(db_body), "ADMIN", decided, prev, seal))
            prev = seal
        req_rows.append((rid, f"C0{rid}", "westfix.fr", who, site[0], site[1][0], ",".join(reasons), gate._canon(lines), total, state,
                         verdict if state != "waiting" else None, "the lines match the shelf." if state != "waiting" else None,
                         agent_at, "ADMIN" if decided else None, decided, asked))
    con.executemany("INSERT INTO gate_requests VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", req_rows)
    con.executemany("INSERT INTO gate_log VALUES (?,?,?,?,?,?,?,?)",
                    [(i + 1, r[0], r[1], r[2], r[3], r[4], r[5], r[6]) for i, r in enumerate(log_rows)])
    bump(con, "gate_seq", n_req)
    bump(con, "seal_seq", len(log_rows))
    say(f"gate: {n_req} requests, {len(log_rows)} sealed entries")
    for i, (name, supplier) in enumerate(ar[:15]):
        p = float(price.get(name) or 5.0)
        con.execute("INSERT INTO baskets (line_id, channel, shop, article, quantity, site, work_item, note, added_by, added_at, supplier, unit_price, shop_ref) "
                    "VALUES (?, ?, 'northgate.fr', ?, ?, ?, ?, NULL, ?, now(), ?, ?, NULL)",
                    [i + 1, f"C0LIVE{i % 3}", name, float(nrng.choice([2, 6, 12])), S["sites"][i][0], S["sites"][i][1][0],
                     DRAFTERS[i % 4], supplier, round(p, 2)])
    bump(con, "basket_seq", 15)

    # -- documents read, and what went wrong
    sql(con, f"""
        INSERT INTO readings (reading_id, measured_at, document, fields, corrected, lines)
        SELECT row_number() OVER (ORDER BY note_id), CAST({D} + day AS TIMESTAMP) + INTERVAL 9 HOUR,
               ['delivery_note', 'delivery_note', 'bill'][1 + CAST(hash(note_id) % 3 AS INTEGER)], 8 + n,
               CASE WHEN hash(note_id * 3) % 100 < 9 THEN 1 + CAST(hash(note_id) % 3 AS INTEGER) ELSE 0 END, n
        FROM t_note WHERE day > {today_off - 730}""", "readings")
    bump(con, "reading_seq", con.execute("SELECT count(*) FROM readings").fetchone()[0])
    sql(con, f"""
        INSERT INTO intake (intake_id, source, name, media_type, sha256, path, bytes, sender, subject, arrived_at, state, note)
        SELECT row_number() OVER (ORDER BY note_id), ['mailbox', 'drop', 'phone'][1 + CAST(hash(note_id) % 3 AS INTEGER)],
               'BL_' || note_id || '.jpg', 'image/jpeg', md5('note' || note_id), '', 900000 + hash(note_id) % 900000,
               'livraison@example.com', 'Bon de livraison', CAST({D} + day AS TIMESTAMP) + INTERVAL 8 HOUR, 'done', NULL
        FROM t_note WHERE day > {today_off - 400}""", "intake")
    bump(con, "intake_seq", con.execute("SELECT count(*) FROM intake").fetchone()[0])
    for i in range(300):
        d = datetime.now() - timedelta(days=int(nrng.integers(0, 900)), hours=int(nrng.integers(0, 24)))
        con.execute("INSERT INTO errors (error_no, happened_at, kind, description, context, user_name, sms_status) VALUES (?,?,?,?,?,?, 'sent')",
                    [i + 1, d, ["request", "monitor", "backup"][i % 3], ["a photo could not be read", "mailbox timeout",
                     "a bill without a number"][i % 3], None, ACTORS[i % 9]])
    bump(con, "error_seq", 300)

    scan_pages(S, args, con, base, today_off)
    # -- the audit chain: every note, order and bill is an event
    return audited(S, args, scope, con, D, TODAY, base, today_off)


def scan_pages(S, args, con, base, today_off):
    """Archived pages for the most recent notes and bills older than a month; the rest have no photo."""
    from make_big_photos import archive

    con.execute("UPDATE purchase_orders SET image_sha256 = NULL, image_path = NULL")
    con.execute("UPDATE notes SET image_sha256 = NULL, image_path = NULL")
    con.execute("UPDATE bills SET image_sha256 = NULL, image_path = NULL")
    if not args.scans:
        return
    n_bills = int(args.scans * 0.12)
    notes = con.execute(f"""SELECT n.note_id, s.name, n.day, n.body, n.total FROM t_note n JOIN t_sup s USING (supplier_id)
                            WHERE n.day <= {today_off - 30} ORDER BY n.day DESC LIMIT {args.scans - n_bills}""").fetchall()
    bills = con.execute(f"""SELECT bill_id, supplier, number, bill_date, net, tax, total FROM bills
                            WHERE source = 'scan' AND bill_date <= DATE '{S['today']}' - 30 ORDER BY bill_date DESC
                            LIMIT {n_bills}""").fetchall()
    got = archive(con, today_off, base, notes, bills, args.grain, say)
    say(f"scans: {got} pages archived, {con.execute('SELECT round(sum(octet_length(data)) / 1e9, 2) FROM photo_archive').fetchone()[0]} GB")


def audited(S, args, scope, con, D, TODAY, base, today_off):
    from inventory import audit

    say("audit: collecting events")
    df = con.execute(f"""
        SELECT happened AS "at", who, action, target, detail FROM (
            SELECT CAST({D} + min(day) AS TIMESTAMP) + INTERVAL 16 HOUR + INTERVAL 1 SECOND * CAST(note_id % 3600 AS INTEGER) AS happened,
                   ['JEAN','PAUL','MEHDI','SARAH','OLIVIER'][1 + CAST(hash(note_id) % 5 AS INTEGER)] AS who, 'note.save' AS action,
                   'note ' || note_id AS target, count(*) || ' line(s)' AS detail FROM t_nl GROUP BY note_id
            UNION ALL
            SELECT created_at + INTERVAL 1 SECOND * CAST(list_id % 50 AS INTEGER), created_by, 'order.save', 'order ' || list_id,
                   NULL FROM purchase_orders
            UNION ALL
            SELECT CAST(added_at AS TIMESTAMP), 'MARIE', 'bill.save', 'bill ' || bill_id, supplier FROM bills
            UNION ALL
            SELECT executed_at, done_by, 'payment.done', 'bill ' || bill_id, supplier FROM payments)
        ORDER BY happened""").fetchdf()
    say(f"audit: {len(df)} events, sealing")
    from inventory import vault
    key = vault._key()
    prev = None
    out = []
    ats = df["at"].to_numpy()
    for i, (at, who, action, target, detail) in enumerate(zip(df["at"], df["who"], df["action"], df["target"], df["detail"]), 1):
        det = detail if isinstance(detail, str) and detail else None
        at = pd.Timestamp(at).to_pydatetime()
        stem = "\x1f".join([prev or "", str(i), at.isoformat(), who, action, target or "", det or "", ""])
        digest = hmac.new(key, stem.encode("utf-8"), hashlib.sha256).hexdigest()
        out.append((i, at, who, action, target, det, None, prev, digest))
        prev = digest
        if len(out) >= 250000:
            _flush(con, out)
            out = []
    _flush(con, out)
    bump(con, "audit_seq", len(df))
    return wrap(S, args, scope, con, D, TODAY, base, today_off)


def _flush(con, rows):
    if rows:
        d = pd.DataFrame(rows, columns=["event_id", "happened_at", "user_name", "action", "target", "detail", "address",
                                        "prev_hash", "row_hash"])
        con.execute("INSERT INTO audit SELECT * FROM d")


def wrap(S, args, scope, con, D, TODAY, base, today_off):
    for t in [r[0] for r in con.execute("SELECT table_name FROM information_schema.tables WHERE table_name LIKE 't\\_%' ESCAPE '\\'").fetchall()]:
        con.execute(f"DROP TABLE {t}")
    con.execute("CHECKPOINT")
    con.execute("VACUUM")
    con.execute("CHECKPOINT")
    say("done")
    rows = con.execute("""SELECT table_name, estimated_size, column_count FROM duckdb_tables() ORDER BY estimated_size DESC""").fetchall()
    for name, n, c in rows[:18]:
        print(f"  {name:<22} {n:>12,} rows")
    return S
