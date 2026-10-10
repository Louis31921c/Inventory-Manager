"""The desktop app: a window of its own, the pages served from inside it.

Nothing listens outside this machine: the server binds to 127.0.0.1 on a free port and stops when
the window closes.
"""

import socket
import sys
import threading
import time
import urllib.error
import urllib.request

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

    def on_key(_widget, event):
        if event.keyval == Gdk.KEY_F11:
            if window.get_window().get_state() & Gdk.WindowState.FULLSCREEN:
                window.unfullscreen()
            else:
                window.fullscreen()
            return True
        return False

    window.connect("key-press-event", on_key)
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
