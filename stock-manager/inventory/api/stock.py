"""Stock, receiving, counting, requests, planning and the dashboard."""

import time
from fastapi import HTTPException
from fastapi import Request
from pydantic import BaseModel

from .. import agents
from .. import autonomy
from .. import backorders
from .. import baskets
from .. import charters
from .. import counting
from .. import dashboard
from .. import db
from .. import ddmrp
from .. import feedback
from .. import forecast
from .. import leadtimes
from .. import llm
from .. import prices
from .. import receiving
from .. import reorder
from .. import replenish
from .. import requisitions
from .. import stock
from .. import stockctl
from .. import units
from fastapi import APIRouter

from .. import server as s    # the server module, read at call time: its config and helpers

router = APIRouter()

@router.get("/api/prices/{article}")
def read_prices(article: str):
    return {"article": article, "usual": prices.usual(s.config.db_path, article),
            "seen": prices.sightings(s.config.db_path, article),
            "suppliers": sorted(prices.suppliers(s.config.db_path, article))}


_autonomy_cache = {"at": 0.0, "value": None}


@router.get("/api/dashboard")
def read_dashboard():
    return dashboard.build(s.config.db_path)


@router.get("/api/planned-orders")
def read_planned_orders():
    return dashboard.planned_orders(s.config.db_path)


@router.get("/api/autonomy")
def read_autonomy():
    """The figure on the landing page. Worked out at most every few seconds."""
    now = time.monotonic()
    if _autonomy_cache["value"] is None or now - _autonomy_cache["at"] > 4:
        _autonomy_cache.update(at=now, value=autonomy.measure(s.config.db_path, config=s.config))
    return _autonomy_cache["value"]


class Pack(BaseModel):
    article: str
    pack_qty: float
    supplier: str | None = ""
    base_unit: str | None = "piece"
    pack_label: str | None = None
    moq_packs: int = 1


@router.get("/api/units")
def read_units():
    return {"packs": units.listing(s.config.db_path)}


@router.put("/api/units")
def put_pack(request: Request, body: Pack):
    s._do(request, "units.set", body.article, lambda: units.set_pack(
        s.config.db_path, body.article, body.pack_qty, body.supplier or "", body.base_unit or "piece",
        body.pack_label, body.moq_packs))
    return read_units()


@router.delete("/api/units")
def delete_pack(request: Request, article: str, supplier: str = ""):
    if not units.remove(s.config.db_path, article, supplier):
        raise HTTPException(404, "No such pack size.")
    s.event(request, "units.delete", article)
    return read_units()


@router.get("/api/replenish")
def read_replenish():
    """What the DDMRP buffers ask for now, and what is left alone because it is already on its way."""
    return replenish.overview(s.config.db_path)


@router.post("/api/replenish/run")
def run_replenish(request: Request, kind: str = "cycle"):
    if kind not in ("cycle", "urgent"):
        raise HTTPException(422, "The order is a cycle order or an urgent one.")
    done = replenish.place(s.config.db_path, s.who(request)[0] or "PLUTUS", kind=kind)
    s.event(request, "replenish.run", f"order #{done['list_id']}" if done["list_id"] else "nothing to order",
          f"{kind}, {done['lines']} line(s)")
    return {**done, **replenish.overview(s.config.db_path)}


@router.post("/api/replenish/auto")
def switch_replenish(request: Request, body: s.Switch):
    replenish.set_auto(s.config.db_path, body.on, s.who(request)[0])
    return replenish.overview(s.config.db_path)


class RequestBody(BaseModel):
    site: str
    work_item: str
    needed_by: str | None = None
    text: str = ""
    note: str | None = None


@router.get("/api/requisitions")
def read_requisitions(all: bool = False):
    return {"requests": requisitions.listing(s.config.db_path, include_done=all)}


@router.post("/api/requisitions")
def open_requisition(request: Request, body: RequestBody):
    items = baskets.parse_items(body.text)
    user = s.who(request)[0] or "?"
    try:
        made = requisitions.submit(s.config.db_path, user, body.site, body.work_item, body.needed_by, items, body.note)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    s.event(request, "request.open", f"request #{made['req_id']}", f"{made['site']} / {made['work_item']}")
    return {"request": made, "requests": requisitions.listing(s.config.db_path)}


