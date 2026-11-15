"""What the broker reads from the web: towns, addresses and road distances.

Every answer is kept in a cache folder, so a second run gives the same numbers without asking
again, and `offline=True` never asks at all: it uses what the cache holds, then fallbacks that
say they are fallbacks.

| What | From | Licence |
|---|---|---|
| Towns, centres and population | geo.api.gouv.fr | Licence Ouverte |
| Addresses to coordinates | api-adresse.data.gouv.fr (Base Adresse Nationale) | Licence Ouverte |
| Road distances and times | router.project-osrm.org (OpenStreetMap) | ODbL |
"""

import hashlib
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path

from ..routing import haversine

USER_AGENT = "StockManager-broker/0.1 (simulation; https://openstreetmap.org/copyright)"
COMMUNES = "https://geo.api.gouv.fr/communes?codeDepartement={dept}&fields=nom,centre,population,codesPostaux&format=json"
BAN = "https://api-adresse.data.gouv.fr/search/?q={q}&limit=1"
OSRM = "https://router.project-osrm.org/table/v1/driving/{pairs}?annotations=distance,duration"
ROAD_FACTOR = 1.3               # crow's flight to road, when OSRM is not there
FALLBACK_KMH = 45.0


class Offline(Exception):
    pass


class Sources:
    def __init__(self, cache_dir, offline=False, fetch=None):
        self.cache = Path(cache_dir)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.offline = offline
        self._fetch = fetch or _fetch
        self.used = {}           # what answered: "web", "cache" or "fallback", per source

    def _get(self, url, key):
        path = self.cache / key
        if path.exists():
            self.used.setdefault(key.split("-")[0], "cache")
            return path.read_bytes()
        if self.offline:
            raise Offline(key)
        data = self._fetch(url)
        path.write_bytes(data)
        self.used[key.split("-")[0]] = "web"
        return data

    # Towns -------------------------------------------------------------------------------
    def communes(self, dept="33"):
        """[{name, lat, lon, population, postcode}] for a French département."""
        try:
            raw = self._get(COMMUNES.format(dept=dept), f"communes-{dept}.json")
        except Exception:  # noqa: BLE001 - a missing source falls back, it never stops a run
            self.used["communes"] = "fallback"
            return []
        towns = []
        for town in json.loads(raw):
            centre = (town.get("centre") or {}).get("coordinates")
            if not centre:
                continue
            towns.append({"name": town["nom"], "lon": centre[0], "lat": centre[1],
                          "population": town.get("population") or 0,
                          "postcode": (town.get("codesPostaux") or [""])[0]})
        return towns

    # Addresses ---------------------------------------------------------------------------
    def geocode(self, address):
        """(lat, lon) of a French address from the national address base, or None."""
        book_path = self.cache / "geocode-book.json"
        book = json.loads(book_path.read_text()) if book_path.exists() else {}
        if address in book:
            self.used.setdefault("geocode", "cache")
            return tuple(book[address]) if book[address] else None
        if self.offline:
            return None
        try:
            answer = json.loads(self._fetch(BAN.format(q=urllib.parse.quote(address))))
            features = answer.get("features") or []
            point = None
            if features and features[0]["properties"].get("score", 0) >= 0.4:
                lon, lat = features[0]["geometry"]["coordinates"]
                point = (lat, lon)
            self.used["geocode"] = "web"
        except Exception:  # noqa: BLE001 - a missing source falls back, it never stops a run
            return None
        book[address] = point
        book_path.write_text(json.dumps(book, indent=1))
        return point

    # Roads -------------------------------------------------------------------------------
    def road_matrix(self, points):
        """(km, minutes, source) between every pair of {lat, lon} points."""
        pairs = ";".join(f"{p['lon']:.5f},{p['lat']:.5f}" for p in points)
        key = "roads-" + hashlib.sha256(pairs.encode()).hexdigest()[:16] + ".json"
        try:
            answer = json.loads(self._get(OSRM.format(pairs=pairs), key))
            if answer.get("code") != "Ok" or any(c is None for row in answer["distances"] for c in row):
                raise ValueError(answer.get("message") or "incomplete table")
            km = [[cell / 1000 for cell in row] for row in answer["distances"]]
            minutes = [[cell / 60 for cell in row] for row in answer["durations"]]
            return km, minutes, "road (OSRM)"
        except Exception:  # noqa: BLE001 - a missing source falls back, it never stops a run
            (self.cache / key).unlink(missing_ok=True)
            self.used["roads"] = "fallback"
            km = [[haversine(a, b) * ROAD_FACTOR for b in points] for a in points]
            minutes = [[cell / FALLBACK_KMH * 60 for cell in row] for row in km]
            return km, minutes, "straight line x1.3 (fallback)"


def _fetch(url, tries=3):
    last = None
    for attempt in range(tries):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=60) as answer:
                return answer.read()
        except Exception as error:  # noqa: BLE001 - a busy public server: wait a little and ask again
            last = error
            time.sleep(1.5 * (attempt + 1))
    raise last
