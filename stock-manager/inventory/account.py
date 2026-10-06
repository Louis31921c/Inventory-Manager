"""The one account that unlocks this copy: a name, a password, and a signed session.

The password is stored as a scrypt hash in the settings folder, never in the database and never in
plain text. It keeps the window shut, nothing more: the data file itself is readable by anyone who
can read your home folder, so use full disk encryption if the machine travels.
"""

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path

from .audit import clean_name
from .config import config_dir

COOKIE = "stock_unlock"
SCRYPT = {"n": 2**14, "r": 8, "p": 1, "dklen": 32}
SALT_BYTES = 16
MAX_ATTEMPTS = 5
WINDOW_SECONDS = 300
SESSION_HOURS = 12
MIN_LENGTH = 8


class TooManyAttempts(Exception):
    def __init__(self, retry_after):
        super().__init__(f"Too many tries. Wait {retry_after // 60 + 1} minute(s).")
        self.retry_after = retry_after


def account_file():
    return config_dir() / "account.json"


def hash_password(password, salt=None):
    salt = salt or secrets.token_bytes(SALT_BYTES)
    key = hashlib.scrypt(password.encode(), salt=salt, **SCRYPT)
    return f"{base64.b64encode(salt).decode()}${base64.b64encode(key).decode()}"


def check_password(password, stored):
    try:
        salt = base64.b64decode(stored.split("$", 1)[0])
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(hash_password(password, salt), stored)


def read_account(path=None):
    path = path or account_file()
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def write_account(user, password, path=None):
    path = Path(path or account_file())
    name = clean_name(user)
    if not name:
        raise ValueError("A name is 1 to 32 characters: letters, digits, space, dot, dash, "
                         "underscore.")
    if len(password) < MIN_LENGTH:
        raise ValueError(f"The password needs at least {MIN_LENGTH} characters.")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"user": name, "hash": hash_password(password),
                                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S")}),
                    encoding="utf-8")
    path.chmod(0o600)
    return name


def change_password(old, new, path=None):
    account = read_account(path)
    if account is None:
        raise ValueError("No account yet.")
    if not check_password(old, account["hash"]):
        raise ValueError("The old password does not match.")
    return write_account(account["user"], new, path)


class Lock:
    """Checks the password, hands out signed sessions, and slows down guessing."""

    def __init__(self, data_dir, path=None):
        self.data_dir = Path(data_dir)
        self.path = path
        self._attempts = []

    @property
    def account(self):
        return read_account(self.path)

    @property
    def ready(self):
        return self.account is not None

    @property
    def user(self):
        account = self.account
        return account["user"] if account else None

    def unlock(self, password):
        now = time.time()
        self._attempts = [t for t in self._attempts if now - t < WINDOW_SECONDS]
        if len(self._attempts) >= MAX_ATTEMPTS:
            raise TooManyAttempts(int(WINDOW_SECONDS - (now - self._attempts[0])))
        account = self.account
        if account and check_password(password, account["hash"]):
            self._attempts = []
            return True
        self._attempts.append(now)
        return False

    @property
    def key(self):
        path = self.data_dir / "secret.key"
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(secrets.token_bytes(32))
            path.chmod(0o600)
        return path.read_bytes()

    def _sign(self, payload):
        return hmac.new(self.key, payload.encode(), hashlib.sha256).hexdigest()

    def new_session(self, hours=SESSION_HOURS):
        payload = f"{int(time.time()) + int(hours * 3600)}:{self.user or ''}"
        return f"{payload}.{self._sign(payload)}"

    def read_session(self, cookie):
        if not cookie or "." not in cookie:
            return None
        payload, _, signature = cookie.rpartition(".")
        if not hmac.compare_digest(signature, self._sign(payload)):
            return None
        expiry, _, user = payload.partition(":")
        try:
            if int(expiry) <= time.time():
                return None
        except ValueError:
            return None
        if user != (self.user or ""):
            return None
        return user or None
