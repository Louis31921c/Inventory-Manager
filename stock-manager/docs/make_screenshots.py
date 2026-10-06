"""Draw the program's screens as PNGs for the README, from invented data.

    python docs/make_screenshots.py
"""

import sys
import tempfile
from datetime import date
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from inventory import alerts, audit, db, tui, vocabulary  # noqa: E402
from inventory.config import Config, SmsConfig  # noqa: E402
from inventory.models import Line, Note  # noqa: E402

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"
SIZE = 17
COLORS = {"normal": "#39ff14", "dim": "#1c9c10", "bright": "#7dff5c", "invert": "#000000",
          "tab": "#5f5fff", "tab_on": "#000000", "key": "#f4f4f4", "warn": "#ffd23f"}
BACKGROUNDS = {"invert": "#39ff14", "tab_on": "#5f5fff"}
BOLD = {"bright", "invert", "tab_on", "key"}


def demo_config(data_dir):
    sms = SmsConfig(provider="twilio", to="+33600000012", sender="+33700000000",
                    account_sid="sid", auth_token="token", webhook_url="", webhook_token="",
                    max_per_hour=6)
    return Config(backend="claude", gemini_api_key="", photo_retention_days=42,
                  data_dir=Path(data_dir), model="", user="ALEX", sms=sms)


def note(supplier, day, site, work_item, lines, order=None):
    return Note(supplier=supplier, delivery_date=day, order_date=order, site=site,
                work_item=work_item,
                lines=[Line(article=a, quantity=q, backorder=b, designation=d)
                       for a, q, b, d in lines])


def seed(app):
    path = app.config.db_path
    db.save_note(path, note("NORTHGATE FIXINGS", "2026-09-30", "HARBOUR POINT", "PARAPET", [
        ("DOME NUT D10", 400, False, "327600 - DOME NUT D10 A4 STAINLESS BOX 100"),
        ("THROUGH ANCHOR 10X95", 350, False, "314961 - THROUGH ANCHOR 10X95/35-15 A4 BOX 50"),
        ("WASHER M6", 400, False, "327075 - WASHER SERIES M 6 A4 STAINLESS BOX 200"),
        ("BLIND RIVET NUT M8", 1000, True, "348964 - BLIND RIVET NUT CSK M08 STEEL BOX 500"),
    ], "2026-09-24"), "sha1", "", "ALEX")
    db.save_note(path, note("METALWORKS", "2026-09-29", "LIME TREE COURT", "COPINGS", [
        ("SHEARED FOLDED PART 3000X370 RAL 9010", 8, False,
         "Sheared folded part (ref:COPING) 3000 x 370 mm coated 75/100 RAL 9010"),
        ("SPLICE PLATE 370X200 RAL 9010", 8, False,
         "Sheared folded part (ref:SPLICE) 370 x 200 mm coated 75/100 RAL 9010"),
    ], "2026-09-21"), "sha2", "", "MARIE")
    db.save_note(path, note("FORMA STEEL", "2026-09-27", "RIVERSIDE DEPOT", "HANDRAIL", [
        ("PUNCHED FLAT GALVANISED", 2, False, "A14 - Punched flat L 2077 dev 798 DX51D Z275"),
        ("VERGE TRIM GALVANISED", 6, True, "B02 - Verge trim L 3000 dev 333 DX51D Z275"),
    ]), "sha3", "", "ALEX")

    db.save_hardware_list(path, {"list_date": date(2026, 9, 24), "site": "HARBOUR POINT",
                                 "work_item": "PARAPET", "drafter": "N.T", "lines": [
        {"article": "HEX BOLT M6X25", "quantity": 110, "in_stock": True},
        {"article": "WASHER M6", "quantity": 220, "in_stock": True},
        {"article": "STUD M10X100", "quantity": 40, "in_stock": False},
        {"article": "INSERT M6", "quantity": 60, "in_stock": None}]}, "sha4", "", "MARIE")
    audit.record(path, "ALEX", "note.save", "note #3", "FORMA STEEL - 2 line(s)", "terminal")
    audit.record(path, "MARIE", "note.save", "note #2", "METALWORKS - 2 line(s)", "terminal")
    audit.record(path, "MARIE", "list.tick", "list #1 line 3", "to order", "terminal")
    audit.record(path, "ALEX", "vocabulary.merge", "WASHER M6", "RONDELLE M6 -> WASHER M6",
                 "terminal")
    audit.record(path, "ALEX", "export", "deliveries.csv", None, "terminal")
    vocabulary.merge(path, "RONDELLE M6", "WASHER M6", "ALEX")
    alerts.report(app.config, "backup", "backup failed: no space left on device", "backup")
    alerts.report(app.config, "extract", "note reading failed: model timed out", "/read")
    db.record_reading(path, "note", 24, 1, 4)
    db.record_reading(path, "note", 20, 0, 3)
    db.record_reading(path, "list", 12, 2, 4)
    app.reload()


