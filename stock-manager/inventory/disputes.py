"""A bill that does not add up, followed through to the credit note.

Marking a bill disputed used to be the end of it. A dispute now has a reason worked out from
the match (over by so much, or nothing was delivered), a mail to the supplier that lists the
deliveries the bill should be paying for, and a state that runs until the credit note is
recorded: open, then credited, then closed once the credit covers what was claimed. A dispute
that has gone 14 days without a credit shows up as something that needs a person.
"""

from datetime import date

from . import bills, db, matching, outbox

OVERDUE_DAYS = 14
SETTLED = 0.98          # a credit this share of the claim, or more, settles it


def _reason(row):
    verdict = row["verdict"]
    if verdict == "over":
        return f"billed {row['billed']:.2f}, the deliveries come to {row['expected']:.2f}"
    if verdict == "nothing delivered":
        return "billed with no delivery since the previous bill"
    if verdict == "under":
        return f"billed {row['billed']:.2f}, the deliveries come to {row['expected']:.2f} (under)"
    return verdict


def open_for_bill(db_path, bill_id, user=None, today=None):
    found = [row for row in matching.bills(db_path, today, detail_for=bill_id)["bills"]
             if row["bill_id"] == bill_id]
    if not found:
        raise ValueError(f"No bill #{bill_id} to dispute.")
    row = found[0]
    known = db.read(db_path, "SELECT dispute_id FROM disputes WHERE bill_id = ? "
                             "AND state IN ('open', 'credited')", [bill_id])
    if known:
        return one(db_path, known[0][0])
    claimed = (max(row["gap"], 0.0) if row["verdict"] == "over" and row["gap"] is not None
               else (row["billed"] if row["verdict"] == "nothing delivered" else None))
    reason = _reason(row)

    body = ["Bonjour,", "",
            f"Nous contestons votre facture {row['number'] or '#' + str(bill_id)} du "
            f"{row['bill_date']} : {reason}.", ""]
    if row.get("lines"):
        body.append("Livraisons reçues depuis votre facture précédente :")
        body += [f"- {l['day']} : {outbox._quantity(l['quantity'])} x {l['article']}"
                 + (f" à {l['price']:.2f}" if l["price"] is not None else "")
                 for l in row["lines"]]
        body.append("")
    body += ["Merci de nous adresser un avoir"
             + (f" de {claimed:.2f} EUR" if claimed else "") + " ou de nous expliquer l'écart.",
             "", "Cordialement"]
    mail_id = outbox._add(db_path, direction="out", kind="dispute", supplier=row["supplier"],
                          to_addr=outbox.contact_email(db_path, row["supplier"]),
                          subject=f"Contestation facture {row['number'] or bill_id}",
                          body="\n".join(body), ref_kind="bill", ref_id=bill_id, state="draft",
                          created_by=user)
    new = db.write(db_path, """
            INSERT INTO disputes (bill_id, supplier, reason, claimed, mail_id, opened_by)
            VALUES (?, ?, ?, ?, ?, ?) RETURNING dispute_id""",
                   [bill_id, row["supplier"], reason, claimed, mail_id, user])
    bills.update(db_path, bill_id, {"state": "disputed"})
    return one(db_path, new[0])


def one(db_path, dispute_id):
    rows = db.read(db_path, f"{_SELECT} WHERE dispute_id = ?", [dispute_id])
    return _shape(rows[0]) if rows else None


_SELECT = """SELECT dispute_id, bill_id, supplier, reason, claimed, state, opened_on, credit_ref,
                    credited, closed_on, mail_id FROM disputes"""


def _shape(values, today=None):
    (dispute_id, bill_id, supplier, reason, claimed, state, opened, ref, credited, closed,
     mail_id) = values
    today = today or date.today()
    age = ((closed or today) - opened).days
    return {"dispute_id": dispute_id, "bill_id": bill_id, "supplier": supplier, "reason": reason,
            "claimed": claimed, "state": state, "opened_on": opened.isoformat(),
            "credit_ref": ref, "credited": credited,
            "closed_on": closed.isoformat() if closed else None, "mail_id": mail_id,
            "age_days": age, "overdue": state == "open" and age >= OVERDUE_DAYS}


def listing(db_path, today=None):
    rows = db.read(db_path, f"{_SELECT} ORDER BY (state = 'closed'), dispute_id DESC")
    return [_shape(r, today) for r in rows]


def record_credit(db_path, dispute_id, ref, amount, today=None):
    """The supplier's credit note arrived. It closes the dispute when it covers the claim."""
    held = one(db_path, dispute_id)
    if held is None:
        raise ValueError("No such dispute.")
    try:
        amount = abs(float(amount))
    except (TypeError, ValueError):
        raise ValueError("The credit is not a number.") from None
    if amount <= 0:
        raise ValueError("A credit note is for more than zero.")
    today = today or date.today()
    total = (held["credited"] or 0.0) + amount
    settled = held["claimed"] is None or total >= held["claimed"] * SETTLED
    db.write(db_path, """
            UPDATE disputes SET credit_ref = ?, credited = ?, state = ?, closed_on = ?
            WHERE dispute_id = ? RETURNING dispute_id""",
             [ref or held["credit_ref"], total, "closed" if settled else "credited",
              today if settled else None, dispute_id])
    if settled:
        bills.update(db_path, held["bill_id"], {"note": f"credit note {ref or ''} {total:.2f}".strip()})
    return one(db_path, dispute_id)


def close(db_path, dispute_id, today=None):
    held = one(db_path, dispute_id)
    if held is None or held["state"] == "closed":
        return False
    db.write(db_path, "UPDATE disputes SET state = 'closed', closed_on = ? WHERE dispute_id = ? "
                      "RETURNING dispute_id", [today or date.today(), dispute_id])
    return True
