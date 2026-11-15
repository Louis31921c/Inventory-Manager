"""A big, coherent history to stress the app with: fifteen years of a large trade group.

    python sample_data/make_big_data.py --out ~/Desktop/Stock-Manager-5GB
    python sample_data/make_big_data.py --out /tmp/small --articles 600 --years 2   # a quick one

It writes a DATA_DIR (inventory.duckdb and its settings), never touching the real one. Open it with

    DATA_DIR=~/Desktop/Stock-Manager-5GB STOCK_NO_PASSWORD=1 .venv/bin/python -m inventory.desktop

Everything is made up and the same seed gives the same history. The numbers are built so they hold
together, not drawn one table at a time:

  * Demand first. Every article has a demand rate, a season and a life (introduced, then perhaps
    dropped). Customer orders (purchase_orders) are that demand, grouped by site and day.
  * Replenishment from the demand. Receipts are planned so that the shelf never goes below zero:
    each delivery covers the next cycle plus the supplier's lead time plus a safety stock that
    differs per article, so some articles sit in red and others in green. Stock on hand is
    therefore the true receipts minus the true shipments.
  * Prices step up with inflation. A bill is the month's deliveries of one supplier at the price
    in force that day, so the three-way match finds it right. A few percent are wrong on purpose.
  * Lead times come from each supplier's habit, and a few things are wrong on purpose so the
    anomaly engine has something to find (see FAULTS at the end of the run).

The scale knobs are --articles and --years; 40000 articles over 15 years comes to about 5 GB.
"""
import argparse
import hashlib
import hmac
import json
import os
import random
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

T0 = time.time()


def say(text):
    print(f"[{time.time() - T0:7.1f}s] {text}", flush=True)


# ---- the firm ----------------------------------------------------------------------------------

# group -> suppliers. The first ones are the trade names the app's own sample already uses.
SUPPLIERS = {
    "fix": ["NORTHGATE", "WESTFIX", "LR SEALTEC", "PROMARK", "BOULONNERIE ATLANTIQUE", "SOFIX DISTRIBUTION",
            "VISSERIE DU SUD-OUEST", "DUPRAT FIXATIONS", "TECHNIFIX 33"],
    "steel": ["METALWORKS", "CUTSTEEL", "VARCO", "FORMA STEEL", "SIDERURGIE GARONNE", "ACIERS DU MEDOC",
              "TOLES ET PROFILES 33", "ALUMINIUM ATLANTIC"],
    "glass": ["CLEARVIEW GLASS", "VERRERIE DE GIRONDE", "CLAIRVERRE SUD", "MIROITERIE BORDELAISE"],
    "seal": ["PLASTOR", "JOINT TWIN", "ETANCHEITE DU SUD", "MASTIC ET CIE"],
    "lock": ["NOVAHARD", "QUINCAILLERIE DU PONT", "SERRURES ET FERMETURES", "BERNARD DISTRIBUTION"],
    "paint": ["AKRON PAINTS", "BERNAY ALKOR", "ZINCO", "THERMOLAQUAGE ATLANTIQUE"],
    "ppe": ["LINKOFFICE", "EPI PRO 33", "SECURITE DU TRAVAIL"],
    "pack": ["MARTEL", "EMBALLAGES DU LITTORAL"],
    "tool": ["OUTILLAGE GIRONDIN", "ABRASIFS SUD", "MACHINES ET OUTILS 33"],
}
SLOW_SUPPLIER = "VERRERIE DE GIRONDE"      # gets slower in the last weeks: something to find

