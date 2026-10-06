# Stock manager

A desktop app for a workshop. Photograph a delivery note or a handwritten hardware list, a model
reads it, you correct what it got wrong, and the lines land in a DuckDB file on your own machine.
Then search them, ask questions in plain words, or run SQL.

It opens in its own window. Nothing is hosted: the pages are served inside the app on 127.0.0.1,
on a port that changes every run, and stop when you close the window. There is no account and no
login; the data is a file in your home folder.

The same program also runs as a terminal screen (`stock-tui`) and as plain commands (`stock ...`)
if you prefer those.

![Search](docs/app-search.png)
## Install

Python 3.11 or newer.

```bash
pipx install git+https://github.com/Louis31921c/Inventory-Manager.git#subdirectory=stock-manager
```

From a clone, in a virtual environment:

```bash
python3 -m venv .venv && .venv/bin/pip install .
```

You get three commands:

| Command | Opens |
|---|---|
| `stock-manager` | the desktop app, in its own window |
| `stock-tui` | the same thing as a full screen terminal program |
| `stock ...` | one-off commands (`read`, `search`, `ask`, `export`, `backup`, `where`) |

The window needs GTK and WebKit2 (`python3-gi`, `gir1.2-webkit2-4.1` on Debian and Ubuntu, both
already present on most desktops). Without them the app falls back to your browser. If you install
into a virtual environment, create it with `--system-site-packages` so it can see them.

Reading photos needs a model. Two are supported:

- **Claude Code CLI** (default): install it, run `claude` once and `/login`. No API key.
- **Gemini API**: `pip install "stock-manager[gemini]"`, then put `LLM_BACKEND=gemini` and
  `GEMINI_API_KEY=...` in the settings file.

Everything except reading photos works with no model at all.

## Bringing data over from the web version

The earlier version of this ran as a website and kept its tables in French. One command moves that
data here, matching notes and sheets on the photo hash so it can be run again without duplicating
anything:

```bash
stock sync --server ubuntu@your-server --key ~/.ssh/your_key --photos
stock sync --file ~/Downloads/inventaire.duckdb --photos-from ~/Downloads/images
```

It prints what it found there and what it added here. Photos are optional; without them the
records still come across, with their lines, article names, measured accuracy and catalog.

## The password

The first time the window opens it asks for a name and a password, and writes them to
`~/.config/stock-manager/account.json` (scrypt hash, never the password itself). After that the
app is locked on every start: the right password opens your data, the wrong one gets nothing, and
five wrong tries pause it for five minutes.

`lock` in the header or `F8` locks it again without quitting; the session expires on its own after
twelve hours. Change the password with `stock password`.

It is a lock on the window, not on the file: the DuckDB file in your home folder is still readable
by anything running as you. Use full disk encryption if the machine travels.

## What protects the data

| Guard | What it stops |
|---|---|
| Loopback only, random port | nothing on the network can reach it |
| Password on open, scrypt hash, five tries per five minutes | someone sitting down at your machine |
| Signed session cookie, `SameSite=strict`, twelve hours | another site reusing your session |
| Origin check on every write | a page in your browser posting to the app |
| Read-only SQL, filesystem access off, one statement | a question turning into a delete |
| `0600` on the database, settings and account file, `0700` on the data folder | other accounts on the machine |
| 30 MB cap on uploads, photos purged after six weeks | the disk filling up quietly |
| Encrypted snapshots when `BACKUP_PASSPHRASE` is set | a stolen backup |

What it does not do: encrypt the database at rest, or protect you from software running as your own
user. For a laptop that leaves the workshop, turn on full disk encryption.

## Use it

```bash
stock-manager              # the desktop app
stock-tui                  # the same, in a terminal
stock read note.jpg        # read one photo, print what came back
stock search "hex bolt"    # the stored lines
stock ask "how many units per supplier last month?"
stock export deliveries --format xlsx
stock where                # where the data lives
```

### The five tabs

`Tab` moves between them, or press `1` to `5`.

| Tab | What you do |
|---|---|
| New note | `R` reads a photo. Correct the fields and lines, `S` saves. |
| Search | `/` filters, `B` shows backorders only, `E` edits the work item of a line. |
| SQL | `Enter` asks a question in plain words, or type a `SELECT` yourself. |
| Hardware list | `R` reads a sheet. `S` in stock, `O` to order, `C` clears, `D` deletes. |
| Settings | `B` backup, `T` test message, `M` merge two names, `E` export, `P` purge photos. |

