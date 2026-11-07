"""The accounts that unlock this copy: a name, a password, a role, and a signed session.

The first account is the administrator. Others can be added with a role (viewer, buyer, approver,
admin); what each role may do is in access.py. Passwords are stored as scrypt hashes in the settings
folder, never in the database and never in plain text. They keep the window shut, nothing more: the data
file itself is readable by anyone who can read your home folder, so use full disk encryption if the
machine travels.
"""

import base64
import hashlib
import hmac
import json
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
ROLES = ("viewer", "buyer", "approver", "admin")
RANK = {role: number for number, role in enumerate(ROLES)}


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
    """The first account's password, as `stock password` changes it."""
    account = read_account(path)
    if account is None:
        raise ValueError("No account yet.")
    if not check_password(old, account["hash"]):
        raise ValueError("The old password does not match.")
    return write_account(account["user"], new, path)


def _save(account, path=None):
    path = Path(path or account_file())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(account), encoding="utf-8")
    path.chmod(0o600)


def _same(one, other):
    return (one or "").strip().upper() == (other or "").strip().upper()


def everyone(account):
    """Every account with its hash: the first (an administrator) and then the others."""
    if not account:
        return []
    first = {"user": account["user"], "hash": account["hash"], "role": "admin",
             "created_at": account.get("created_at"), "first": True}
    return [first] + [dict(other, first=False) for other in account.get("users", [])]


def find(account, name):
    return next((one for one in everyone(account) if _same(one["user"], name)), None)


def users(path=None):
    """Who may sign in, and as what. Never the hashes."""
    return [{"user": one["user"], "role": one["role"], "created_at": one["created_at"], "first": one["first"]}
            for one in everyone(read_account(path))]


def _check_role(role):
    if role not in ROLES:
        raise ValueError("A role is " + ", ".join(ROLES) + ".")
    return role


def add_user(user, role, password, path=None):
    account = read_account(path)
    if account is None:
        raise ValueError("No account yet.")
    name = clean_name(user)
    if not name:
        raise ValueError("A name is 1 to 32 characters: letters, digits, space, dot, dash, underscore.")
    _check_role(role)
    if len(password) < MIN_LENGTH:
        raise ValueError(f"The password needs at least {MIN_LENGTH} characters.")
    if find(account, name):
        raise ValueError(f"{name} already has an account.")
    account.setdefault("users", []).append({"user": name, "hash": hash_password(password), "role": role,
                                            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S")})
    _save(account, path)
    return name


def _other(account, name):
    found = find(account, name)
    if found is None:
        raise ValueError(f"No account named {name}.")
    if found["first"]:
        raise ValueError("The first account is the administrator and keeps that role.")
    return found


def set_role(user, role, path=None):
    account = read_account(path)
    found = _other(account, user)
    _check_role(role)
    for one in account["users"]:
        if _same(one["user"], found["user"]):
            one["role"] = role
    _save(account, path)
    return found["user"]


def remove_user(user, path=None):
    account = read_account(path)
    found = _other(account, user)
    account["users"] = [one for one in account["users"] if not _same(one["user"], found["user"])]
    _save(account, path)
    return found["user"]


def reset_password(user, new, path=None):
    """An administrator sets someone's password; the person changes it from their own screen."""
    account = read_account(path)
    found = find(account, user)
    if found is None:
        raise ValueError(f"No account named {user}.")
    if len(new) < MIN_LENGTH:
        raise ValueError(f"The password needs at least {MIN_LENGTH} characters.")
    if found["first"]:
        account["hash"] = hash_password(new)
    else:
        for one in account["users"]:
            if _same(one["user"], found["user"]):
                one["hash"] = hash_password(new)
    _save(account, path)
    return found["user"]


def change_own_password(user, old, new, path=None):
    found = find(read_account(path), user)
    if found is None or not check_password(old, found["hash"]):
        raise ValueError("The old password does not match.")
    return reset_password(user, new, path)


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

    def authenticate(self, user, password):
        """The account's name when the password is right, else None. Guessing is slowed for everyone at once."""
        now = time.time()
        self._attempts = [t for t in self._attempts if now - t < WINDOW_SECONDS]
        if len(self._attempts) >= MAX_ATTEMPTS:
            raise TooManyAttempts(int(WINDOW_SECONDS - (now - self._attempts[0])))
        account = self.account
        candidates = everyone(account)
        found = (find(account, user) if (user or "").strip()
                 else (candidates[0] if len(candidates) == 1 else None))
        if found and check_password(password, found["hash"]):
            self._attempts = []
            return found["user"]
        self._attempts.append(now)
        return None

    def unlock(self, password, user=None):
        """True when the password is right, for the first account unless another is named."""
        first = self.user
        return self.authenticate(user or first, password) is not None

    def role(self, user):
        found = find(self.account, user)
        return found["role"] if found else None

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

    def new_session(self, hours=SESSION_HOURS, user=None):
        payload = f"{int(time.time()) + int(hours * 3600)}:{user or self.user or ''}"
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
        found = find(self.account, user)
        return found["user"] if found else None
