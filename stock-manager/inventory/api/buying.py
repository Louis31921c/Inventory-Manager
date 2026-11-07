"""Buying: baskets, the gate, the agents and their rules, quotes, orders, outgoing mail, suppliers."""

import asyncio
import duckdb
import threading
from datetime import date
from fastapi import HTTPException
from fastapi import Request
from fastapi import UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .. import agents
from .. import baskets
from .. import channels
from .. import charters
from .. import contacts
from .. import db
from .. import discord_bot
from .. import feedback
from .. import gate
from .. import judge
from .. import leadtimes
from .. import llm
from .. import outbox
from .. import photos
from .. import policies
from .. import pricing
from .. import purchasing
from .. import quotes
from .. import replies
from .. import slack_bot
from .. import verify
from .. import vocabulary
from .. import watch
from .. import workdays
from ..models import parse_date
from fastapi import APIRouter

from .. import server as s    # the server module, read at call time: its config and helpers

router = APIRouter()

@router.get("/api/baskets")
def read_baskets():
    return {"baskets": baskets.open_baskets(s.config.db_path), "chat": discord_bot.status(),
            "slack": slack_bot.status()}


@router.post("/api/baskets/{channel}/order")
def basket_to_order(request: Request, channel: str):
    """What the chat asked for becomes a purchase order, for a person to place."""
    answer = baskets.to_purchase_order(s.config.db_path, channel, s.who(request)[0])
    if answer is None:
        raise HTTPException(404, "That basket is empty.")
    s.event(request, "basket.order", f"list #{answer['list_id']}", f"{len(answer['lines'])} line(s)")
    return {"list_id": answer["list_id"], "lines": len(answer["lines"]), "shop": answer["shop"]}


class GateAsk(BaseModel):
    channel: str | None = None
    shop: str | None = None
    site: str | None = None
    work_item: str | None = None
    supplier: str | None = None
    lines: list[dict]


class GateWord(BaseModel):
    why: str | None = None


@router.get("/api/chain")
def read_chain(limit: int = 200):
    """The sealed log, whether it still holds, and what the two agents have done."""
    whole, broken, entries = gate.verify_chain(s.config.db_path)
    rows = gate.chain(s.config.db_path)[-min(max(limit, 1), 500):]
    held = gate.waiting(s.config.db_path)
    in_flight = {str(row["channel"]) for row in held if row["channel"]}
    now = gate.where_we_are(s.config.db_path)
    standing = agents.status()

    faults = {}
    if not whole:
        # A seal that does not hold is a fault of the whole round, so it lands on the step
        # the round is standing on, which is where somebody is looking.
        faults[str(now["step"] or 1)] = f"the sealed log is broken at entry {broken}"
    if not standing["ready"]:
        faults["3"] = "Terminus cannot be reached: " + standing["detail"]
    return {
        "log": {"whole": whole, "broken_at": broken, "entries": entries,
                "last_seal": gate.last_seal(s.config.db_path)},
        "entries": list(reversed(rows)),
        "doings": gate.doings(s.config.db_path),
        "agents": standing,
        "rules": {"ceiling": gate.CEILING, "dear": gate.DEAR, "drift": gate.DRIFT},
        "words": gate.REASON_WORDS,
        "steps": [{"number": number, "name": name, "detail": detail,
                   "built": number not in gate.UNBUILT}
                  for number, name, detail in gate.STEPS],
        "now": now,
        # Red is for something being wrong, so it only ever comes from something that is.
        "faults": faults,
        "approval": held,
        # A basket is emptied when it becomes an order, so whatever is still in one is
        # whatever nobody has bought — except the ones already held, which are in flight
        # and are answered for on the line above rather than twice over.
        "baskets": [row for row in baskets.open_baskets(s.config.db_path)
                    if str(row["channel"]) not in in_flight],
    }


