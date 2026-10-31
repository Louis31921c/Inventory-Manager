# A year of made-up paperwork

Everything here is invented, so the app can be driven hard without touching the real
database: a year of delivery notes, customer orders, quote requests, bills, hand counts,
contacts and delivery addresses for a metalwork and glazing shop around Bordeaux. The
article names, suppliers, sites and work items follow the shape of the real paperwork;
the prices, people and phone numbers do not exist.

## Running a simulation on it

```bash
python sample_data/make_sample_data.py --db demo
DATA_DIR="sample_data/demo" "$HOME/Desktop/Stock Manager/venv/bin/stock-manager"
```

The first line writes the csv files and pours the same year into `sample_data/demo/`
through the app's own code, so the database is built the way the app builds one.
`DATA_DIR` then points the app at it: the real database in `~/.local/share/stock-manager`
is never opened. The first screen asks for a name and a password, as it always does on a
fresh folder; anything will do, it belongs to that folder only.

Delete `sample_data/demo` to start the year over. The seed is fixed, so the same command
gives the same year every time; `--seed 7` gives a different one and `--days 30` a
shorter one.

## What is in the year

| File | What it holds |
|---|---|
| `delivery_notes.csv` | the note headers: supplier, the day it arrived, the day it was ordered, site, work item |
| `delivery_note_lines.csv` | what each note carried, with the lines marked as a backorder and the day promised for the rest |
| `purchase_orders.csv` | what customers ordered, by day, site and work item |
| `purchase_order_lines.csv` | the articles on each order, mostly ones the notes had already brought to that site |
| `quotes.csv`, `quote_lines.csv` | quote requests, by the metre, the cube, the tonne and the hour. No prices: a request asks for one |
| `bills.csv` | a bill a month per supplier, priced off what that month's notes carried; some paid, some still to pay, a few disputed |
| `stock_counts.csv` | twenty-five articles counted by hand, a little beside what the paper says |
| `places.csv` | the yard and twelve delivery addresses, with their coordinates so no map service is needed |

Contacts come from `../../supplier-contacts.csv`, the same file the app reads on the
Purchase orders screen.

Roughly a year: about 510 notes over 1,500 lines, 465 orders over 3,400 lines, 65 quote
requests, 160 bills. Around one line in twelve on a note is a backorder, and two in three
of those have a day promised, some of which have already gone by — so "Partly delivered"
has late lines in it. Most order lines are answered by the notes for that site and some
are not, so the Stored purchase orders screen shows both "in stock" and "to order".

The orders carry off most of what the notes bring, the way a real year does, so what is
left on the shelf is of the same order as a buffer rather than a year of stock: the DDMRP
screen comes out with articles in every zone, not all of them over the top of green.

## The files on their own

`make_sample_data.py` with no `--db` only writes the csv files. They are plain text with
a header row, so they can be read, edited, loaded through the Import data screen, or used
as fixtures somewhere else. `catalogue.py` holds the firm itself — the articles, the
suppliers, the sites and their work items, the addresses — and is the file to change to
make the year look like somewhere else.