# family: (group, base name, dimension list, variant list, price range, usual quantity, step)
SIZES_M = [f"M{n}X{l}" for n in (4, 5, 6, 8, 10, 12, 14, 16) for l in (12, 16, 20, 25, 30, 40, 50, 60, 80, 100, 120)]
SIZES_S = [f"{d}X{l}" for d in (3.5, 4.2, 4.8, 5.5, 6.3) for l in (13, 16, 19, 25, 32, 38, 45, 50, 60, 70, 80)]
PROFILE = [f"{a}X{b}" for a in (20, 25, 30, 35, 40, 50, 60, 80, 100, 120) for b in (20, 30, 40, 50, 60, 80)]
THICK = ["10/10", "15/10", "20/10", "25/10", "30/10", "40/10", "50/10", "60/10", "80/10"]
SHEETS = ["1000X2000", "1250X2500", "1500X3000", "2000X1000", "2500X1250"]
DIAM = [str(d) for d in (6, 8, 10, 12, 14, 16, 18, 20, 22, 25, 30, 35, 40)]
LENGTHS = ["1500", "2000", "3000", "4000", "6000"]
GLASS_DIM = [f"{w}X{h}" for w in (500, 600, 700, 800, 900, 1000, 1200, 1400) for h in (800, 1000, 1200, 1400, 1600, 2000, 2200)]
FIN_FIX = ["ZN", "INOX A2", "INOX A4", "GALVA", "NOIR", "BRUT", "BICHRO", "DACROMET"]
FIN_STEEL = ["GALVA Z275", "BRUT", "LAQUE RAL 9010", "LAQUE RAL 7016", "LAQUE RAL 9005", "ANODISE", "INOX 304", "INOX 316"]
COLOURS = ["RAL 9010", "RAL 7016", "RAL 9005", "RAL 9006", "RAL 3000", "RAL 5010", "RAL 6005", "RAL 1013"]
FAMILIES = [
    ("fix", "VIS TH", SIZES_M, FIN_FIX, (0.04, 0.9), 400, 50),
    ("fix", "VIS METAUX BHC", SIZES_M, FIN_FIX, (0.05, 1.1), 400, 50),
    ("fix", "VIS TETE FRAISEE", SIZES_M, FIN_FIX, (0.04, 0.9), 400, 50),
    ("fix", "BOULON TH+ECROU", SIZES_M, FIN_FIX, (0.12, 2.4), 200, 25),
    ("fix", "TIGE FILETEE", [f"M{n}X1000" for n in (6, 8, 10, 12, 16, 20)] + ["M8X2000", "M10X2000", "M12X2000"],
     FIN_FIX, (1.1, 9.0), 20, 5),
    ("fix", "ECROU HU", [f"M{n}" for n in (4, 5, 6, 8, 10, 12, 14, 16, 20, 24)], FIN_FIX, (0.02, 0.5), 500, 50),
    ("fix", "RONDELLE", [f"M{n}" for n in (4, 5, 6, 8, 10, 12, 14, 16, 20, 24)],
     ["LARGE ZN", "MOYENNE ZN", "INOX A2", "INOX A4", "GROWER ZN", "FREIN DENTEE"], (0.01, 0.2), 500, 50),
    ("fix", "CHEVILLE ACIER", SIZES_M, ["BTE 50", "BTE 100", "BTE 25", "ZN", "INOX A4"], (0.3, 4.8), 100, 25),
    ("fix", "CHEVILLE NYLON", SIZES_S, ["BTE 100", "BTE 50", "BLANC", "GRIS"], (0.03, 0.7), 200, 50),
    ("fix", "VIS AUTOFOREUSE", SIZES_S, FIN_FIX + COLOURS, (0.03, 0.8), 500, 100),
    ("fix", "RIVET ALU", SIZES_S, ["BTE 500", "BTE 250", "ACIER", "INOX"], (0.03, 0.6), 500, 100),
    ("fix", "CACHE ECROU", [f"M{n}" for n in (6, 8, 10, 12, 14, 16)], COLOURS + ["NYLON BLC", "NYLON NOIR"], (0.03, 0.2), 400, 100),
    ("steel", "TUBE CARRE", PROFILE, [f"EP{e} {f}" for e in (15, 2, 25, 3, 4) for f in ("GALVA", "BRUT", "INOX 304")],
     (11.0, 160.0), 12, 1),
    ("steel", "CORNIERE", PROFILE, [f"EP{e} {f}" for e in (3, 4, 5, 6) for f in ("GALVA", "BRUT", "ALU", "INOX 304")],
     (9.0, 120.0), 10, 1),
    ("steel", "PLAT", PROFILE, ["BRUT", "GALVA", "ALU", "INOX 304", "INOX 316"], (6.0, 140.0), 10, 1),
    ("steel", "TOLE", SHEETS, THICK, (28.0, 380.0), 6, 1),
    ("steel", "PROFIL EN U", PROFILE, ["GALVA Z275", "BRUT", "LAQUE RAL 9010", "ANODISE"], (14.0, 190.0), 6, 1),
    ("steel", "PLATINE", [f"{a}X{a}" for a in (100, 120, 150, 180, 200, 250, 300)],
     [f"EP{e} {f}" for e in (6, 8, 10, 12, 15, 20) for f in ("BRUT", "GALVA", "INOX")], (4.0, 92.0), 20, 1),
    ("steel", "RONDS", DIAM, [f"LG {l} {f}" for l in LENGTHS for f in ("BRUT", "GALVA", "INOX 304")], (5.0, 130.0), 20, 1),
    ("steel", "PIECE PLIEE", [f"{l}X{w}" for l in (300, 500, 800, 1500, 2000, 3000) for w in (150, 200, 250, 370, 500)],
     COLOURS, (14.0, 210.0), 8, 1),
    ("glass", "STADIP PROTECT 44.2", GLASS_DIM, ["INCOLORE", "OPALE", "JPP 4 COTES", "JPP 2 COTES"], (62.0, 420.0), 2, 1),
    ("glass", "DOUBLE VITRAGE 4/16/4", GLASS_DIM, ["ARGON", "WARM EDGE", "FE", "TREMPE", "SOLAIRE"], (58.0, 480.0), 3, 1),
    ("glass", "VERRE FEUILLETE", GLASS_DIM, ["33.2", "44.2", "55.2", "66.2", "88.2"], (66.0, 560.0), 2, 1),
    ("glass", "VERRE TREMPE 10MM", GLASS_DIM, ["INCOLORE", "EXTRA CLAIR", "SATINE", "GRAVE"], (90.0, 640.0), 2, 1),
    ("seal", "JOINT EPDM", [f"{n}MM" for n in (3, 4, 5, 6, 8, 10)], ["NOIR COURONNE 50M", "NOIR COURONNE 25M", "GRIS 50M"],
     (14.0, 120.0), 4, 1),
    ("seal", "SILICONE NEUTRE", ["310ML"], COLOURS + ["TRANSPARENT", "NOIR", "BLANC", "GRIS", "BRONZE"], (4.5, 16.0), 24, 12),
    ("seal", "MOUSSE PU", ["750ML", "500ML", "300ML"], ["PISTOLABLE", "MANUELLE", "COUPE FEU", "BASSE EXPANSION"], (5.0, 21.0), 12, 6),
    ("seal", "MASTIC", ["310ML", "600ML"], ["PU", "MS POLYMERE", "ACRYLIQUE", "BUTYL", "COUPE FEU"], (4.0, 24.0), 24, 12),
    ("lock", "SERRURE", ["AXE 30", "AXE 40", "AXE 50", "AXE 60", "A PICOTER", "A ENCASTRER"],
     ["INOX", "NOIR", "ARGT", "LAITON", "CYLINDRE EUROPEEN"], (24.0, 260.0), 2, 1),
    ("lock", "FERME PORTE", [f"EN{n}" for n in ("2/3", "3/4", "4/5", "3")], ["COULISSE", "BRAS COMPAS", "ARGT", "NOIR", "BLANC"],
     (46.0, 290.0), 4, 1),
    ("lock", "PAUMELLE", [f"HT{h}MM" for h in (80, 100, 120, 140, 160, 200)], ["A SOUDER", "A VISSER", "INOX", "ZN", "NOIR"],
     (2.4, 24.0), 12, 6),
    ("lock", "POIGNEE", ["DOUBLE BEQUILLE", "SIMPLE BEQUILLE", "CUVETTE", "PLAQUE LONGUE", "PLAQUE COURTE"],
     ["INOX", "NOIR", "ALU", "LAITON", "BLANC", "ARGT"], (14.0, 140.0), 4, 1),
    ("paint", "POUDRE POLYESTER", ["25KG", "5KG"], [f"{c} {f}" for c in COLOURS for f in ("MAT", "BRILLANT", "SATINE", "TEXTURE")],
     (62.0, 240.0), 2, 1),
    ("paint", "PEINTURE ANTIROUILLE", ["5L", "20L", "1L"], COLOURS + ["GRIS", "MINIUM", "NOIR"], (12.0, 140.0), 6, 1),
    ("paint", "DEGRAISSANT", ["5L", "20L", "200L"], ["ALCALIN", "ACIDE", "ALU", "SOLVANT", "NEUTRE"], (14.0, 280.0), 4, 1),
    ("ppe", "GANT", [f"T{t}" for t in (7, 8, 9, 10, 11)], ["MANUTENTION PU", "ANTI COUPURE", "SOUDEUR", "NITRILE", "FROID"],
     (0.9, 14.0), 60, 12),
    ("ppe", "LUNETTE", ["INCOLORE", "TEINTEE", "SURLUNETTE", "ANTI BUEE"], ["EN166", "PANORAMIQUE", "ANTI RAYURE"], (1.8, 18.0), 24, 12),
    ("ppe", "CHAUSSURE SECURITE", [f"P{p}" for p in range(37, 48)], ["S3", "S1P", "S3 HAUTE", "SOUDEUR"], (28.0, 96.0), 6, 1),
    ("ppe", "CASQUE ANTIBRUIT", ["SNR 27", "SNR 30", "SNR 34"], ["BLANC", "JAUNE", "SERRE TETE", "EN352"], (6.0, 42.0), 10, 5),
    ("pack", "PALETTE", ["1200X800", "1200X1000", "800X600"], ["CONSIGNEE", "PERDUE", "CP", "EPAL"], (4.0, 24.0), 10, 5),
    ("pack", "FILM ETIRABLE", ["450MM", "500MM", "300MM"], ["17U", "20U", "23U", "MACHINE", "MANUEL"], (3.0, 28.0), 18, 6),
    ("pack", "FEUILLARD", ["13MM", "16MM", "19MM"], ["PP", "PET", "ACIER", "BOBINE 1000M"], (6.0, 64.0), 8, 4),
    ("tool", "DISQUE A TRONCONNER", ["115", "125", "180", "230"], ["INOX 1.0", "ACIER 1.6", "ACIER 3.0", "BTE 25", "BTE 50"], (0.9, 6.0), 50, 25),
    ("tool", "FORET HSS", DIAM, ["BTE 10", "BTE 5", "COBALT", "TIN", "LONG"], (0.6, 38.0), 30, 10),
    ("tool", "LAME SCIE", ["BTE 5", "BTE 10", "BTE 25"], [f"{n} DENTS" for n in (14, 18, 24, 32)], (6.0, 52.0), 10, 5),
    ("tool", "DISQUE LAMELLE", ["115", "125", "180"], [f"GR {g}" for g in (40, 60, 80, 120)], (1.8, 14.0), 50, 25),
]