@router.get("/api/chain/step/{step}")
def read_chain_step(step: int, limit: int = 60):
    """What has happened in one wedge of the round: the audit behind a click on it."""
    if step not in {number for number, _, _ in gate.STEPS}:
        raise HTTPException(404, f"There is no step {step}.")
    return gate.step_history(s.config.db_path, step, limit=min(max(limit, 1), 200))


@router.get("/api/agents")
def read_agents():
    """The two agents and whether they can run. There is no key to show."""
    return agents.status()


@router.get("/api/gate")
def read_gate():
    """What is waiting on a person, what has been decided, and whether the log is whole."""
    whole, broken, entries = gate.verify_chain(s.config.db_path)
    return {
        "waiting": gate.waiting(s.config.db_path),
        "history": gate.history(s.config.db_path),
        "words": gate.REASON_WORDS,
        "rules": {"ceiling": gate.CEILING, "dear": gate.DEAR, "drift": gate.DRIFT},
        "log": {"whole": whole, "broken_at": broken, "entries": entries},
    }


@router.get("/api/gate/{request_id}")
def read_one_gate(request_id: int):
    found = gate.one(s.config.db_path, request_id)
    if found is None:
        raise HTTPException(404, f"No request #{request_id}.")
    return {"request": found, "log": gate.chain(s.config.db_path, request_id)}


@router.post("/api/gate")
def ask_gate(request: Request, body: GateAsk):
    """Hold a basket. Nothing is held that nothing holds back."""
    reading = gate.look(s.config.db_path, body.lines, supplier=body.supplier)
    if not reading["held"]:
        return {"held": False, "reading": reading}
    request_id = gate.ask(s.config.db_path, reading, channel=body.channel, shop=body.shop,
                          asked_by=s.who(request)[0], site=body.site, work_item=body.work_item)
    s.event(request, "gate.ask", f"request #{request_id}", ",".join(reading["reasons"]))
    return {"held": True, "request_id": request_id, "reading": reading}


@router.post("/api/gate/{request_id}/check")
async def check_gate(request: Request, request_id: int):
    """The first factor: Terminus reads the names, and nothing else."""
    found = gate.one(s.config.db_path, request_id)
    if found is None:
        raise HTTPException(404, f"No request #{request_id}.")
    answer = await verify.check(s.config.db_path, found["lines"])
    gate.record_check(s.config.db_path, request_id, answer["verdict"], answer["note"],
                      answer["lines"])
    s.event(request, "gate.check", f"request #{request_id}", answer["verdict"])
    await asyncio.to_thread(judge.decide, s.config.db_path, request_id)
    await asyncio.to_thread(policies.auto_decide, s.config.db_path, request_id)
    return {"request": gate.one(s.config.db_path, request_id), "agent": answer}


@router.post("/api/gate/{request_id}/approve")
def approve_gate(request: Request, request_id: int):
    """The second factor, and the only one that lets money out."""
    try:
        found = gate.approve(s.config.db_path, request_id, s.who(request)[0])
    except ValueError as e:
        raise HTTPException(409, str(e)) from e
    s.event(request, "gate.approve", f"request #{request_id}", f"{found['total']} EUR")
    return {"request": found}


@router.post("/api/gate/{request_id}/cancel")
def cancel_gate(request: Request, request_id: int, body: GateWord | None = None):
    try:
        found = gate.cancel(s.config.db_path, request_id, s.who(request)[0],
                            body.why if body else None)
    except ValueError as e:
        raise HTTPException(409, str(e)) from e
    s.event(request, "gate.cancel", f"request #{request_id}", body.why if body else None)
    return {"request": found}


@router.delete("/api/baskets/{channel}")
def drop_basket(request: Request, channel: str):
    held = baskets.clear(s.config.db_path, channel)
    if not held:
        raise HTTPException(404, "That basket is empty.")
    s.event(request, "basket.clear", channel, f"{held} line(s)")
    return {"dropped": held}


class MailRef(BaseModel):
    list_id: int | None = None
    quote_id: int | None = None
    mail_id: int | None = None
    supplier: str | None = None


class DraftEdit(BaseModel):
    to: str | None = None
    subject: str | None = None
    body: str | None = None