def draw(screen, out):
    font = ImageFont.truetype(FONT, SIZE)
    bold = ImageFont.truetype(FONT_BOLD, SIZE)
    cell_w = font.getlength("M")
    cell_h = SIZE + 7
    image = Image.new("RGB", (int(cell_w * screen.width) + 24, cell_h * screen.height + 20),
                      "#000000")
    pen = ImageDraw.Draw(image)
    for row, cells in enumerate(screen.cells):
        for col, (char, style) in enumerate(cells):
            x, y = 12 + col * cell_w, 10 + row * cell_h
            if style in BACKGROUNDS:
                pen.rectangle([x, y - 2, x + cell_w, y + cell_h - 2], fill=BACKGROUNDS[style])
            if char != " ":
                pen.text((x, y), char, font=bold if style in BOLD else font,
                         fill=COLORS.get(style, COLORS["normal"]))
    image.save(out)
    print(out)


def main():
    out_dir = Path(__file__).resolve().parent
    with tempfile.TemporaryDirectory() as tmp:
        app = tui.App(demo_config(tmp))
        app.start()
        seed(app)

        app.tab = 0
        app.draft = note("NORTHGATE FIXINGS", "2026-09-30", "HARBOUR POINT", "PARAPET", [
            ("DOME NUT D10", 400, False, "327600 - DOME NUT D10 A4 STAINLESS BOX 100"),
            ("THROUGH ANCHOR 10X95", 350, False, "314961 - THROUGH ANCHOR 10X95/35-15 A4 BOX 50"),
            ("SOCKET CAP SCREW M6X30", 200, False, "272001 - SOCKET CAP SCREW 6X30 A4 BOX 200"),
            ("WASHER M6", 400, False, "327075 - WASHER SERIES M 6 A4 STAINLESS BOX 200"),
            ("BLIND RIVET NUT M8", 1000, True, "348964 - BLIND RIVET NUT CSK M08 STEEL BOX 500"),
        ], "2026-09-24")
        app.draft.warnings = ["line 5: the quantity could be 1000 or 1600"]
        app.draft_sel = 5
        app.say("read in 11 s, check it, then S to save")
        draw(tui.render(app, 104, 26), out_dir / "new-note.png")

        app.draft = None
        app.tab = 1
        app.search_sel = 1
        app.say("E edits the work item of the selected line")
        draw(tui.render(app, 104, 26), out_dir / "search.png")

        app.tab = 2
        app.question = "what was asked for on the hardware lists but never delivered?"
        app.answer = {
            "sql": "SELECT l.article, sum(l.quantity) AS asked,\n"
                   "       coalesce(sum(d.units), 0) AS delivered\n"
                   "FROM hardware_list_lines l\n"
                   "LEFT JOIN (SELECT article, sum(quantity) AS units FROM deliveries "
                   "GROUP BY article) d ON d.article = l.article\n"
                   "GROUP BY l.article ORDER BY asked DESC",
            "note": None,
            "columns": ["article", "asked", "delivered"],
            "rows": [("WASHER M6", 220.0, 400.0), ("HEX BOLT M6X25", 110.0, 0.0),
                     ("INSERT M6", 60.0, 0.0), ("STUD M10X100", 40.0, 0.0)],
            "truncated": False}
        app.say("4 row(s)")
        draw(tui.render(app, 104, 26), out_dir / "sql.png")

        app.tab = 3
        app.sheet_cursor = 0
        app.line_cursor = 2
        app.say("S in stock, O to order, C clears it")
        draw(tui.render(app, 104, 26), out_dir / "hardware.png")

        app.tab = 4
        app.say("B backup now, T test message, M merge two names")
        draw(tui.render(app, 104, 32), out_dir / "settings.png")


if __name__ == "__main__":
    main()
