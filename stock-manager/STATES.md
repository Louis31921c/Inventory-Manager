# Life cycles

Every table with a `state` column, its steps and the moves between them. Written by
`python -m inventory.states > STATES.md` from `inventory/states.py`; do not edit by hand.

## Files in the inbox (`intake`)

A photo, PDF or mail attachment that has arrived and is waiting to be read.

A person can set any of these directly.

- **waiting** (start): arrived, not read yet or needs a person. Next: read, dropped.
- **read**: turned into a delivery note, bill or quote. Next: waiting.
- **dropped**: thrown away on purpose. Next: waiting.

## Baskets needing approval (`gate_requests`)

A purchase basket on its way through Terminus's check, a person's approval and the order.

- **waiting** (start): drafted, nobody has looked. Next: checked, approved, cancelled.
- **checked**: Terminus has read the names and prices. Next: approved, cancelled.
- **approved**: a person (or a policy) said yes. Next: ordered, cancelled.
- **ordered**: the order went out. End.
- **cancelled**: turned down or taken back. End.

## The 15-day queue (`order_queue`)

An article that will be ordered at the next sitting, not today.

- **waiting** (start): queued. Next: ordered, dropped.
- **ordered**: went into an order. End.
- **dropped**: no longer needed. End.

## Bills (`bills`)

A supplier's invoice.

A person can set any of these directly.

- **to pay** (start): received, not paid. Next: paid, disputed.
- **paid**: paid. Next: to pay.
- **disputed**: something is wrong with it. Next: to pay, paid.

## Bill disputes (`disputes`)

A claim against a supplier over a bill.

- **open** (start): claimed, nothing back yet. Next: credited, closed.
- **credited**: part of a credit note arrived. Next: credited, closed.
- **closed**: the credit covers the claim, or it was dropped. End.

## Quote requests (`quotes`)

A request for a price sent to suppliers.

A person can set any of these directly.

- **open** (start): being prepared. Next: sent, answered, closed.
- **sent**: asked. Next: answered, closed.
- **answered**: prices came back. Next: closed.
- **closed**: finished. End.

## Dispatches to sites (`dispatches`)

Goods going from the shelf to a site on a van.

- **planned** (start): set for a day and a van. Next: loaded, delivered, part delivered, failed.
- **loaded**: on the van. Next: delivered, part delivered, failed.
- **delivered**: signed for, all of it. End.
- **part delivered**: signed for, some of it. End.
- **failed**: did not arrive. End.

## Mail with suppliers (`supplier_mail`)

An order or question mail going out, or an answer coming in.

- **draft** (start): written, not sent. Next: sent, failed, closed.
- **failed**: sending did not work. Next: sent, draft, closed.
- **sent**: gone out. Next: confirmed, closed.
- **confirmed**: the supplier confirmed. Next: closed.
- **received**: came in, not answered. Next: closed.
- **closed**: no longer waiting on it. End.

## Payment batches (`payment_batches`)

A set of bill payments handed to the bank together.

- **open** (start): being put together. Next: exported, rejected.
- **exported**: file handed to the bank. Next: executed, open, rejected.
- **executed**: the bank paid. End.
- **rejected**: the bank refused it. End.

## Payments (`payments`)

One bill's payment inside a batch.

- **instructed** (start): in a batch, not paid. Next: executed, cancelled.
- **executed**: paid. End.
- **cancelled**: stopped. End.

## Money set aside (`commitments`)

Wallet money held back for an order not yet paid.

- **open** (start): held. Next: released.
- **released**: given back or spent. End.

## Price agreements (`agreements`)

A price a supplier agreed to hold.

- **active** (start): in force. Next: ended.
- **ended**: over. End.

## Stock held back (`reservations`)

Shelf stock kept for a site or a job.

- **active** (start): held. Next: released.
- **released**: free again. End.

## Site requests (`requisitions`)

A site asking for material.

- **open** (start): asked. Next: done, cancelled.
- **done**: supplied. End.
- **cancelled**: withdrawn. End.

## Put-away tasks (`putaways`)

Delivered goods waiting for a shelf place.

- **to put away** (start): on the floor. Next: put away.
- **put away**: on its shelf. End.

## Count tasks (`count_tasks`)

An article someone is asked to count today.

- **open** (start): to count. Next: done.
- **done**: counted. End.

## Recounts (`count_checks`)

A second count of an article whose first count looked wrong.

- **recount** (start): waiting for the second count. Next: confirmed, disputed.
- **confirmed**: the two counts agree. End.
- **disputed**: they still differ. Next: settled.
- **settled**: a person decided. End.

