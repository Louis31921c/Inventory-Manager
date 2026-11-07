"""Writes docs/pipeline.html, the whole pipeline step by step.

    python3 tools/pipeline_schema.py docs/pipeline.html

The steps are the lists below. Each line is length-checked so no text leaves its box."""
import html
import sys

W, H_BOX, GAP_X, GAP_Y, X0, Y0 = 760, 96, 40, 44, 24, 16
BW = 210
LIMIT = {"title": 21, "sub": 31, "else": 31}

# kind -> css class
KINDS = {"auto": "teal", "agent": "purple", "person": "amber", "money": "coral", "gap": "gray"}
LEGEND = [("auto", "runs by itself"), ("agent", "decided by Plutus or Terminus"),
          ("person", "a person acts"), ("money", "money moves"), ("gap", "missing or manual only")]

D = []

def diagram(key, title, lead, steps, notes=""):
    D.append(dict(key=key, title=title, lead=lead, steps=steps, notes=notes))

# step = (kind, title, sub1, sub2, else)
diagram("in", "1. Delivery notes come in",
 "Mail is checked every 15 minutes (MAIL_EVERY_MINUTES). By hand, a note joins at step 5.",
 [("person", "By hand", "scan a photo, or import", "a csv/xlsx and map columns", "joins at Read"),
  ("auto", "Mail check", "unread mail, every 15 min;", "attachments: pdf, photo, sheet", "no mailbox: skipped"),
  ("auto", "Intake queue", "file hashed; same file twice", "is dropped; waits as waiting", ""),
  ("auto", "Bill or quote?", "file name or subject says", "invoice, avoir, devis, bill", "yes: waits for a person"),
  ("agent", "Read (Claude CLI)", "photo or pdf becomes a note:", "supplier, date, lines", "unreadable: waits"),
  ("auto", "Clean?", "date valid, an article on", "every line, quantities", "to fix: waits"),
  ("auto", "Store the note", "photo shrunk to 1600 px;", "signed MAIL; audit entry", "seen before: skipped"),
  ("auto", "Article names", "aliases applied from the", "shared vocabulary", "unknown: kept as typed"),
  ("auto", "Books updated", "received += lines; lines marked", "backorder become owed lines", "")],
 "Waiting files have no screen any more: the intake table is not on a page.")

diagram("stock", "2. Stock, receiving and counting",
 "The books are worked out from paperwork. A hand count wins over the sum for that article.",
 [("auto", "Books", "received minus shipped (a day", "after the order) +/- moves", "count replaces the sum"),
  ("auto", "Free stock", "on hand minus what is held", "back for work items", ""),
  ("auto", "Receiving check", "hourly: each new line against", "orders of the last 120 days", ""),
  ("auto", "Against the orders", "as ordered, more than ordered,", "or on no order at all", "odd: one alert"),
  ("person", "Put away", "suggested place = last one for", "that article, else RECEIVING", "person confirms"),
  ("auto", "Count list", "daily, 5 articles: busiest", "monthly, then 2, then 3 months", "pushed to alerts"),
  ("person", "Blind count", "the person counts; the books", "are never shown", ""),
  ("agent", "Within 2%?", "yes (at least 1 unit): posted", "now; no: 2nd count, causes", "named"),
  ("person", "Second count", "agrees: posted. Disagrees:", "nothing posted, person settles", "alert raised")],
 "")

diagram("need", "3. From a need to a written order",
 "Four ways a need appears. All end as a purchase order or a basket for the gate.",
 [("auto", "Forecast", "use per day from shipped", "orders; lead time learned", "per supplier (3+ deliveries)"),
  ("auto", "DDMRP buffers", "red, yellow, green zones;", "pack sizes; feedback factors", "empty shelf: +15% safety"),
  ("person", "Site request", "site, work item, day;", "screen or /request in chat", ""),
  ("auto", "Shelf first", "free stock is held back", "for the work item", "rest goes to the gate"),
  ("person", "Ticked to order", "lines on a customer order;", "queue reads them hourly", "ordered after 15 days"),
  ("agent", "Cycle order", "every 14 days, Monday: all that", "fall under yellow before next", "up to green + cycle use"),
  ("agent", "Urgent order", "other working days after 8h:", "only articles already in red", "once a day at most"),
  ("agent", "Common sense", "on its way: left alone; 90 days", "cover cap; no use in 8 weeks", "supplier under 30 EUR waits"),
  ("auto", "Order written", "purchase order drafted PLUTUS,", "or a basket for the request", "enters the gate")],
 "")

