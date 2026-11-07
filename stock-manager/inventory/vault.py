"""Secrets are never written to disk in the clear.

Tokens, passwords and API keys (the mailbox password, the Slack and Discord tokens, the model keys,
a supplier's webhook token) are encrypted with Fernet (AES-128-CBC with an HMAC) before they are stored,
in the settings file and in the database. A stored secret looks like `enc:v1:<token>`.

The key lives in the OS keyring when the `keyring` package is installed and a keyring answers, and
otherwise in `vault.key` in the settings folder, readable by its owner only. That protects a settings
file or a database that is copied, backed up, mailed or committed by mistake. It does not protect against
somebody who can already run code as the same user: nothing a program stores for itself can.

A value that is not encrypted is read as it is, so an old file keeps working, and `seal_file` /
`seal_database` encrypt it in place.
"""

import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from .config import config_dir

PREFIX = "enc:v1:"
SERVICE = "stock-manager"

# The settings that hold a secret. A setting not listed here is stored as typed.
SECRET_KEYS = frozenset({
    "IMAP_PASSWORD", "GEMINI_API_KEY", "OPENAI_API_KEY", "SLACK_BOT_TOKEN", "SLACK_APP_TOKEN",
    "DISCORD_BOT_TOKEN", "TWILIO_AUTH_TOKEN", "SMS_WEBHOOK_TOKEN", "HEALTH_TOKEN"})

_cache: dict[Path, bytes] = {}


def key_file():
    return config_dir() / "vault.key"


def _from_keyring():
    try:
        import keyring

        found = keyring.get_password(SERVICE, "vault")
        if found:
            return found.encode()
        fresh = Fernet.generate_key()
        keyring.set_password(SERVICE, "vault", fresh.decode())
        return fresh
    except Exception:
        return None


def _key():
    path = key_file()
    if path in _cache:
        return _cache[path]
    if os.environ.get("STOCK_VAULT_KEYRING", "1") != "0":
        found = _from_keyring()
        if found and not path.exists():
            _cache[path] = found
            return found
    if path.exists():
        found = path.read_bytes().strip()
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        found = Fernet.generate_key()
        path.write_bytes(found)
    path.chmod(0o600)
    _cache[path] = found
    return found


def forget_cached_key():
    _cache.clear()


def is_sealed(value):
    return isinstance(value, str) and value.startswith(PREFIX)


def seal(value):
    """Encrypt a secret. An empty value, or one already sealed, is returned as it is."""
    if value is None or value == "" or is_sealed(value):
        return value
    return PREFIX + Fernet(_key()).encrypt(str(value).encode("utf-8")).decode("ascii")


def unseal(value):
    """The secret in the clear. A value that was never sealed is returned as it is; one that cannot be
    opened (the key was lost) is returned empty, so a missing secret reads as not set."""
    if not is_sealed(value):
        return value
    try:
        return Fernet(_key()).decrypt(value[len(PREFIX):].encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        return ""


def seal_file(path):
    """Encrypt every secret in a KEY=value settings file, in place. Gives back how many were sealed."""
    path = Path(path)
    if not path.is_file():
        return 0
    changed, out = 0, []
    for line in path.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() in SECRET_KEYS and value and not is_sealed(value):
            line = f"{key}={seal(value)}"
            changed += 1
        out.append(line)
    if changed:
        path.write_text("\n".join(out) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return changed


def unseal_environment():
    """Open the sealed secrets that were loaded into the environment from the settings file."""
    for key in SECRET_KEYS:
        value = os.environ.get(key)
        if is_sealed(value):
            os.environ[key] = unseal(value)


def seal_database(db_path):
    """Encrypt the supplier tokens stored in the database. Gives back how many were sealed."""
    from . import db

    sealed = 0
    for supplier, token in db.read(db_path, "SELECT supplier, token FROM supplier_channels"):
        if token and not is_sealed(token):
            db.write(db_path, "UPDATE supplier_channels SET token = ? WHERE supplier = ? RETURNING supplier",
                     [seal(token), supplier])
            sealed += 1
    return sealed


def harden(*folders):
    """Owner-only permissions on the settings and data folders and on what holds a secret."""
    fixed = []
    for folder in folders:
        folder = Path(folder)
        if not folder.is_dir():
            continue
        if folder.stat().st_mode & 0o077:
            folder.chmod(0o700)
            fixed.append(str(folder))
        for name in ("settings.env", "settings.env.bak", "account.json", "vault.key", "secret.key"):
            path = folder / name
            if path.is_file() and path.stat().st_mode & 0o077:
                path.chmod(0o600)
                fixed.append(str(path))
    return fixed
