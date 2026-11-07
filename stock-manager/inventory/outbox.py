"""Mail to the suppliers, and what comes back.

Four things go out, every one of them as a draft first. A person reads it, changes it if
they want, and presses send; nothing here sends by itself.

  order       the lines of a stored purchase order, one mail a supplier
  quote       a quote request
  chase       "have you got this?" for an order that has not been confirmed, or is past its day
  backorder   "where is the rest?" for what a delivery note left owing

What comes back is read by the mailbox check: a reply from the supplier's own address that
names the order or the quote number confirms it, and a date in it becomes the day to expect
the goods. A chase is suggested when an order has gone unanswered for three days, or the day
promised has gone by, and the day to expect is otherwise the day it was sent plus what that
supplier usually takes.

The mails are written in French, because the suppliers are: they are plain enough to be
changed in the draft.
"""

import re
import smtplib
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from email.utils import make_msgid

from . import backorders, db, leadtimes, mailbox, pricing, quotes, units, workdays
from .models import parse_date

CHASE_AFTER_DAYS = 3
CHASE_EVERY_DAYS = 3
BACKORDER_AFTER_DAYS = 7

# Where each provider takes outgoing mail. Microsoft's sign-in here is for reading only.
SMTP = {
    "proton": ("127.0.0.1", 1025, "starttls"),
    "gmail": ("smtp.gmail.com", 465, "ssl"),
    "yahoo": ("smtp.mail.yahoo.com", 465, "ssl"),
    "icloud": ("smtp.mail.me.com", 587, "starttls"),
}


class SendError(Exception):
    pass


def smtp_for(mail):
    """(host, port, security) for this mailbox, or None with the reason in `why`."""
    import os

    host = os.environ.get("SMTP_HOST", "").strip()
    if host:
        port = int(os.environ.get("SMTP_PORT", "").strip() or 587)
        security = os.environ.get("SMTP_SECURITY", "").strip().lower() or (
            "ssl" if port == 465 else "starttls")
        return host, port, security
    return SMTP.get(mail.provider)


def readiness(mail):
    """Whether a mail could go out now, in words."""
    if not mail.user or not (mail.password or mail.oauth):
        return {"ready": False, "detail": "No mailbox is set up: Settings, Mailbox."}
    if mail.oauth:
        return {"ready": False, "detail": "Sending is not available with the Microsoft "
                "sign-in, which only lets this app read. Use another mailbox to send."}
    found = smtp_for(mail)
    if not found:
        return {"ready": False, "detail": "This provider has no outgoing server here: set "
                "SMTP_HOST (and SMTP_PORT) in the settings file."}
    return {"ready": True, "detail": f"{mail.user} through {found[0]}"}


# ---- drafts ---------------------------------------------------------------

_COLUMNS = ("mail_id, direction, kind, supplier, to_addr, from_addr, subject, body, ref_kind, "
            "ref_id, thread_id, state, error, message_id, created_at, created_by, sent_at, "
            "confirmed_at, eta, chase_count, last_chased_at")
_NAMES = _COLUMNS.replace("\n", "").split(", ")


def _shape(values):
    row = dict(zip(_NAMES, values))
    for key in ("created_at", "sent_at", "confirmed_at", "last_chased_at"):
        row[key] = row[key].isoformat(timespec="minutes") if row[key] else None
    row["eta"] = row["eta"].isoformat() if row["eta"] else None
    return row


def one(db_path, mail_id):
    rows = db.read(db_path, f"SELECT {_COLUMNS} FROM supplier_mail WHERE mail_id = ?", [mail_id])
    return _shape(rows[0]) if rows else None


def _add(db_path, **fields):
    if fields.get("direction") == "out" and "body" in fields:
        fields.setdefault("orig_body", fields["body"])       # to tell later whether it was edited
    names = ", ".join(fields)
    marks = ", ".join("?" for _ in fields)
    row = db.write(db_path, f"INSERT INTO supplier_mail ({names}) VALUES ({marks}) "
                            "RETURNING mail_id", list(fields.values()))
    return row[0]


def contact_email(db_path, supplier):
    rows = db.read(db_path, """
            SELECT email FROM contacts
            WHERE upper(company) = upper(?) AND email IS NOT NULL AND email <> ''
            ORDER BY contact_id LIMIT 1""", [supplier or ""])
    return rows[0][0] if rows else None


