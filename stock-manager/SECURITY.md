# Security

## Model

One licensed copy runs on one machine for one business. The program trusts the machine's owner and nobody on the
network: it listens on `127.0.0.1` only. What it protects against is a curious colleague, a stolen or copied file, a
web page open in the same browser, hostile mail from a supplier address, and mistakes by its own agents.

## What is in place

| Area | Measure |
|---|---|
| Network | binds to 127.0.0.1 on a free port; no inbound webhooks; every outbound address is https (localhost excepted) |
| Sign-in | scrypt password hashes, a global limit of 5 wrong tries per 5 minutes, signed session cookie (HttpOnly, SameSite=Strict, 12 hours) |
| Roles | viewer, buyer, approver, admin; one permission table (`access.py`), checked on every request; a test walks every route |
| Browser | same-origin check on every write, CSP, X-Frame-Options DENY, no-sniff, no referrer, `Cache-Control: no-store` on the API |
| Secrets | mail password, chat tokens, model keys and supplier tokens are encrypted (Fernet) in the settings file and the database; the key is in the OS keyring or a 0600 file |
| Files | settings and data folders are owner-only; loose permissions are tightened at start and reported by `stock doctor` |
| SQL | every value is a bound parameter; the only text put into SQL is a fixed column or table name; the SQL screen is read-only |
| Supplier mail | read as data: the reader only fills a form, an unknown sender is ignored, prices become offers (never awards), dates must be near |
| Money | a wallet a person funds; limits per order, per day and per supplier; dry run for 3 days before live; every decision logged |
| Audit | every change is an event; events are chained with a keyed hash, and a broken chain is a critical alert |
| Backups | encrypted with a passphrase; `stock doctor` warns when the newest is older than 3 days |
| Dependencies | pinned in `requirements.lock`; `pip-audit` runs in CI and before a release |
| Program files | signed manifest checked at start and every 5 minutes; a changed, missing or added file stops the program from serving |
| Licence | signed by the vendor's key; the program holds only the public key |

## Limits, stated plainly

- The database file is not encrypted at rest. Use full-disk encryption on any machine that travels.
- Anyone who can run code as the same operating-system user can read the key and the data. Nothing a program stores
  for itself can stop that.
- The program is Python. Someone who can edit it can remove the licence and integrity checks; the signature makes that
  detectable and the licence terms make it a breach. A compiled build raises the effort, it does not remove it.
- Roles are enforced by the server, but there is one database: an administrator can see everything.

## Reporting a problem

Write to the vendor with the version (`stock version`) and the output of `stock doctor`. Do not send the database,
the settings file or `vault.key`.
