"""Route wrangler: the yard, the day's deliveries, and the shortest way round them.

Addresses are turned into coordinates by Nominatim and distances come from OSRM, both run by
OpenStreetMap. Those two calls are the only time this app talks to the internet about a place,
and only when somebody asks for a route. If either is unreachable the ordering falls back to
straight-line distance, which is rougher but needs nothing.
"""
import json
import math
import time
import urllib.parse
import urllib.request

from . import db

USER_AGENT = "StockManager/1.0 (local inventory app; https://openstreetmap.org/copyright)"
NOMINATIM = "https://nominatim.openstreetmap.org/search"
OSRM = "https://router.project-osrm.org"
TIMEOUT = 20
POLITE_SECONDS = 1.1
MAX_STOPS = 25

_last_call = 0.0


class MapError(Exception):
    pass


def _fetch(url):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                                   "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as answer:
        return json.loads(answer.read().decode("utf-8", errors="replace"))


def _polite():
    """Nominatim asks for one call a second, and OSRM's demo server is somebody's gift too."""
    global _last_call
    wait = POLITE_SECONDS - (time.monotonic() - _last_call)
    if wait > 0:
        time.sleep(wait)
    _last_call = time.monotonic()


def geocode(address, fetch=None):
    """One address -> one point. Raises MapError when the map has never heard of it."""
    address = str(address or "").strip()
    if not address:
        raise MapError("No address to look up.")
    fetch = fetch or _fetch
    if fetch is _fetch:
        _polite()
    query = urllib.parse.urlencode({"q": address, "format": "jsonv2", "limit": 1})
    try:
        found = fetch(f"{NOMINATIM}?{query}")
    except Exception as e:
        raise MapError(f"The map service did not answer ({e}).") from e
    if not found:
        raise MapError(f"No place found for {address}.")
    first = found[0]
    return {"lat": float(first["lat"]), "lon": float(first["lon"]),
            "name": first.get("display_name") or address}


