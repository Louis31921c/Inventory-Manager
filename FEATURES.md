# Logistics Manager: functional and technical overview

One desk for a logistics manager's week: what arrived, what is on the shelf, what to order, who to chase, what to pay. It reads the paperwork a team already has, keeps it in one local database, and puts every decision that spends money in front of a person.

## At a glance

| | |
|---|---|
| Runs as | Desktop window (GTK WebKit), browser tab, terminal screen (`stock-tui`) or command line (`stock`) |
| Stack | Python 3.11+, FastAPI on a loopback port, one DuckDB file, a single-page web UI |
| Data | One file, `inventory.duckdb`, plus a photos folder. 20 schema migrations applied on start |
| Size | About 16,000 lines of Python in 62 modules, about 170 HTTP routes, about 210 automated tests |
| AI | Claude command line by default (no key), or Gemini or OpenAI. Used to read documents, draft orders and check article names |

## 1. Receiving: delivery notes in

**Ways in** (all land in one queue, one copy per file by SHA-256, so the same bytes from a mail and a folder are one row):

| Source | Detail |
|---|---|
| Photo or scan | JPG, PNG, WEBP, PDF. Read by the model against a JSON schema |
| Spreadsheet | CSV or XLSX. Columns guessed from the header in English or French (`article`, `quantite`, `fournisseur`, `date livraison`, `chantier`), corrected by a person, dry-run before writing |
| Mailbox | IMAP: Proton (through Bridge), Gmail, Outlook and Microsoft 365 (OAuth device code), Yahoo, iCloud, any IMAP host. Default every 15 minutes, up to 20 messages a run, 30 MB per attachment |
| Terminal | `stock read photo.jpg` |

**What is extracted:** supplier, delivery date, order date, site, work item, and per line the article, quantity, designation and whether it is a backorder. Dates in several formats are normalised; a note with a missing supplier, an unreadable date or no line is refused with the reason.

**Automatic storing from mail** (setting "Store notes by itself", on by default): a clean note is saved signed `MAIL`. Anything with a fault, any spreadsheet, and any file or subject containing facture, invoice, bill, avoir, devis or quote stays in the queue with the reason shown.

**Article names:** each name is resolved through `article_names` and `article_aliases` (two names can be merged), and supplier references are learned into a catalogue so "CHEVILLE SPIT FIX 10x100" and a shelf name with a reference code meet. Each reading is scored by how many fields a person corrected, which feeds an accuracy view.

**Part deliveries:** a backorder line can carry a promised date and a settled date. It turns red when the date passes.

**Photos:** kept as proof, cut to 1,600 px on the long side and recompressed (about 250 KB from 2 MB), metadata dropped. After 42 days (`PHOTO_RETENTION_DAYS`) the photo is packed into a small array and the file deleted (see section 10). `stock shrink` does the same to photos already stored.

## 2. Stock: what is on the shelf

- **On hand (per article)** = sum of delivery-note quantities minus sum of order quantities. An order counts as shipped one day after it is written. A hand count replaces the sum, and corrections made after the count are added back.
- **By site:** the same sum per article and site, plus transfers and corrections.
- **Transfers** write two movements (out of one site, into the other) and are refused when the first site has less free than asked.
- **Corrections** are signed quantities and need a written reason.
- **Reservations** hold stock for a work item at a site. An order for the same article, site and work item written since uses the reservation up. Free stock is on hand minus what is still held.
- **Cycle counts:** articles ranked by quantity shipped over 90 days. Class A is the articles making up the first 80%, B the next 15%, C the rest. Counted every 30, 60 and 90 days. The list puts the furthest overdue first, and for never-counted articles the busiest first. The count gap against the paperwork is kept as a movement.

## 3. Planning: what to order and when

**DDMRP buffers** (per article, from your own notes):

