"""Paying supplier bills out of the wallet.

Paying is in two parts, and only the first is this program's. A payment is *instructed*: the
wallet is debited, the supplier's commitment is freed, and the payment goes into the open batch.
A batch becomes a SEPA credit-transfer file (pain.001) that is uploaded to the bank, or handed to
a connector. Only when the batch is confirmed as executed does the bill turn paid. Nothing here
claims money has moved before then.
"""

import re
from datetime import date, datetime
from xml.sax.saxutils import escape

from . import audit, bills, db, wallet

IBAN = re.compile(r"^[A-Z]{2}\d{2}[A-Z0-9]{10,30}$")


def valid_iban(text):
    iban = re.sub(r"\s+", "", str(text or "")).upper()
    if not IBAN.match(iban):
        return None
    rearranged = iban[4:] + iban[:4]
    digits = "".join(str(int(ch, 36)) for ch in rearranged)
    return iban if int(digits) % 97 == 1 else None


def set_bank(db_path, supplier, iban, bic=None, holder=None):
    clean = valid_iban(iban)
    if clean is None:
        raise ValueError("That is not a valid IBAN.")
    name = (supplier or "").strip().upper()
    if not name:
        raise ValueError("No supplier named.")
    db.write(db_path, """
            INSERT INTO supplier_bank (supplier, iban, bic, holder) VALUES (?, ?, ?, ?)
            ON CONFLICT (supplier) DO UPDATE SET iban = excluded.iban, bic = excluded.bic,
                holder = excluded.holder RETURNING supplier""", [name, clean, (bic or "").strip() or None,
                                                                 (holder or "").strip() or None])
    return name


def banks(db_path):
    return [{"supplier": s, "iban": i, "bic": b, "holder": h} for s, i, b, h in db.read(
        db_path, "SELECT supplier, iban, bic, holder FROM supplier_bank ORDER BY supplier")]


def bank_for(db_path, supplier):
    rows = db.read(db_path, "SELECT iban, bic, holder FROM supplier_bank WHERE upper(supplier) = upper(?)",
                   [supplier or ""])
    return {"iban": rows[0][0], "bic": rows[0][1], "holder": rows[0][2]} if rows else None


def _open_batch(db_path):
    rows = db.read(db_path, "SELECT batch_id FROM payment_batches WHERE state = 'open' ORDER BY batch_id LIMIT 1")
    if rows:
        return rows[0][0]
    return db.write(db_path, "INSERT INTO payment_batches DEFAULT VALUES RETURNING batch_id")[0]


def instruct(db_path, bill_id, amount, user="PLUTUS"):
    """Instruct the payment of a bill: debit the wallet, put it in the open batch."""
    bill = bills.one(db_path, bill_id)
    if bill is None:
        raise ValueError(f"No bill #{bill_id}.")
    if bill["state"] != "to pay":
        raise ValueError(f"Bill #{bill_id} is {bill['state']}, not to pay.")
    if db.read(db_path, "SELECT 1 FROM payments WHERE bill_id = ? AND state IN ('instructed', 'executed')",
               [bill_id]):
        raise ValueError(f"Bill #{bill_id} already has a payment.")
    bank = bank_for(db_path, bill["supplier"])
    if bank is None:
        raise ValueError(f"No bank details for {bill['supplier']}.")
    wallet.pay(db_path, bill["supplier"], amount, bill_id, user)
    batch = _open_batch(db_path)
    reference = f"{bill['supplier'][:20]} {bill.get('number') or bill_id}"[:35]
    row = db.write(db_path, """
            INSERT INTO payments (bill_id, supplier, amount, currency, iban, batch_id, reference, done_by)
            VALUES (?, ?, ?, 'EUR', ?, ?, ?, ?) RETURNING payment_id""",
                   [bill_id, bill["supplier"], round(float(amount), 2), bank["iban"], batch, reference, user])
    bills.update(db_path, bill_id, {"note": f"payment #{row[0]} instructed in batch #{batch}"})
    audit.record(db_path, user, "payment.instruct", f"bill #{bill_id}", f"{float(amount):.2f} EUR in batch #{batch}")
    return {"payment_id": row[0], "batch_id": batch}


def cancel(db_path, payment_id, user=None):
    """Take a payment back out of a batch that has not been exported: the money returns."""
    row = db.read(db_path, """
            SELECT p.bill_id, p.supplier, p.amount, b.state FROM payments p
            JOIN payment_batches b USING (batch_id) WHERE p.payment_id = ? AND p.state = 'instructed'""",
                  [payment_id])
    if not row or row[0][3] != "open":
        raise ValueError("Only a payment in a batch not yet exported can be cancelled.")
    bill_id, supplier, amount, _ = row[0]
    db.write(db_path, "UPDATE payments SET state = 'cancelled' WHERE payment_id = ? RETURNING payment_id", [payment_id])
    wallet.refund(db_path, supplier, amount, bill_id, "payment cancelled", user)
    bills.update(db_path, bill_id, {"note": "payment cancelled"})
    return True


def reject(db_path, batch_id, why="rejected by the bank", user=None):
    """The bank refused a batch it was given: every payment in it is cancelled and its money returns."""
    for payment_id, bill_id, supplier, amount in db.read(db_path, """
            SELECT payment_id, bill_id, supplier, amount FROM payments
            WHERE batch_id = ? AND state = 'instructed'""", [batch_id]):
        db.write(db_path, "UPDATE payments SET state = 'cancelled' WHERE payment_id = ? RETURNING payment_id",
                 [payment_id])
        wallet.refund(db_path, supplier, amount, bill_id, why, user)
        bills.update(db_path, bill_id, {"note": why})
    db.write(db_path, "UPDATE payment_batches SET state = 'rejected' WHERE batch_id = ? RETURNING batch_id",
             [batch_id])
    audit.record(db_path, user or "PAYMENTS", "payment.reject", f"batch #{batch_id}", why)
    return True