def supplier_of(db_path, article):
    """Who this article is bought from: the supplier it has an agreement with, else the
    supplier of its latest price, else of its latest delivery."""
    agreed = pricing.agreement_for(db_path, article)
    if agreed:
        return agreed["supplier"]
    rows = db.read(db_path, """
            SELECT supplier FROM (
                SELECT supplier, seen_on AS day, 1 AS rank FROM article_prices
                WHERE article = ? AND supplier IS NOT NULL
                UNION ALL
                SELECT supplier, delivery_date, 2 FROM inventory_lines
                WHERE article = ? AND supplier IS NOT NULL)
            ORDER BY rank, day DESC LIMIT 1""", [article, article])
    return rows[0][0] if rows else None


def _quantity(value):
    return int(value) if float(value).is_integer() else value


def _existing(db_path, kind, ref_kind, ref_id, supplier=None):
    found = db.read(db_path, """
            SELECT mail_id FROM supplier_mail
            WHERE direction = 'out' AND kind = ? AND ref_kind = ? AND ref_id IS NOT DISTINCT FROM ?
              AND supplier IS NOT DISTINCT FROM ? AND state IN ('draft', 'failed')
            ORDER BY mail_id DESC LIMIT 1""", [kind, ref_kind, ref_id, supplier])
    return found[0][0] if found else None


def _order_groups(db_path, list_id):
    """(site, work item, {supplier: lines}, articles with no supplier) for a stored order.
    Lines ticked "to order" are what goes out; an order nobody ticked goes out whole."""
    head = db.read(db_path, "SELECT list_date, site, work_item FROM purchase_orders "
                            "WHERE list_id = ?", [list_id])
    if not head:
        raise ValueError(f"No purchase order #{list_id}.")
    _day, site, work_item = head[0]
    lines = db.read(db_path, """
            SELECT article, designation, reference, quantity, in_stock
            FROM purchase_order_lines WHERE list_id = ? AND quantity IS NOT NULL
            ORDER BY line_no""", [list_id])
    ticked = [row for row in lines if row[4] is False]
    chosen = ticked or lines
    if not chosen:
        raise ValueError(f"Order #{list_id} has no line with a quantity.")
    groups, unassigned = {}, []
    for article, designation, reference, quantity, _stock in chosen:
        supplier = supplier_of(db_path, article)
        if supplier is None:
            unassigned.append(article)
        else:
            groups.setdefault(supplier, []).append((article, designation, reference, quantity))
    return site, work_item, groups, unassigned


def _line_text(db_path, supplier, article, designation, reference, quantity):
    """One order line as it reads in the mail, in packs when the supplier sells in packs."""
    extra = " - ".join(part for part in (designation, reference and f"réf. {reference}") if part)
    packed = units.to_packs(db_path, article, supplier, quantity)
    if packed:
        text = (f"{packed['packs']} x {packed['pack_label']} de {_quantity(packed['pack_qty'])} "
                f"{article} (soit {_quantity(packed['base'])} {packed['base_unit']})")
        shown = f"{packed['packs']} x {packed['pack_label']} de {_quantity(packed['pack_qty'])}"
    else:
        text = f"{_quantity(quantity)} x {article}"
        shown = str(_quantity(quantity))
    return text + (f" ({extra})" if extra else ""), (designation or "") + (
        f" réf. {reference}" if reference else ""), shown


def bill_to(db_path, list_id):
    """Who the order is billed to, as written when it was saved, or None."""
    rows = db.read(db_path, "SELECT bill_company, bill_contact, bill_address FROM purchase_orders "
                            "WHERE list_id = ?", [list_id])
    if not rows or not any(rows[0]):
        return None
    company, contact, address = rows[0]
    return {"company": company, "contact": contact, "address": address}


def bill_to_lines(billing):
    """The lines of a mail or a PDF that say where the bill goes."""
    if not billing:
        return []
    out = ["Facturation à : " + (billing["company"] or "")]
    if billing["contact"]:
        out.append("À l'attention de : " + billing["contact"])
    if billing["address"]:
        out += [line.strip() for line in billing["address"].splitlines() if line.strip()]
    return out


