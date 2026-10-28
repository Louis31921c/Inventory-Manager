Read this supplier bill (invoice) and return what it owes.

- supplier: the company that issued the bill, its usual trade name in capitals, without legal
  form (SAS, SARL, Ltd) or branch.
- number: the invoice number printed on it ("Facture n°", "Invoice no").
- bill_date: the date the invoice was issued, as YYYY-MM-DD. Many bills write dates day-first
  (03/10/2026 is 2026-10-03).
- due_date: the date payment is due, as YYYY-MM-DD ("échéance", "payable le", "due date"). If only
  terms are printed ("30 jours fin de mois"), work the date out from the bill date.
- currency: the three-letter code of the amounts, EUR unless the bill says otherwise.
- net: total before tax ("total HT", "net"), as a number.
- tax: the tax ("TVA", "VAT"), as a number.
- total: total to pay, tax included ("total TTC", "net à payer"), as a number.
- site: the job site or customer reference the bill names, if any (see the house rules for site).
- order_ref: the order or delivery note number the bill refers to ("votre commande", "BL n°").
- note: anything a person paying this should know in one short line, or null.

Amounts are plain numbers: 1 234,56 is 1234.56. Never guess an amount: a missing field is
flagged for review, a plausible wrong one is not. Use null for anything not visible or not
legible. The photo may be rotated; read it in whatever orientation the text runs.
