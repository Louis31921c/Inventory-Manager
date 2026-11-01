"""The five doors on the landing page.

Slack, Discord and the two mailboxes live outside this app, and the contact sheet is a
file on the disk. None of them is something the app can draw, so the shortcut hands the
target to the desktop and lets whatever already opens that kind of thing open it.

The window is WebKit, not a browser: following an https link in it would walk the app
itself off its own page, so nothing here is a link. The page asks the server by name and
the server opens it outside the window.

Only the five names below can be asked for. The page never sends a url or a path, so
there is nothing here that a page in somebody's browser could point at a file of its
own choosing.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

# Where each door goes. A `url` is handed to the desktop as it is; `file` is read from the
# settings at the time of asking, since it is a path on this machine and not ours to fix.
DOORS = {
    "slack": {"name": "Slack", "url": "https://app.slack.com/client/T0CDDRRKFT9/C0CDDRT54LF"},
    "discord": {"name": "Discord", "url": "https://discord.com/channels/@me"},
    "outlook": {"name": "Outlook", "url": "https://outlook.live.com/mail/?bO=1&login_hint=M%24EjZ4nOM4Nav7wt5ta35ECYkn5ySWpRZlpubkl2YWO6TnJmbm6CXn50ocOPRn0e__QMAIAApUF8A"},
    "proton": {"name": "Proton Mail", "url": "https://mail.proton.me/u/11/inbox/pq3s00YFcvy1JzRBo1Cp_3pkPIFDXwdCToNXf4kJpmriI70M5txO8HiFzTtEYFWSx7d4gzOcGefMhtO8l9mqCA==#category=primary"},
    "contacts": {"name": "Supplier contacts", "file": "CONTACTS_CSV",
                 "default": "supplier-contacts.csv"},
}

ORDER = ("slack", "discord", "outlook", "proton", "contacts")


def contacts_path(data_dir):
    """The contact sheet: CONTACTS_CSV if it is set, otherwise beside the database."""
    named = os.environ.get("CONTACTS_CSV", "").strip()
    if named:
        return Path(named).expanduser()
    return Path(data_dir) / DOORS["contacts"]["default"]


def target(key, data_dir):
    """What `key` points at, and whether it is there to be opened."""
    door = DOORS.get(key)
    if door is None:
        raise ValueError(f"No such shortcut: {key}.")
    if "url" in door:
        return door["name"], door["url"], True
    path = contacts_path(data_dir)
    return door["name"], str(path), path.is_file()


def listing(data_dir):
    """What the landing page draws: the five doors, in the order they are shown."""
    out = []
    for key in ORDER:
        name, where, ready = target(key, data_dir)
        out.append({"key": key, "name": name, "where": where, "ready": ready})
    return out


def _opener():
    """The command this desktop opens things with."""
    if sys.platform == "darwin":
        return ["open"]
    if os.name == "nt":
        return None  # os.startfile does it there
    found = shutil.which("xdg-open") or shutil.which("gio")
    if found and found.endswith("gio"):
        return [found, "open"]
    return [found] if found else None


def open_it(key, data_dir):
    """Hand one door to the desktop. Gives back what was opened, or says why it was not."""
    name, where, ready = target(key, data_dir)
    if not ready:
        raise FileNotFoundError(
            f"{name} is not there: {where}. Put the file there, or set CONTACTS_CSV in "
            "the settings to where it is.")
    if os.name == "nt":
        os.startfile(where)  # noqa: S606 - the only way on Windows
        return {"key": key, "name": name, "where": where}
    command = _opener()
    if command is None:
        raise RuntimeError("This desktop has no xdg-open, so the app cannot open it for you.")
    # Detached on purpose: the viewer outlives the request, and the app waits on nothing.
    subprocess.Popen(command + [where], stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)
    return {"key": key, "name": name, "where": where}