@router.post("/api/requisitions/{req_id}/cancel")
def cancel_requisition(request: Request, req_id: int):
    s._do(request, "request.cancel", f"request #{req_id}", lambda: requisitions.cancel(
        s.config.db_path, req_id, s.who(request)[0]))
    return {"requests": requisitions.listing(s.config.db_path)}


class CountBody(BaseModel):
    article: str
    quantity: float


class QuantityBody(BaseModel):
    quantity: float


@router.get("/api/receiving")
def read_receiving():
    return receiving.overview(s.config.db_path)


@router.post("/api/receiving/confirm-suggested")
def receiving_confirm(request: Request):
    done = receiving.confirm_suggested(s.config.db_path, s.who(request)[0])
    s.event(request, "receiving.confirm", None, f"{done} line(s)")
    return receiving.overview(s.config.db_path)


@router.post("/api/receiving/{putaway_id}/put-away")
def receiving_put_away(request: Request, putaway_id: int, body: s.PutAwayBody):
    s._do(request, "receiving.putaway", f"line #{putaway_id}", lambda: receiving.put_away(
        s.config.db_path, putaway_id, body.location, s.who(request)[0]))
    return receiving.overview(s.config.db_path)


@router.get("/api/counting")
def read_counting():
    return counting.overview(s.config.db_path)


@router.post("/api/counting/count")
def count_article(request: Request, body: CountBody):
    try:
        done = counting.submit(s.config.db_path, body.article, body.quantity, s.who(request)[0])
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    s.event(request, "count", done["article"], done["state"])
    return {"result": done, **counting.overview(s.config.db_path)}


@router.post("/api/counting/{check_id}/settle")
def settle_count(request: Request, check_id: int, body: QuantityBody):
    s._do(request, "count.settle", f"count #{check_id}", lambda: counting.settle(
        s.config.db_path, check_id, body.quantity, s.who(request)[0]))
    return counting.overview(s.config.db_path)


@router.get("/api/tuning")
def read_tuning():
    """What the app has learned from corrections, refusals, stock-outs and short counts."""
    return {"tuning": feedback.listing(s.config.db_path), "recent": feedback.recent(s.config.db_path)}


@router.delete("/api/tuning/{article}")
def forget_tuning(request: Request, article: str):
    if not feedback.forget(s.config.db_path, article):
        raise HTTPException(404, "Nothing is learned for that article.")
    s.event(request, "tuning.forget", article)
    return read_tuning()


@router.get("/api/leadtimes")
def read_lead_times():
    """How long each supplier really takes, from the delivery notes."""
    return leadtimes.overview(s.config.db_path)


@router.get("/api/stock")
def read_stock():
    return stock.summary(s.config.db_path)


class Waiting(BaseModel):
    expected_on: str | None = None
    settled: bool | None = None


@router.get("/api/backorders")
def read_backorders():
    return {"notes": backorders.notes(s.config.db_path),
            "lines": backorders.rows(s.config.db_path),
            "summary": backorders.summary(s.config.db_path)}


@router.patch("/api/backorders/{note_id}/{line_no}")
def edit_backorder(request: Request, note_id: int, line_no: int, body: Waiting):
    if body.expected_on is not None:
        backorders.expect(s.config.db_path, note_id, line_no, body.expected_on)
    if body.settled is not None:
        backorders.settle(s.config.db_path, note_id, line_no, body.settled)
    s.event(request, "backorder.edit", f"note #{note_id} line {line_no}",
          body.expected_on or ("arrived" if body.settled else "still waiting"))
    return {"summary": backorders.summary(s.config.db_path),
            "notes": backorders.notes(s.config.db_path)}


@router.get("/api/order-queue")
def read_queue():
    return reorder.listing(s.config.db_path)


@router.post("/api/order-queue/collect")
def collect_queue(request: Request):
    """Read every line ticked "to order" now, rather than at the next hour."""
    done = reorder.collect(s.config.db_path)
    s.event(request, "order.queue.read", f"{done['added']} line(s)",
          f"{done['dropped']} left the queue")
    return {**done, **reorder.listing(s.config.db_path)}


@router.post("/api/order-queue/place")
def place_queue(request: Request):
    """Order what has waited its fifteen days, now."""
    done = reorder.place_due(s.config.db_path)
    s.event(request, "order.queue.place", f"{len(done['orders'])} order(s)", "by Plutus")
    return {**done, **reorder.listing(s.config.db_path)}


