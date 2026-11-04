# Logistics Manager

**One desk for a logistics manager's whole week: what arrived, what is on the shelf, what to order, who to chase, what to pay.**

It runs on your own computer, reads the paperwork you already have, and puts what needs a decision in front of a person.

## 1. Receiving: no more retyping delivery notes

- **Four ways in:** photo of a note, PDF or image file, a spreadsheet (CSV or Excel, columns recognised in English or French), or attachments from your mailbox.
- **Mailbox checked every 15 minutes** (Gmail, Outlook, Proton, Yahoo, iCloud, or any IMAP mailbox). A note that reads cleanly is stored on its own; anything with a gap, plus anything named invoice, bill or quote, waits for a person with the reason shown.
- **Each note records** supplier, delivery date, order date, site, work item and every line (article, quantity, backorder or not). The same file twice is stored once.
- **Part deliveries are tracked:** a missing line gets the date the supplier promised, and turns red once that date passes.
- **Photos are kept as proof,** reduced to about 250 KB, and cleared after 6 weeks (adjustable). The data stays.

## 2. Stock you can trust

- **On hand = delivered minus shipped,** per article and per site. A hand count always overrides the arithmetic.
- **Move stock between sites,** hold stock back for a work item (an order for that work item uses the hold up), and correct mistakes, always with a written reason.
- **Count smart:** the app lists which articles to count next. The busiest ones (80% of what left in the last 90 days) every 30 days, the next band every 60, the rest every 90. The gap each count finds is kept.

## 3. Ordering at the right time

- **Buffers per article** (red, yellow, green zones) sized from your own history. Lead times are measured from your notes (ordered date to delivered date), per supplier and per article, not guessed.
- **A demand outlook** labels every week quiet, light, usual or spike and shows the chance of a spike ahead.
- **Plutus, the built-in buyer,** drafts an order for what is running short. Lines ticked "to order" wait 15 days, then Plutus turns them into one purchase order per site and work item, so nothing is ordered twice.

## 4. Suppliers: draft, send, chase

- **One click turns** a purchase order or quote request into a ready mail per supplier, in French, addressed from your contact book. Nothing is sent until a person presses send.
- **Replies are filed:** a supplier reply that quotes the order or quote number marks it confirmed and takes the delivery date from the text.
- **Chasing:** an order unanswered after 3 days, or past its promised day, gets a reminder draft. Late delivery-note lines get one reminder per supplier.
- **Quote requests** are measured in what the trade uses: piece, metre, m2, m3, kg, tonne, litre, hour.

## 5. Pay only for what arrived

- **Bills are read from a photo or PDF,** then checked against the deliveries they cover, priced at what each article has cost. A bill within 2% (or 1 EUR) is matched. Over, under, no price known, or nothing delivered are flagged, and one click marks a bill disputed.
- **Also listed:** deliveries not billed after 45 days, and orders against what actually came in.

## 6. Spending under control

- **Anyone can fill a basket from Slack or Discord** (`/basket`, `/purchase`, `/quote`, `/stock`). Buying is held for a person when the basket has a new article, a new supplier, a total over 500 EUR, a line over twice the usual price, or a line with no price.
- **Terminus, a second AI check,** says whether an article is one you already know under another wording. It never approves anything.
- **Every decision is written to a tamper-evident log.** If any entry is edited afterwards, the screen shows which one.

## 7. Planning the round

- **Route wrangler** holds the vans (two Renault Partner at 3.3 m3 and 650 kg, one Iveco Daily at 19.6 m3 and 1,800 kg) and the stops with their opening hours.

## Built for a small team

- **Private by design:** data stays on your machine, reachable only from it, behind a password. Five wrong tries pause it for five minutes, sessions end after 12 hours, backups can be encrypted.
- **Try before you trust:** a new database fills itself with about six months of realistic made-up paperwork in roughly 20 seconds.
- **Everything is traceable:** who did what, and when.
- **One click to the tools you use:** Slack, Discord, Outlook, Proton Mail and the supplier contact sheet sit on the home screen.

## Good to know

- **Reading documents and the Plutus and Terminus assistants need an AI model:** the Claude command line signed in on the machine, or a Gemini or OpenAI key.
- **Proton Mail needs a paid Proton plan** (through its Bridge app). **Outlook** can be read but not sent from.
- **Not included yet:** carrier booking and tracking, export to accounting software, and paying or placing the order with the supplier. Approved orders are placed by hand.
