"""The checks that guard a release (licence, file signature) are off for the suite, and tested on their own."""

import os

os.environ.setdefault("STOCK_LICENSE", "off")
os.environ.setdefault("STOCK_INTEGRITY", "off")
os.environ.setdefault("STOCK_VAULT_KEYRING", "0")

import json

import pytest

from inventory import db, llm
from helpers import FAKE_CLAUDE


@pytest.fixture(autouse=True)
def no_sample_year(monkeypatch):
    """A new database is filled with made-up paperwork. The suite wants to start empty.

    The settings file of whoever runs the suite is read into the environment when the
    server is imported, so the mailbox it names is cleared: a test about a mailbox that
    is not set up must not meet the real one.
    """
    monkeypatch.setenv("SAMPLE_DATA", "0")
    for name in ("MAIL_PROVIDER", "IMAP_HOST", "IMAP_USER", "IMAP_PASSWORD", "IMAP_PORT",
                 "IMAP_SECURITY", "IMAP_FOLDER", "IMAP_AUTO_READ", "IMAP_MARK_READ",
                 "MAIL_EVERY_MINUTES"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "t.duckdb"
    db.init(path)
    db.init(path)
    return path


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    binary = tmp_path / "claude"
    binary.write_text(FAKE_CLAUDE)
    binary.chmod(0o755)
    log = tmp_path / "log.json"
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))

    def respond(payload):
        monkeypatch.setenv("FAKE_CLAUDE_OUT", json.dumps(payload))
        return llm.ClaudeCodeBackend(binary=str(binary)), log

    return respond


@pytest.fixture
def vault_home(tmp_path, monkeypatch):
    from inventory import vault

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("STOCK_VAULT_KEYRING", "0")
    vault.forget_cached_key()
    yield tmp_path / "cfg" / "stock-manager"
    vault.forget_cached_key()