def _outbox_work(request, action, target, work):
    """The mails to suppliers: a refusal is in words the person can act on."""
    try:
        done = work()
    except (ValueError, outbox.SendError) as e:
        raise HTTPException(422, str(e)) from e
    s.event(request, f"supplier.{action}", target, None)
    return {**(done if isinstance(done, dict) else {}),
            "outbox": outbox.overview(s.config.db_path, s.config.mail)}


class ClosedDay(BaseModel):
    day: str
    label: str | None = None


class Terms(BaseModel):
    supplier: str
    cutoff: str | None = None
    closed_days: str | None = None
    payment_days: int | None = None
    currency: str | None = None
    note: str | None = None


class Rate(BaseModel):
    day: str
    currency: str
    per_eur: float


@router.get("/api/calendar")
def read_calendar(year: int | None = None):
    year = year or date.today().year
    return {"year": year, "days": workdays.calendar_view(s.config.db_path, year),
            "terms": workdays.all_terms(s.config.db_path),
            "rates": [{"day": d.isoformat(), "currency": c, "per_eur": r} for d, c, r in db.read(
                s.config.db_path, "SELECT day, currency, per_eur FROM fx_rates ORDER BY day DESC, "
                                "currency LIMIT 40")]}


@router.post("/api/calendar/close")
def close_a_day(request: Request, body: ClosedDay):
    day = parse_date(body.day)
    if day is None:
        raise HTTPException(422, "That day is not a date.")
    workdays.close_day(s.config.db_path, day, body.label)
    s.event(request, "calendar.close", day.isoformat(), body.label)
    return read_calendar(day.year)


@router.delete("/api/calendar/{day}")
def reopen_a_day(request: Request, day: str):
    when = parse_date(day)
    if when is None or not workdays.reopen_day(s.config.db_path, when):
        raise HTTPException(404, "That day is not one you closed.")
    s.event(request, "calendar.open", when.isoformat())
    return read_calendar(when.year)


@router.put("/api/terms")
def put_terms(request: Request, body: Terms):
    name = s._do(request, "terms.set", body.supplier, lambda: workdays.set_terms(
        s.config.db_path, body.supplier, body.cutoff, body.closed_days, body.payment_days,
        body.currency, body.note))
    return {"supplier": name, "terms": workdays.all_terms(s.config.db_path)}


@router.post("/api/rates")
def put_rate(request: Request, body: Rate):
    day = parse_date(body.day)
    if day is None:
        raise HTTPException(422, "That day is not a date.")
    s._do(request, "rate.set", body.currency, lambda: workdays.set_rate(
        s.config.db_path, day, body.currency, body.per_eur))
    return read_calendar(day.year)


@router.post("/api/rates/fetch")
def fetch_rates(request: Request):
    try:
        got = workdays.fetch_rates(s.config.db_path)
    except Exception as e:
        raise HTTPException(502, f"The rates could not be fetched: {e}") from e
    s.event(request, "rate.fetch", got["day"], f"{got['stored']} currencies")
    return {**got, **read_calendar()}


class Offer(BaseModel):
    quote_id: int
    line_no: int
    unit_price: float
    lead_days: int | None = None
    valid_until: str | None = None
    note: str | None = None


class Award(BaseModel):
    quote_id: int
    line_no: int
    valid_to: str | None = None


class Others(BaseModel):
    suppliers: list[str]


@router.get("/api/offers/{quote_id}")
def read_offers(quote_id: int):
    return {"offers": pricing.offers(s.config.db_path, quote_id)}


@router.put("/api/offers")
def put_offer(request: Request, body: Offer):
    s._do(request, "offer.set", f"quote #{body.quote_id} line {body.line_no}", lambda: pricing.set_offer(
        s.config.db_path, body.quote_id, body.line_no, body.unit_price, body.lead_days,
        body.valid_until, body.note, s.who(request)[0]))
    return {"offers": pricing.offers(s.config.db_path, body.quote_id), **pricing.compare(s.config.db_path)}


