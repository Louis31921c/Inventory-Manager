"""Is the order Plutus wrote still coherent with the books it was written from?

Before an order goes anywhere, Terminus reads it for sense, as a second pair of eyes would. The
order was written from the buffers some time ago; the shelf, the prices and the suppliers may
have moved since, and a line can be wrong in ways no limit catches: an article asked for twice,
one the buffers no longer want, a quantity that is not what the buffer would ask for now, a
supplier nobody knows, a price nobody can check, a quantity that is not whole packs. Each
finding is a plain sentence. No findings means coherent.
"""

from datetime import date

from . import db, outbox, prices, replenish, units
from .models import normalize_name

TOLERANCE = 0.25          # a line within this share of what the buffers ask for now is the same order


def findings(db_path, list_id, today=None):
    today = today or date.today()
    head = db.read(db_path, "SELECT work_item FROM purchase_orders WHERE list_id = ?", [list_id])
    if not head:
        return [f"purchase order #{list_id} no longer exists"]
    kind = "urgent" if (head[0][0] or "").upper() == "URGENT" else "cycle"
    lines = db.read(db_path, "SELECT article, quantity FROM purchase_order_lines WHERE list_id = ? "
                             "AND quantity IS NOT NULL ORDER BY line_no", [list_id])
    if not lines:
        return ["the order has no line"]
    wanted, _left, _count = replenish._build(db_path, today, kind, ignore_list=list_id)
    asked = {normalize_name(r["article"]): r["quantity"] for r in wanted}

    out, seen = [], set()
    for article, quantity in lines:
        name = normalize_name(article)
        if name in seen:
            out.append(f"{name} is on the order twice")
            continue
        seen.add(name)
        if float(quantity) <= 0:
            out.append(f"{name} has no quantity")
            continue
        if name not in asked:
            out.append(f"{name} is no longer asked for by the buffers")
        elif abs(float(quantity) - asked[name]) > TOLERANCE * asked[name]:
            out.append(f"{name}: {float(quantity):g} was ordered, the buffers now ask for {asked[name]:g}")
        supplier = outbox.supplier_of(db_path, name)
        if supplier is None:
            out.append(f"no supplier is known for {name}")
        elif not prices.sightings(db_path, name, 1):
            out.append(f"no price has ever been seen for {name}: the order cannot be checked")
        packed = units.to_packs(db_path, name, supplier, quantity)
        if packed and abs(packed["base"] - float(quantity)) > 1e-9:
            out.append(f"{name}: {float(quantity):g} is not whole packs of {packed['pack_qty']:g}")
    return out
