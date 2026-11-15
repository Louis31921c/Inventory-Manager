# Freight broker: functionalities, process, output and results

A broker that controls **both when orders leave and which vehicle carries them**. Most digital
freight brokers match a load that already exists to a truck. This one sits on the purchasing
side too: it knows each client's stock buffers, so it knows how much slack every order has,
and it uses that slack to fill vans and cut empty driving.

> **Where the code lives.** `~/Desktop/Inventory-Manager-main`, branch `pre-licence`:
> `stock-manager/inventory/broker/`, `stock-manager/tests/test_broker.py` and
> `bot-rules/hermes.md`.

---

## 1. Functionalities

| Function | What it does |
|---|---|
| **Live data pull** | Fetches today's diesel prices, real towns, supplier addresses and road distances from French open data and OpenStreetMap. Cached, so reruns give the same numbers. |
| **Client network** | Builds a hub, the catalogue's 17 suppliers at their contact-sheet addresses, and client firms in real towns of a département (Gironde by default). |
| **DDMRP buffers** | Sizes red, yellow and green zones per client and article from a warm-up period, the same way the app does (FEATURES.md, section 3). |
| **Order timing** | Gives every order a window, from the day it's written to the last day it can leave and still arrive before stock reaches red. Never releases an order late. |
| **Early orders (pull-ins)** | Orders an article early when it's near its reorder point and a van passing anyway can carry it for 6 km or less. |
| **Shared tours** | One van fleet from the hub collects from several suppliers and delivers to several clients per tour, ordered by nearest neighbour plus 2-opt. |
| **Smart detours** | An order that must leave today joins a tour when the detour costs less than 80% of a van of its own (always within 40 km). |
| **Pickup days for distant suppliers** | Suppliers more than 60 km from the hub are visited only when one of their orders can wait no longer. Everything else waiting from that supplier, plus articles close to reordering, comes on the same visit. |
| **Hermes, the AI dispatcher** | Reviews each day's plan and may pull orders in or drop early riders. Hard limits are checked in code: 5 changes a day, a 15 km detour cap, van capacity, and only ids from the brief. |
| **Desk mode** | Writes Hermes's daily brief to a file and waits for an answer file, so a person or an assistant can act as dispatcher. |
| **Three-way comparison** | Plays the same weeks, on the same demand, three ways: today, shared fleet, broker. |

### The three scenarios

- **Today (each supplier alone):** each client orders when its buffer says so, and each supplier delivers its own orders from its own yard and drives home.
- **Shared fleet:** the same orders on the same days, carried by one shared fleet in shared tours.
- **Broker:** the shared fleet, plus the broker choosing *when* each order leaves (timing windows, pull-ins, pickup days).

---

## 2. Process

```
Open data ──► Network ──► Demand ──► Buffers ──► Daily loop (x N working days) ──► Report
```

### 2.1 Data sources

| Data | Source | Licence |
|---|---|---|
| Diesel price per station, today | donnees.roulez-eco.fr (French government feed) | Licence Ouverte |
| Towns, centres and population | geo.api.gouv.fr | Licence Ouverte |
| Supplier addresses to coordinates | api-adresse.data.gouv.fr (national address base) | Licence Ouverte |
| Road distances and drive times | router.project-osrm.org (OpenStreetMap) | ODbL |
| Demand pattern | The app's own sample year (`inventory/simulation.py`) | Made-up data |

When a source doesn't answer, the run uses the cache, then labelled fallbacks: the catalogue's
sites for towns, the supplier's town when the street isn't found, and straight-line distance x1.3
for roads.

Demand: the UCI Online Retail II dataset couldn't be reached from this machine, so each client's
demand follows the sample year's outflow per article. It's scaled (15–45% of the sample firm),
shifted in time, reduced to the articles that client uses, and varied by about 35%. **The client
firms are made up; their towns are real.**

### 2.2 Daily loop

For each working day:

1. **Arrivals:** what was released yesterday goes on the client's shelf.
2. **Demand:** clients use what they use; anything not on the shelf counts as a stockout.
3. **Orders:** a buffer at or under the top of yellow writes an order up to the top of green, with
   a last safe day to leave.