| Quantity | How it is worked out |
|---|---|
| Daily use (ADU) | quantity delivered / days in the window (at least 7 days) |
| Lead time | median of delivery date minus order date: article first, then supplier, then everyone (fallback 6 days) |
| Lead factor | 0.7 up to 7 days, 0.5 up to 21 days, 0.3 beyond |
| Variability factor | by coefficient of variation of weekly use: 0.35 steady (up to 0.5), 0.5 lumpy (up to 1.0), 0.7 erratic |
| Red | `ADU x lead x lead factor`, plus that times the variability factor |
| Yellow | `ADU x lead` |
| Green | the larger of `ADU x lead x lead factor` and `ADU x order cycle` (7 days by default) |

Net flow is the counted shelf minus what open orders still ask for. It is placed against the top of red, yellow and green. Articles nobody counted are marked "not counted", not assumed empty.

**Demand outlook:** each week of each article is labelled quiet, light, usual or spike. A pooled four-state transition matrix (smoothing 0.5) gives the chance of a spike inside the horizon.

**Safety stock watch:** reorder point = `daily use x lead + z x weekly spread / 7 x sqrt(lead)`, with z set by service level (80% = 0.84, 90% = 1.28, 95% = 1.65, 99% = 2.33). Defaults: lead 7 days, service 95%, horizon 14 days. Suggested quantity = reorder point + horizon demand - on hand - on order, rounded up.

**Learned lead times:** per supplier and per article from the notes: count, median, 80th percentile (the delay one delivery in five exceeds), fastest, slowest. An article with 3 or more deliveries is planned with its own 80th percentile. Otherwise the lead setting applies.

**Plutus** (the buyer) drafts an order from those numbers and signs nothing. A line ticked "to order" on a stored purchase order is read into a queue (hourly, or on demand) with a due day 15 days later. When due, lines become one purchase order per site and work item (same article merged), drafted by Plutus. It is a list, not a purchase.

## 4. Suppliers: mail out and back