def batch_file(db_path, batch_id, payer_name, payer_iban, payer_bic=None, now=None):
    """The batch as a SEPA credit transfer initiation (pain.001.001.03), and mark it exported."""
    pays = db.read(db_path, """
            SELECT payment_id, supplier, amount, iban, reference FROM payments
            WHERE batch_id = ? AND state = 'instructed' ORDER BY payment_id""", [batch_id])
    if not pays:
        raise ValueError("That batch holds nothing to pay.")
    debtor = valid_iban(payer_iban)
    if debtor is None or not (payer_name or "").strip():
        raise ValueError("The paying account needs a name and a valid IBAN (Settings, Wallet).")
    now = now or datetime.now()
    total = round(sum(float(p[2]) for p in pays), 2)
    stamp = now.strftime("%Y-%m-%dT%H:%M:%S")
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<Document xmlns="urn:iso:std:iso:20022:tech:xsd:pain.001.001.03">',
        '<CstmrCdtTrfInitn>',
        f'<GrpHdr><MsgId>STOCK-{batch_id}-{now.strftime("%Y%m%d%H%M%S")}</MsgId><CreDtTm>{stamp}</CreDtTm>'
        f'<NbOfTxs>{len(pays)}</NbOfTxs><CtrlSum>{total:.2f}</CtrlSum>'
        f'<InitgPty><Nm>{escape(payer_name.strip()[:70])}</Nm></InitgPty></GrpHdr>',
        f'<PmtInf><PmtInfId>BATCH-{batch_id}</PmtInfId><PmtMtd>TRF</PmtMtd><NbOfTxs>{len(pays)}</NbOfTxs>'
        f'<CtrlSum>{total:.2f}</CtrlSum><PmtTpInf><SvcLvl><Cd>SEPA</Cd></SvcLvl></PmtTpInf>'
        f'<ReqdExctnDt>{now.date().isoformat()}</ReqdExctnDt>'
        f'<Dbtr><Nm>{escape(payer_name.strip()[:70])}</Nm></Dbtr>'
        f'<DbtrAcct><Id><IBAN>{debtor}</IBAN></Id></DbtrAcct>'
        f'<DbtrAgt><FinInstnId>{f"<BIC>{escape(payer_bic.strip())}</BIC>" if payer_bic else "<Othr><Id>NOTPROVIDED</Id></Othr>"}</FinInstnId></DbtrAgt>',
    ]
    for payment_id, supplier, amount, iban, reference in pays:
        lines.append(
            f'<CdtTrfTxInf><PmtId><EndToEndId>P{payment_id}</EndToEndId></PmtId>'
            f'<Amt><InstdAmt Ccy="EUR">{float(amount):.2f}</InstdAmt></Amt>'
            f'<Cdtr><Nm>{escape(str(supplier)[:70])}</Nm></Cdtr>'
            f'<CdtrAcct><Id><IBAN>{iban}</IBAN></Id></CdtrAcct>'
            f'<RmtInf><Ustrd>{escape(str(reference or "")[:140])}</Ustrd></RmtInf></CdtTrfTxInf>')
    lines += ['</PmtInf>', '</CstmrCdtTrfInitn>', '</Document>']
    db.write(db_path, "UPDATE payment_batches SET state = 'exported', exported_at = now() "
                      "WHERE batch_id = ? AND state = 'open' RETURNING batch_id", [batch_id])
    return "\n".join(lines)


def confirm(db_path, batch_id, user=None, today=None):
    """The bank has taken the batch: its bills are paid."""
    held = db.read(db_path, "SELECT state FROM payment_batches WHERE batch_id = ?", [batch_id])
    if not held or held[0][0] != "exported":
        raise ValueError("Only a batch that has been exported can be confirmed as paid.")
    done = date.today() if today is None else today
    for payment_id, bill_id in db.read(db_path, "SELECT payment_id, bill_id FROM payments WHERE batch_id = ? "
                                                "AND state = 'instructed'", [batch_id]):
        db.write(db_path, "UPDATE payments SET state = 'executed', executed_at = now() WHERE payment_id = ? "
                          "RETURNING payment_id", [payment_id])
        bills.update(db_path, bill_id, {"state": "paid", "paid_on": done.isoformat()})
    db.write(db_path, "UPDATE payment_batches SET state = 'executed', executed_at = now() WHERE batch_id = ? "
                      "RETURNING batch_id", [batch_id])
    audit.record(db_path, user or "PAYMENTS", "payment.confirm", f"batch #{batch_id}")
    return True


def listing(db_path):
    batches = [{"batch_id": i, "state": s, "created_at": c.isoformat(timespec="minutes")}
               for i, s, c in db.read(db_path, "SELECT batch_id, state, created_at FROM payment_batches "
                                               "ORDER BY batch_id DESC LIMIT 20")]
    pays = [{"payment_id": i, "bill_id": b, "supplier": s, "amount": a, "iban": iban, "batch_id": bt,
             "state": st, "reference": r}
            for i, b, s, a, iban, bt, st, r in db.read(db_path, """
                SELECT payment_id, bill_id, supplier, amount, iban, batch_id, state, reference
                FROM payments ORDER BY payment_id DESC LIMIT 60""")]
    return {"batches": batches, "payments": pays, "banks": banks(db_path)}