FIRST_SITES = [
    ("LE PRIEURE", ["PHASE 3", "PORTE C003", "CELLIER TERRASSE BAT C", "SEPARATIFS", "GC"]),
    ("CORAIL", ["PORTAIL BAT C", "GC ATTIQUE BAT. A", "SEPARATIFS BAT. A"]),
    ("ALBATROS", ["ESCALIER", "ECROU ACOUSTIQUE", "COMMANDE FIXATION"]),
    ("SQUARE DES ORMES", ["BAT B", "GC BALCONS BAT C", "GC FENETRES BAT C"]),
    ("PLACE NEUVE", ["PM", "PM ET GC"]),
    ("LES PEUPLIERS", ["PM ET PAC", "PORTE PAC", "PORTES GAINE TECH"]),
    ("ECRIN DES QUAIS", ["HABILLAGE JARDINIERE"]),
    ("CADENCE", ["VITRAGE", "VITRE"]),
    ("TRESSES", ["COUVERTINES"]),
    ("1003", ["ATELIER", "STOCK"]),
    ("LETTRES", ["GRILLE RESEAUX"]),
    ("LA BASTIDE", ["GC TYPE 02"]),
    ("VIA VERDE", ["GC"]),
    ("TURENNE", ["PM"]),
]
SITE_WORDS_A = ["RESIDENCE", "DOMAINE", "CLOS", "PARC", "ECOQUARTIER", "LES JARDINS DE", "VILLA", "TOUR", "HALLE",
                "GROUPE SCOLAIRE", "CLINIQUE", "ZAC", "ILOT", "QUAI", "CENTRE"]