diagram("round", "4. The purchase round (every hour)",
 "Mode comes first: off does nothing, dry run only writes down what it would do, live acts.",
 [("auto", "Mode", "off | dry run (3 days min)", "| live", "limits from the charter"),
  ("auto", "Gate request", "Plutus's order enters the gate", "like any chat basket", "sealed in the log"),
  ("agent", "Terminus reads", "same article, new, or unsure", "(Claude CLI, names only)", "unsure: held"),
  ("agent", "Terminus decides", "approves if all same and within", "500 EUR, 1000/day; cancels", "repeat, refused, 14 days old"),
  ("agent", "Plutus approves", "same limits; dear line, new", "supplier, no price: held", "unless the charter allows"),
  ("person", "Held: a person", "sees the reasons, approves", "or cancels; no expiry", "10% price drift asks again"),
  ("money", "Wallet", "free = balance - committed", "- reserve; sending commits", "short: held"),
  ("auto", "Window and limits", "8h-17h working days; 5000 per", "order, 20000/day; price drift", "over 10%: held"),
  ("auto", "Send or place", "mail (with PDF) or signed", "webhook; a basket becomes PO", "marked ordered")],
 "")

diagram("supp", "5. Suppliers, after the order",
 "A reply only counts from a supplier on file. The mail is data, never instructions.",
 [("auto", "Thread opens", "order sent; expected day =", "sent + usual lead time", "working days, cut-off hour"),
  ("auto", "Reply arrives", "sender must be a supplier on", "file (address or domain)", "stranger: ignored"),
  ("auto", "Reply filed", "names the order number:", "confirmed; first date = eta", ""),
  ("agent", "Reply read", "Claude fills a form: date,", "prices, credit, question", "no CLI: only filed"),
  ("auto", "Applied", "date: eta. Prices: offers,", "not awarded. Credit: closes", "question: alert"),
  ("agent", "Silent order", "3 working days with no answer,", "or past its promised day", "chased; 2 times then person"),
  ("agent", "Owed lines", "owed 7 days or past promise:", "a reminder, once a week", ""),
  ("person", "Quote flow", "request, send, prices, compare", "side by side, then award", "award = agreed price"),
  ("auto", "Goods arrive", "delivery note stored", "lead time learned from it", "goes to Receiving")],
 "")

diagram("money", "6. Bills and payment",
 "A bill is checked against every delivery since the supplier's previous bill, at known prices.",
 [("gap", "Bill in", "API only: Claude reads, then", "a person confirms; no screen", "path today"),
  ("auto", "Match", "deliveries since previous bill", "x prices the app has seen", ""),
  ("auto", "Verdict", "matched, over, under, nothing", "delivered, unchecked (no price)", ""),
  ("agent", "Dispute", "over / nothing delivered:", "Plutus mails it if <= 2000", "above: a person decides"),
  ("auto", "Credit note", "supplier's reply is read;", "closed when credit covers claim", ""),
  ("agent", "May it be paid?", "matched; within 2 days of due;", "bank details; <= 5000; 10k/day", "else: left, reason logged"),
  ("money", "Instruct", "wallet debited; payment goes", "in the open SEPA batch", ""),
  ("money", "Bank drop", "pain.001 uploaded by SFTP key;", "or downloaded by a person", "upload refused: batch reopens"),
  ("money", "Bank report", "accepted: bills paid.", "Rejected: money back to wallet", "pending: asked again")],
 "")

