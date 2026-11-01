"""Signing in to Outlook the way Microsoft still allows: a token, not a password.

Microsoft turned basic IMAP auth off for most tenants, so `box.login(user, password)` is
refused however right the password is. What is left is OAuth: the mailbox is opened with a
short-lived access token, and the app keeps a refresh token to get the next one.

The flow is the device code one, because this app has no address of its own for Microsoft
to send anybody back to. Stock Manager asks for a code, you type it into
microsoft.com/devicelogin in a browser, and the token lands here when you have said yes.

Nothing of this is ever shown back: the tokens live in a file only the owner can read, and
the screens say signed in or not, never with what.
"""

import json
import logging
import os
import threading

from .config import config_dir

log = logging.getLogger(__name__)

CLIENT_ID_KEY = "AZURE_CLIENT_ID"
TENANT_KEY = "AZURE_TENANT"
OAUTH_KEY = "IMAP_OAUTH"

# Delegated, and only this one: read and write the signed-in person's own mailbox over
# IMAP. offline_access comes with the device flow, which is what buys the refresh token.
SCOPES = ["https://outlook.office.com/IMAP.AccessAsUser.All"]

# "common" lets a work account and a personal one both in. A tenant id narrows it to one firm.
DEFAULT_TENANT = "common"

# The flow in the middle of being answered: the code is shown, nobody has typed it yet.
_pending = {"flow": None, "code": None, "uri": None, "message": None, "error": None,
            "done": False}
_lock = threading.Lock()


def cache_file():
    return config_dir() / "outlook-token.json"


def available():
    try:
        import msal  # noqa: F401
        return True
    except ImportError:
        return False


def settings():
    return {"client_id": os.environ.get(CLIENT_ID_KEY, "").strip(),
            "tenant": os.environ.get(TENANT_KEY, "").strip() or DEFAULT_TENANT,
            "on": os.environ.get(OAUTH_KEY, "").strip().lower() in ("1", "true", "yes")}


def _cache():
    import msal

    store = msal.SerializableTokenCache()
    path = cache_file()
    if path.exists():
        try:
            store.deserialize(path.read_text())
        except Exception as e:  # a cache that cannot be read is a cache to start again
            log.warning("outlook: the token cache could not be read (%s)", e)
    return store


def _save(store):
    if not store.has_state_changed:
        return
    path = cache_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(store.serialize())
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _client(kept=None):
    import msal

    kept = kept or settings()
    if not kept["client_id"]:
        raise OAuthError("No application id yet: register one in Azure and paste its id here.")
    store = _cache()
    app = msal.PublicClientApplication(
        kept["client_id"],
        authority=f"https://login.microsoftonline.com/{kept['tenant']}",
        token_cache=store,
    )
    return app, store


class OAuthError(Exception):
    pass


def signed_in():
    """Whether there is an account in the cache to get a token for."""
    if not available() or not settings()["client_id"] or not cache_file().exists():
        return False
    try:
        app, _ = _client()
        return bool(app.get_accounts())
    except Exception:
        return False


def who():
    """The address the cached account belongs to, for the screen to show."""
    try:
        app, _ = _client()
        accounts = app.get_accounts()
        return accounts[0].get("username") if accounts else None
    except Exception:
        return None


def start():
    """Ask Microsoft for a code, and hand back what the person has to do with it."""
    if not available():
        raise OAuthError("The library is missing: pip install 'stock-manager[outlook]'.")
    app, store = _client()
    flow = app.initiate_device_flow(scopes=SCOPES)
    if "user_code" not in flow:
        raise OAuthError(flow.get("error_description") or "Microsoft refused to start.")

    with _lock:
        _pending.update({"flow": flow, "code": flow["user_code"],
                         "uri": flow.get("verification_uri"), "message": flow.get("message"),
                         "error": None, "done": False})

    def wait():
        # This blocks until the code is typed in or it runs out, which is why it is off
        # on a thread of its own: the app must not stop serving while someone finds a
        # browser.
        try:
            answer = app.acquire_token_by_device_flow(flow)
            if "access_token" in answer:
                _save(store)
                with _lock:
                    _pending.update({"done": True, "error": None, "flow": None})
            else:
                with _lock:
                    _pending.update({"done": True, "flow": None,
                                     "error": answer.get("error_description")
                                     or answer.get("error") or "Microsoft said no."})
        except Exception as e:
            with _lock:
                _pending.update({"done": True, "flow": None, "error": str(e)})

    threading.Thread(target=wait, name="outlook-device-flow", daemon=True).start()
    return {"code": flow["user_code"], "uri": flow.get("verification_uri"),
            "message": flow.get("message"), "expires_in": flow.get("expires_in")}


def state():
    """Where the sign-in has got to, for the screen to poll."""
    with _lock:
        waiting = _pending["flow"] is not None
        return {"available": available(), "signed_in": signed_in(), "who": who(),
                "waiting": waiting, "code": _pending["code"] if waiting else None,
                "uri": _pending["uri"] if waiting else None,
                "message": _pending["message"] if waiting else None,
                "error": _pending["error"], "done": _pending["done"],
                **settings()}


def forget():
    """Drop the tokens. The next fetch asks to sign in again."""
    path = cache_file()
    if path.exists():
        path.unlink()
    with _lock:
        _pending.update({"flow": None, "code": None, "uri": None, "message": None,
                         "error": None, "done": False})
    return True


def token():
    """A fresh access token, from the cache, refreshing it quietly if it has to."""
    if not available():
        raise OAuthError("The library is missing: pip install 'stock-manager[outlook]'.")
    app, store = _client()
    accounts = app.get_accounts()
    if not accounts:
        raise OAuthError("Nobody is signed in to Outlook yet.")
    answer = app.acquire_token_silent(SCOPES, account=accounts[0])
    _save(store)
    if not answer or "access_token" not in answer:
        raise OAuthError("The sign-in has run out; sign in to Outlook again.")
    return answer["access_token"]


def sasl(user, access_token):
    """The one string IMAP wants for XOAUTH2, as Microsoft spells it."""
    return f"user={user}\x01auth=Bearer {access_token}\x01\x01"


def dump_cache_for_test(payload):
    """Only the tests use this: put a cache in place without talking to Microsoft."""
    path = cache_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))
    return path