@router.post("/api/quotes/{quote_id}/ask-others")
def ask_other_suppliers(request: Request, quote_id: int, body: Others):
    made = s._do(request, "quote.copy", f"quote #{quote_id}", lambda: pricing.ask_others(
        s.config.db_path, quote_id, body.suppliers, s.who(request)[0]))
    return {"made": made, "quotes": quotes.recent(s.config.db_path)}


@router.get("/api/compare")
def compare_offers():
    return pricing.compare(s.config.db_path)


@router.post("/api/award")
def award_offer(request: Request, body: Award):
    done = s._do(request, "offer.award", f"quote #{body.quote_id} line {body.line_no}",
               lambda: pricing.award(s.config.db_path, body.quote_id, body.line_no, body.valid_to,
                                     s.who(request)[0]))
    return {**done, **pricing.compare(s.config.db_path)}


@router.delete("/api/agreements/{agreement_id}")
def end_agreement(request: Request, agreement_id: int):
    if not pricing.end(s.config.db_path, agreement_id):
        raise HTTPException(404, "That agreement is not active.")
    s.event(request, "agreement.end", f"agreement #{agreement_id}")
    return pricing.compare(s.config.db_path)


@router.get("/api/outbox")
def read_outbox():
    return outbox.overview(s.config.db_path, s.config.mail)


@router.post("/api/outbox/draft-order")
def outbox_draft_order(request: Request, body: MailRef):
    return _outbox_work(request, "draft", f"order #{body.list_id}", lambda: outbox.draft_order(
        s.config.db_path, body.list_id, s.who(request)[0]))


@router.post("/api/outbox/draft-quote")
def outbox_draft_quote(request: Request, body: MailRef):
    return _outbox_work(request, "draft", f"quote #{body.quote_id}", lambda: outbox.draft_quote(
        s.config.db_path, body.quote_id, s.who(request)[0]))


@router.post("/api/outbox/draft-chase")
def outbox_draft_chase(request: Request, body: MailRef):
    return _outbox_work(request, "draft", f"chase #{body.mail_id}", lambda: outbox.draft_chase(
        s.config.db_path, body.mail_id, s.who(request)[0]))


@router.post("/api/outbox/draft-backorder")
def outbox_draft_backorder(request: Request, body: MailRef):
    return _outbox_work(request, "draft", body.supplier or "", lambda: outbox.draft_backorder(
        s.config.db_path, body.supplier or "", s.who(request)[0]))


@router.patch("/api/outbox/{mail_id}")
def outbox_edit(request: Request, mail_id: int, body: DraftEdit):
    return _outbox_work(request, "edit", f"mail #{mail_id}", lambda: {
        "draft": outbox.update_draft(s.config.db_path, mail_id, body.to, body.subject, body.body)})


@router.delete("/api/outbox/{mail_id}")
def outbox_discard(request: Request, mail_id: int):
    if not outbox.discard(s.config.db_path, mail_id):
        raise HTTPException(404, "That draft is not there, or it has already been sent.")
    s.event(request, "supplier.discard", f"mail #{mail_id}")
    return {"outbox": outbox.overview(s.config.db_path, s.config.mail)}


@router.post("/api/outbox/{mail_id}/send")
def outbox_send(request: Request, mail_id: int):
    """The one place a mail leaves: only on this call, for a draft a person has seen."""
    return _outbox_work(request, "send", f"mail #{mail_id}", lambda: {
        "sent": outbox.send(s.config.db_path, mail_id, s.config.mail, s.who(request)[0])})


@router.post("/api/outbox/{mail_id}/close")
def outbox_close(request: Request, mail_id: int):
    if not outbox.close(s.config.db_path, mail_id):
        raise HTTPException(404, "That order is not waiting.")
    s.event(request, "supplier.close", f"mail #{mail_id}")
    return {"outbox": outbox.overview(s.config.db_path, s.config.mail)}


