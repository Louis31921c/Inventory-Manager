#!/bin/sh
# Commit the source, push it, and put it where the app will actually read it.
#
#     tools/ship.sh              commit, push, reinstall
#     tools/ship.sh "a message"  the same, with your own commit message
#
# Only source files are taken: the extensions in KINDS below, anywhere in the repo.
# Databases, images and whatever else is lying about are left alone.
#
# The reinstall is the part that is easy to forget. The app serves app.html and sap.css
# from the installed package and re-reads them on every request, so a reload in the window
# picks those up — but a .py file is imported once at startup, from the package, so an edit
# to one reaches a running app only after this script has copied it in and the app has been
# started again. Reloading alone will not do it.
set -eu

here=$(cd "$(dirname "$0")/.." && pwd)
repo=$(cd "$here/.." && pwd)
venv="$HOME/Desktop/Stock Manager/venv"

KINDS="py html css md txt yaml yml sh json toml"

cd "$repo"

# Stage the source and nothing else, new files included.
staged=0
for kind in $KINDS; do
    if git add --all -- "*.$kind" 2>/dev/null; then
        staged=1
    fi
done
[ "$staged" = 1 ] || true

if git diff --cached --quiet; then
    echo "Nothing to ship: no source file has changed."
    exit 0
fi

files=$(git diff --cached --name-only | sed 's|.*/||' | sort -u | tr '\n' ' ')
count=$(git diff --cached --name-only | wc -l)
message=${1:-"Edits by hand: $files"}

echo "--- shipping $count file(s) ---"
git diff --cached --name-only | sed 's/^/    /'

git commit -q -m "$message"
echo "committed  $(git log --oneline -1)"

if git remote get-url origin >/dev/null 2>&1; then
    git push -q && echo "pushed     $(git rev-parse --abbrev-ref HEAD) -> origin"
else
    echo "no remote: committed only"
fi

# Into the venv, so a .py edit is one the running app can actually load.
if [ -x "$venv/bin/pip" ]; then
    "$venv/bin/pip" install -q "$here" && echo "installed  $venv"
    echo
    echo "Restart Stock Manager for the Python to take. A reload is enough for html and css."
else
    echo "no venv at $venv: nothing installed"
fi