def haversine(one, other):
    """Kilometres as the crow flies, for when the road service is not there."""
    radius = 6371.0
    lat1, lon1, lat2, lon2 = map(math.radians, (one["lat"], one["lon"],
                                                other["lat"], other["lon"]))
    step = (math.sin((lat2 - lat1) / 2) ** 2
            + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 2 * radius * math.asin(min(1.0, math.sqrt(step)))


def straight_matrix(points):
    return [[haversine(one, other) * 1000 for other in points] for one in points]


def road_matrix(points, fetch=None):
    """Metres by road between every pair, from OSRM. Falls back to the crow's flight."""
    if len(points) < 2:
        return straight_matrix(points), "straight"
    fetch = fetch or _fetch
    if fetch is _fetch:
        _polite()
    pairs = ";".join(f"{point['lon']},{point['lat']}" for point in points)
    try:
        answer = fetch(f"{OSRM}/table/v1/driving/{pairs}?annotations=distance,duration")
        if answer.get("code") != "Ok":
            raise MapError(answer.get("message") or "OSRM refused the table.")
        distances = answer["distances"]
        if any(cell is None for row in distances for cell in row):
            raise MapError("OSRM could not reach every address by road.")
        return distances, "road"
    except MapError:
        raise
    except Exception:
        return straight_matrix(points), "straight"


def _length(order, matrix, round_trip):
    total = sum(matrix[order[i]][order[i + 1]] for i in range(len(order) - 1))
    if round_trip and len(order) > 1:
        total += matrix[order[-1]][order[0]]
    return total


def order_stops(matrix, round_trip=True):
    """Nearest neighbour from the yard, then 2-opt until it stops getting shorter."""
    count = len(matrix)
    if count < 3:
        return list(range(count))

    order, left = [0], set(range(1, count))
    while left:
        here = order[-1]
        nearest = min(left, key=lambda other: matrix[here][other])
        order.append(nearest)
        left.discard(nearest)

    best = _length(order, matrix, round_trip)
    improved = True
    while improved:
        improved = False
        for first in range(1, count - 1):
            for second in range(first + 1, count):
                swapped = order[:first] + order[first:second + 1][::-1] + order[second + 1:]
                length = _length(swapped, matrix, round_trip)
                if length < best - 1e-6:
                    order, best, improved = swapped, length, True
    return order


def plan(points, round_trip=True, fetch=None):
    """points[0] is the yard. Gives the order to drive them in and what it costs."""
    if len(points) < 2:
        raise MapError("Give the yard and at least one delivery address.")
    matrix, source = road_matrix(points, fetch=fetch)
    order = order_stops(matrix, round_trip)
    legs = []
    for step in range(len(order) - 1):
        legs.append({"from": points[order[step]]["label"], "to": points[order[step + 1]]["label"],
                     "metres": round(matrix[order[step]][order[step + 1]])})
    if round_trip:
        legs.append({"from": points[order[-1]]["label"], "to": points[order[0]]["label"],
                     "metres": round(matrix[order[-1]][order[0]])})
    total = sum(leg["metres"] for leg in legs)
    return {
        "source": source,
        "round_trip": round_trip,
        "stops": [dict(points[index], position=step) for step, index in enumerate(order)],
        "legs": legs,
        "metres": total,
        "km": round(total / 1000, 1),
    }


# ---- the vans -------------------------------------------------------------

def vehicles(db_path):
    rows = db.read(db_path, """
            SELECT vehicle_id, name, shape, plate, cubic, kilos, note
            FROM vehicles ORDER BY vehicle_id
            """)
    return [{"vehicle_id": vehicle_id, "name": name, "shape": shape, "plate": plate,
             "cubic": cubic, "kilos": kilos, "note": note}
            for vehicle_id, name, shape, plate, cubic, kilos, note in rows]


def set_vehicle(db_path, place_id, vehicle_id):
    db.write(db_path, "UPDATE places SET vehicle_id = ? WHERE place_id = ?",
             (int(vehicle_id) if vehicle_id else None, place_id))
    return True


def _minutes(clock):
    """"08:30" -> 510. Anything else is no window at all."""
    text = str(clock or "").strip()
    if not text or ":" not in text:
        return None
    hours, _, rest = text.partition(":")
    try:
        return int(hours) * 60 + int(rest[:2])
    except ValueError:
        return None


def load_of(stops):
    return {"cubic": round(sum(stop.get("cubic") or 0 for stop in stops), 2),
            "kilos": round(sum(stop.get("kilos") or 0 for stop in stops), 1)}


def trouble(van, stops):
    """What does not fit: the load, or a window that closes before the one before it opens."""
    said = []
    carried = load_of(stops)
    if van.get("cubic") and carried["cubic"] > van["cubic"] + 1e-9:
        said.append(f"{carried['cubic']} m3 loaded, {van['cubic']} m3 fits")
    if van.get("kilos") and carried["kilos"] > van["kilos"] + 1e-9:
        said.append(f"{carried['kilos']} kg loaded, {van['kilos']} kg allowed")
    closes = None
    for stop in stops:
        opens, shuts = _minutes(stop.get("open_from")), _minutes(stop.get("open_to"))
        if shuts is not None and closes is not None and shuts < closes:
            said.append(f"{stop['label']} shuts before an earlier stop opens")
        if opens is not None:
            closes = max(closes or 0, opens)
    return said


# ---- the places themselves ------------------------------------------------

def _row(values):
    (place_id, kind, label, address, lat, lon, note, done, added_at, added_by,
     vehicle_id, cubic, kilos, open_from, open_to) = values
    return {"place_id": place_id, "kind": kind, "label": label, "address": address,
            "lat": lat, "lon": lon, "note": note, "done": bool(done),
            "added_at": added_at.isoformat(timespec="minutes") if added_at else None,
            "added_by": added_by, "vehicle_id": vehicle_id, "cubic": cubic, "kilos": kilos,
            "open_from": open_from, "open_to": open_to}


PLACES = """SELECT place_id, kind, label, address, lat, lon, note, done, added_at, added_by,
                   vehicle_id, cubic, kilos, open_from, open_to
            FROM places"""


def places(db_path, kind=None):
    where = " WHERE kind = ?" if kind else ""
    args = [kind] if kind else []
    rows = db.read(db_path, PLACES + where + " ORDER BY kind, place_id", args)
    return [_row(row) for row in rows]


def depot(db_path):
    found = places(db_path, "depot")
    return found[0] if found else None


def add(db_path, address, label=None, kind="stop", note=None, user=None, fetch=None,
        vehicle_id=None, cubic=None, kilos=None, open_from=None, open_to=None):
    """A new address, looked up straight away so the route does not have to wait for it."""
    address = str(address or "").strip()
    if not address:
        raise MapError("No address given.")
    point = geocode(address, fetch=fetch)
    if kind == "depot":
        db.write(db_path, "DELETE FROM places WHERE kind = 'depot'")
    return db.write(db_path, """
            INSERT INTO places (kind, label, address, lat, lon, note, added_by,
                                vehicle_id, cubic, kilos, open_from, open_to)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING place_id
            """, (kind, (label or "").strip() or point["name"].split(",")[0], address,
                  point["lat"], point["lon"], note, user,
                  int(vehicle_id) if vehicle_id else None,
                  float(cubic) if cubic not in (None, "") else None,
                  float(kilos) if kilos not in (None, "") else None,
                  (open_from or "").strip() or None, (open_to or "").strip() or None))[0]


def mark(db_path, place_id, done):
    db.write(db_path, "UPDATE places SET done = ? WHERE place_id = ?", (bool(done), place_id))
    return True


def drop(db_path, place_id):
    if not db.read(db_path, "SELECT count(*) FROM places WHERE place_id = ?",
                   [place_id])[0][0]:
        return False
    db.write(db_path, "DELETE FROM places WHERE place_id = ?", [place_id])
    return True


def clear_stops(db_path):
    held = db.read(db_path, "SELECT count(*) FROM places WHERE kind = 'stop'")[0][0]
    db.write(db_path, "DELETE FROM places WHERE kind = 'stop'")
    return held


def route(db_path, round_trip=True, fetch=None, vehicle_id=None):
    """The day's run: the yard first, then every stop still to do."""
    yard = depot(db_path)
    if yard is None:
        raise MapError("Save the address you leave from first.")
    stops = [place for place in places(db_path, "stop") if not place["done"]]
    if vehicle_id is not None:
        stops = [stop for stop in stops if stop["vehicle_id"] == int(vehicle_id)]
    if not stops:
        raise MapError("No delivery address to drive to.")
    if len(stops) > MAX_STOPS:
        raise MapError(f"That is more than {MAX_STOPS} stops; split the run in two.")
    missing = [place["label"] for place in [yard, *stops] if place["lat"] is None]
    if missing:
        raise MapError(f"No coordinates for: {', '.join(missing)}")
    answer = plan([yard, *stops], round_trip=round_trip, fetch=fetch)
    if vehicle_id is not None:
        van = next((one for one in vehicles(db_path) if one["vehicle_id"] == int(vehicle_id)), None)
        if van:
            answer["vehicle"] = van
            answer["load"] = load_of(stops)
            answer["trouble"] = trouble(van, stops)
    return answer
