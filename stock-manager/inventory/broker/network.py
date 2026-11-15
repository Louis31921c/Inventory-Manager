"""The world the broker works in: a hub, the suppliers, the client firms, what they buy, and when.

- The suppliers are the sample catalogue's, placed at the address the contact sheet gives for
  them (geocoded through the national address base). One with no address is put in a town.
- The client firms are real towns of the département (the firms themselves are made up),
  picked among towns of 3,000 to 100,000 people.
- Demand is the sample year's outflow per article (what the app's own simulation ships out),
  given to each client scaled, shifted in time, thinned to the articles it uses, and shaken.
"""

import csv
import os
import random
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .. import simulation
from ..simulation_catalogue import ARTICLES, DEPOT, STOPS

# Pallets one unit takes, by what the designation says. A rough freight table: a box of 200
# screws is a few litres, a 6 m tube is a slice of a long pallet, a sheet is a slice of a stack.
PALLET_RULES = [
    (("TOLE",), 0.05), (("PROFIL", "PLAT ", "COUVERTINE"), 0.04), (("TUBE", "CORNIERE"), 0.03),
    (("VITRAGE", "STADIP"), 0.08), (("PEINTURE", "DEGRAISSANT", "ZINGAGE"), 0.06),
    (("PALETTE",), 1.0), (("ECLISSE", "PLATINE", "PIECE"), 0.01),
    (("SERRURE", "FERME PORTE", "BEQUILLE", "CREMONE", "TRINGLE", "PAUMELLE", "PLAQUE"), 0.01),
    (("JOINT", "FILM", "MOUSSE", "SILICONE", "RESINE"), 0.004),
    (("GANT", "LUNETTE", "DISQUE", "FORET", "TAMIS"), 0.002),
]
SMALL_PART = 0.0004                 # a screw, a nut, a washer, a rivet


def pallets_per_unit(article):
    for words, share in PALLET_RULES:
        if any(word in article for word in words):
            return share
    return SMALL_PART


@dataclass
class Place:
    name: str
    kind: str                       # hub, supplier, client
    lat: float
    lon: float
    address: str = ""
    located_by: str = ""


@dataclass
class Article:
    name: str
    supplier: str
    usual: float
    price: float
    pallets: float


@dataclass
class Network:
    hub: Place
    suppliers: dict
    clients: dict
    articles: dict
    days: list
    demand: dict                    # demand[client][article] -> [quantity per working day]
    notes: list = field(default_factory=list)

    def places(self):
        return [self.hub, *self.suppliers.values(), *self.clients.values()]


def _contacts():
    """{company: address} from the supplier contact sheet beside the program, if there is one."""
    candidates = [os.environ.get("CONTACTS_CSV", ""),
                  Path(__file__).resolve().parents[3] / "supplier-contacts.csv"]
    for path in candidates:
        if path and Path(path).is_file():
            with open(path, newline="", encoding="utf-8") as handle:
                return {row["company"].strip().upper(): row["address"].strip()
                        for row in csv.DictReader(handle) if row.get("company")}
    return {}


def build(sources, clients=12, seed=2026, dept="33"):
    rng = random.Random(seed)
    notes = []
    hub = Place("BROKER HUB", "hub", DEPOT[2], DEPOT[3], DEPOT[1], "catalogue")

    towns = [t for t in sources.communes(dept) if 3000 <= t["population"] <= 100000]
    if not towns:
        towns = [{"name": name, "lat": lat, "lon": lon, "population": 0, "postcode": ""}
                 for name, _address, lat, lon in STOPS]
        notes.append("towns: geo.api.gouv.fr did not answer, the catalogue's sites stand in")

    book = _contacts()
    suppliers = {}
    for name in sorted({supplier for _a, _d, supplier, _u, _p in ARTICLES}):
        address = book.get(name.upper(), "")
        point = sources.geocode(address) if address else None
        located = "address base"
        if not point and "," in address:          # the street is unknown: the town still is
            point, located = sources.geocode(address.rsplit(",", 1)[1].strip()), "address base (town)"
        if point:
            suppliers[name] = Place(name, "supplier", point[0], point[1], address, located)
        else:
            town = rng.choice(towns)
            suppliers[name] = Place(name, "supplier", town["lat"], town["lon"], town["name"],
                                    "town (no address on file)")

    # Client firms: towns weighted by size, never the same town (or town name) twice.
    pool, picked, names = list(towns), [], set()
    while pool and len(picked) < clients:
        town = rng.choices(pool, [max(t["population"], 1) ** 0.5 for t in pool])[0]
        pool.remove(town)
        if town["name"].upper() not in names:
            names.add(town["name"].upper())
            picked.append(town)
    if len(picked) < clients:
        notes.append(f"clients: only {len(picked)} towns of the right size, not {clients}")
    client_places = {f"CLIENT {t['name'].upper()}": Place(f"CLIENT {t['name'].upper()}", "client",
                                                         t["lat"], t["lon"], t["name"], "town centre")
                     for t in picked}

    articles = {a: Article(a, s, float(u), float(p), pallets_per_unit(a))
                for a, _d, s, u, p in ARTICLES}
    days, base = _sample_outflow(seed)
    demand = {}
    for client in client_places:
        scale = rng.uniform(0.15, 0.45)               # each client is a fraction of the sample firm
        shift = rng.randrange(len(days))
        used = rng.sample(sorted(base), k=max(8, int(len(base) * rng.uniform(0.35, 0.7))))
        demand[client] = {}
        for article in used:
            series = base[article][shift:] + base[article][:shift]
            demand[client][article] = [round(q * scale * rng.lognormvariate(0, 0.35), 2) if q else 0.0
                                       for q in series]
    return Network(hub, suppliers, client_places, articles, days, demand, notes)


def _sample_outflow(seed):
    """(working days, {article: [quantity shipped out each day]}) from the app's sample year."""
    year = simulation.build(random.Random(seed))
    days = sorted({o["list_date"] for o in year["orders"]})
    index = {day: i for i, day in enumerate(days)}
    when = {o["list_id"]: index[o["list_date"]] for o in year["orders"]}
    base = {a: [0.0] * len(days) for a, *_ in ARTICLES}
    for line in year["order_lines"]:
        if line["article"] in base:
            base[line["article"]][when[line["list_id"]]] += float(line["quantity"])
    return [date.fromisoformat(d) for d in days], base