### Keys everywhere

| Key | Does |
|---|---|
| F1 | SQL tab |
| F2 | ask a question |
| F3 | search |
| F4 | hardware lists |
| F5 / F6 | export the delivery lines to CSV / Parquet |
| F7 | quit |
| Ctrl-U | clear the line you are typing |
| Esc | cancel |

### Correcting a reading


On the New note tab the model's answer is a draft. Arrow keys move, `Enter` edits the field under
the cursor, `Q` edits a quantity, `B` ticks a backorder, `A` adds a line, `D` deletes one. What you
change is counted: the Settings tab shows the share of fields the model got right on your own
documents.

### Asking

A question in plain words goes to the model, which writes one `SELECT`. The query is shown above
the answer, so a misunderstood question is obvious. Anything starting with `select` or `with` runs
as typed. Every query runs read-only with file access switched off.

### Hardware lists

![Wanted lists](docs/app-wanted.png)

The handwritten sheets go through the same reader, and each line is ticked in stock or to order.
They share the article vocabulary with the delivery notes, which is what makes "asked for against
delivered" a single join.

## Where things are

| What | Path |
|---|---|
| Database and photos | `~/.local/share/stock-manager/` |
| Settings | `~/.config/stock-manager/settings.env` |
| Exports | `~/.local/share/stock-manager/exports/` |
| Backups | `~/.local/share/stock-manager/backups/` |

Override the data folder with `--data /some/path` or `DATA_DIR`. Copy `.env.example` to the
settings file to see every key.

![Settings](docs/app-settings.png)

## Three things it does that a spreadsheet does not

**One article vocabulary.** Both readers see the same list of names and write back into it, so a
sheet line and a delivery line meet on the same article. Two spellings that slip through get merged
with `M` (or `stock vocab --merge "VIS TH" "HEX BOLT"`), which rewrites the rows already stored.

**An audit trail.** Every save, correction, tick, merge, backup and export is written down with the
user name, which is your login name unless `STOCK_USER` or `--user` says otherwise.

**Errors that reach you.** Any failure gets a number, a row in the database and, with
`SMS_PROVIDER` set, a text message: `Stock manager error #14: backup failed - no space left on
device`. Repeats are muted; `stock errors --test` sends one on purpose.

## Backups

```bash
stock backup                         # encrypted snapshot, 14 days kept
stock restore ~/.local/share/stock-manager/backups/stock-2026-10-02-0300.tar.gz.enc
```

Set `BACKUP_PASSPHRASE` or the snapshot is written in the clear. Restore rebuilds the database
beside the live one; you swap it in yourself after looking at it. For a nightly snapshot, point
cron or a systemd timer at `stock backup`.

## Tables

`inventory.duckdb`:

| Table | Holds |
|---|---|
| `notes`, `deliveries` | one row per note, one per article line |
| `hardware_lists`, `hardware_list_lines` | the sheets and their lines, with `in_stock` |
| `article_names`, `article_aliases` | the shared vocabulary and its merges |
| `catalog` | supplier line -> confirmed name |
| `readings` | fields compared against fields corrected |
| `users`, `audit` | who exists, who did what |
| `errors` | numbered failures, and whether the message went out |

A dozen worked queries: [docs/queries.sql](docs/queries.sql).

## Reading rules

`inventory/rules.md` says how to name an article, how to count a box of 200, what counts as a
backorder. `inventory/prompts/` holds the prompts. Both are read on every photo, so edits apply
without restarting. Anything naming your company goes in `private_rules.md` inside the data folder,
which never leaves the machine.

## Development

```bash
.venv/bin/python -m pytest                   # 66 tests, no model call
.venv/bin/python -m inventory.eval           # reading accuracy on tests/corpus
python docs/make_screenshots.py              # redraw the screenshots above
```

| File | Role |
|---|---|
| `inventory/tui.py` | the full screen program: screens, keys, curses loop |
| `inventory/cli.py` | the commands |
| `inventory/screen.py` | the character grid both the terminal and the screenshots draw into |
| `inventory/db.py` | DuckDB storage and exports |
| `inventory/llm.py` | the two model backends |
| `inventory/vocabulary.py` | shared article names, aliases, merges |
| `inventory/audit.py`, `alerts.py` | who did what, and what broke |

## Licence

MIT: see [LICENSE](LICENSE). Copyright @louis31921c.

What comes back from a photo is a draft. Models misread quantities, references and dates, which is
why the correction step exists. This is not a system of record.