4. **Distant suppliers (broker only):** if any order from a supplier more than 60 km away has reached
   its last day, everything waiting from that supplier, plus articles close to reordering, leaves today.
5. **Tours:** the orders that must leave today are grouped into as few, short tours as the vans
   allow (smart detour rule).
6. **Pull-ins (broker only):** orders whose window is still open, and articles near reordering, ride
   along if they add 6 km or less.
7. **Hermes (optional):** reviews what was left behind and what's due in the next 3 days, then pulls
   orders in or drops riders within its limits.
8. **Accounting:** km, empty km, load, fuel, cost, CO2e and service level.

### 2.3 Cost and emissions model

| Parameter | Van (default, Iveco Daily 3.5 t) | Truck (19 t rigid) |
|---|---|---|
| Capacity | 8 pallets | 18 pallets |
| Fuel | 11 L/100 km | 28 L/100 km |
| Running cost | €0.28/km | €0.62/km |
| Driver | €28/h | €32/h |
| Time per stop | 15 min | 20 min |
| CO2e | 3.17 kg per litre (well to wheel, GLEC / ISO 14083 default) | same |

Pallets per unit come from a rough freight table by article type: a box of screws is a small
fraction of a pallet, a steel sheet or glass unit a larger one.

---

## 3. How to run

```bash
cd ~/Desktop/Inventory-Manager-main/stock-manager
DATA_DIR=./data .venv/bin/python -m inventory.broker run --days 60
```

| Option | Default | Meaning |
|---|---|---|
| `--clients` | 12 | number of client firms |
| `--days` | 60 | working days to play after the warm-up |
| `--warmup` | 100 | working days used to size the buffers |
| `--dept` | 33 | French département for client towns |
| `--vehicle` | van | `van` or `truck` |
| `--capacity` | (vehicle) | pallets per vehicle |
| `--pull-km` | 6 | km an early order may add |
| `--hold` | 4 | most days the broker may hold an order |
| `--agent` | off | let Hermes (Claude) review the broker's days |
| `--agent-days` | 5 | how many days Hermes is asked about |
| `--agent-desk DIR` | off | write briefs to `DIR` and wait for answer files |
| `--offline` | off | use only the cache, never the web |
| `--seed` | 2026 | random seed for the client network and demand |

`--agent` needs the `claude` command signed in on your own account (`claude`, then `/login`).

Tests: `.venv/bin/python -m pytest -q tests/test_broker.py` (9 tests, offline).

---

## 4. Output

### 4.1 Console table

One row per measure, one column per scenario, then a one-line summary of the broker against today:

```
Period 2026-03-31 to 2026-06-22, 12 clients, 17 suppliers, diesel 2.167 EUR/l (web), distances: road (OSRM)

                       Today (each supplier alone)   Shared fleet   Broker (timing + shared)
Km driven                                   65,942         37,518                     34,196
...
Broker vs today: +48.1% km saved, +42.0% cost saved, +48.1% CO2e saved, service +3.35% points
```

| Measure | Meaning |
|---|---|
| Vehicle tours / Vehicles sent | tours driven, and vehicles needed for them |
| Km driven / Km empty | total distance, and distance driven with nothing on board |
| Loaded share of km | 1 − empty km / km |
| Vehicle fill (pallet-km) | pallet-km carried / pallet-km the vans could carry |
| Diesel, Cost, Cost per pallet, CO2e | from the cost model above |
| Orders, of which early | orders written, and how many were ordered early |
| Pickup days, distant suppliers | visits to suppliers more than 60 km away |
| Order-days held back | orders waiting at the end of each day, summed |
| Orders released late | released after their last safe day (should always be 0) |
| Service level, Stockout events | share of demand served from the shelf, and days a client ran short |
| Avg stock value | average value of what sits on clients' shelves |
| Hermes changes applied / refused | when the dispatcher is on |

### 4.2 Run file

Each run is saved as `data/broker_runs/run-YYYYMMDD-HHMMSS.json`:

| Key | Contents |
|---|---|
| `period` | first and last day played |
| `settings`, `fleet`, `broker` | every parameter used |
| `data` | diesel price and its source, road source, which sources answered from the web, cache or fallback, and every supplier and client with coordinates |
| `results` | the totals above, per scenario |
| `days` | per scenario, per day: each tour's stops, km, pallets and order ids; pull-ins; distant-supplier pickups; Hermes's decisions with reasons |