def draft_order(db_path, list_id, user=None):
    """One draft a supplier for a stored purchase order. Lines nobody can say a supplier
    for come back apart: they are the ones to ring about."""
    site, work_item, groups, unassigned = _order_groups(db_path, list_id)
    made = []
    for supplier, rows in groups.items():
        known = _existing(db_path, "order", "po", list_id, supplier)
        if known is None:                        # one already sent for this order is the order: not a new draft
            gone = db.read(db_path, """
                    SELECT mail_id FROM supplier_mail WHERE direction = 'out' AND kind = 'order'
                      AND ref_kind = 'po' AND ref_id = ? AND supplier IS NOT DISTINCT FROM ?
                      AND state IN ('sent', 'confirmed', 'closed') ORDER BY mail_id DESC LIMIT 1""",
                           [list_id, supplier])
            known = gone[0][0] if gone else None
        if known:
            made.append(one(db_path, known))
            continue
        where = " / ".join(part for part in (site, work_item) if part)
        body = ["Bonjour,", "",
                f"Veuillez trouver ci-dessous notre commande n° {list_id}"
                + (f" ({where})" if where else "") + " :", ""]
        for article, designation, reference, quantity in rows:
            body.append("- " + _line_text(db_path, supplier, article, designation, reference,
                                          quantity)[0])
        billing = bill_to_lines(bill_to(db_path, list_id))
        if billing:
            body += [""] + billing
        body += ["", "Merci de nous confirmer la commande et la date de livraison.",
                 "", "Cordialement"]
        mail_id = _add(db_path, direction="out", kind="order", supplier=supplier,
                       to_addr=contact_email(db_path, supplier),
                       subject=f"Commande n° {list_id}" + (f" - {where}" if where else ""),
                       body="\n".join(body), ref_kind="po", ref_id=list_id, state="draft",
                       created_by=user)
        made.append(one(db_path, mail_id))
    return {"drafts": made, "unassigned": unassigned}


def order_attachment(db_path, row):
    """The order as a PDF to go with its mail, or None when it is not an order or the PDF
    library is not there."""
    if row["kind"] != "order" or row["ref_kind"] != "po" or not row["ref_id"]:
        return None
    try:
        from . import documents

        _site, _work, groups, _left = _order_groups(db_path, row["ref_id"])
        lines = [(article, *_line_text(db_path, row["supplier"], article, designation, reference,
                                       quantity)[1:])
                 for article, designation, reference, quantity in groups.get(row["supplier"], [])]
        if not lines:
            return None
        return (f"commande-{row['ref_id']}.pdf",
                documents.order_pdf(db_path, row["ref_id"], row["supplier"], lines,
                                    bill_to_lines(bill_to(db_path, row["ref_id"]))))
    except Exception:
        return None


def draft_quote(db_path, quote_id, user=None):
    quote = quotes.one(db_path, quote_id)
    if quote is None:
        raise ValueError(f"No quote request #{quote_id}.")
    if not quote["supplier"]:
        raise ValueError("The quote request names no supplier.")
    known = _existing(db_path, "quote", "quote", quote_id, quote["supplier"])
    if known:
        return {"drafts": [one(db_path, known)], "unassigned": []}
    body = ["Bonjour,", "", f"Pourriez-vous nous faire parvenir un prix pour la demande n° "
            f"{quote_id}" + (f" ({quote['site']})" if quote["site"] else "") + " :", ""]
    for row in quote["rows"]:
        body.append(f"- {quotes.show_quantity(row)} {row['article']}"
                    + (f" ({row['designation']})" if row["designation"] else ""))
    if quote["needed_by"]:
        body += ["", f"Besoin pour le {quote['needed_by']}."]
    if quote["note"]:
        body += ["", quote["note"]]
    body += ["", "Merci d'indiquer aussi le délai de livraison.", "", "Cordialement"]
    mail_id = _add(db_path, direction="out", kind="quote", supplier=quote["supplier"],
                   to_addr=contact_email(db_path, quote["supplier"]),
                   subject=f"Demande de prix n° {quote_id}", body="\n".join(body),
                   ref_kind="quote", ref_id=quote_id, state="draft", created_by=user)
    return {"drafts": [one(db_path, mail_id)], "unassigned": []}


def update_draft(db_path, mail_id, to=None, subject=None, body=None):
    row = one(db_path, mail_id)
    if row is None or row["state"] not in ("draft", "failed"):
        raise ValueError("Only a draft can be changed.")
    db.write(db_path, """
            UPDATE supplier_mail SET to_addr = ?, subject = ?, body = ?
            WHERE mail_id = ? RETURNING mail_id""",
             [(to if to is not None else row["to_addr"]),
              (subject if subject is not None else row["subject"]),
              (body if body is not None else row["body"]), mail_id])
    return one(db_path, mail_id)


