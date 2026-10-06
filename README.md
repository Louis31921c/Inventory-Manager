# Inventory Manager

A lightweight inventory management ERP for tracking your company's hardware. Snap a photo of a delivery note or a handwritten hardware list, review what the model extracted, edit any fields as needed, and save the results to DuckDB. Then query it with SQL or export it (csv, parquet, xlsx). accessible from your phone or computer.each user has its own session to track activity and be able to communicate within the company.

This tool's been custom designed on demand to solve a real business need : tracking two bottlenecks of a company's inventory to funnel its available stock without doing a manual audit of every article. Basic UI UX and possibility to query/export both tables : inventory & purchase order

```
photo -> model -> you check it -> DuckDB -> search, SQL, questions, exports
```

## Install and run

```bash
pipx install git+https://github.com/Louis31921c/Inventory-Manager.git#subdirectory=stock-manager
stock-manager
```

Python 3.11 or newer. Reading photos uses the Claude Code CLI by default (no API key) or the Gemini
API. Feel free to make it run on your own model !


### Search

![Search](stock-manager/docs/app-search.png)

Every stored line is one row: article, quantity, supplier, delivery date, site and work item.
`/` filters, `B` shows backorders only, `E` edits the work item in place, F5 and F6 export the lot
to CSV or Parquet. Query one, or JOIN both for more advanced indicators


### Settings

![Settings](stock-manager/docs/app-settings.png)

Disk and database size, measured reading accuracy, the article vocabulary and its merges, who did
what, numbered errors, snapshots, force a backup, each user's commit audit

### Wanted lists

![Wanted lists](stock-manager/docs/app-wanted.png)

Photograph a purchase order and every line is read. Tick each one in stock or to order; the
sheets share the article names with the delivery notes, so the two can be joined.

## Things worth knowing

- **One list of article names.** Notes and sheets share it. Merge two spellings and the rows
  already stored are rewritten too.
- **Every change is signed.** Saves, corrections, ticks, merges, backups and exports are recorded
  against a user name.
- **Failures reach you.** Each one gets a number, a row, and a text message to the Admin phone (SMS) if you configured one.


## Licence

MIT: see [LICENSE](LICENSE). Copyright @louis31921c. What a model reads off a photo is a draft a
human must check against the original. This is not a system of record, and the licence gives no
warranty of any kind.