SITE_WORDS_B = ["BACALAN", "CAUDERAN", "MERIGNAC", "PESSAC", "TALENCE", "BEGLES", "LORMONT", "CENON", "FLOIRAC", "BRUGES",
                "EYSINES", "LE BOUSCAT", "VILLENAVE", "GRADIGNAN", "BASSENS", "AMBARES", "MARGAUX", "LIBOURNE", "ARCACHON",
                "SAINT-MEDARD", "BLANQUEFORT", "LA TESTE", "GUJAN", "LORMONT", "SAINT-AUBIN", "CARBON-BLANC", "BOULIAC"]
WORK_ITEMS = ["GC", "GC BALCONS", "PORTE", "PORTAIL", "ESCALIER", "GARDE CORPS", "VITRAGE", "FACADE", "COUVERTINES",
              "SEPARATIFS", "HABILLAGE", "GRILLE", "PM", "PM ET GC", "FENETRES", "VERRIERE", "SAS", "LOCAL VELOS"]
DRAFTERS = ["ALEX", "MARIE", "SAM", "LEO", "INES", "THOMAS", "CAMILLE", "KARIM"]
WHO_RECEIVES = ["JEAN", "PAUL", "MEHDI", "SARAH", "OLIVIER"]
COMPANIES = ["Aurelie Fournier", "Celine Guerin", "Sandrine Dupont", "Marc Bernard", "Claire Roux", "Julien Martin",
             "Nadia Lambert", "Pierre Petit", "Hugo Marchand", "Lea Morel", "Samir Benali", "Anne Rousseau"]


