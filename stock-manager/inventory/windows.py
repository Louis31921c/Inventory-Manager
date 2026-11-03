"""Find a browser window that already shows a page, and bring it to the front (KDE).

A second click on Slack should go back to the Slack that is open, not make another tab.
The browser will not say what it has open, but KDE knows every window and its title, and
a browser's window is titled after the tab that is showing. So a small script is handed to
the window manager, which looks for a browser window whose title has the name in it and
raises the one it finds. It tells this program what it found through the session bus.

That is all it can know: a page in a tab that is not the one showing has no window of its
own to find, and then the page is opened as usual. Outside KDE nothing here is available
and the answer is always "not found".
"""

import itertools
import json
import os
import subprocess
import tempfile

BROWSERS = ("firefox", "chromium", "chrome", "brave", "librewolf", "zen", "vivaldi", "opera")

NAME = "org.stockmanager.Windows"
PATH = "/org/stockmanager/Windows"
INTERFACE = "org.stockmanager.Windows"
XML = f"""<node><interface name="{INTERFACE}">
  <method name="Found"><arg type="s" direction="in"/><arg type="b" direction="in"/></method>
</interface></node>"""

SCRIPT = """
const pattern = new RegExp(%(pattern)s, "i");
const classes = %(classes)s;
let found = false;
const all = workspace.windowList ? workspace.windowList() : workspace.clientList();
for (const w of all) {
  const kind = String(w.resourceClass || "").toLowerCase();
  if (!classes.some((one) => kind.includes(one))) continue;
  if (!pattern.test(String(w.caption))) continue;
  found = true;
  if (!%(dry)s) {
    w.minimized = false;
    if ("activeWindow" in workspace) workspace.activeWindow = w;
    else workspace.activeClient = w;
  }
  break;
}
callDBus(%(name)s, %(path)s, %(interface)s, "Found", %(token)s, found);
"""

_counter = itertools.count(1)
_waiting = {}
_owned = []
_after_owning = []


def _kwin(path, method, *args):
    return subprocess.run(
        ["gdbus", "call", "--session", "--dest", "org.kde.KWin", "--object-path", path,
         "--method", method, *args],
        capture_output=True, text=True, timeout=5, check=True).stdout


def _answer(_connection, _sender, _path, _interface, _method, parameters, invocation):
    token, found = parameters.unpack()
    invocation.return_value(None)
    done = _waiting.pop(token, None)
    if done:
        done(found)


def _listen(then):
    """Own a name on the session bus, once, so the script has someone to answer to. `then`
    runs when the name is ours: a script that calls before that has nobody to call."""
    if _owned and not _after_owning:
        return then()
    _after_owning.append(then)
    if len(_owned) > 0:
        return
    from gi.repository import Gio

    info = Gio.DBusNodeInfo.new_for_xml(XML).interfaces[0]

    def acquired(connection, _name):
        connection.register_object(PATH, info, _answer, None, None)

    def owned(_connection, _name):
        waiting = list(_after_owning)
        _after_owning.clear()
        for step in waiting:
            step()

    _owned.append(Gio.bus_own_name(Gio.BusType.SESSION, NAME, Gio.BusNameOwnerFlags.REPLACE,
                                   acquired, owned, None))


def raise_existing(title, done, classes=BROWSERS, dry=False, wait_ms=1500):
    """Look for a browser window titled like `title`. Calls done(True) when it was raised,
    done(False) when there is none or this desktop cannot say."""
    from gi.repository import GLib

    token = f"w{next(_counter)}"
    plugin = f"stock-manager-{token}-{os.getpid()}"
    handle = None
    settled = []

    def finish(found):
        if settled:
            return
        settled.append(True)
        _waiting.pop(token, None)
        try:
            _kwin("/Scripting", "org.kde.kwin.Scripting.unloadScript", plugin)
        except Exception:
            pass
        if handle is not None:
            try:
                os.unlink(handle)
            except OSError:
                pass
        done(bool(found))

    def start():
        nonlocal handle
        try:
            with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as script:
                script.write(SCRIPT % {
                    "pattern": json.dumps(title), "classes": json.dumps(list(classes)),
                    "dry": "true" if dry else "false", "name": json.dumps(NAME),
                    "path": json.dumps(PATH), "interface": json.dumps(INTERFACE),
                    "token": json.dumps(token)})
                handle = script.name
            _waiting[token] = finish
            out = _kwin("/Scripting", "org.kde.kwin.Scripting.loadScript", handle, plugin)
            number = "".join(ch for ch in out.split(",")[0] if ch.isdigit())
            _kwin(f"/Scripting/Script{number}", "org.kde.kwin.Script.run")
        except Exception:
            finish(False)

    try:
        _listen(start)
    except Exception:
        return done(False)

    def give_up():
        finish(False)
        return False

    GLib.timeout_add(wait_ms, give_up)