diagram("loop", "7. The hourly loop, in order",
 "One pass, nine steps, every hour. Each step is wrapped so one failure does not stop the next run.",
 [("auto", "Learn", "feedback factors from cuts,", "empty counts, refused baskets", ""),
  ("agent", "Replenish", "cycle or urgent order, if the", "day and hour say so", "off: skipped"),
  ("agent", "Terminus", "decides the open baskets", "(approve or cancel)", "off or dry: logs only"),
  ("auto", "Receiving", "new delivery lines checked", "and given a suggested place", ""),
  ("auto", "Count list", "5 articles, once a day;", "pushed to the alert channels", ""),
  ("auto", "Requests", "each request's state refreshed;", "finished ones are pushed", ""),
  ("agent", "Plutus", "the purchase round: send,", "approve, place, chase, pay", "off: skipped"),
  ("auto", "Policies", "older rules: send an order, a", "reminder, approve; kill switch", "off by default"),
  ("auto", "Notices", "scan the books for what needs", "a person; push what is new", "daily digest at NOTIFY_HOUR")],
 "Beside it: mail check every 15 min, the to-order queue hourly, photo purge daily, Discord and Slack bots always on.")

diagram("rails", "8. The safety rails",
 "What stops an agent doing more than it should, whatever the model answers.",
 [("auto", "Charters", "numbers a person sets, enforced", "again in code on every answer", ""),
  ("auto", "Modes", "off, then dry run, live after", "3 days of dry run", "per agent"),
  ("money", "Wallet", "a person funds it; Plutus", "spends only what is free", "program ledger, not a bank"),
  ("person", "Held baskets", "reasons shown; approve or", "cancel on the landing page", "no expiry"),
  ("auto", "Sealed log", "every gate event in one hash", "chain; an edit shows its entry", "no network, no coin"),
  ("auto", "Alerts", "dashboard, push (Slack, SMS,", "mail), daily digest, score", "autonomy score out of 100")],
 "")

def esc(s): return html.escape(s, quote=False)

def render(d):
    steps = d["steps"]
    for k, t, a, b, e in steps:
        assert len(t) <= LIMIT["title"], (d["key"], t, len(t))
        for s in (a, b):
            assert len(s) <= LIMIT["sub"], (d["key"], s, len(s))
        assert len(e) <= LIMIT["else"], (d["key"], e, len(e))
    rows = (len(steps) + 2) // 3
    height = Y0 + rows * H_BOX + (rows - 1) * GAP_Y + 24
    pos = []
    for i in range(len(steps)):
        r, c = divmod(i, 3)
        col = c if r % 2 == 0 else 2 - c
        x = X0 + col * (BW + GAP_X)
        y = Y0 + r * (H_BOX + GAP_Y)
        pos.append((x, y))
    out = [f'<svg viewBox="0 0 {W} {height}" role="img" aria-label="{esc(d["title"])}">']
    out.append('<defs><marker id="a" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">'
               '<path d="M2 1L8 5L2 9" fill="none" stroke="context-stroke" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></marker></defs>')
    for i, (k, t, a, b, e) in enumerate(steps):
        x, y = pos[i]
        dash = ' stroke-dasharray="5 4"' if k == "gap" else ""
        out.append(f'<g class="k-{KINDS[k]}"><rect x="{x}" y="{y}" width="{BW}" height="{H_BOX}" rx="8"{dash}/>')
        out.append(f'<text class="th" x="{x+12}" y="{y+20}"><tspan class="num">{i+1}</tspan>  {esc(t)}</text>')
        out.append(f'<text class="ts" x="{x+12}" y="{y+41}">{esc(a)}</text>')
        out.append(f'<text class="ts" x="{x+12}" y="{y+57}">{esc(b)}</text>')
        if e:
            out.append(f'<text class="te" x="{x+12}" y="{y+80}">{esc(e)}</text>')
        out.append('</g>')
    for i in range(len(steps) - 1):
        (x1, y1), (x2, y2) = pos[i], pos[i + 1]
        if y1 == y2:
            cy = y1 + H_BOX // 2
            if x2 > x1:
                out.append(f'<line class="arr" x1="{x1+BW+3}" y1="{cy}" x2="{x2-3}" y2="{cy}" marker-end="url(#a)"/>')
            else:
                out.append(f'<line class="arr" x1="{x1-3}" y1="{cy}" x2="{x2+BW+3}" y2="{cy}" marker-end="url(#a)"/>')
        else:
            cx = x1 + BW // 2
            out.append(f'<line class="arr" x1="{cx}" y1="{y1+H_BOX+3}" x2="{cx}" y2="{y2-3}" marker-end="url(#a)"/>')
    out.append('</svg>')
    return "\n".join(out)