def holidays(first, last):
    from inventory import workdays

    out = {}
    for year in range(first.year, last.year + 2):
        out.update(workdays.french_holidays(year))
    return out


def mix64(x):
    """A cheap hash of an integer array to a pseudo-random uint64, to draw the same number twice."""
    x = x.astype(np.uint64)
    x = (x ^ (x >> np.uint64(30))) * np.uint64(0xBF58476D1CE4E5B9)
    x = (x ^ (x >> np.uint64(27))) * np.uint64(0x94D049BB133111EB)
    return x ^ (x >> np.uint64(31))


def unit(x):
    return (mix64(x) >> np.uint64(11)).astype(np.float64) / float(1 << 53)


# ---- the build -----------------------------------------------------------------------------------

def catalogue(rng, wanted):
    """Articles: name, designation, supplier group and prices, up to `wanted` of them."""
    rows = []
    seen = set()
    for family, (group, base, dims, variants, (lo, hi), usual, step) in enumerate(FAMILIES):
        combos = [(d, v) for d in dims for v in variants]
        rng.shuffle(combos)
        for d, v in combos:
            name = f"{base} {d} {v}"
            if name in seen:
                continue
            seen.add(name)
            # A price that grows with the size in the name, so a long tube costs more than a short one.
            size = (hash(d) % 1000) / 1000
            price = lo + (hi - lo) * (0.1 + 0.9 * size ** 2) * rng.uniform(0.7, 1.3)
            rows.append({"name": name, "family": family, "group": group, "price": round(max(price, 0.01), 2),
                         "usual": usual, "step": step})
    rng.shuffle(rows)
    # The app's own sample articles are always in, so the gate and the baskets have something familiar.
    from inventory.simulation_catalogue import ARTICLES

    keep = []
    known = {a[0] for a in ARTICLES}
    for article, designation, supplier, usual, price in ARTICLES:
        keep.append({"name": article, "family": -1, "group": None, "price": price, "usual": usual,
                     "step": max(1, int(usual / 10)), "designation": designation, "supplier": supplier})
    for row in rows:
        if row["name"] not in known:
            keep.append(row)
        if len(keep) >= wanted:
            break
    return keep