def discard(db_path, mail_id):
    row = one(db_path, mail_id)
    if row is None or row["state"] not in ("draft", "failed"):
        return False
    db.write(db_path, "DELETE FROM supplier_mail WHERE mail_id = ? RETURNING mail_id", [mail_id])
    return True


# ---- sending --------------------------------------------------------------

def _connect(mail, found):
    host, port, security = found
    if security == "ssl":
        link = smtplib.SMTP_SSL(host, port, context=mailbox._context(mail), timeout=30)
    else:
        link = smtplib.SMTP(host, port, timeout=30)
        link.starttls(context=mailbox._context(mail))
    link.login(mail.user, mail.password)
    return link


def send(db_path, mail_id, mail, user=None, connect=_connect):
    """Send one draft. On a refusal the draft stays, marked failed, with the reason."""
    row = one(db_path, mail_id)
    if row is None or row["state"] not in ("draft", "failed"):
        raise SendError("That mail is not a draft any more.")
    from . import channels

    # A supplier that takes its orders over a webhook gets the same draft posted, not mailed.
    if row["kind"] == "order" and channels.has(db_path, row["supplier"]):
        return _post_order(db_path, row)

    if not (row["to_addr"] or "").strip():
        raise SendError("No address to send it to: add the supplier's e-mail to the draft.")
    ready = readiness(mail)
    if not ready["ready"]:
        raise SendError(ready["detail"])

    message = EmailMessage()
    message["From"] = mail.user
    message["To"] = row["to_addr"].strip()
    message["Subject"] = row["subject"] or ""
    message["Message-ID"] = make_msgid(domain=(mail.user.split("@")[-1] or "localhost"))
    message.set_content(row["body"] or "")
    attached = order_attachment(db_path, row)
    if attached:
        message.add_attachment(attached[1], maintype="application", subtype="pdf",
                               filename=attached[0])
    try:
        link = connect(mail, smtp_for(mail))
        try:
            link.send_message(message)
        finally:
            try:
                link.quit()
            except Exception:
                pass
    except Exception as e:
        db.write(db_path, "UPDATE supplier_mail SET state = 'failed', error = ? "
                          "WHERE mail_id = ? RETURNING mail_id", [str(e)[:300], mail_id])
        raise SendError(f"Not sent: {e}") from e

    db.write(db_path, """
            UPDATE supplier_mail SET state = 'sent', error = NULL, sent_at = now(),
                   from_addr = ?, message_id = ?
            WHERE mail_id = ? RETURNING mail_id""", [mail.user, message["Message-ID"], mail_id])
    if row["kind"] == "chase" and row["thread_id"]:
        db.write(db_path, """
                UPDATE supplier_mail SET chase_count = chase_count + 1, last_chased_at = now()
                WHERE mail_id = ? RETURNING mail_id""", [row["thread_id"]])
    if row["kind"] == "quote" and row["ref_id"]:
        quotes.update(db_path, row["ref_id"], {"state": "sent"})
    return one(db_path, mail_id)


def _post_order(db_path, row):
    from . import channels

    try:
        answer = channels.deliver(db_path, row)
    except Exception as e:
        db.write(db_path, "UPDATE supplier_mail SET state = 'failed', error = ? "
                          "WHERE mail_id = ? RETURNING mail_id", [str(e)[:300], row["mail_id"]])
        raise SendError(f"Not sent: {e}") from e
    db.write(db_path, """
            UPDATE supplier_mail SET state = 'sent', error = NULL, sent_at = now(),
                   from_addr = 'webhook', message_id = ?
            WHERE mail_id = ? RETURNING mail_id""", [f"stock-manager-order-{row['mail_id']}", row["mail_id"]])
    if answer["confirmed"] or answer["promised_date"]:
        set_eta(db_path, row["mail_id"], answer["promised_date"])
    return one(db_path, row["mail_id"])


# ---- waiting on suppliers -------------------------------------------------