def _planner_settings(lead, service, horizon):
    lead = min(max(int(lead), 1), 120)
    service = min(max(int(service), 50), 99)
    horizon = min(max(int(horizon), 1), 180)
    return lead, service, horizon


@router.get("/api/movements")
def read_movements(period: str = "week"):
    if period not in forecast.PERIODS:
        raise HTTPException(422, f"Unknown period: {period}.")
    rows = forecast.movements(s.config.db_path, period)
    return {"period": period, "rows": rows,
            "totals": {
                "in_quantity": round(sum(row["in_quantity"] for row in rows), 2),
                "out_quantity": round(sum(row["out_quantity"] for row in rows), 2),
                "net": round(sum(row["net"] for row in rows), 2),
            }}


@router.get("/api/ddmrp")
def read_ddmrp(cycle: int = ddmrp.DEFAULT_CYCLE, horizon: int = ddmrp.DEFAULT_HORIZON):
    cycle, horizon = s._buffer_settings(cycle, horizon)
    return ddmrp.summary(s.config.db_path, cycle, horizon)


@router.get("/api/forecast")
def read_forecast(lead: int = forecast.DEFAULT_LEAD, service: int = forecast.DEFAULT_SERVICE,
                  horizon: int = forecast.DEFAULT_HORIZON):
    lead, service, horizon = _planner_settings(lead, service, horizon)
    answer = forecast.summary(s.config.db_path, lead, service, horizon)
    answer["counts_source"] = len(db.stock_counts(s.config.db_path))
    return answer


class StockCount(BaseModel):
    article: str
    quantity: float | None = None


@router.put("/api/stock-count")
def put_stock_count(request: Request, body: StockCount):
    try:
        article = db.save_stock_count(s.config.db_path, body.article, body.quantity,
                                      s.who(request)[0])
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    s.event(request, "stock.count", body.article,
          "cleared" if body.quantity is None else str(body.quantity))
    return {"article": article or body.article, "quantity": body.quantity}


def _stock_control(request, action, target, work):
    """One door for the stock-control writes: a refusal is the person's to read, not an error."""
    try:
        done = work()
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    s.event(request, f"stock.{action}", target, None)
    return {**done, "control": stockctl.overview(s.config.db_path)}


class Transfer(BaseModel):
    article: str
    from_site: str
    to_site: str
    quantity: float
    note: str | None = None
    force: bool = False


class Adjustment(BaseModel):
    article: str
    site: str | None = None
    quantity: float
    reason: str = ""


class Reservation(BaseModel):
    article: str
    site: str | None = None
    work_item: str | None = None
    quantity: float
    needed_by: str | None = None
    note: str | None = None
    force: bool = False


class CycleCount(BaseModel):
    article: str
    quantity: float


@router.get("/api/stockctl")
def read_stock_control():
    return stockctl.overview(s.config.db_path)


@router.post("/api/stockctl/transfer")
def stock_transfer(request: Request, body: Transfer):
    return _stock_control(request, "transfer", body.article, lambda: stockctl.transfer(
        s.config.db_path, body.article, body.from_site, body.to_site, body.quantity,
        s.who(request)[0], body.note, body.force))


@router.post("/api/stockctl/adjust")
def stock_adjust(request: Request, body: Adjustment):
    return _stock_control(request, "adjust", body.article, lambda: stockctl.adjust(
        s.config.db_path, body.article, body.site, body.quantity, body.reason, s.who(request)[0]))


@router.post("/api/stockctl/reserve")
def stock_reserve(request: Request, body: Reservation):
    return _stock_control(request, "reserve", body.article, lambda: stockctl.reserve(
        s.config.db_path, body.article, body.site, body.work_item, body.quantity,
        body.needed_by, s.who(request)[0], body.note, body.force))


@router.delete("/api/stockctl/reservations/{reservation_id}")
def stock_release(request: Request, reservation_id: int):
    if not stockctl.release(s.config.db_path, reservation_id):
        raise HTTPException(404, "That reservation is not held any more.")
    s.event(request, "stock.release", f"reservation #{reservation_id}")
    return {"control": stockctl.overview(s.config.db_path)}


@router.post("/api/stockctl/count")
def stock_cycle_count(request: Request, body: CycleCount):
    return _stock_control(request, "count", body.article, lambda: stockctl.count(
        s.config.db_path, body.article, body.quantity, s.who(request)[0]))


