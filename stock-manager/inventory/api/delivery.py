"""Deliveries and the route."""

import asyncio
from fastapi import HTTPException
from fastapi import Request
from fastapi.responses import Response
from pydantic import BaseModel

from .. import db
from .. import documents
from .. import outbound
from .. import routing
from .. import shortcuts
from fastapi import APIRouter

from .. import server as s    # the server module, read at call time: its config and helpers

router = APIRouter()

class NewDispatch(BaseModel):
    list_id: int
    vehicle_id: int | None = None
    planned_on: str | None = None
    note: str | None = None


class Handover(BaseModel):
    received_by: str
    delivered: dict[int, float] | None = None
    note: str | None = None


class Failure(BaseModel):
    note: str = ""


@router.get("/api/dispatches")
def read_dispatches():
    return {"dispatches": outbound.listing(s.config.db_path),
            "vehicles": [{"vehicle_id": i, "name": n} for i, n in db.read(
                s.config.db_path, "SELECT vehicle_id, name FROM vehicles ORDER BY vehicle_id")]}


@router.get("/api/dispatches/{dispatch_id}")
def read_dispatch(dispatch_id: int):
    held = outbound.one(s.config.db_path, dispatch_id)
    if held is None:
        raise HTTPException(404, "No such dispatch.")
    return held


@router.post("/api/dispatches")
def new_dispatch(request: Request, body: NewDispatch):
    made = s._do(request, "dispatch.create", f"order #{body.list_id}", lambda: outbound.create_from_order(
        s.config.db_path, body.list_id, body.vehicle_id, body.planned_on, s.who(request)[0], body.note))
    return {"dispatch": made, **read_dispatches()}


@router.post("/api/dispatches/{dispatch_id}/load")
def load_dispatch(request: Request, dispatch_id: int):
    s._do(request, "dispatch.load", f"dispatch #{dispatch_id}", lambda: outbound.load(
        s.config.db_path, dispatch_id))
    return read_dispatches()


@router.post("/api/dispatches/{dispatch_id}/deliver")
def deliver_dispatch(request: Request, dispatch_id: int, body: Handover):
    s._do(request, "dispatch.deliver", f"dispatch #{dispatch_id}", lambda: outbound.deliver(
        s.config.db_path, dispatch_id, body.received_by, body.delivered, body.note))
    return read_dispatches()


@router.post("/api/dispatches/{dispatch_id}/fail")
def fail_dispatch(request: Request, dispatch_id: int, body: Failure):
    s._do(request, "dispatch.fail", f"dispatch #{dispatch_id}", lambda: outbound.fail(
        s.config.db_path, dispatch_id, body.note))
    return read_dispatches()


def _pdf(make, name):
    return Response(make(), media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{name}"'})


@router.get("/api/dispatches/{dispatch_id}/note.pdf")
def dispatch_note_pdf(dispatch_id: int):
    return _pdf_or_422(lambda: documents.delivery_note_pdf(s.config.db_path, dispatch_id),
                       f"bl-{dispatch_id}.pdf")


@router.get("/api/dispatches/{dispatch_id}/labels.pdf")
def dispatch_labels_pdf(dispatch_id: int):
    return _pdf_or_422(lambda: documents.labels_pdf(s.config.db_path, dispatch_id),
                       f"etiquettes-{dispatch_id}.pdf")


def _pdf_or_422(make, name):
    try:
        return _pdf(make, name)
    except (ValueError, documents.Unavailable) as e:
        raise HTTPException(422, str(e)) from e


def _route_screen():
    stops = routing.places(s.config.db_path, "stop")
    vans = routing.vehicles(s.config.db_path)
    for van in vans:
        mine = [stop for stop in stops
                if stop["vehicle_id"] == van["vehicle_id"] and not stop["done"]]
        van["stops"] = len(mine)
        van["load"] = routing.load_of(mine)
        van["trouble"] = routing.trouble(van, mine)
    return {"depot": routing.depot(s.config.db_path), "stops": stops, "vehicles": vans}


@router.get("/api/route")
def read_route_places():
    return _route_screen()


@router.post("/api/route/places")
def add_route_place(request: Request, body: s.PlaceBody):
    kind = "depot" if body.kind == "depot" else "stop"
    try:
        place_id = routing.add(s.config.db_path, body.address, body.label, kind,
                               body.note, s.who(request)[0], vehicle_id=body.vehicle_id,
                               cubic=body.cubic, kilos=body.kilos,
                               open_from=body.open_from, open_to=body.open_to)
    except routing.MapError as e:
        raise HTTPException(422, str(e)) from e
    s.event(request, "route.place", f"{kind} #{place_id}", body.address)
    return {"place_id": place_id, **_route_screen()}


@router.patch("/api/route/places/{place_id}")
def mark_route_place(place_id: int, body: dict):
    if "done" in body:
        routing.mark(s.config.db_path, place_id, bool(body.get("done")))
    if "vehicle_id" in body:
        routing.set_vehicle(s.config.db_path, place_id, body.get("vehicle_id"))
    return _route_screen()


@router.delete("/api/route/places/{place_id}")
def remove_route_place(request: Request, place_id: int):
    if not routing.drop(s.config.db_path, place_id):
        raise HTTPException(404, f"No place #{place_id}.")
    s.event(request, "route.delete", f"place #{place_id}")
    return {"deleted": place_id}


@router.delete("/api/route/stops")
def clear_route_stops(request: Request):
    held = routing.clear_stops(s.config.db_path)
    s.event(request, "route.clear", f"{held} stop(s)")
    return {"deleted": held}


@router.post("/api/route/plan")
async def plan_route(round_trip: bool = True, vehicle_id: int | None = None):
    """Asks OpenStreetMap for road distances; the only call that leaves this machine."""
    try:
        return await asyncio.to_thread(routing.route, s.config.db_path, round_trip, None, vehicle_id)
    except routing.MapError as e:
        raise HTTPException(422, str(e)) from e


@router.get("/api/shortcuts")
def read_shortcuts():
    """The five doors on the landing page: Slack, Discord, the mailboxes, the contact sheet."""
    return {"shortcuts": shortcuts.listing(s.config.data_dir)}


@router.post("/api/shortcuts/{key}")
def open_shortcut(request: Request, key: str):
    """Open one of them outside this window. The page sends a name, never a path."""
    try:
        opened = shortcuts.open_it(key, s.config.data_dir)
    except ValueError as e:
        raise HTTPException(404, str(e)) from e
    except FileNotFoundError as e:
        raise HTTPException(422, str(e)) from e
    except RuntimeError as e:
        raise HTTPException(501, str(e)) from e
    s.event(request, "shortcut.open", key, opened["where"])
    return opened