class PolicyBody(BaseModel):
    name: str = ""
    action: str = ""
    supplier: str | None = None
    max_total: float | None = None
    per_day: float | None = None
    per_month: float | None = None
    price_band: float = 0.10
    min_deliveries: int = 3
    allowed_reasons: str = ""
    terminus_same: bool = True
    hours_from: int = 8
    hours_to: int = 17
    per_supplier_day: int = 2
    max_chases: int = 2


class PolicyChange(BaseModel):
    active: bool | None = None
    mode: str | None = None
    max_total: float | None = None
    price_band: float | None = None
    per_day: float | None = None
    per_month: float | None = None


class LimitValue(BaseModel):
    value: float


@router.get("/api/policies")
def read_policies():
    return policies.overview(s.config.db_path)


@router.post("/api/policies")
def create_policy(request: Request, body: PolicyBody):
    made = s._do(request, "policy.create", body.name, lambda: policies.create(
        s.config.db_path, body.model_dump(), s.who(request)[0]))
    return {"policy": made, **policies.overview(s.config.db_path)}


@router.patch("/api/policies/{policy_id}")
def change_policy(request: Request, policy_id: int, body: PolicyChange):
    s._do(request, "policy.update", f"policy #{policy_id}", lambda: policies.update(
        s.config.db_path, policy_id, body.model_dump(exclude_none=True), s.who(request)[0]))
    return policies.overview(s.config.db_path)


@router.delete("/api/policies/{policy_id}")
def delete_policy(request: Request, policy_id: int):
    if not policies.remove(s.config.db_path, policy_id):
        raise HTTPException(404, "No such policy.")
    s.event(request, "policy.delete", f"policy #{policy_id}")
    return policies.overview(s.config.db_path)


@router.post("/api/policies/switch")
def switch_policies(request: Request, body: s.Switch):
    policies.set_enabled(s.config.db_path, body.on, s.who(request)[0])
    return policies.overview(s.config.db_path)


@router.post("/api/policies/run")
def run_policies(request: Request):
    """Offer what is waiting to the policies now, rather than at the next hour."""
    done = policies.run(s.config.db_path, s.config)
    s.event(request, "policy.run", str(done.get("acted", 0)), done.get("skipped"))
    return {**done, **policies.overview(s.config.db_path)}


@router.get("/api/charters")
def read_charters():
    return charters.overview(s.config.db_path)


@router.put("/api/charters/{bot}/{key}")
def put_limit(request: Request, bot: str, key: str, body: LimitValue):
    s._do(request, "charter.set", f"{bot} {key}", lambda: charters.set_limit(
        s.config.db_path, bot, key, body.value))
    return charters.overview(s.config.db_path)


@router.delete("/api/charters/{bot}/{key}")
def reset_limit(request: Request, bot: str, key: str):
    charters.reset(s.config.db_path, bot, key)
    s.event(request, "charter.reset", f"{bot} {key}")
    return charters.overview(s.config.db_path)


class Mode(BaseModel):
    mode: str


@router.get("/api/judge")
def read_judge():
    return judge.status(s.config.db_path)


@router.post("/api/judge/mode")
def set_judge_mode(request: Request, body: Mode):
    s._do(request, "terminus.mode", body.mode, lambda: judge.set_mode(s.config.db_path, body.mode, s.who(request)[0]))
    return judge.status(s.config.db_path)


@router.post("/api/judge/run")
def run_judge(request: Request):
    done = judge.decide(s.config.db_path)
    s.event(request, "terminus.run", f"{len(done)} basket(s)")
    return {"decided": [{"request_id": i, "does": what} for i, what in done], **judge.status(s.config.db_path)}


@router.post("/api/plutus/mode")
def set_plutus_mode(request: Request, body: Mode):
    s._do(request, "plutus.mode", body.mode, lambda: purchasing.set_mode(s.config.db_path, body.mode, s.who(request)[0]))
    return s._wallet_overview()


@router.post("/api/plutus/run")
def run_plutus(request: Request):
    done = purchasing.run(s.config.db_path, s.config)
    watch.scan(s.config.db_path)
    s.event(request, "plutus.run", str(done.get("done", 0)), done.get("skipped"))
    return {**done, **s._wallet_overview()}


