#!/bin/sh
# Open the app on the made-up year instead of the real database.
#
# The app always opens ~/.local/share/stock-manager unless DATA_DIR says otherwise, so a
# simulation needs that one variable set. This sets it, and nothing else: the real
# database is not opened, not read and not written to.
#
#     sample_data/run-demo.sh
#
# Build the year first if it is not there:
#
#     python sample_data/make_sample_data.py --db demo
set -eu

here=$(cd "$(dirname "$0")" && pwd)
demo="$here/demo"
app="$HOME/Desktop/Stock Manager/venv/bin/stock-manager"

if [ ! -f "$demo/inventory.duckdb" ]; then
    echo "No demo yet. Build it with:"
    echo "    python \"$here/make_sample_data.py\" --db demo"
    exit 1
fi

# Its own config folder too, so the demo's password and settings are not the real ones.
DATA_DIR="$demo" XDG_CONFIG_HOME="$demo/config" exec "$app" "$@"
