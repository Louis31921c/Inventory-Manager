# Developing

```
python3 -m venv .venv && .venv/bin/pip install -e ".[dev,slack,discord,outlook]"
STOCK_LICENSE=off STOCK_INTEGRITY=off .venv/bin/python -m inventory.server     # run without the release checks
.venv/bin/python -m pytest -q                                                  # the suite sets both switches itself
ruff check inventory tools tests && mypy                                       # lint, and types for the security modules
pip-audit -r requirements.lock --no-deps --disable-pip
```

- **Tests**: `tests/`, one file per area. A test that needs a database uses the `db_path` fixture.
- **Migrations**: append a numbered entry to `inventory/migrations.py`; never edit one that has shipped.
- **Permissions**: a new write route is a buyer's by default (a delete an approver's); put it in `inventory/access.py`
  if it should need more. `tests` fails if a write is left to a viewer.
- **Secrets**: a new secret setting goes in `vault.SECRET_KEYS`.
- **Docs**: `python3 tools/architecture.py` refreshes the module map, `python3 tools/pipeline_schema.py docs/pipeline.html`
  the pipeline, `python3 tools/third_party.py` the licence list.

## Release (vendor only)

```
python3 tools/release.py          # builds dist/stock_manager-<version>-py3-none-any.whl, signed
python3 tools/vendor.py license "Customer" --days 365 --machine <their id> --out customer-license.json
```

The vendor key lives in `~/.stock-manager-vendor/vendor.key` and nowhere else. Back it up; never commit it.
`tools/ship.sh` installs into the desktop copy and signs that install.
