"""The desktop app: a window of its own, the pages served from inside it.

Nothing listens outside this machine: the server binds to 127.0.0.1 on a free port and stops when
the window closes.
"""

import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

TITLE = "Stock Manager"


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def wait_until_up(url, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1):
                return True
        except urllib.error.HTTPError:
            return True
        except Exception:
            time.sleep(0.15)
    return False


def start_server(port):
    from . import server

    thread = threading.Thread(target=server.serve, kwargs={"port": port}, daemon=True)
    thread.start()
    return thread


def downloads_dir():
    folder = os.environ.get("XDG_DOWNLOAD_DIR") or Path.home() / "Downloads"
    folder = Path(folder).expanduser()
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError:
        folder = Path.home()
    return folder


def free_name(folder, suggested):
    name = Path(suggested or "download").name or "download"
    target = folder / name
    stem, dot, suffix = name.partition(".")
    count = 2
    while target.exists():
        target = folder / f"{stem}-{count}{dot}{suffix}"
        count += 1
    return target


def wire_downloads(context, tell):
    """WebKit drops a download on the floor unless somebody says where it goes."""
    def decide(download, suggested):
        target = free_name(downloads_dir(), suggested)
        download.set_destination(target.as_uri())
        return True

    def finished(download):
        path = download.get_destination() or ""
        tell(f"Saved to {path.removeprefix('file://')}")

    def failed(download, error):
        tell(f"The download failed: {error}")

    def started(_context, download):
        download.connect("decide-destination", decide)
        download.connect("finished", finished)
        download.connect("failed", failed)

    context.connect("download-started", started)


def _name_of(action):
    try:
        path = action.get_request().get_uri().rstrip("/").rsplit("/", 1)[-1]
    except Exception:
        path = ""
    return path or "window"


def open_window(url, stop):
    """A GTK WebKit window. Returns False when the toolkit is not installed."""
    try:
        import gi

        gi.require_version("Gtk", "3.0")
        gi.require_version("Gdk", "3.0")
        try:
            gi.require_version("WebKit2", "4.1")
        except ValueError:
            gi.require_version("WebKit2", "4.0")
        from gi.repository import Gdk, Gtk, WebKit2
    except (ImportError, ValueError):
        return False

    window = Gtk.Window(title=TITLE)
    window.set_default_size(1180, 820)
    window.set_icon_name("utilities-terminal")

    view = WebKit2.WebView()
    view.load_uri(url)
    settings = view.get_settings()
    settings.set_property("enable-developer-extras", True)
    window.add(view)

    def shut(*_):
        stop()
        Gtk.main_quit()

    window.connect("destroy", shut)

    def fullscreen_key(which):
        def on_key(_widget, event):
            if event.keyval != Gdk.KEY_F11:
                return False
            if which.get_window().get_state() & Gdk.WindowState.FULLSCREEN:
                which.unfullscreen()
            else:
                which.fullscreen()
            return True
        return on_key

    def second_window(source, action):
        """window.open() from the page: its own window, not a tab and not a browser."""
        child = WebKit2.WebView.new_with_related_view(source)
        extra = Gtk.Window(title=f"{TITLE} - {_name_of(action)}")
        extra.set_default_size(1180, 900)
        extra.set_icon_name("utilities-terminal")
        extra.add(child)
        extra.connect("key-press-event", fullscreen_key(extra))
        child.connect("create", second_window)
        child.connect("notify::title",
                      lambda view, _spec: extra.set_title(f"{TITLE} - {view.get_title()}"))
        child.connect("ready-to-show", lambda *_: extra.show_all())
        child.connect("close", lambda *_: extra.destroy())
        return child

    def tell(message):
        print(message, flush=True)
        bar = Gtk.MessageDialog(transient_for=window, modal=False,
                                message_type=Gtk.MessageType.INFO,
                                buttons=Gtk.ButtonsType.OK, text=message)
        bar.connect("response", lambda dialog, _response: dialog.destroy())
        bar.show()

    wire_downloads(view.get_context(), tell)
    view.connect("create", second_window)
    window.connect("key-press-event", fullscreen_key(window))
    window.show_all()
    Gtk.main()
    return True


def open_browser(url):
    import webbrowser

    webbrowser.open(url)
    print(f"{TITLE} is at {url}")
    print("close this window to stop it")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


def main(argv=None):
    port = free_port()
    url = f"http://127.0.0.1:{port}/"
    start_server(port)
    if not wait_until_up(url):
        print("the program did not start", file=sys.stderr)
        return 1

    stopped = threading.Event()

    def stop():
        if stopped.is_set():
            return
        stopped.set()
        try:
            request = urllib.request.Request(url + "exit", method="POST")
            urllib.request.urlopen(request, timeout=2).read()
        except Exception:
            pass

    if not open_window(url, stop):
        open_browser(url)
    stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
