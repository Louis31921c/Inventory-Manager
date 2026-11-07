"""Money: bills, disputes, payments, the wallet and the bank."""

import duckdb
import subprocess
from fastapi import HTTPException
from fastapi import Request
from fastapi import UploadFile
from fastapi.responses import Response
from pydantic import BaseModel

from .. import bankdrop
from .. import bills
from .. import db
from .. import disputes
from .. import intake
from .. import matching
from .. import outbox
from .. import payments
from .. import photos
from .. import wallet
from fastapi import APIRouter

from .. import server as s    # the server module, read at call time: its config and helpers

router = APIRouter()

class BillBody(BaseModel):
    bill: dict
    sha256: str | None = None
    intake_id: int | None = None
    sender: str | None = None


@router.get("/api/bills")
def read_bills():
    """What the months cost. The bills themselves are not listed on the screen."""
    return {"months": bills.by_month(s.config.db_path),
            "summary": bills.summary(s.config.db_path)}


@router.post("/api/bills/read")
async def read_bill_photo(request: Request, photo: UploadFile):
    """A bill dropped straight on the screen, same road as a delivery note."""
    raw, media_type, sha = await s._photo_bytes(photo)
    already = bills.find_by_image(s.config.db_path, sha)
    if already is not None:
        raise HTTPException(409, f"This bill is already stored (bill #{already}).")
    read = await s._read_bill(raw, media_type)
    photos.keep(s.config.images_dir / f"{sha}{s.SUPPORTED[media_type]}", raw)
    s.event(request, "bill.read", photo.filename or "upload")
    return {"sha256": sha, "bill": read}


@router.post("/api/bills")
def save_bill(request: Request, body: BillBody):
    source = "mail" if body.sender else ("file" if body.sha256 else "hand")
    row = bills.shape(body.bill, source=source, user=s.who(request)[0], sender=body.sender,
                      sha256=body.sha256 or None, path=s._stored_photo(body.sha256) or None)
    wrong = bills.problems(row)
    if wrong:
        raise HTTPException(422, "To fix: " + ", ".join(wrong))

    twin = bills.find_same(s.config.db_path, row["supplier"], row["number"])
    if twin is not None:
        raise HTTPException(409, f"{row['supplier']} {row['number']} is already stored "
                                 f"(bill #{twin}).")
    try:
        bill_id = bills.save(s.config.db_path, row)
    except duckdb.ConstraintException as e:
        raise HTTPException(409, "This bill is already stored.") from e

    if body.intake_id is not None:
        intake.mark(s.config, body.intake_id, intake.DONE, f"bill #{bill_id}")
    s.event(request, "bill.save", f"bill #{bill_id}",
          f"{row['supplier']} · {row['total']} {row['currency']}")
    return {"bill_id": bill_id, "bill": bills.one(s.config.db_path, bill_id)}


class Dispute(BaseModel):
    bill_id: int


class Credit(BaseModel):
    ref: str | None = None
    amount: float


@router.get("/api/disputes")
def read_disputes():
    return {"disputes": disputes.listing(s.config.db_path)}


@router.post("/api/disputes")
def open_dispute(request: Request, body: Dispute):
    made = s._do(request, "dispute.open", f"bill #{body.bill_id}", lambda: disputes.open_for_bill(
        s.config.db_path, body.bill_id, s.who(request)[0]))
    return {"dispute": made, "disputes": disputes.listing(s.config.db_path),
            "outbox": outbox.overview(s.config.db_path, s.config.mail)}


@router.post("/api/disputes/{dispute_id}/credit")
def credit_dispute(request: Request, dispute_id: int, body: Credit):
    s._do(request, "dispute.credit", f"dispute #{dispute_id}", lambda: disputes.record_credit(
        s.config.db_path, dispute_id, body.ref, body.amount))
    return {"disputes": disputes.listing(s.config.db_path)}


@router.post("/api/disputes/{dispute_id}/close")
def close_dispute(request: Request, dispute_id: int):
    if not disputes.close(s.config.db_path, dispute_id):
        raise HTTPException(404, "That dispute is not open.")
    s.event(request, "dispute.close", f"dispute #{dispute_id}")
    return {"disputes": disputes.listing(s.config.db_path)}


class Money(BaseModel):
    amount: float
    note: str | None = None


class Bank(BaseModel):
    supplier: str
    iban: str
    bic: str | None = None
    holder: str | None = None


class Payer(BaseModel):
    name: str = ""
    iban: str = ""
    bic: str = ""


@router.get("/api/wallet")
def read_wallet():
    return s._wallet_overview()


@router.post("/api/wallet/deposit")
def wallet_deposit(request: Request, body: Money):
    s._do(request, "wallet.deposit", f"{body.amount} EUR", lambda: wallet.deposit(
        s.config.db_path, body.amount, body.note, s.who(request)[0]))
    return s._wallet_overview()


@router.post("/api/wallet/withdraw")
def wallet_withdraw(request: Request, body: Money):
    s._do(request, "wallet.withdraw", f"{body.amount} EUR", lambda: wallet.withdraw(
        s.config.db_path, body.amount, body.note, s.who(request)[0]))
    return s._wallet_overview()