def threads(db_path, today=None):
    """Orders and quotes that went out, with the day to expect an answer or the goods.

    Days waited are working days, and the day to expect counts from the day the supplier's
    cut-off lets the order start, so a Friday evening order is not late on Monday."""
    today = today or date.today()
    rows = db.read(db_path, f"""
            SELECT {_COLUMNS} FROM supplier_mail
            WHERE direction = 'out' AND kind IN ('order', 'quote') AND state IN ('sent', 'confirmed')
            ORDER BY sent_at DESC""")
    out = []
    for values in rows:
        row = _shape(values)
        sent_at = (datetime.fromisoformat(row["sent_at"]) if row["sent_at"]
                   else datetime.combine(today, datetime.min.time()))
        sent = sent_at.date()
        calendar = workdays.Calendar(db_path, row["supplier"])
        usual = leadtimes.for_supplier(db_path, row["supplier"], default=7)
        expected = (parse_date(row["eta"]) if row["eta"]
                    else calendar.arrival(sent_at, usual))
        waited = calendar.between(sent, today)
        last = parse_date(row["last_chased_at"][:10]) if row["last_chased_at"] else None
        quiet = last is None or calendar.between(last, today) >= CHASE_EVERY_DAYS
        silent = row["state"] == "sent" and waited >= CHASE_AFTER_DAYS
        late = row["state"] == "confirmed" and calendar.between(expected, today) >= 1
        row.update({"expected_on": expected.isoformat(), "days_waiting": waited,
                    "needs_chase": bool(quiet and (silent or late)),
                    "why": ("no answer yet" if silent else "past the day it was promised")
                    if (silent or late) else None})
        out.append(row)
    return out


def draft_chase(db_path, mail_id, user=None):
    thread = one(db_path, mail_id)
    if thread is None or thread["kind"] not in ("order", "quote"):
        raise ValueError("Only an order or a quote that went out can be chased.")
    known = db.read(db_path, """
            SELECT mail_id FROM supplier_mail
            WHERE kind = 'chase' AND thread_id = ? AND state IN ('draft', 'failed')""",
                    [mail_id])
    if known:
        return {"drafts": [one(db_path, known[0][0])], "unassigned": []}
    what = "notre commande" if thread["kind"] == "order" else "notre demande de prix"
    number = thread["ref_id"]
    if thread["state"] == "confirmed" and thread["eta"]:
        ask = (f"Elle devait nous parvenir le {thread['eta']} et nous n'avons rien reçu. "
               "Pouvez-vous nous donner une nouvelle date ?")
    elif thread["kind"] == "order":
        ask = "Pouvez-vous nous confirmer qu'elle est bien prise en compte, avec la date de livraison ?"
    else:
        ask = "Pouvez-vous nous envoyer votre prix et votre délai ?"
    body = f"Bonjour,\n\nNous revenons vers vous au sujet de {what} n° {number} " \
           f"envoyée le {(thread['sent_at'] or '')[:10]}. {ask}\n\nMerci d'avance.\n\nCordialement"
    new = _add(db_path, direction="out", kind="chase", supplier=thread["supplier"],
               to_addr=thread["to_addr"], subject="Relance : " + (thread["subject"] or ""),
               body=body, ref_kind=thread["ref_kind"], ref_id=thread["ref_id"],
               thread_id=mail_id, state="draft", created_by=user)
    return {"drafts": [one(db_path, new)], "unassigned": []}


def close(db_path, mail_id):
    """The goods came, or it no longer matters: stop asking after it."""
    done = db.write(db_path, """
            UPDATE supplier_mail SET state = 'closed'
            WHERE mail_id = ? AND state IN ('sent', 'confirmed') RETURNING mail_id""", [mail_id])
    return done is not None


def owing(db_path, today=None):
    """Suppliers with delivery-note lines still owed: promised for a day that has gone, or
    owed for a week with no day given."""
    today = today or date.today()
    by_supplier = {}
    for line in backorders.rows(db_path):
        promised = parse_date(line["expected_on"])
        since = parse_date(line["delivery_date"])
        late = (promised is not None and promised < today) or (
            promised is None and since is not None
            and (today - since).days >= BACKORDER_AFTER_DAYS)
        if late and line["supplier"]:
            by_supplier.setdefault(line["supplier"], []).append(line)
    return [{"supplier": supplier, "lines": rows} for supplier, rows in sorted(by_supplier.items())]