class OrderDraft(BaseModel):
    lead: int = forecast.DEFAULT_LEAD
    service: int = forecast.DEFAULT_SERVICE
    horizon: int = forecast.DEFAULT_HORIZON
    site: str | None = None
    work_item: str | None = None
    only: list[str] | None = None


@router.post("/api/forecast/draft")
async def draft_order(request: Request, body: OrderDraft):
    """Plutus turns the planner's numbers into an order, for a person to sign."""
    lead, service, horizon = _planner_settings(body.lead, body.service, body.horizon)
    plan = forecast.summary(s.config.db_path, lead, service, horizon)
    rows = plan["articles"]
    asked_for = [name.strip() for name in (body.only or []) if name.strip()]
    if asked_for:
        # Typed by hand, so the whole name is rarely typed: "rondelle m8" finds the one
        # the shelf calls RONDELLE M8, and the exact name still wins over a part of it.
        rows = s._named(rows, asked_for)
        if not rows:
            raise HTTPException(422, "No article on the shelf matches "
                                     + ", ".join(asked_for) + ".")
    else:
        rows = plan["shortlist"]
        if not rows:
            raise HTTPException(422,
                                "Nothing worth ordering: no article is under its reorder point.")

    refused = feedback.refusals(s.config.db_path)
    # The DDMRP is the rule for what to order: its zone and its number go beside the forecast's.
    buffers, _chain, _span = ddmrp.buffers(s.config.db_path)
    zones = {b["article"]: b for b in buffers}
    for row in rows:
        b = zones.get(row["article"])
        if b is not None:
            row["ddmrp"] = {"zone": b["zone"], "net_flow": b["net_flow"], "top_of_red": b["top_of_red"],
                            "top_of_yellow": b["top_of_yellow"], "top_of_green": b["top_of_green"],
                            "to_order": b["to_order"] if b["zone"] in ("red", "yellow") else 0}
    keep = ("article", "ddmrp", "designation", "reference", "supplier", "per_week", "on_hand", "on_order",
            "lead_days", "lead_source", "qty_factor", "safety_factor", "lead_demand", "safety_stock",
            "reorder_point", "cover_days", "suggested", "state",
            "risk", "backorders")
    planner = {
        "settings": plan["settings"],
        "window": plan["window"],
        "counted_articles": plan["counts"]["counted"],
        "history_is_thin": plan["thin"],
        # Named by the person, which is a different question from "what should we order?"
        "asked_for_by_name": asked_for,
        "articles": [dict({key: row[key] for key in keep},
                          refused_90_days=refused.get(row["article"], 0)) for row in rows[:60]],
        "note_on_factors": "qty_factor and safety_factor are what people's corrections have taught "
                           "the app about each article: the numbers above already include them.",
    }

    try:
        answer = await agents.draft_order(planner, model=s.config.model or None,
                                          charter=charters.prompt_block(s.config.db_path, "plutus"))
    except llm.ExtractionError as e:
        s.report(request, "draft", str(e), "Predictions window")
        raise HTTPException(502, str(e)) from e

    lines = []
    taught = {row["article"].upper(): row for row in feedback.listing(s.config.db_path)
              if row["qty_factor"] != 1.0 or row["safety_factor"] != 1.0}
    for line in answer.get("lines", []):
        article = str(line.get("article") or "").strip()
        quantity = line.get("quantity")
        if not article or quantity is None:
            continue
        learned = taught.get(article.upper())
        lines.append({"article": article, "quantity": float(quantity),
                      "unit": line.get("unit"), "supplier": line.get("supplier"),
                      "reference": line.get("reference"),
                      "reason": str(line.get("reason") or ""),
                      "learned": learned})
    # The charter's limits are applied again here, to whatever the model wrote.
    lines, cuts, changed = charters.enforce_plutus(s.config.db_path, lines, rows, asked_for, refused)
    # No line is an answer, not a failure: the agent is told to leave out what is
    # comfortable, so a shelf in good order is meant to come back empty.
    s.event(request, "order.draft", f"{len(lines)} line(s)", "by Plutus")
    return {"lines": lines, "note": str(answer.get("note") or ""),
            "model": s.config.model or "claude", "agent": agents.PLUTUS,
            "drafted_from": len(planner["articles"]),
            "cuts": cuts, "adjusted": changed,
            "asked_for": asked_for,
            "read": [row["article"] for row in rows[:60]],
            "site": body.site, "work_item": body.work_item}
