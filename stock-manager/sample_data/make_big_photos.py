"""Archived scans for make_big_data.py: a delivery note or a bill, one bit a pixel, packed like inventory/archive.py.

A page is drawn from the very rows it belongs to (supplier, number, date, lines, quantities), then given
scanner grain, levelled to one bit, packed and compressed exactly as archive.pack does, so archive.unpack
gives a PNG back. The app keeps such pages for years, which is where most of a long history's bytes are.
"""
import hashlib
import multiprocessing as mp
import zlib
from datetime import date, timedelta

import numpy as np
from PIL import Image, ImageDraw, ImageFont

W, H = 1414, 2000
_font = {}


def _fonts():
    if not _font:
        _font["big"] = ImageFont.load_default(size=44)
        _font["mid"] = ImageFont.load_default(size=30)
        _font["small"] = ImageFont.load_default(size=26)
    return _font


def page(task):
    kind, ident, supplier, number, day, lines, totals, grain = task
    f = _fonts()
    rng = np.random.default_rng(ident * 2 + (kind == "bill"))
    img = Image.new("L", (W, H), 255)
    d = ImageDraw.Draw(img)
    d.text((90, 90), supplier, font=f["big"], fill=0)
    d.text((90, 160), "BON DE LIVRAISON" if kind == "note" else "FACTURE", font=f["mid"], fill=0)
    d.text((W - 560, 100), f"N° {number}", font=f["mid"], fill=0)
    d.text((W - 560, 150), day.strftime("%d/%m/%Y"), font=f["mid"], fill=0)
    d.rectangle((70, 240, W - 70, 300), outline=0, width=3)
    d.text((90, 252), "Designation", font=f["small"], fill=0)
    d.text((W - 360, 252), "Quantite" if kind == "note" else "Montant", font=f["small"], fill=0)
    y = 320
    for text, qty in lines[:34]:
        d.text((90, y), text[:62], font=f["small"], fill=0)
        d.text((W - 300, y), qty, font=f["small"], fill=0)
        d.line((70, y + 36, W - 70, y + 36), fill=150, width=1)
        y += 46
    if totals:
        d.text((W - 640, H - 330), totals, font=f["mid"], fill=0)
    d.text((90, H - 260), "Reception :", font=f["small"], fill=0)
    # a signature, as a wandering line
    pts = [(300 + i * 18, H - 230 + int(30 * np.sin(i / 2.0 + ident % 7) + rng.integers(-6, 6))) for i in range(30)]
    d.line(pts, fill=0, width=4)
    a = np.asarray(img, dtype=np.uint8)
    ink = a < 128
    # scanner grain: specks of dust and a shaded edge
    ink |= rng.integers(0, 4096, a.shape, dtype=np.uint16) < int(grain * 4096 * 16) // 16
    ink[:, :6] |= rng.integers(0, 2, (H, 6), dtype=np.uint8).astype(bool)
    bits = np.packbits(~ink, axis=1)
    data = zlib.compress(bits.tobytes(), 6)
    return kind, ident, hashlib.sha256(data).hexdigest(), W, H, data, 900000 + int(rng.integers(0, 900000))


def archive(con, today_off, base, notes, bills, grain, say):
    import pandas as pd

    tasks = []
    for note_id, supplier, day, body, total in notes:
        lines = []
        for row in (body or "").split("\n"):
            name, _, qty = row.rpartition(" x ")
            lines.append((name, qty))
        tasks.append(("note", note_id, supplier, f"BL-{note_id:07d}", base + timedelta(days=day), lines,
                      f"{len(lines)} lignes", grain))
    for bill_id, supplier, number, bdate, net, tax, total in bills:
        lines = [("Marchandises livrees dans le mois", f"{net:,.2f}"), ("TVA 20 %", f"{tax:,.2f}")]
        tasks.append(("bill", bill_id, supplier, number, bdate, lines, f"TOTAL TTC  {total:,.2f} EUR", grain))
    say(f"scans: drawing {len(tasks)} pages")
    done, batch = 0, []
    with mp.get_context("fork").Pool(4) as pool:
        for out in pool.imap(page, tasks, chunksize=64):
            batch.append(out)
            if len(batch) >= 4000:
                done += _store(con, batch, base, pd)
                batch = []
                say(f"scans: {done} pages stored")
    done += _store(con, batch, base, pd)
    return done


def _store(con, batch, base, pd):
    if not batch:
        return 0
    df = pd.DataFrame(batch, columns=["kind", "ident", "sha256", "width", "height", "data", "bytes_from"])
    con.execute("""INSERT INTO photo_archive (sha256, width, height, data, bytes_from, archived_on)
                   SELECT sha256, width, height, data, bytes_from, CURRENT_DATE - 30 FROM df ON CONFLICT DO NOTHING""")
    con.execute("UPDATE notes SET image_sha256 = df.sha256, image_path = NULL FROM df WHERE df.kind = 'note' AND notes.note_id = df.ident")
    con.execute("UPDATE bills SET image_sha256 = df.sha256, image_path = NULL FROM df WHERE df.kind = 'bill' AND bills.bill_id = df.ident")
    return len(batch)