def draft_backorder(db_path, supplier, user=None, today=None):
    held = {row["supplier"]: row for row in owing(db_path, today)}
    if supplier not in held:
        raise ValueError(f"Nothing is owed late by {supplier}.")
    known = _existing(db_path, "chase", "backorder", None, supplier)
    if known:
        return {"drafts": [one(db_path, known)], "unassigned": []}
    body = ["Bonjour,", "", "Les lignes suivantes de nos bons de livraison n'ont pas "
            "été livrées :", ""]
    for line in held[supplier]["lines"]:
        due = f", annoncée pour le {line['expected_on']}" if line["expected_on"] else ""
        body.append(f"- BL du {line['delivery_date']} : {_quantity(line['quantity'] or 0)} x "
                    f"{line['article']}{due}")
    body += ["", "Pouvez-vous nous donner la date de livraison du reste ?", "", "Cordialement"]
    new = _add(db_path, direction="out", kind="chase", supplier=supplier,
               to_addr=contact_email(db_path, supplier),
               subject="Reliquat de livraison", body="\n".join(body),
               ref_kind="backorder", ref_id=None, state="draft", created_by=user)
    return {"drafts": [one(db_path, new)], "unassigned": []}


# ---- what comes back ------------------------------------------------------

_NUMBER = re.compile(r"(?:n\s*[°ºo]|no\.?|#|commande|order|devis|demande|po)\s*[:.]?\s*0*(\d+)",
                     re.IGNORECASE)
_DATE = re.compile(r"\b(\d{4}-\d{2}-\d{2}|\d{1,2}[/.]\d{1,2}[/.]\d{2,4})\b")


def _domain(address):
    match = re.search(r"@([\w.-]+)", address or "")
    return match.group(1).lower() if match else None


def _address(sender):
    match = re.search(r"[\w.+-]+@[\w.-]+", sender or "")
    return match.group(0).lower() if match else ""


def take_reply(db_path, sender, subject, body, today=None):
    """File a mail that answers an order or a quote we sent. Gives back the thread it
    belongs to, or None when it is not about any: a reply is only taken when it comes from
    the address or domain the order went to and names the order's number."""
    today = today or date.today()
    mine = _domain(_address(sender))
    if not mine:
        return None
    text = f"{subject or ''}\n{body or ''}"
    numbers = {int(found) for found in _NUMBER.findall(text)}
    if not numbers:
        return None
    for thread in threads(db_path, today):
        if _domain(thread["to_addr"]) != mine or thread["ref_id"] not in numbers:
            continue
        eta = None
        for raw in _DATE.findall(body or ""):
            day = parse_date(raw)
            if day and day >= today - timedelta(days=1):
                eta = day
                break
        _add(db_path, direction="in", kind="confirmation", supplier=thread["supplier"],
             from_addr=_address(sender), subject=(subject or "")[:300], body=(body or "")[:4000],
             ref_kind=thread["ref_kind"], ref_id=thread["ref_id"], thread_id=thread["mail_id"],
             state="received")
        db.write(db_path, """
                UPDATE supplier_mail SET state = 'confirmed', confirmed_at = now(),
                       eta = coalesce(?, eta)
                WHERE mail_id = ? RETURNING mail_id""", [eta, thread["mail_id"]])
        if thread["kind"] == "quote":
            quotes.update(db_path, thread["ref_id"], {"state": "answered"})
        return thread["mail_id"]
    return None


def set_eta(db_path, mail_id, day):
    """A supplier confirmed an order, with a day or without: the thread is confirmed and the
    day to expect the goods is the one given (kept as it was when none is given)."""
    done = db.write(db_path, """
            UPDATE supplier_mail SET state = 'confirmed', confirmed_at = coalesce(confirmed_at, now()),
                   eta = coalesce(?, eta)
            WHERE mail_id = ? AND direction = 'out' AND state IN ('sent', 'confirmed')
            RETURNING mail_id""", [day, mail_id])
    return done is not None


def overview(db_path, mail, today=None):
    drafts = [_shape(v) for v in db.read(db_path, f"""
            SELECT {_COLUMNS} FROM supplier_mail
            WHERE direction = 'out' AND state IN ('draft', 'failed') ORDER BY mail_id DESC""")]
    log = [_shape(v) for v in db.read(db_path, f"""
            SELECT {_COLUMNS} FROM supplier_mail
            WHERE state IN ('sent', 'confirmed', 'received', 'closed')
            ORDER BY coalesce(sent_at, created_at) DESC LIMIT 40""")]
    return {"smtp": readiness(mail), "drafts": drafts, "threads": threads(db_path, today),
            "owing": owing(db_path, today), "log": log}
