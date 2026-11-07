# Operations

## First start

1. Install the wheel the vendor sent: `pipx install stock_manager-<version>-py3-none-any.whl`.
2. Start `stock-manager`. With no licence it shows its licence page: copy the machine id to the vendor, then paste the
   licence file they return. (`license.json` can also be put in the settings folder by hand.)
3. Create the first account: it is the administrator. Add the others on **System, Users**.
4. Set the mailbox, chat and connections on **System**. Secrets are encrypted as soon as they are saved.

## Every day

- `stock doctor` checks version, licence, program files, database schema, audit chain, permissions, secrets and the age
  of the newest backup. Run it after an upgrade and when something looks wrong.
- Alerts (what needs a person) are on the landing page and pushed to Slack, SMS or mail when set up.

## Back up

- The program writes an encrypted snapshot daily (`BACKUP_PASSPHRASE`). Restore with `stock restore`.
- Keep a copy of the **settings folder** (`~/.config/stock-manager/`), above all `vault.key` (or the OS keyring entry
  `stock-manager`): without it the encrypted settings, supplier tokens and the audit chain cannot be read.
- Test a restore on another machine once a quarter.

## Upgrade

- From the vendor: install the new wheel over the old one, start the program, run `stock doctor`. The database is
  upgraded by numbered migrations on first start; take a backup first.
- A copy whose program files were changed, or whose licence has ended, refuses to serve and says why on
  `/license-required`.

## Move to another machine

Install, install a licence for the new machine id, copy the data folder and the settings folder (permissions 700), start.

## Trouble

| Sign | Look at |
|---|---|
| Page says the licence is missing or ended | `/license-required`; ask the vendor for a file |
| "program files were changed" | reinstall the vendor's wheel; do not edit the installed package |
| A critical alert says the audit log was tampered with | `stock audit --verify`; keep the database and tell the vendor |
| Mail not arriving | **System, Mailbox**: test the connection; the check runs every 15 minutes |
| An agent does nothing | its mode is off or dry run (`stock doctor`, **Blockchain** screen) |
