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

**Plutus** (the buyer) orders from the DDMRP buffers, by itself. Every hour it writes one purchase order, drafted by PLUTUS, for the articles whose net flow has fallen into the yellow or red zone. The quantity is the buffer's own (up to the top of the green, in whole packs where a pack size is known), red before yellow. An article that already has an order, a draft, a queued line, a recent Plutus order or a delivery still owed is left alone, so nothing is ordered twice. It is a list, not a purchase: the order mails come from it, and the policies decide whether those go without a person. It can be switched off or run on demand from its card. A line ticked "to order" on a stored purchase order still waits 15 days and is then ordered as one purchase order per site and work item. Its card has a "Bill to" section (company name, contact name, address), remembered for the next order, saved on the order and printed on the supplier mail and PDF.

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

- **Terminus** reads the names (same, new or unsure) and then decides the baskets the gate holds: it approves when every line is the same article and its limits allow, and cancels a repeat, an article refused twice, or a basket nobody answered for 14 days. It starts off, then in dry run, and can decide for real only after 3 days of that. A person's decision is never overridden, and a doubt keeps the gate shut.
- **Approval never expires,** but it remembers the price it was given at: an order placed later at a price that moved more than 10% is asked for again.
- **Sealed log:** every turn (requested, checked, approved, cancelled, ordered) is an entry in `gate_log` carrying the SHA-256 of the one before it. An edited entry breaks the chain from there and the screen names it.
- **Blockchain screen:** the seven-step round (reading, shelf check, Terminus, decision, waiting for you, wallet, placing the order) as clickable wedges, each opening an audit of that step. Steps 6 and 7 are drawn but not built: an approved order is placed by hand.

## 7. Route wrangler

Vans with volume and payload (default two Renault Partner at 3.3 m3 and 650 kg, one Iveco Daily at 19.6 m3 and 1,800 kg) and up to 25 stops with opening hours. Addresses are geocoded by Nominatim and distances come from OSRM (OpenStreetMap). If either is unreachable it falls back to straight-line distance.

## 8. Control tower: dashboard, alerts, autonomy score

- **Landing dashboard:** counts of line down, critical and at risk, units short, then one row per short article: status, units short, the day it is needed, the work it blocks, the root cause, the supplier, its lead time, and the action to take. An article is short when what is needed (stock held for a work item, lines ticked "to order", all in the next 14 days) is more than the shelf, or when a used article is at zero or will run out before a delivery can arrive. Status is line down (at zero and needed within 3 days), critical (within 7) or at risk. The root cause is the first true of: held for approval, order sent with no confirmation, supplier ETA after the need date, order late, drafted not sent, waiting in the 15-day queue, part delivered, no order placed.
- **Notices:** every hour the app looks over its books and keeps a list of what needs a person (stock at zero, orders past their day, a basket held over 24 hours, bills past due, a quote needed within 3 days, no reply, owed lines, mismatched bills, disputes with no credit note, a price over 25% above usual). A notice is one entry for as long as it lasts and clears itself when fixed. Critical and high ones are pushed once to Slack, SMS or mail (`NOTIFY_VIA`), and a digest of everything open goes out daily at `NOTIFY_HOUR`. Nothing is pushed unless set.
- **Autonomy score (0 to 100, two decimals, live):** how far the architecture lets the software be a logistics manager, as a map to build from, not a count of what was done today. Nineteen capabilities in four areas (intake, planning, execution, control) carry fixed weights summing to 100 (placing orders 14, replenishment 8, spend control 8, demand and buffers 7, and so on). Each is rated on a ladder: 0 not built, 1 records only, 2 assisted (it prepares, a person carries out), 3 supervised (it acts alone on routine cases, a person takes the exceptions), 4 autonomous. Points are weight x level / 4. Levels move when something is built (a `policies`, `requisitions`, `scanning`, `payments` or `projects` module, or step 7 of the purchase round) or switched on (a mailbox, outgoing mail, alert pushing, the Claude command line). The landing page shows only the figure (DEV-SCORE); `/api/autonomy` returns the full table and the six biggest gaps ranked by points to win, each with the step that would close it.
- **Feedback loop:** what people correct changes what the app proposes. Each article carries two learned factors: one scales the quantity to order, one scales its safety stock. Cutting a Plutus quantity (or removing the line) pulls the first down by half of what you changed, raising it pulls it up. An article that a hand count found empty (counts under 30 days old only) gets 15% more safety stock, and one whose counts keep coming up at least 5% short gets 5% more. A calm article, with nothing for 60 days, drifts back toward 1. Factors stay between 0.4 and 1.6 (quantity) and 0.6 and 2.0 (safety). A basket refused at the gate is remembered per article and shown to Plutus. The Plutus card shows every factor and why, and any can be forgotten.
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

## 11. Policies and the rules for the two bots

**Policies** let routine work go through without a person, inside limits a person sets. Three actions: send a drafted order, send an order reminder, approve a held basket. Settings, Policies.

