"""The wallet Plutus spends from.

It is a ledger kept in this program, funded by a person and nobody else. Money goes in by a
deposit and out by a withdrawal, both a person's, and Plutus can only spend what is there:

  balance     what the wallet holds
  committed   what is promised to suppliers in orders sent and not yet paid
  available   balance less committed, less the reserve a person asks to keep

An order sent commits its value, so a second order cannot spend the same money. A bill paid
debits the balance and frees the commitment it settles, first sent first. Nothing here can go
below zero and nothing moves without a row in the ledger saying who and why.

This is the program's own record of the wallet. It does not move money at a bank: a payment is
instructed into a batch (payments.py) that becomes a bank file or is handed to a connector.
"""

from . import audit, db


def _last(db_path):
    rows = db.read(db_path, "SELECT balance_after, committed_after FROM wallet_entries "
                            "ORDER BY entry_id DESC LIMIT 1")
    return (float(rows[0][0]), float(rows[0][1])) if rows else (0.0, 0.0)


def balance(db_path):
    return _last(db_path)[0]


def committed(db_path):
    return round(float(db.read(db_path, "SELECT coalesce(sum(amount), 0) FROM commitments "
                                        "WHERE state = 'open'")[0][0]), 2)


def available(db_path, reserve=0.0):
    return round(balance(db_path) - committed(db_path) - max(0.0, float(reserve or 0)), 2)


def _entry(db_path, kind, amount, ref_kind=None, ref_id=None, supplier=None, note=None, user=None):
    bal, _ = _last(db_path)
    after = round(bal + amount, 2) if kind in ("deposit", "withdraw", "payment", "refund") else bal
    db.write(db_path, """
            INSERT INTO wallet_entries (kind, amount, balance_after, committed_after, ref_kind, ref_id,
                                        supplier, note, done_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING entry_id""",
             [kind, round(amount, 2), after, committed(db_path), ref_kind, ref_id, supplier, note, user])
    return after


def _positive(amount):
    try:
        amount = round(float(amount), 2)
    except (TypeError, ValueError):
        raise ValueError("The amount is not a number.") from None
    if amount <= 0:
        raise ValueError("The amount must be more than zero.")
    return amount


def deposit(db_path, amount, note=None, user=None):
    """Put money in. Only ever a person's act."""
    amount = _positive(amount)
    audit.record(db_path, user or "WALLET", "wallet.deposit", f"{amount:.2f} EUR", note)
    return _entry(db_path, "deposit", amount, note=note or "deposit", user=user)


def withdraw(db_path, amount, note=None, user=None):
    """Take money out: never more than is free."""
    amount = _positive(amount)
    if amount > available(db_path) + 1e-9:
        raise ValueError(f"Only {available(db_path):.2f} EUR is free; the rest is committed to orders.")
    audit.record(db_path, user or "WALLET", "wallet.withdraw", f"{amount:.2f} EUR", note)
    return _entry(db_path, "withdraw", -amount, note=note or "withdrawal", user=user)


def commit(db_path, supplier, amount, ref_kind, ref_id, reserve=0.0, user="PLUTUS"):
    """Promise `amount` to a supplier in an order. Refused when the wallet cannot cover it."""
    amount = _positive(amount)
    if amount > available(db_path, reserve) + 1e-9:
        raise ValueError(f"The wallet has {available(db_path, reserve):.2f} EUR free, not {amount:.2f}.")
    db.write(db_path, "INSERT INTO commitments (supplier, amount, ref_kind, ref_id) VALUES (?, ?, ?, ?) "
                      "RETURNING commit_id", [supplier, amount, ref_kind, ref_id])
    _entry(db_path, "order", -amount, ref_kind, ref_id, supplier, "committed to an order", user)
    return available(db_path, reserve)


def release(db_path, supplier, amount):
    """Free what a payment settles: that supplier's oldest commitments first."""
    left = float(amount)
    for commit_id, held in db.read(db_path, "SELECT commit_id, amount FROM commitments WHERE state = 'open' "
                                            "AND upper(coalesce(supplier, '')) = upper(?) ORDER BY commit_id",
                                   [supplier or ""]):
        if left <= 1e-9:
            break
        if held <= left + 1e-9:
            db.write(db_path, "UPDATE commitments SET state = 'released', released_on = current_date "
                              "WHERE commit_id = ? RETURNING commit_id", [commit_id])
            left -= float(held)
        else:
            db.write(db_path, "UPDATE commitments SET amount = amount - ? WHERE commit_id = ? RETURNING commit_id",
                     [left, commit_id])
            left = 0.0


def pay(db_path, supplier, amount, bill_id, user="PLUTUS"):
    """Take a payment out of the balance. Refused when the balance cannot cover it."""
    amount = _positive(amount)
    if amount > balance(db_path) + 1e-9:
        raise ValueError(f"The wallet holds {balance(db_path):.2f} EUR, not {amount:.2f}.")
    release(db_path, supplier, amount)
    return _entry(db_path, "payment", -amount, "bill", bill_id, supplier, "payment instructed", user)


def refund(db_path, supplier, amount, bill_id, note, user=None):
    """A payment that was cancelled before it went: the money is back."""
    return _entry(db_path, "refund", _positive(amount), "bill", bill_id, supplier, note, user)


def ledger(db_path, limit=60):
    return [{"entry_id": i, "at": t.isoformat(timespec="minutes"), "kind": k, "amount": a,
             "balance": b, "committed": c, "ref": f"{rk} #{ri}" if rk else None, "supplier": s,
             "note": n, "by": u}
            for i, t, k, a, b, c, rk, ri, s, n, u in db.read(db_path, """
                SELECT entry_id, happened_at, kind, amount, balance_after, committed_after, ref_kind,
                       ref_id, supplier, note, done_by FROM wallet_entries ORDER BY entry_id DESC LIMIT ?""",
                                                           [limit])]


def overview(db_path, reserve=0.0):
    return {"balance": balance(db_path), "committed": committed(db_path),
            "available": available(db_path, reserve), "ledger": ledger(db_path)}