CSS = """
:root{--bg:#fff;--fg:#1f2328;--mut:#59636e;--line:#8c959f;--rail:#d0d7de;
--teal-f:#E1F5EE;--teal-s:#0F6E56;--teal-t:#085041;
--purple-f:#EEEDFE;--purple-s:#534AB7;--purple-t:#3C3489;
--amber-f:#FAEEDA;--amber-s:#854F0B;--amber-t:#633806;
--coral-f:#FAECE7;--coral-s:#993C1D;--coral-t:#712B13;
--gray-f:#F1EFE8;--gray-s:#5F5E5A;--gray-t:#444441;--else:#A32D2D}
@media (prefers-color-scheme:dark){:root{--bg:#161b22;--fg:#e6edf3;--mut:#9da7b3;--line:#7d8590;--rail:#30363d;
--teal-f:#085041;--teal-s:#5DCAA5;--teal-t:#E1F5EE;
--purple-f:#3C3489;--purple-s:#AFA9EC;--purple-t:#EEEDFE;
--amber-f:#633806;--amber-s:#EF9F27;--amber-t:#FAEEDA;
--coral-f:#712B13;--coral-s:#F0997B;--coral-t:#FAECE7;
--gray-f:#444441;--gray-s:#B4B2A9;--gray-t:#F1EFE8;--else:#F09595}}
*{box-sizing:border-box}body{margin:0;padding:24px 16px 48px;background:var(--bg);color:var(--fg);
font:15px/1.6 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:800px;margin:0 auto}h1{font-size:22px;font-weight:600;margin:0 0 4px}
h2{font-size:17px;font-weight:600;margin:36px 0 2px;padding-bottom:4px;border-bottom:1px solid var(--rail)}
p{margin:4px 0 10px;color:var(--mut)}.note{font-size:13px;margin-top:6px}
svg{width:100%;height:auto;display:block}
.legend{display:flex;flex-wrap:wrap;gap:6px 16px;font-size:13px;color:var(--mut);margin:10px 0 0}
.legend span{display:inline-flex;align-items:center;gap:6px}.legend i{width:12px;height:12px;border-radius:3px;border:1px solid}
.th{font-size:14px;font-weight:600}.ts{font-size:12px}.te{font-size:12px;font-style:italic}
.num{font-weight:400;opacity:.7}.arr{stroke:var(--line);stroke-width:1.5;fill:none}
""" + "".join(
 f".k-{n} rect{{fill:var(--{n}-f);stroke:var(--{n}-s);stroke-width:1}}"
 f".k-{n} .th{{fill:var(--{n}-t)}}.k-{n} .ts{{fill:var(--{n}-s)}}.k-{n} .te{{fill:var(--else)}}"
 for n in ("teal", "purple", "amber", "coral", "gray"))

def page():
    legend = "".join(f'<span><i style="background:var(--{KINDS[k]}-f);border-color:var(--{KINDS[k]}-s);'
                     f'{"border-style:dashed" if k=="gap" else ""}"></i>{esc(t)}</span>' for k, t in LEGEND)
    body = ["<h1>Stock Manager: the whole pipeline, step by step</h1>",
            "<p>Read each diagram as a snake: left to right, then down, then right to left. The red italic line under a step is what happens when it does not pass.</p>",
            f'<div class="legend">{legend}</div>']
    for d in D:
        body.append(f'<h2>{esc(d["title"])}</h2><p>{esc(d["lead"])}</p>')
        body.append(render(d))
        if d["notes"]:
            body.append(f'<p class="note">{esc(d["notes"])}</p>')
    return ('<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>Stock Manager pipeline</title><style>{CSS}</style><main>' + "\n".join(body) + '</main></html>')

if __name__ == "__main__":
    open(sys.argv[1], "w", encoding="utf-8").write(page())
    print("written", sys.argv[1], sum(len(d["steps"]) for d in D), "steps")
