import re
from datetime import date

from pydantic import BaseModel

HEADER_FIELDS = ("supplier", "delivery_date", "order_date", "site", "work_item")


_DMY = re.compile(r"^(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2}|\d{4})$")


def normalize_name(value):

    if value is None:
        return None
    return " ".join(str(value).split()).upper() or None


def parse_date(text):

    if not text:
        return None
    text = text.strip()
    try:
        return date.fromisoformat(text)
    except ValueError:
        pass
    match = _DMY.match(text)
    if not match:
        return None
    day, month, year = (int(g) for g in match.groups())
    if year < 100:
        year += 2000
    try:
        return date(year, month, day)
    except ValueError:  
        return None


class Line(BaseModel):
    article: str | None = None
    quantity: float | None = None
    backorder: bool = False
    designation: str | None = None
    known: bool = False  


class Note(BaseModel):
   

    supplier: str | None = None
    delivery_date: str | None = None
    order_date: str | None = None
    site: str | None = None
    work_item: str | None = None
    lines: list[Line] = []
    warnings: list[str] = []  

    def problems(self):
        
        out = []
        if not (self.supplier or "").strip():
            out.append("supplier missing")
        if not self.delivery_date:
            out.append("delivery date missing")
        elif parse_date(self.delivery_date) is None:
            out.append(f"delivery date unreadable: {self.delivery_date!r}")
        if self.order_date and parse_date(self.order_date) is None:
            out.append(f"order date unreadable: {self.order_date!r}")
        if not self.lines:
            out.append("no article")
        for i, line in enumerate(self.lines, 1):
            if not (line.article or "").strip():
                out.append(f"article {i} empty")
        return out

    def rows(self):
      
        delivered = parse_date(self.delivery_date)
        ordered = parse_date(self.order_date) if self.order_date else None
        return [
            {
                "article": normalize_name(line.article),
                "quantity": line.quantity,
                "supplier": normalize_name(self.supplier),
                "delivery_date": delivered,
                "order_date": ordered,
                "site": normalize_name(self.site),
                "work_item": normalize_name(self.work_item),
                "backorder": line.backorder,
                "designation": line.designation,
            }
            for line in self.lines
        ]
