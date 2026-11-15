"""The working calendar, what a supplier's day looks like, and what a foreign bill is worth.

Lead times are measured in days on the notes, but goods do not travel on Sundays or the
14th of July. This knows the French public holidays (the fixed ones, and the three that
hang on Easter), any extra closure a person adds, and per supplier the weekdays it is shut
and the hour after which an order counts as the next day's. A bill in another currency is
turned into euros at the last rate known on its date, or is left unconverted when there is none.
"""

import re
import urllib.request
from datetime import date, datetime, timedelta

from . import db

CUTOFF = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
ECB_DAILY = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"


def easter(year):
    """Easter Sunday (the anonymous Gregorian algorithm)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    el = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * el) // 451
    month, day = divmod(h + el - 7 * m + 114, 31)
    return date(year, month, day + 1)


def french_holidays(year):
    pasques = easter(year)
    return {
        date(year, 1, 1): "Jour de l'an", date(year, 5, 1): "Fete du travail",
        date(year, 5, 8): "Victoire 1945", date(year, 7, 14): "Fete nationale",
        date(year, 8, 15): "Assomption", date(year, 11, 1): "Toussaint",
        date(year, 11, 11): "Armistice", date(year, 12, 25): "Noel",
        pasques + timedelta(days=1): "Lundi de Paques",
        pasques + timedelta(days=39): "Ascension",
        pasques + timedelta(days=50): "Lundi de Pentecote",
    }


def _closures(db_path):
    return {day for (day,) in db.read(db_path, "SELECT day FROM calendar_days")}


def _terms(db_path, supplier):
    rows = db.read(db_path, "SELECT cutoff, closed_days, payment_days, currency, note "
                            "FROM supplier_terms WHERE upper(supplier) = upper(?)",
                   [supplier or ""])
    if not rows:
        return {}
    cutoff, closed, payment, currency, note = rows[0]
    return {"cutoff": cutoff, "closed_days": {int(x) for x in (closed or "").split(",") if x.strip().isdigit()},
            "payment_days": payment, "currency": currency, "note": note}


class Calendar:
    """The calendar read once, so a loop over many days is one query and not hundreds."""

    def __init__(self, db_path, supplier=None):
        self.extra = _closures(db_path)
        self.terms = _terms(db_path, supplier) if supplier else {}
        self._holidays = {}

    def holiday(self, day):
        if day.year not in self._holidays:
            self._holidays[day.year] = french_holidays(day.year)
        return self._holidays[day.year].get(day)

    def working(self, day):
        if day.weekday() >= 5 or self.holiday(day) or day in self.extra:
            return False
        return day.weekday() not in self.terms.get("closed_days", set())

    def next_working(self, day):
        while not self.working(day):
            day += timedelta(days=1)
        return day

    def add(self, day, n):
        """`n` working days after `day`; a day that is not one counts from the next that is."""
        day = self.next_working(day) if n > 0 and not self.working(day) else day
        step = 1 if n >= 0 else -1
        left = abs(int(n))
        while left:
            day += timedelta(days=step)
            if self.working(day):
                left -= 1
        return day

    def between(self, first, last):
        """Working days from the day after `first` up to and including `last`."""
        if last <= first:
            return 0
        count, day = 0, first
        while day < last:
            day += timedelta(days=1)
            if self.working(day):
                count += 1
        return count

    def order_day(self, when):
        """The day an order placed at `when` starts to count: after the supplier's cut-off hour
        or on a day it is shut, it is the next working day."""
        day = when.date() if isinstance(when, datetime) else when
        cutoff = self.terms.get("cutoff")
        late = False
        if cutoff and isinstance(when, datetime) and CUTOFF.match(cutoff):
            hour, minute = (int(x) for x in cutoff.split(":"))
            late = (when.hour, when.minute) >= (hour, minute)
        if late or not self.working(day):
            return self.next_working(day + timedelta(days=1 if late else 0))
        return day

    def arrival(self, when, lead_days):
        """When goods ordered at `when` are expected: the order day plus the lead time in
        calendar days, moved on to a day the supplier can deliver."""
        start = self.order_day(when)
        return self.next_working(start + timedelta(days=int(round(lead_days))))


def set_terms(db_path, supplier, cutoff=None, closed_days=None, payment_days=None,
              currency=None, note=None):
    supplier = (supplier or "").strip().upper()
    if not supplier:
        raise ValueError("No supplier named.")
    if cutoff and not CUTOFF.match(cutoff.strip()):
        raise ValueError("The cut-off hour is written HH:MM.")
    days = [str(int(x)) for x in str(closed_days or "").split(",") if x.strip().isdigit()
            and 0 <= int(x) <= 6]
    db.write(db_path, """
            INSERT INTO supplier_terms (supplier, cutoff, closed_days, payment_days, currency, note)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (supplier) DO UPDATE SET cutoff = excluded.cutoff,
                closed_days = excluded.closed_days, payment_days = excluded.payment_days,
                currency = excluded.currency, note = excluded.note
            RETURNING supplier""",
             [supplier, (cutoff or "").strip() or None, ",".join(days) or None,
              payment_days, (currency or "").strip().upper() or None, note or None])
    return supplier


def all_terms(db_path):
    return [{"supplier": s, "cutoff": c, "closed_days": d, "payment_days": p, "currency": cur,
             "note": n} for s, c, d, p, cur, n in db.read(
                 db_path, "SELECT supplier, cutoff, closed_days, payment_days, currency, note "
                          "FROM supplier_terms ORDER BY supplier")]


def close_day(db_path, day, label=None):
    db.write(db_path, "INSERT INTO calendar_days (day, kind, label) VALUES (?, 'closed', ?) "
                      "ON CONFLICT (day) DO UPDATE SET label = excluded.label RETURNING day",
             [day, label])


def reopen_day(db_path, day):
    return db.write(db_path, "DELETE FROM calendar_days WHERE day = ? RETURNING day", [day]) is not None


def calendar_view(db_path, year):
    extra = {d: l for d, l in db.read(db_path, "SELECT day, label FROM calendar_days")}
    days = {**{d: n for d, n in french_holidays(year).items()},
            **{d: (l or "closed") for d, l in extra.items() if d.year == year}}
    return [{"day": d.isoformat(), "label": n, "own": d in extra} for d, n in sorted(days.items())]


# ---- currency -------------------------------------------------------------

def set_rate(db_path, day, currency, per_eur):
    currency = (currency or "").strip().upper()
    if not currency or currency == "EUR":
        raise ValueError("A rate is for a currency other than EUR.")
    if not per_eur or float(per_eur) <= 0:
        raise ValueError("The rate must be more than zero.")
    db.write(db_path, "INSERT INTO fx_rates (day, currency, per_eur) VALUES (?, ?, ?) "
                      "ON CONFLICT (day, currency) DO UPDATE SET per_eur = excluded.per_eur "
                      "RETURNING day", [day, currency, float(per_eur)])


def rate_on(db_path, currency, day):
    rows = db.read(db_path, "SELECT per_eur FROM fx_rates WHERE currency = ? AND day <= ? "
                            "ORDER BY day DESC LIMIT 1", [(currency or "").upper(), day])
    return float(rows[0][0]) if rows else None


def to_eur(db_path, amount, currency, day):
    """`amount` in euros, or None when it is in a currency with no rate known by `day`."""
    if amount is None:
        return None
    if not currency or currency.upper() == "EUR":
        return float(amount)
    rate = rate_on(db_path, currency, day)
    return None if rate is None else float(amount) / rate


def fetch_rates(db_path, opener=urllib.request.urlopen):
    """Today's reference rates from the European Central Bank. Gives back how many were stored."""
    with opener(ECB_DAILY, timeout=15) as reply:
        text = reply.read().decode("utf-8", errors="replace")
    day = re.search(r"time=['\"](\d{4}-\d{2}-\d{2})['\"]", text)
    when = date.fromisoformat(day.group(1)) if day else date.today()
    found = re.findall(r"currency=['\"]([A-Z]{3})['\"]\s+rate=['\"]([\d.]+)['\"]", text)
    for currency, rate in found:
        set_rate(db_path, when, currency, float(rate))
    return {"day": when.isoformat(), "stored": len(found)}