class ChannelBody(BaseModel):
    supplier: str
    url: str
    token: str = ""


@router.get("/api/connections")
def read_connections():
    return s._connections()


@router.put("/api/channels")
def put_channel(request: Request, body: ChannelBody):
    s._do(request, "channel.set", body.supplier, lambda: channels.save(
        s.config.db_path, body.supplier, body.url, body.token))
    return s._connections()


@router.delete("/api/channels/{supplier}")
def delete_channel(request: Request, supplier: str):
    s._do(request, "channel.remove", supplier, lambda: channels.remove(s.config.db_path, supplier))
    return s._connections()


@router.post("/api/channels/{supplier}/test")
def test_channel(request: Request, supplier: str):
    try:
        done = channels.test(s.config.db_path, supplier)
    except (ValueError, outbox.SendError) as e:
        raise HTTPException(422, str(e)) from e
    s.event(request, "channel.test", supplier)
    return {"status": done["status"]}


@router.get("/api/supplier-questions")
def supplier_questions():
    return {"questions": replies.open_questions(s.config.db_path)}


@router.post("/api/supplier-questions/{mail_id}/answered")
def supplier_question_answered(request: Request, mail_id: int):
    replies.answered(s.config.db_path, mail_id)
    s.event(request, "reply.answered", f"mail #{mail_id}")
    return {"questions": replies.open_questions(s.config.db_path)}


class QuoteBody(BaseModel):
    supplier: str | None = None
    site: str | None = None
    work_item: str | None = None
    needed_by: str | None = None
    note: str | None = None
    lines: list[dict] = []
    text: str | None = None


@router.get("/api/quotes")
def read_quotes():
    return {"quotes": quotes.recent(s.config.db_path), "units": quotes.units(),
            "summary": quotes.summary(s.config.db_path)}


@router.get("/api/quotes/{quote_id}")
def read_quote(quote_id: int):
    found = quotes.one(s.config.db_path, quote_id)
    if found is None:
        raise HTTPException(404, f"No quote #{quote_id}.")
    found["text"] = quotes.as_text(found)
    return found


@router.post("/api/quotes")
def save_quote(request: Request, body: QuoteBody):
    lines = body.lines or quotes.parse_request(body.text)
    if not lines:
        raise HTTPException(422, "Name at least one thing to be quoted.")
    head = body.model_dump(exclude={"lines", "text"})
    try:
        quote_id = quotes.save(s.config.db_path, head, lines, s.who(request)[0])
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    s.event(request, "quote.save", f"quote #{quote_id}",
          f"{head.get('supplier') or 'no supplier'} · {len(lines)} line(s)")
    return {"quote_id": quote_id, "quote": quotes.one(s.config.db_path, quote_id)}


@router.patch("/api/quotes/{quote_id}")
def edit_quote(request: Request, quote_id: int, body: dict):
    if quotes.one(s.config.db_path, quote_id) is None:
        raise HTTPException(404, f"No quote #{quote_id}.")
    if not quotes.update(s.config.db_path, quote_id, body):
        raise HTTPException(422, "Nothing to change.")
    s.event(request, "quote.edit", f"quote #{quote_id}", ", ".join(sorted(body)))
    return {"quote": quotes.one(s.config.db_path, quote_id)}


@router.delete("/api/quotes/{quote_id}")
def remove_quote(request: Request, quote_id: int):
    if not quotes.drop(s.config.db_path, quote_id):
        raise HTTPException(404, f"No quote #{quote_id}.")
    s.event(request, "quote.delete", f"quote #{quote_id}")
    return {"deleted": quote_id}


class ContactBody(BaseModel):
    company: str
    person: str | None = None
    phone: str | None = None
    email: str | None = None
    address: str | None = None
    incoterm: str | None = None
    site: str | None = None
    note: str | None = None


class OrderTerms(BaseModel):
    contact_id: int | None = None
    incoterm: str | None = None
    ship_by: str | None = None