- **Kill switch:** everything is off until it is switched on, and switching it off stops all policies at once.
- **Dry run first:** a new policy is inactive and in dry run, which writes down what it would have done and does nothing. It can go live only after 3 days of that. The screen shows what each policy allowed and refused over the last 7 days.
- **Limits per policy:** supplier (or any), maximum euros per action, euros per day and per month, price band (default 10% off the agreed or usual price), minimum deliveries from the supplier (default 3), hours (default 8h to 17h), mails to one supplier per day (default 2), reminders per order (default 2). For baskets, the gate reasons it may cover (over-ceiling, dear-line, first-article, new-supplier, no-price).
- **Absolute limits no policy can raise:** 5,000 EUR in one action; nothing outside working days and the policy's hours; an order is never sent without an address, with a price that cannot be checked, or from a supplier with too little history; a basket is never approved unless Terminus read the names as the same, and only for the reasons the policy lists; an order for a price that moved more than the band is not sent.
- **Everything is logged:** each decision, allowed or refused, live or dry, with its reasons, in a log on the screen; actions are signed POLICY in the audit with the policy's name.

**Rules for the two bots** (Settings, Rules for the two bots). Each bot has a charter: what it is free to do, what it must never do, and numbers a person sets. The charter is added to the model's instructions, and the limits are applied again in code to whatever the model answers, so a model that ignores its instructions changes nothing.

| Bot | Free to | Never | Limits (defaults) |
|---|---|---|---|
| Plutus (buyer) | order what the DDMRP buffers ask for (red and yellow zones, up to the top of the green), inside a narrow band around the buffer's number; group by supplier; round to boxes | sign, send or approve anything; invent an article, supplier, price or quantity; pass the limits | 20 lines; 0.8 to 1.25 times the buffer's quantity; only DDMRP or planner articles or named ones; skip articles whose baskets were refused twice; 5,000 EUR a draft; a reason on every line; whole packs |
| Terminus (gate keeper) | read the names, then approve or cancel the baskets the gate holds | approve unless every line is the same article, past its limits, or for a reason a person decides; override a person's decision | a "same" under 0.8 confidence becomes unsure; approves at most 500 EUR a basket and 1,000 EUR a day; dear line, new supplier and no price stay with a person unless allowed; cancels a repeat of an approved basket (3 days), an article refused twice, a basket unanswered for 14 days |

Terminus's decisions are signed TERMINUS in the gate's sealed log. Lines Plutus wrote that break a limit are cut or corrected, and the draft says which and why. The charters live as plain files in the `bot-rules` folder next to the program (`plutus.md`, `terminus.md`): edit the text or the default limits there, and the next request uses them. A limit set on the screen overrides the file until it is reset.

## 12. Security and traceability

| Guard | Detail |
|---|---|
| Network | Loopback only, random port |
| Password | scrypt (N=2^14, r=8, p=1), 8 characters minimum, 5 wrong tries pause for 5 minutes |
| Session | Signed cookie, SameSite strict, 12 hours. Origin check on every write |
| Files | Database, settings and account file mode 0600, data folder 0700 |
| SQL screen | One statement, SELECT only, external file access off, 500 rows |
| Backups | `stock backup`, encrypted with AES-256-CBC (PBKDF2, 200,000 rounds) when `BACKUP_PASSPHRASE` is set |
| Audit | Every change recorded with user, time and detail. Failures are logged and can raise an SMS alert (Twilio or a webhook, 6 an hour at most) |

## 13. Data model (main tables)

Paperwork: `notes`, `inventory_lines`, `purchase_orders`, `purchase_order_lines`, `quotes`, `quote_lines`, `bills`, `intake`. Catalogue: `catalog`, `article_names`, `article_aliases`, `article_prices`, `contacts`. Stock: `stock_counts`, `stock_moves`, `reservations`, `article_units`. Buying: `baskets`, `gate_requests`, `gate_log`, `order_queue`, `supplier_mail`, `quote_offers`, `agreements`, `disputes`. Logistics: `places`, `vehicles`, `dispatches`, `dispatch_lines`, `calendar_days`, `supplier_terms`, `fx_rates`. Control: `notices`, `photo_archive`, `policies`, `policy_log`, `policy_settings`, `bot_limits`, `tuning`, `feedback`. System: `audit`, `errors`, `readings`, `users`, `schema_migrations`.

Exports: inventory, purchase orders, bills and quotes as CSV, XLSX or Parquet (`stock export`), and any table through the SQL screen.

## 14. What leaves the machine

| To | When |
|---|---|
| Model provider (Claude, Gemini or OpenAI) | Reading a document, drafting an order, checking names. The photo or text is sent to that provider |
| Mail servers | Fetching mail, and sending a draft someone approved |
| Slack, Discord | Only if their tokens are set |
| OpenStreetMap services | Only when a route is asked for |

## 15. Command line

`stock run | read | search | ask | sql | export | backup | restore | vocab | audit | errors | sync | password | where | shrink | archive | sample`. `stock sample` adds made-up quotes, part deliveries, bills and baskets to an existing database; a new database fills itself with about six months of made-up paperwork in around 20 seconds (`SAMPLE_DATA=0` to start empty).

## 16. Not built

Carrier booking and tracking, export to accounting software, a payment or wallet step, and placing the order with the supplier (steps 6 and 7 of the round), and site requisitions. Microsoft mailboxes cannot send. Proton mail needs a paid plan for Bridge.