- **Drafts:** a purchase order becomes one draft per supplier (supplier taken from the latest price or delivery of each article, address from the contact book). A quote request becomes one draft. Written in French, editable. Asking again returns the same draft.
- **Sending** happens only when a person presses send, through the mailbox's own SMTP server: Gmail (465, SSL), Yahoo (465, SSL), iCloud (587, STARTTLS), Proton Bridge (127.0.0.1:1025). Any other server through `SMTP_HOST`. Microsoft sign-in is read-only, so Outlook cannot send here. A refused send keeps the draft, marked failed, with the reason.
- **Replies:** the mailbox check files a reply that comes from the same domain the order went to and names its number (n°, no, #, commande, devis, PO) as the confirmation. The first date in the text from yesterday onward becomes the day to expect the goods. A reply to a quote marks it answered.
- **Chasing:** an order unanswered after 3 days, or confirmed but past its promised day by more than a day, can be chased. At most one reminder every 3 days. Delivery-note lines owed for 7 days with no date, or past their date, get one reminder per supplier. The expected day of an order with no promise is the day sent plus that supplier's usual lead time.
- **Quote requests** carry units: piece, linear metre, m2, m3, kg, tonne, litre, hour. States: open, sent, answered, closed.
- **Contacts:** a contact book loaded from `supplier-contacts.csv` (search by name and company), with Incoterms.

## 5. Money: bills

- **Reading:** a photo or PDF gives supplier, number, dates, net, tax, total, order reference. States: to pay, paid, disputed. A monthly view shows owed, late and average per month.
- **Three-way match:** each bill is checked against every delivery of that supplier since its previous bill, priced at the last price seen for each article from that supplier by the delivery day. Within 2% or 1 EUR it is matched. Otherwise over, under, unchecked (articles with no known price) or nothing delivered. A bill whose order reference matches no stored order is noted.
- **Also listed:** deliveries not billed 45 days after the oldest, and ordered lines against what came in at the same site and work item (complete, part, over, late after 21 days).

## 6. Spending control: the purchase gate

- **Baskets** are filled from Slack or Discord (`/basket`, `/basket_show`, `/basket_clear`, `/purchase`, `/quote`, `/stock`). A line reads like `12 vis th 6x25 @ 0.11`; up to 60 lines.
- **`/purchase` holds a basket for a person** when any of these is true:

| Reason | Rule |
|---|---|
| First article | never bought before |
| New supplier | article known, supplier not |
| Over the ceiling | total above 500 EUR |
| Dear line | a line above twice the article's usual price (median of its last 12 prices) |
| No price | a line has no price, so the money rules cannot run |

- **Terminus** (a second model call) answers only "is this an article we know under another wording": same, new or unsure. It approves nothing, and when unsure or unreachable the gate stays shut.
- **Approval never expires,** but it remembers the price it was given at: an order placed later at a price that moved more than 10% is asked for again.
- **Sealed log:** every turn (requested, checked, approved, cancelled, ordered) is an entry in `gate_log` carrying the SHA-256 of the one before it. An edited entry breaks the chain from there and the screen names it.
- **Blockchain screen:** the seven-step round (reading, shelf check, Terminus, decision, waiting for you, wallet, placing the order) as clickable wedges, each opening an audit of that step. Steps 6 and 7 are drawn but not built: an approved order is placed by hand.

## 7. Route wrangler

Vans with volume and payload (default two Renault Partner at 3.3 m3 and 650 kg, one Iveco Daily at 19.6 m3 and 1,800 kg) and up to 25 stops with opening hours. Addresses are geocoded by Nominatim and distances come from OSRM (OpenStreetMap). If either is unreachable it falls back to straight-line distance.

## 8. Control tower: dashboard, alerts, autonomy score

- **Landing dashboard:** counts of line down, critical and at risk, units short, then one row per short article: status, units short, the day it is needed, the work it blocks, the root cause, the supplier, its lead time, and the action to take. An article is short when what is needed (stock held for a work item, lines ticked "to order", all in the next 14 days) is more than the shelf, or when a used article is at zero or will run out before a delivery can arrive. Status is line down (at zero and needed within 3 days), critical (within 7) or at risk. The root cause is the first true of: held for approval, order sent with no confirmation, supplier ETA after the need date, order late, drafted not sent, waiting in the 15-day queue, part delivered, no order placed.
- **Notices:** every hour the app looks over its books and keeps a list of what needs a person (stock at zero, orders past their day, a basket held over 24 hours, bills past due, a quote needed within 3 days, no reply, owed lines, mismatched bills, disputes with no credit note, a price over 25% above usual). A notice is one entry for as long as it lasts and clears itself when fixed. Critical and high ones are pushed once to Slack, SMS or mail (`NOTIFY_VIA`), and a digest of everything open goes out daily at `NOTIFY_HOUR`. Nothing is pushed unless set.
- **Autonomy score (0 to 100, two decimals, refreshed every 5 seconds):** eight parts of the job with fixed weights: receiving 15, reading 10, stock accuracy 15, planning 20, ordering 15, follow-up 10, bills 10, deliveries 5. Each is measured from what was recorded (for example, notes stored by the mailbox with no person over all notes in 30 days). A part with nothing to measure scores zero, and a part the software cannot do is capped (a mail it drafted still needs a person to send it: half credit unedited, a quarter edited). The screen shows every line and what it was worked out from.
- **Orders to come (DDMRP screen):** what the buffers say to order now, what waits in the 15-day queue, and what was sent and has not arrived, with the day to order and the day to expect it.

## 9. Prices, units and calendars

- **Quote answers:** type in a supplier's price, lead time and how long it stands, per line. Copy the request to other suppliers to compare: price, lead time and the total for the wanted quantity are shown side by side with the cheapest and fastest marked. Award one and it ends the previous agreement for that article, enters the price history (so the usual price and the bill check know it), and becomes the supplier that order drafts use.
- **Pack sizes:** per article and per supplier: units per pack, the pack's name, minimum packs. An order for 130 washers sold by 100 with a minimum of 2 goes out as 2 boxes of 100.
- **Working calendar:** French public holidays (fixed ones and the three that follow Easter), your own closures, and per supplier a cut-off hour (an order after it counts as the next day) and shut weekdays. Expected delivery, days waited and chase timing all count working days.
- **Currency:** a bill in another currency is converted to euros at the last rate known on its date (typed, or fetched from the European Central Bank) before it is matched, or left "no rate".
- **Lead time per supplier** is also shown next to each contact.

## 10. Disputes, outbound, retention

- **Disputes:** one click on a mismatched bill opens a dispute with the reason worked out, drafts the mail to the supplier listing the deliveries the bill should pay for, and tracks it until the credit note is recorded: open, credited, closed once the credit covers 98% of the claim. No credit note after 14 days is flagged.
- **Deliveries to sites:** a stored order becomes a dispatch (van, day, lines), then loaded, then delivered with the name of who signed and what really arrived per line. A shortfall marks it part delivered. PDFs: a delivery note with a box to sign, 100 x 60 mm labels (site, work item, article, quantity), and the order itself, attached automatically to the supplier mail.
- **Retention:** a stored photo is kept as taken for 42 days, then packed into a small array in the database and the file deleted. The array is the page levelled for uneven light, flattened to one bit a pixel, packed eight to a byte and compressed: about 30 KB for a 1.6 MB photo, print still readable, colour gone. Kept for 10 years (`DOCUMENT_RETENTION_YEARS`). PDFs are kept as they are. `stock archive` packs everything now.

## 11. Security and traceability

| Guard | Detail |
|---|---|
| Network | Loopback only, random port |
| Password | scrypt (N=2^14, r=8, p=1), 8 characters minimum, 5 wrong tries pause for 5 minutes |
| Session | Signed cookie, SameSite strict, 12 hours. Origin check on every write |
| Files | Database, settings and account file mode 0600, data folder 0700 |
| SQL screen | One statement, SELECT only, external file access off, 500 rows |
| Backups | `stock backup`, encrypted with AES-256-CBC (PBKDF2, 200,000 rounds) when `BACKUP_PASSPHRASE` is set |
| Audit | Every change recorded with user, time and detail. Failures are logged and can raise an SMS alert (Twilio or a webhook, 6 an hour at most) |

## 12. Data model (main tables)

Paperwork: `notes`, `inventory_lines`, `purchase_orders`, `purchase_order_lines`, `quotes`, `quote_lines`, `bills`, `intake`. Catalogue: `catalog`, `article_names`, `article_aliases`, `article_prices`, `contacts`. Stock: `stock_counts`, `stock_moves`, `reservations`, `article_units`. Buying: `baskets`, `gate_requests`, `gate_log`, `order_queue`, `supplier_mail`, `quote_offers`, `agreements`, `disputes`. Logistics: `places`, `vehicles`, `dispatches`, `dispatch_lines`, `calendar_days`, `supplier_terms`, `fx_rates`. Control: `notices`, `photo_archive`. System: `audit`, `errors`, `readings`, `users`, `schema_migrations`.

Exports: inventory, purchase orders, bills and quotes as CSV, XLSX or Parquet (`stock export`), and any table through the SQL screen.

## 13. What leaves the machine

| To | When |
|---|---|
| Model provider (Claude, Gemini or OpenAI) | Reading a document, drafting an order, checking names. The photo or text is sent to that provider |
| Mail servers | Fetching mail, and sending a draft someone approved |
| Slack, Discord | Only if their tokens are set |
| OpenStreetMap services | Only when a route is asked for |

## 14. Command line

`stock run | read | search | ask | sql | export | backup | restore | vocab | audit | errors | sync | password | where | shrink | archive | sample`. `stock sample` adds made-up quotes, part deliveries, bills and baskets to an existing database; a new database fills itself with about six months of made-up paperwork in around 20 seconds (`SAMPLE_DATA=0` to start empty).

## 15. Not built

Carrier booking and tracking, export to accounting software, a payment or wallet step, and placing the order with the supplier (steps 6 and 7 of the round), site requisitions, and rules that let routine buys pass the gate by themselves. Microsoft mailboxes cannot send. Proton mail needs a paid plan for Bridge.