@router.put("/api/bank")
def put_bank(request: Request, body: Bank):
    s._do(request, "bank.set", body.supplier, lambda: payments.set_bank(
        s.config.db_path, body.supplier, body.iban, body.bic, body.holder))
    return s._wallet_overview()


@router.put("/api/payer")
def put_payer(request: Request, body: Payer):
    if body.iban.strip() and payments.valid_iban(body.iban) is None:
        raise HTTPException(422, "That is not a valid IBAN.")
    for key, value in (("payer_name", body.name.strip()), ("payer_iban", body.iban.replace(" ", "").upper()),
                       ("payer_bic", body.bic.strip())):
        db.write(s.config.db_path, "INSERT INTO policy_settings (key, value) VALUES (?, ?) "
                                 "ON CONFLICT (key) DO UPDATE SET value = excluded.value RETURNING key", [key, value])
    s.event(request, "payer.set", body.name.strip() or "empty")
    return s._wallet_overview()


@router.get("/api/payments/batch/{batch_id}.xml")
def payment_file(request: Request, batch_id: int):
    payer = s._payer()
    try:
        xml = payments.batch_file(s.config.db_path, batch_id, payer["name"], payer["iban"], payer["bic"])
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    s.event(request, "payment.export", f"batch #{batch_id}")
    return Response(xml, media_type="application/xml",
                    headers={"Content-Disposition": f'attachment; filename="sepa-batch-{batch_id}.xml"'})


@router.post("/api/payments/batch/{batch_id}/executed")
def payments_executed(request: Request, batch_id: int):
    s._do(request, "payment.confirm", f"batch #{batch_id}", lambda: payments.confirm(
        s.config.db_path, batch_id, s.who(request)[0]))
    return s._wallet_overview()


@router.post("/api/payments/{payment_id}/cancel")
def cancel_payment(request: Request, payment_id: int):
    s._do(request, "payment.cancel", f"payment #{payment_id}", lambda: payments.cancel(
        s.config.db_path, payment_id, s.who(request)[0]))
    return s._wallet_overview()


class BankDropBody(BaseModel):
    host: str = ""
    port: str = "22"
    user: str = ""
    key: str = ""
    dir: str = ""
    status_dir: str = ""


@router.put("/api/bank-drop")
def put_bank_drop(request: Request, body: BankDropBody):
    s._do(request, "bankdrop.set", body.host or "empty", lambda: bankdrop.save(
        s.config.db_path, body.host, body.port, body.user, body.key, body.dir, body.status_dir))
    return s._connections()


@router.post("/api/bank-drop/test")
def test_bank_drop(request: Request):
    try:
        bankdrop.check(s.config.db_path)
    except (ValueError, OSError, subprocess.SubprocessError) as e:
        raise HTTPException(422, str(e)) from e
    s.event(request, "bankdrop.test", None)
    return {"ok": True}


class BillTo(BaseModel):
    company: str = ""
    contact: str = ""
    address: str = ""


@router.get("/api/bill-to")
def read_bill_to():
    """Who orders are billed to unless said otherwise."""
    rows = db.read(s.config.db_path, "SELECT company, contact, address FROM bill_to_default WHERE id = 1")
    company, contact, address = rows[0] if rows else (None, None, None)
    return {"company": company or "", "contact": contact or "", "address": address or ""}


@router.put("/api/bill-to")
def put_bill_to(request: Request, body: BillTo):
    db.write(s.config.db_path, """
            INSERT INTO bill_to_default (id, company, contact, address) VALUES (1, ?, ?, ?)
            ON CONFLICT (id) DO UPDATE SET company = excluded.company, contact = excluded.contact,
                address = excluded.address RETURNING id""",
             [body.company.strip(), body.contact.strip(), body.address.strip()])
    s.event(request, "billto.set", body.company.strip() or "empty")
    return read_bill_to()


@router.get("/api/match")
def three_way_match():
    """Bills against the deliveries they pay for, and orders against what has come in."""
    return matching.report(s.config.db_path)


@router.patch("/api/bills/{bill_id}")
def edit_bill(request: Request, bill_id: int, body: dict):
    if bills.one(s.config.db_path, bill_id) is None:
        raise HTTPException(404, f"No bill #{bill_id}.")
    if not bills.update(s.config.db_path, bill_id, body):
        raise HTTPException(422, "Nothing to change.")
    s.event(request, "bill.edit", f"bill #{bill_id}", ", ".join(sorted(body)))
    return {"bill": bills.one(s.config.db_path, bill_id)}


@router.delete("/api/bills/{bill_id}")
def remove_bill(request: Request, bill_id: int):
    stored = bills.one(s.config.db_path, bill_id)
    if stored is None:
        raise HTTPException(404, f"No bill #{bill_id}.")
    if not bills.drop(s.config.db_path, bill_id):
        raise HTTPException(404, f"No bill #{bill_id}.")
    s.event(request, "bill.delete", f"bill #{bill_id}")
    return {"deleted": bill_id}