# Two requests for the contact book can arrive together, and each would find it empty and
# fill it. One fills, the other waits and then reads what the first wrote.
_filling_contacts = threading.Lock()


@router.get("/api/contacts")
def read_contacts():
    found = contacts.find_file()
    book = contacts.all_of(s.config.db_path)
    if not book and found:
        # Nobody has to press anything: an empty book fills itself from the file.
        with _filling_contacts:
            book = contacts.all_of(s.config.db_path)
            if not book:
                contacts.load_file(s.config.db_path, found, "file")
                book = contacts.all_of(s.config.db_path)
    return {"contacts": _with_lead_times(book), "incoterms": contacts.terms(),
            "file": str(found) if found else None}


def _with_lead_times(rows):
    """Each contact with how long that supplier really takes, learned from the delivery notes."""
    leads = leadtimes.per_supplier(s.config.db_path)
    for row in rows:
        known = leads.get(str(row.get("company") or "").strip().upper())
        row["lead_days"] = known["plan_days"] if known and known["n"] >= leadtimes.MIN_SAMPLES else None
        row["lead_usual"] = known["days"] if known else None
        row["lead_n"] = known["n"] if known else 0
    return rows


@router.get("/api/contacts/file")
def search_contact_file(name: str | None = None, company: str | None = None,
                        all: bool = False):
    """Straight from the csv on the disk, not from the book in the database."""
    try:
        found = contacts.look_up(name, company, limit=None if all else 50)
        _with_lead_times(found["rows"])
        return found
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e
    except (OSError, UnicodeDecodeError) as e:
        raise HTTPException(422, f"That file could not be read ({e}).") from e


@router.post("/api/contacts/load")
def load_contact_file(request: Request, path: str | None = None):
    """Read the supplier contact file on the disk into the contact book."""
    try:
        done = contacts.load_file(s.config.db_path, path, s.who(request)[0])
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e
    except (OSError, UnicodeDecodeError) as e:
        raise HTTPException(422, f"That file could not be read ({e}).") from e
    s.event(request, "contact.load", done["file"],
          f"{done['added']} added, {done['updated']} updated")
    return {**done, "contacts": contacts.all_of(s.config.db_path)}


@router.post("/api/contacts")
def save_contact(request: Request, body: ContactBody):
    try:
        contact_id = contacts.save(s.config.db_path, body.model_dump(), s.who(request)[0])
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    s.event(request, "contact.save", f"contact #{contact_id}", body.company)
    return {"contact_id": contact_id, "contacts": contacts.all_of(s.config.db_path)}


@router.patch("/api/contacts/{contact_id}")
def edit_contact(request: Request, contact_id: int, body: dict):
    if contacts.one(s.config.db_path, contact_id) is None:
        raise HTTPException(404, f"No contact #{contact_id}.")
    try:
        contacts.update(s.config.db_path, contact_id, body)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    s.event(request, "contact.edit", f"contact #{contact_id}", ", ".join(sorted(body)))
    return {"contacts": contacts.all_of(s.config.db_path)}


@router.delete("/api/contacts/{contact_id}")
def remove_contact(request: Request, contact_id: int):
    if not contacts.drop(s.config.db_path, contact_id):
        raise HTTPException(404, f"No contact #{contact_id}.")
    s.event(request, "contact.delete", f"contact #{contact_id}")
    return {"deleted": contact_id}


@router.patch("/api/orders/{list_id}/terms")
def set_order_terms(request: Request, list_id: int, body: OrderTerms):
    if not db.read(s.config.db_path, "SELECT count(*) FROM purchase_orders WHERE list_id = ?",
                   [list_id])[0][0]:
        raise HTTPException(404, f"No purchase order #{list_id}.")
    contacts.put_on_order(s.config.db_path, list_id, body.contact_id, body.incoterm, body.ship_by)
    s.event(request, "order.terms", f"order #{list_id}",
          f"contact {body.contact_id or 'none'} · {body.incoterm or 'no incoterm'}")
    return {"list_id": list_id}


