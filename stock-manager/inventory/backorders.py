"""What a delivery note still owes: the lines marked as a backorder when it was read."""
from datetime import date

from . import db
from .models import parse_date


def reconcile(db_path):
    """A waiting line settles itself when a later delivery note brings the article in full.

    Same supplier, same article, same site and work item, on a note read after the one that
    marked it missing and not itself marked as a backorder. Nobody presses anything."""
    waiting = db.read(db_path, """
            SELECT note_id, line_no, article, coalesce(supplier, ''), coalesce(site, ''),
                   coalesce(work_item, '')
            FROM inventory_lines WHERE backorder AND settled_on IS NULL
            """)
    if not waiting:
        return 0
    came = db.read(db_path, """
            SELECT note_id, article, coalesce(supplier, ''), coalesce(site, ''),
                   coalesce(work_item, '')
            FROM inventory_lines WHERE NOT coalesce(backorder, FALSE) AND quantity > 0
            """)
    brought = {}
    for note_id, article, supplier, site, work_item in came:
        key = (article, supplier, site, work_item)
        brought[key] = max(brought.get(key, 0), note_id)
    done = [(note_id, line_no) for note_id, line_no, article, supplier, site, work_item in waiting
            if brought.get((article, supplier, site, work_item), 0) > note_id]
    for note_id, line_no in done:
        settle(db_path, note_id, line_no)
    return len(done)


def rows(db_path, settled=False):
    """One line per article still to come, newest note first."""
    reconcile(db_path)
    where = "l.settled_on IS NOT NULL" if settled else "l.settled_on IS NULL"
    found = db.read(db_path, f"""
            SELECT l.note_id, l.line_no, l.article, l.quantity, l.designation,
                   l.supplier, l.delivery_date, l.site, l.work_item,
                   l.expected_on, l.settled_on,
                   n.image_path IS NOT NULL
            FROM inventory_lines l JOIN notes n USING (note_id)
            WHERE l.backorder AND {where}
            ORDER BY coalesce(l.expected_on, l.delivery_date) NULLS LAST, l.note_id DESC,
                     l.line_no
            """)
    out = []
    for (note_id, line_no, article, quantity, designation, supplier, delivered, site,
         work_item, expected, settled_on, photo) in found:
        out.append({
            "note_id": note_id, "line_no": line_no, "article": article, "quantity": quantity,
            "designation": designation, "supplier": supplier,
            "delivery_date": delivered.isoformat() if delivered else None,
            "site": site, "work_item": work_item,
            "expected_on": expected.isoformat() if expected else None,
            "settled_on": settled_on.isoformat() if settled_on else None,
            "photo": bool(photo),
        })
    return out


def notes(db_path):
    """The same thing gathered by delivery note, which is how a partial order reads."""
    waiting = rows(db_path)
    by_note = {}
    for line in waiting:
        held = by_note.setdefault(line["note_id"], {
            "note_id": line["note_id"], "supplier": line["supplier"],
            "delivery_date": line["delivery_date"], "site": line["site"],
            "work_item": line["work_item"], "photo": line["photo"], "missing": [],
        })
        held["missing"].append(line)
    return sorted(by_note.values(), key=lambda note: note["note_id"], reverse=True)


def expect(db_path, note_id, line_no, day):
    """The day the supplier says the rest is coming."""
    db.write(db_path, """
            UPDATE inventory_lines SET expected_on = ?
            WHERE note_id = ? AND line_no = ?
            """, (parse_date(day), note_id, line_no))
    return True


def settle(db_path, note_id, line_no, done=True):
    """It arrived (or it never will): the line stops waiting."""
    db.write(db_path, """
            UPDATE inventory_lines SET settled_on = ?
            WHERE note_id = ? AND line_no = ?
            """, (date.today() if done else None, note_id, line_no))
    return True


def summary(db_path):
    waiting = rows(db_path)
    today = date.today()
    late = [line for line in waiting
            if line["expected_on"] and parse_date(line["expected_on"]) < today]
    suppliers = {line["supplier"] for line in waiting if line["supplier"]}
    return {"lines": len(waiting), "notes": len({line["note_id"] for line in waiting}),
            "suppliers": len(suppliers), "late": len(late),
            "undated": len([line for line in waiting if not line["expected_on"]])}
