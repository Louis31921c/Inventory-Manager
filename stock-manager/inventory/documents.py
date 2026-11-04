"""The paper: an order to attach to a supplier's mail, a delivery note with a box to sign, labels.

Drawn with reportlab, which is optional: without it these say so rather than fail on import.
"""

import io
from datetime import date

from . import db, outbound

try:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
except ImportError:                                  # pragma: no cover
    canvas = None


class Unavailable(Exception):
    pass


def _need():
    if canvas is None:
        raise Unavailable("PDFs need reportlab: pip install reportlab")


def _num(value):
    value = float(value)
    return str(int(value)) if value.is_integer() else f"{value:g}"


def _table(rows, widths):
    table = Table(rows, colWidths=widths, repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#d8e3f3")),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
        ("FONTSIZE", (0, 0), (-1, -1), 9), ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    return table


def order_pdf(db_path, list_id, supplier, lines):
    """An order for one supplier. `lines` is [(article, text, quantity)] as written in the mail."""
    _need()
    styles = getSampleStyleSheet()
    out = io.BytesIO()
    page = SimpleDocTemplate(out, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm,
                             topMargin=16 * mm, bottomMargin=16 * mm,
                             title=f"Commande {list_id}")
    head = db.read(db_path, "SELECT list_date, site, work_item FROM purchase_orders WHERE list_id = ?",
                   [list_id])
    day, site, work = head[0] if head else (None, None, None)
    story = [Paragraph(f"Bon de commande n° {list_id}", styles["Title"]),
             Paragraph(f"Fournisseur : {supplier}", styles["Normal"]),
             Paragraph(" - ".join(str(x) for x in (day, site, work) if x), styles["Normal"]),
             Spacer(1, 8 * mm)]
    rows = [["Article", "Désignation", "Quantité"]] + [
        [Paragraph(article, styles["Normal"]), Paragraph(text or "", styles["Normal"]), qty]
        for article, text, qty in lines]
    story.append(_table(rows, [45 * mm, 90 * mm, 35 * mm]))
    page.build(story)
    return out.getvalue()


def delivery_note_pdf(db_path, dispatch_id):
    _need()
    held = outbound.one(db_path, dispatch_id)
    if held is None:
        raise ValueError("No such dispatch.")
    styles = getSampleStyleSheet()
    out = io.BytesIO()
    page = SimpleDocTemplate(out, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm,
                             topMargin=16 * mm, bottomMargin=16 * mm,
                             title=f"Bon de livraison {dispatch_id}")
    story = [Paragraph(f"Bon de livraison n° {dispatch_id}", styles["Title"]),
             Paragraph(f"Chantier : {held['site'] or '-'}   Lot : {held['work_item'] or '-'}",
                       styles["Normal"]),
             Paragraph(f"Véhicule : {held['vehicle'] or '-'}   Prévu le : {held['planned_on'] or '-'}",
                       styles["Normal"]),
             Spacer(1, 8 * mm)]
    rows = [["Article", "Désignation", "Qté", "Reçu"]] + [
        [Paragraph(l["article"], styles["Normal"]), Paragraph(l["designation"] or "", styles["Normal"]),
         _num(l["quantity"]), "" if l["delivered"] is None else _num(l["delivered"])]
        for l in held["lines"]]
    story += [_table(rows, [48 * mm, 80 * mm, 20 * mm, 22 * mm]), Spacer(1, 14 * mm),
              Paragraph("Reçu par (nom, signature, date) :", styles["Normal"]), Spacer(1, 22 * mm),
              Paragraph("_" * 60, styles["Normal"])]
    if held["received_by"]:
        story.insert(-3, Paragraph(f"Signé par {held['received_by']} le {held['delivered_at']}",
                                   styles["Normal"]))
    page.build(story)
    return out.getvalue()


def labels_pdf(db_path, dispatch_id):
    """One 100 x 60 mm label per line: where it goes, what it is, how many."""
    _need()
    held = outbound.one(db_path, dispatch_id)
    if held is None:
        raise ValueError("No such dispatch.")
    out = io.BytesIO()
    size = (100 * mm, 60 * mm)
    sheet = canvas.Canvas(out, pagesize=size)
    sheet.setTitle(f"Etiquettes {dispatch_id}")
    for line in held["lines"]:
        sheet.setFont("Helvetica-Bold", 16)
        sheet.drawString(6 * mm, 50 * mm, f"{held['site'] or '-'}")
        sheet.setFont("Helvetica", 11)
        sheet.drawString(6 * mm, 43 * mm, f"Lot : {held['work_item'] or '-'}")
        sheet.setFont("Helvetica-Bold", 13)
        sheet.drawString(6 * mm, 30 * mm, line["article"][:34])
        sheet.setFont("Helvetica", 10)
        sheet.drawString(6 * mm, 23 * mm, (line["designation"] or "")[:52])
        sheet.setFont("Helvetica-Bold", 22)
        sheet.drawString(6 * mm, 10 * mm, f"x {_num(line['quantity'])}")
        sheet.setFont("Helvetica", 9)
        sheet.drawRightString(94 * mm, 6 * mm, f"BL {dispatch_id}  {held['planned_on'] or ''}")
        sheet.showPage()
    sheet.save()
    return out.getvalue()