### 4.3 Desk files (desk mode)

`brief-NN.json` holds the day's tours, the early riders, the orders the optimizer passed over (with
the km each would add) and what's due in the next 3 days. `answer-NN.json` is
`{"actions": [{"id", "action": "pull_in" | "drop", "reason"}], "note"}`.

---

## 5. Results

All runs use live data pulled on 9 Nov 2026: diesel at €2.167/l (median over the Gironde stations),
17 suppliers geocoded through the national address base, and road distances from OSRM. Demand is
modelled (section 2.1), so **only the differences between scenarios are meaningful; the absolute
euros are model output, not market prices.**

### 5.1 Main result: 60 working days, 12 clients, vans

| | Today | Shared fleet | Broker |
|---|---|---|---|
| Km driven | 65,942 | 37,518 | **34,196** |
| Km empty | 23,727 | 6,724 | **4,111** |
| Loaded share of km | 64.0% | 82.1% | **88.0%** |
| Cost | €71,009 | €44,052 | **€41,219** |
| CO2e | 22,994 kg | 13,082 kg | **11,924 kg** |
| Service level | 84.9% | 84.9% | **88.3%** |
| Stock value | €15,023 | €15,023 | €16,488 |
| Orders released late | 0 | 0 | 0 |

**Broker vs today: 48% fewer km, 42% lower cost, 48% less CO2e, service up 3.4 points.**
Against the shared fleet alone, the broker's timing saves another 9% of km.

### 5.2 What the improvements were worth

The first version had two flaws, found by reading Hermes's daily briefs:

| Broker, 60 days | First version | After the fixes |
|---|---|---|
| Km | 43,980 | 34,196 (−22%) |
| Empty km | 11,325 | 4,111 |
| Loaded share | 74.2% | 88.0% |
| Saving vs today | 34.5% | 48.1% |

- **Detour cap.** A small order more than 40 km off a tour went out alone, for example 349 km for
  0.01 pallets to La Teste, while another tour was already going there. Fixed with the smart detour rule.
- **Distant suppliers.** Three suppliers sit 111–122 km from the hub (Agen, Mont-de-Marsan), and single
  orders sent vans on 300–380 km trips. Fixed with pickup days.
- A first attempt, a separate shuttle via the hub, was dropped: the tests showed it drove more km
  (1,227 against 756), because the extra leg and day cost more than batching saved.

### 5.3 Density matters (first version, 60 days)

| Clients | Saving vs today | Vehicle fill (broker) |
|---|---|---|
| 12 | 34.5% | 3.5% |
| 40 | 43.4% | 8.4% |

Each extra client in an area makes every tour fuller. That's the business case for a broker.

### 5.4 Hermes, the AI dispatcher (5 days, desk mode)

| Day | Decision | Why |
|---|---|---|
| 31 Mar | Pulled in 2 orders for Saint-Sulpice | They shared one 13 km detour; no van due there for 3 days |
| 1 Apr | No change | The only candidate added 48 km for 0.01 pallets |
| 2 Apr | Pulled in Salles | 6.5 km to a remote southern client |
| 3 Apr | No change | Every candidate added 48–181 km |
| 6 Apr | Pulled in Saint-Ciers | 12.8 km to the most remote client, about 60 km north |

4 changes were applied and 0 refused. Over 20 days that meant 32 km more (+0.2%) and no change in
service, so no measurable gain. Hermes's real value was spotting the optimizer's two flaws.

---

## 6. Limits and next steps

- **Orders don't wait overnight yet.** "Order-days held back" is 0: orders trigger only about a day
  before their deadline, so there's no room to hold them. Next step: fixed delivery days per area,
  which widen the windows.
- **Ordering early costs stock:** about 10% more stock value than the shared fleet.
- **Vans are 3–8% full.** These clients buy parcel-sized freight; more clients per area is the lever.
- **13 near-empty tours over 250 km remain**, mostly distant suppliers' must days.
- **Demand is modelled**, from the app's sample year, not real customers.
- **Hermes needs a signed-in `claude` command** to run automatically; inside the desktop app session
  it can't authenticate, which is why desk mode exists.
