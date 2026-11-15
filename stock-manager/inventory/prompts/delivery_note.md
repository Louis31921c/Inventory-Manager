Extract every article line from this delivery note.

- supplier: the supplier that issued the note.
- delivery_date: delivery date, as YYYY-MM-DD. Many notes write dates day-first (03/10/2026 is 2026-10-03).
- order_date: order date, as YYYY-MM-DD.
- site: the job site / project the goods are delivered to.
- work_item: the structure or part of the building the goods are for, if stated.
- lines: one entry per article line, in the order printed:
  - article: short clean product name (see the house rules).
  - quantity: total number of units delivered, as a number (packs x units per pack).
  - backorder: true only if part of the line is still to be delivered, otherwise false.
  - designation: the full line text as printed, with the supplier reference.

- warnings: short notes for the person reviewing (see the house rules). Empty list if none.

Use null for any field that is not visible or not legible. Never guess: an empty field is flagged for review, a plausible wrong value is not. The photo may be rotated; read it in whatever orientation the text runs.