@router.post("/api/orders/extract")
async def extract_list(request: Request, photo: UploadFile):
    raw, media_type, sha = await s._photo_bytes(photo)
    already = db.find_order_by_image(s.config.db_path, sha)
    if already is not None:
        raise HTTPException(409, f"This list is already stored (list #{already}).")

    known = vocabulary.known_articles(s.config.db_path)
    try:
        sheet = await s.get_backend().extract_list(raw, media_type, known)
    except llm.ExtractionError as e:
        raise HTTPException(502, str(e)) from e
    except Exception as e:
        s.log.exception("list reading failed")
        reported = await asyncio.to_thread(s.report, request, "extract",
                                           f"list reading failed: {e}", "/api/orders/extract")
        raise HTTPException(500, f"Reading failed (error #{reported['error_no']}): {e}") from e

    photos.keep(s.config.images_dir / f"{sha}{s.SUPPORTED[media_type]}", raw)
    return JSONResponse({"sha256": sha, "list": sheet})


class ListLine(BaseModel):
    article: str
    designation: str | None = None
    reference: str | None = None
    quantity: float | None = None
    stock: float | None = None
    in_stock: bool | None = None


class HardwareList(BaseModel):
    list_date: str | None = None
    site: str | None = None
    work_item: str | None = None
    drafter: str | None = None
    bill_company: str | None = None
    bill_contact: str | None = None
    bill_address: str | None = None
    lines: list[ListLine] = []


class SaveList(BaseModel):
    sha256: str | None = None
    list: HardwareList
    measurement: s.Measurement | None = None
    # What Plutus showed and what was kept, line by line, so the app can learn from the edit.
    proposal: list[dict] | None = None


@router.post("/api/orders")
def save_list(request: Request, body: SaveList):
    sheet = body.list
    if not sheet.lines:
        raise HTTPException(422, "To fix: no line.")
    for i, line in enumerate(sheet.lines, 1):
        if not line.article.strip():
            raise HTTPException(422, f"To fix: line {i} has no article.")
    if sheet.list_date and parse_date(sheet.list_date) is None:
        raise HTTPException(422, f"Unreadable date: {sheet.list_date!r}")

    payload = sheet.model_dump()
    payload["list_date"] = parse_date(sheet.list_date) if sheet.list_date else None
    payload["lines"] = [dict(l, article=l["article"].strip()) for l in payload["lines"]]

    try:
        list_id = db.save_purchase_order(s.config.db_path, payload, body.sha256 or None,
                                        s._stored_photo(body.sha256), s.who(request)[0])
    except duckdb.ConstraintException as e:
        raise HTTPException(409, "This list is already stored.") from e

    if body.proposal:
        feedback.review_order(s.config.db_path, body.proposal, s.who(request)[0])

    if body.measurement:
        db.record_reading(s.config.db_path, "list", body.measurement.fields,
                          body.measurement.corrected, body.measurement.lines)
    s.event(request, "order.save", f"order #{list_id}",
          f"{sheet.site or 'no site'} · {len(sheet.lines)} line(s)")
    return {"list_id": list_id}


class Stock(BaseModel):
    in_stock: bool | None = None


@router.patch("/api/orders/{list_id}/lines/{line_no}")
def set_in_stock(request: Request, list_id: int, line_no: int, body: Stock):
    if not db.set_in_stock(s.config.db_path, list_id, line_no, body.in_stock):
        raise HTTPException(404, "Line not found.")
    state = {True: "in stock", False: "to order", None: "not ticked"}[body.in_stock]
    s.event(request, "order.tick", f"order #{list_id} line {line_no}", state)
    return {"list_id": list_id, "line_no": line_no, "in_stock": body.in_stock}


@router.delete("/api/orders/{list_id}")
def remove_list(request: Request, list_id: int):
    s._drop(db.delete_purchase_order(s.config.db_path, list_id), f"No list #{list_id}.")
    s.event(request, "order.delete", f"order #{list_id}")
    return {"deleted": list_id}