def build(args):
    from inventory import audit, db, vault, workdays

    rng = random.Random(args.seed)
    nrng = np.random.default_rng(args.seed)
    today = date.today()
    start = today - timedelta(days=int(365.25 * args.years))
    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    path = out / "inventory.duckdb"
    if path.exists():
        sys.exit(f"{path} already exists: pick a new --out, or delete it first.")
    os.environ["DATA_DIR"] = str(out)
    db.init(path)
    import duckdb

    con = duckdb.connect(str(path))
    con.execute("PRAGMA threads=4")

    # --- calendar ---------------------------------------------------------------------------
    hol = holidays(start, today + timedelta(days=120))
    first_monday = start - timedelta(days=start.weekday())
    weeks_hist = (today - first_monday).days // 7 + 1
    weeks_all = weeks_hist + 14            # demand beyond today is made only to plan what is on its way
    base_ord = first_monday.toordinal()
    max_ord = base_ord + weeks_all * 7 + 14
    is_work = np.zeros(max_ord - base_ord + 1, dtype=bool)
    for i in range(len(is_work)):
        d = date.fromordinal(base_ord + i)
        is_work[i] = d.weekday() < 5 and d not in hol
    next_work = np.zeros(len(is_work), dtype=np.int32)
    nxt = len(is_work) - 1
    for i in range(len(is_work) - 1, -1, -1):
        if is_work[i]:
            nxt = i
        next_work[i] = nxt
    today_off = today.toordinal() - base_ord
    say(f"{weeks_hist} weeks of history from {first_monday}, {int(is_work[:today_off+1].sum())} working days")

    # --- suppliers, sites -----------------------------------------------------------------------
    supplier_list, group_of = [], {}
    for group, names in SUPPLIERS.items():
        for name in names:
            supplier_list.append(name)
            group_of[name] = group
    sid = {name: i for i, name in enumerate(supplier_list)}
    S = len(supplier_list)
    sup_lead = np.array([rng.choice([2, 3, 4, 5, 7, 9, 12, 16, 21]) for _ in supplier_list], dtype=float)
    for i, name in enumerate(supplier_list):
        if group_of[name] == "glass":
            sup_lead[i] = rng.choice([10, 14, 18, 24])
        if name in ("NORTHGATE", "WESTFIX", "LINKOFFICE"):
            sup_lead[i] = rng.choice([2, 3])
    sup_lead[sid[SLOW_SUPPLIER]] = 12

    sites = []          # (name, [items], first_week, last_week, cluster)
    names_used = set()
    K = 8
    for i, (name, items) in enumerate(FIRST_SITES):
        sites.append([name, items, weeks_hist - rng.randint(30, 140), weeks_hist + 20, i % K])
        names_used.add(name)
    target_sites = max(30, int(args.years * 12))
    while len(sites) < target_sites:
        name = f"{rng.choice(SITE_WORDS_A)} {rng.choice(SITE_WORDS_B)}"
        if rng.random() < 0.4:
            name += f" {rng.choice(['A', 'B', 'C', 'NORD', 'SUD', 'EST', 'OUEST', 'II', 'III'])}"
        if name in names_used:
            continue
        names_used.add(name)
        first = rng.randint(-20, max(1, weeks_hist - 26))
        span = rng.randint(26, 130)
        sites.append([name, rng.sample(WORK_ITEMS, rng.randint(1, 6)), first, first + span, rng.randrange(K)])
    n_sites = len(sites)
    # Which sites are open in which week, by cluster: padded so a site can be drawn by index.
    M = 24
    act = np.full((weeks_all, K, M), -1, dtype=np.int32)
    nact = np.zeros((weeks_all, K), dtype=np.int32)
    for w in range(weeks_all):
        for si, (_n, _i, f, l, k) in enumerate(sites):
            if f <= w <= l and nact[w, k] < M:
                act[w, k, nact[w, k]] = si
                nact[w, k] += 1
        for k in range(K):                      # a quiet week in a cluster borrows from any site
            if nact[w, k] == 0:
                pool = [si for si, s in enumerate(sites) if s[2] <= w <= s[3]] or [0]
                act[w, k, 0] = pool[0]
                nact[w, k] = 1
    max_items = max(len(s[1]) for s in sites)
    site_items = np.array([len(s[1]) for s in sites], dtype=np.int64)

    # --- articles -------------------------------------------------------------------------------
    cat = catalogue(rng, args.articles)
    A = len(cat)
    say(f"{A} articles, {S} suppliers, {n_sites} sites")
    art = pd.DataFrame(cat)
    art["article_id"] = np.arange(A)
    s1 = np.zeros(A, dtype=np.int64)
    s2 = np.full(A, -1, dtype=np.int64)
    for i, row in enumerate(cat):
        if row.get("supplier"):
            s1[i] = sid[row["supplier"]]
        else:
            s1[i] = sid[rng.choice(SUPPLIERS[row["group"]])]
        # Roughly one article in three can be had from a second supplier.
        mates = [n for n in SUPPLIERS[group_of[supplier_list[s1[i]]]] if sid[n] != s1[i]]
        if mates and rng.random() < 0.33:
            s2[i] = sid[rng.choice(mates)]
    art["supplier1"] = s1
    art["supplier2"] = s2
    usual = art["usual"].to_numpy(dtype=float)
    step = art["step"].to_numpy(dtype=float)
    price0 = art["price"].to_numpy(dtype=float)
    ref_no = nrng.integers(100000, 999999, A)
    art["designation"] = [row.get("designation") or f"{ref_no[i]} - {row['name']}" for i, row in enumerate(cat)]
    art["reference"] = ref_no.astype(str)

    # How an article lives: its rate, when it is introduced and when it is dropped.
    base = np.clip(nrng.lognormal(np.log(args.rate), 1.1, A), 0.01, 12.0)
    start_w = np.where(nrng.random(A) < 0.55, 0, nrng.integers(0, max(1, weeks_hist - 20), A)).astype(np.int64)
    life = nrng.integers(60, 700, A)
    end_w = np.where(nrng.random(A) < 0.3, np.minimum(start_w + life, weeks_all), weeks_all).astype(np.int64)
    cycle = nrng.choice([1, 2, 3, 4], A, p=[0.35, 0.35, 0.2, 0.1]).astype(np.int64)
    offset = nrng.integers(0, 4, A).astype(np.int64)
    ss_k = nrng.choice([0.0, 0.25, 0.5, 1.0, 1.5, 3.0, 6.0], A, p=[0.1, 0.2, 0.25, 0.2, 0.12, 0.08, 0.05])
    cluster = nrng.integers(0, K, A)
    lead_w = np.ceil(sup_lead[s1] / 7.0).astype(np.int64)
    line_q = np.maximum(step, np.round(usual * 0.35 / step) * step)
    weeks = np.arange(weeks_all)
    # Spring and early summer are the busy months; the trade grows a little each year.
    season = 1.0 + 0.22 * np.sin(2 * np.pi * (weeks / 52.0 - 0.15)) - 0.4 * (((weeks % 52) >= 31) & ((weeks % 52) <= 34))
    growth = 0.55 + 0.9 * weeks / max(1, weeks_hist)

    # --- price steps per (article, supplier) pair ---------------------------------------------------
    pairs = []
    for a in range(A):
        pairs.append((a, s1[a]))
        if s2[a] >= 0:
            pairs.append((a, s2[a]))
    pair_art = np.array([p[0] for p in pairs], dtype=np.int64)
    pair_sup = np.array([p[1] for p in pairs], dtype=np.int64)
    P = len(pairs)
    pair_prim = np.zeros(A, dtype=np.int64)
    pair_sec = np.zeros(A, dtype=np.int64)
    for i, (a, s) in enumerate(pairs):
        if s == s1[a]:
            pair_prim[a] = i
        else:
            pair_sec[a] = i
    inflation = 0.025
    changes = []                       # pair, day offset, price
    mult = np.where(pair_sup == s1[pair_art], 1.0, nrng.uniform(0.93, 1.12, P))
    for p in range(P):
        a = pair_art[p]
        day = int(max(0, start_w[a] * 7 - 20))
        price = price0[a] * mult[p] / (1 + inflation) ** (weeks_hist / 52)
        while day <= today_off + 30:
            changes.append((p, day, round(price, 2)))
            gap = int(nrng.integers(60, 330))
            day += gap
            # About 2.5% a year on average, a little noise on top, never a runaway.
            price = max(0.01, price * (1 + inflation * gap / 365 * nrng.uniform(0.3, 1.7) + nrng.normal(0, 0.01)))
    chg = pd.DataFrame(changes, columns=["pair", "day", "price"]).sort_values(["pair", "day"]).reset_index(drop=True)
    chg_key = chg["pair"].to_numpy(np.int64) * 100000 + chg["day"].to_numpy(np.int64)
    chg_price = chg["price"].to_numpy()
    say(f"{len(chg)} price steps on {P} supplier/article pairs")

    # --- demand and replenishment, article chunks ----------------------------------------------------
    con.execute("""CREATE TABLE t_out (article_id INTEGER, day INTEGER, site_id SMALLINT, item_id SMALLINT, qty DOUBLE)""")
    con.execute("""CREATE TABLE t_in (article_id INTEGER, day INTEGER, supplier_id SMALLINT, site_id SMALLINT,
                   item_id SMALLINT, qty DOUBLE, price DOUBLE, lead INTEGER, future BOOLEAN)""")
    CH = 3000
    for lo in range(0, A, CH):
        hi = min(A, lo + CH)
        n = hi - lo
        sl = slice(lo, hi)
        life_mask = (weeks[None, :] >= start_w[sl, None]) & (weeks[None, :] < end_w[sl, None])
        lam = base[sl, None] * season[None, :] * growth[None, :] * life_mask
        counts = nrng.poisson(lam).astype(np.int32)
        ai, wi = np.nonzero(counts)
        reps = counts[ai, wi]
        ai = np.repeat(ai, reps)
        wi = np.repeat(wi, reps)
        m = len(ai)
        dow = nrng.choice(5, m, p=[0.22, 0.2, 0.2, 0.2, 0.18])
        dayo = next_work[np.minimum(wi * 7 + dow, len(next_work) - 1)]
        gq = line_q[sl][ai] * nrng.lognormal(0, 0.55, m)
        qty = np.maximum(step[sl][ai], np.round(gq / step[sl][ai]) * step[sl][ai])
        # A site open in that week, from the article's own cluster nine times in ten.
        k = np.where(nrng.random(m) < 0.9, cluster[sl][ai], nrng.integers(0, K, m))
        idx = np.minimum((nrng.random(m) * nact[wi, k]).astype(np.int64), nact[wi, k] - 1)
        site = act[wi, k, idx]
        item = (mix64((ai + lo) * 7919 + site) % site_items[site].astype(np.uint64)).astype(np.int64)
        D = np.bincount(ai * weeks_all + wi, weights=qty, minlength=n * weeks_all).reshape(n, weeks_all)
        cum = np.cumsum(D, axis=1)
        # What leaves: only what is dated up to today. The rest was made to plan the deliveries.
        keep = dayo <= today_off
        pd.DataFrame({"article_id": ai[keep] + lo, "day": dayo[keep], "site_id": site[keep], "item_id": item[keep],
                      "qty": qty[keep]}).pipe(lambda d: con.execute("INSERT INTO t_out SELECT * FROM d"))

        rec = np.zeros(n)
        evs = []
        packs = step[sl]
        for w in range(weeks_all):
            on = ((w >= start_w[sl] - lead_w[sl]) & (w < end_w[sl]) & (w >= offset[sl]) &
                  (((w - offset[sl]) % cycle[sl]) == 0))
            if not on.any():
                continue
            h = np.minimum(w + cycle[sl] + lead_w[sl], weeks_all - 1)
            ending = (end_w[sl] - w) <= (cycle[sl] + lead_w[sl] + 1)        # no safety stock on the way out
            need = cum[np.arange(n), h] + np.where(ending, 0.0, ss_k[sl] * base[sl] * line_q[sl]) - rec
            need = np.where(on & (need > 0), need, 0.0)
            got = np.ceil(need / packs - 1e-9) * packs
            who = np.nonzero(got > 0)[0]
            if not len(who):
                continue
            rec[who] += got[who]
            dd = nrng.choice(5, len(who), p=[0.3, 0.3, 0.2, 0.1, 0.1])
            day = next_work[np.minimum(w * 7 + dd, len(next_work) - 1)]
            evs.append((who + lo, day, got[who]))
        if evs:
            ea = np.concatenate([e[0] for e in evs])
            ed = np.concatenate([e[1] for e in evs])
            eq = np.concatenate([e[2] for e in evs])
            second = (s2[ea] >= 0) & (nrng.random(len(ea)) < 0.15)
            sup = np.where(second, s2[ea], s1[ea])
            pid = np.where(second, pair_sec[ea], pair_prim[ea])
            pos = np.searchsorted(chg_key, pid * 100000 + ed, side="right") - 1
            price = chg_price[pos]
            wk = np.minimum(ed // 7, weeks_all - 1)
            kk = cluster[ea]
            idx = np.minimum((nrng.random(len(ea)) * nact[wk, kk]).astype(np.int64), nact[wk, kk] - 1)
            site = act[wk, kk, idx]
            item = (mix64(ea * 7919 + site) % site_items[site].astype(np.uint64)).astype(np.int64)
            gkey = sup * 1000000 + ed
            lead = np.maximum(1, np.round(sup_lead[sup] * np.exp(0.28 * (unit(gkey) * 2 - 1) * 2.0))).astype(np.int64)
            # one supplier slides in the last two months
            slow = (sup == sid[SLOW_SUPPLIER]) & (ed > today_off - 70)
            lead = np.where(slow, np.round(lead * 2.3), lead)
            fut = ed > today_off
            keepr = ~fut | (ed - lead <= today_off)         # a delivery still to come only if already ordered
            pd.DataFrame({"article_id": ea[keepr], "day": ed[keepr], "supplier_id": sup[keepr],
                          "site_id": site[keepr], "item_id": item[keepr], "qty": eq[keepr], "price": price[keepr],
                          "lead": lead[keepr], "future": fut[keepr]}).pipe(
                lambda d: con.execute("INSERT INTO t_in SELECT * FROM d"))
        say(f"articles {lo}-{hi}: {m} order lines")
    return dict(con=con, path=path, out=out, art=art, A=A, S=S, supplier_list=supplier_list, sup_lead=sup_lead,
                sites=sites, n_sites=n_sites, base_ord=base_ord, today=today, today_off=today_off, start=start,
                chg=chg, pairs=pairs, pair_art=pair_art, pair_sup=pair_sup, rng=rng, nrng=nrng, hol=hol,
                first_monday=first_monday, weeks_hist=weeks_hist, group_of=group_of, sid=sid, cluster=cluster)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, help="the new DATA_DIR")
    ap.add_argument("--articles", type=int, default=40000)
    ap.add_argument("--years", type=float, default=15)
    ap.add_argument("--rate", type=float, default=0.25, help="median order lines per article per week")
    ap.add_argument("--scans", type=int, default=100000, help="archived pages (delivery notes and bills) to draw")
    ap.add_argument("--grain", type=float, default=0.004, help="scanner grain: more of it, bigger pages")
    ap.add_argument("--dispatch-days", type=int, default=730, help="how far back dispatches are made")
    ap.add_argument("--seed", type=int, default=2026)
    arguments = ap.parse_args()
    from make_big_tables import finish

    state = build(arguments)
    finish(state, arguments)
