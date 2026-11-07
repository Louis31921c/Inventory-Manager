"""Tests: security."""

import json
from datetime import date
from types import SimpleNamespace

import pytest

from inventory import audit, db
from inventory.config import MailConfig
from helpers import _vendor, make_config, make_note


@pytest.mark.parametrize("raw,expected", [
    ("alex", "ALEX"), ("  marie claire ", "MARIE CLAIRE"), ("N.T", "N.T"), ("a_b-1", "A_B-1"),
    ("", None), (None, None), ("x" * 40, None), ("bad:name", None), ("<script>", None),
])
def test_clean_name(raw, expected):
    assert audit.clean_name(raw) == expected


def test_audit_records_who_did_what(db_path):
    audit.record(db_path, "alex", "login", None, "password", "10.0.0.1")
    audit.record(db_path, "ALEX", "note.save", "note #1", "ACME · 2 line(s)", "10.0.0.1")
    audit.record(db_path, "marie", "order.tick", "list #3 line 2", "to order", "10.0.0.2")
    audit.record(db_path, None, "export", "inventory.csv", None, "10.0.0.3")

    events = audit.events(db_path, limit=10)
    assert [e["action"] for e in events] == ["export", "order.tick", "note.save", "login"]
    assert events[-1]["user"] == "ALEX" and events[0]["user"] == "?"
    assert audit.events(db_path, user="marie")[0]["target"] == "list #3 line 2"

    people = {u["user"]: u for u in audit.users(db_path)}
    assert people["ALEX"]["notes"] == 1 and people["ALEX"]["logins"] == 1 and people["ALEX"]["events"] == 2
    assert people["MARIE"]["corrections"] == 1
    assert people["ALEX"]["last_seen"] is not None


def test_audit_never_raises(tmp_path):
    audit.record(tmp_path / "missing" / "nope.duckdb", "ALEX", "note.save")


def test_saving_records_the_user(db_path):
    db.save_note(db_path, make_note(), "s", "p.jpg", "ALEX")
    db.save_purchase_order(db_path, {"lines": [{"article": "BOLT"}]}, "l", "l.jpg", "MARIE")
    assert db.run_readonly(db_path, "SELECT confirmed_by FROM notes").rows == [("ALEX",)]
    assert db.purchase_orders(db_path)[0]["created_by"] == "MARIE"


def test_account_file_and_password(tmp_path):
    from inventory import account

    path = tmp_path / "account.json"
    assert account.read_account(path) is None
    with pytest.raises(ValueError):
        account.write_account("alex", "short", path)
    with pytest.raises(ValueError):
        account.write_account("bad:name", "longenough", path)

    assert account.write_account("alex", "correct horse", path) == "ALEX"
    stored = account.read_account(path)
    assert stored["user"] == "ALEX" and "$" in stored["hash"]
    assert oct(path.stat().st_mode)[-3:] == "600"
    assert account.check_password("correct horse", stored["hash"])
    assert not account.check_password("wrong", stored["hash"])

    account.change_password("correct horse", "another one", path)
    assert account.check_password("another one", account.read_account(path)["hash"])
    with pytest.raises(ValueError):
        account.change_password("not it", "whatever else", path)


def test_lock_sessions_and_rate_limit(tmp_path):
    from inventory import account

    path = tmp_path / "account.json"
    account.write_account("alex", "correct horse", path)
    lock = account.Lock(tmp_path, path)

    assert lock.ready and lock.user == "ALEX"
    assert lock.unlock("correct horse")
    cookie = lock.new_session()
    assert lock.read_session(cookie) == "ALEX"
    assert lock.read_session(None) is None
    assert lock.read_session(cookie.rpartition(".")[0] + ".deadbeef") is None
    assert lock.read_session(lock.new_session(hours=-1)) is None
    assert account.Lock(tmp_path / "elsewhere", path).read_session(cookie) is None

    for _ in range(account.MAX_ATTEMPTS):
        assert not lock.unlock("wrong")
    with pytest.raises(account.TooManyAttempts):
        lock.unlock("correct horse")


def test_server_locks_everything_until_the_password(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("STOCK_USER", raising=False)

    import importlib

    from inventory import config as config_module
    from inventory import server as server_module

    importlib.reload(config_module)
    server = importlib.reload(server_module)

    mine = {"origin": "http://testserver"}
    with TestClient(server.app) as client:
        assert client.get("/", follow_redirects=False).status_code == 303
        assert client.get("/api/data").status_code == 401
        assert ">create</button>" in client.get("/unlock").text

        assert client.post("/unlock", data={"user": "alex", "password": "x"},
                           headers={"origin": "http://evil.example"}).status_code == 403
        assert client.post("/unlock", data={"user": "alex", "password": "x"}).status_code == 403

        answer = client.post("/unlock", data={"user": "alex", "password": "correct horse",
                                              "again": "nope"}, headers=mine,
                             follow_redirects=False)
        assert answer.status_code == 200
        assert client.get("/api/data").status_code == 401
        answer = client.post("/unlock", data={"user": "alex", "password": "correct horse",
                                              "again": "correct horse"}, headers=mine,
                             follow_redirects=False)
        assert answer.status_code == 303
        assert client.get("/api/data").json()["user"] == "ALEX"

        assert client.post("/lock", headers=mine, follow_redirects=False).status_code == 303
        assert client.get("/api/data").status_code == 401
        page = client.get("/unlock").text
        assert ">ENTER</button>" in page
        assert "readonly" not in page

        answer = client.post("/unlock", data={"user": "someone else",
                                              "password": "correct horse"}, headers=mine,
                             follow_redirects=False)
        assert answer.status_code == 200
        assert client.get("/api/data").status_code == 401

        answer = client.post("/unlock", data={"user": "alex", "password": "correct horse"},
                             headers=mine, follow_redirects=False)
        assert answer.status_code == 303
        assert client.get("/api/data").status_code == 200
        assert client.post("/lock", headers=mine, follow_redirects=False).status_code == 303

        answer = client.post("/unlock", data={"password": "guess"}, headers=mine,
                             follow_redirects=False)
        assert answer.status_code == 200
        assert client.get("/api/data").status_code == 401
        answer = client.post("/unlock", data={"password": "correct horse"}, headers=mine,
                             follow_redirects=False)
        assert answer.status_code == 303
        assert client.get("/api/data").status_code == 200
        assert client.get("/").status_code == 200


def test_the_bridge_is_the_only_unchecked_certificate(tmp_path):
    import ssl

    from inventory import mailbox

    def settings(host):
        return MailConfig(host=host, port=1143, user="me", password="x", folder="INBOX",
                          security="starttls", insecure=True, mark_read=True, every_minutes=15)

    here = mailbox._context(settings("127.0.0.1"))
    assert here.verify_mode == ssl.CERT_NONE

    away = mailbox._context(settings("imap.example.com"))
    assert away.verify_mode == ssl.CERT_REQUIRED and away.check_hostname is True


def test_the_logo_is_served_before_the_password(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("STOCK_USER", raising=False)

    import importlib

    from inventory import config as config_module
    from inventory import server as server_module

    importlib.reload(config_module)
    server = importlib.reload(server_module)

    with TestClient(server.app) as client:
        for path in ("/logo.png", "/icon.png"):
            answer = client.get(path, follow_redirects=False)
            assert answer.status_code == 200
            assert answer.headers["content-type"] == "image/png"
            assert answer.content[:8] == b"\x89PNG\r\n\x1a\n"
        page = client.get("/unlock").text
        assert 'href="/icon.png"' in page
        assert 'url("/logo.png")' in page

        # The landing picture belongs to the app page, so it waits for the password.
        assert client.get("/landing.png", follow_redirects=False).status_code == 303
        client.post("/unlock", data={"user": "alex", "password": "correct horse",
                                     "again": "correct horse"},
                    headers={"origin": "http://testserver"})
        answer = client.get("/landing.png")
        assert answer.status_code == 200 and answer.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_a_secret_is_written_encrypted_and_read_back_in_the_clear(vault_home, monkeypatch):
    from inventory import config as cfg
    from inventory import vault

    monkeypatch.delenv("IMAP_PASSWORD", raising=False)
    cfg.set_setting("IMAP_USER", "buyer@example.com")
    cfg.set_setting("IMAP_PASSWORD", "hunter2-plain")
    text = cfg.settings_file().read_text()
    assert "IMAP_USER=buyer@example.com" in text                     # not a secret: as typed
    assert "hunter2-plain" not in text and "IMAP_PASSWORD=enc:v1:" in text
    assert oct(cfg.settings_file().stat().st_mode & 0o777) == "0o600"
    assert oct(vault.key_file().stat().st_mode & 0o777) == "0o600"

    monkeypatch.delenv("IMAP_PASSWORD")                              # a new run reads the file again
    cfg.load_config(vault_home.parent / "data")
    assert __import__("os").environ["IMAP_PASSWORD"] == "hunter2-plain"

    # An older file with the secret in the clear, and its backup, are encrypted when the program starts.
    cfg.settings_file().write_text("SLACK_BOT_TOKEN=xoxb-oldplain\nMAIL_PROVIDER=gmail\n")
    cfg.settings_file().with_name("settings.env.bak").write_text("SLACK_BOT_TOKEN=xoxb-oldplain\n")
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    cfg.load_config(vault_home.parent / "data")
    assert "xoxb-oldplain" not in cfg.settings_file().read_text()
    assert "xoxb-oldplain" not in cfg.settings_file().with_name("settings.env.bak").read_text()
    assert "MAIL_PROVIDER=gmail" in cfg.settings_file().read_text()
    assert __import__("os").environ["SLACK_BOT_TOKEN"] == "xoxb-oldplain"


def test_a_lost_key_reads_as_not_set_and_a_plain_value_is_still_read(vault_home):
    from inventory import vault

    token = vault.seal("s3cret")
    assert vault.unseal(token) == "s3cret" and vault.unseal("never-sealed") == "never-sealed"
    assert vault.seal(token) == token and vault.seal("") == ""            # sealed twice is sealed once
    vault.key_file().unlink()
    vault.forget_cached_key()
    assert vault.unseal(token) == ""                                      # a new key cannot open the old token


def test_supplier_tokens_are_encrypted_in_the_database_and_old_ones_are_sealed(vault_home, db_path):
    from inventory import channels, vault

    channels.save(db_path, "ACME", "https://acme.example/o", "tok-abc")
    stored = db.read(db_path, "SELECT token FROM supplier_channels")[0][0]
    assert stored.startswith("enc:v1:") and "tok-abc" not in stored
    assert channels.get(db_path, "ACME")["token"] == "tok-abc"
    channels.save(db_path, "ACME", "https://acme.example/o2", "")        # token left empty keeps the old one
    assert channels.get(db_path, "ACME")["token"] == "tok-abc"

    db.write(db_path, "UPDATE supplier_channels SET token = 'plain-old' RETURNING supplier")
    assert vault.seal_database(db_path) == 1 and vault.seal_database(db_path) == 0
    assert channels.get(db_path, "ACME")["token"] == "plain-old"


def test_loose_permissions_are_tightened(tmp_path):
    from inventory import vault

    folder = tmp_path / "cfg"
    folder.mkdir()
    for name in ("settings.env", "account.json"):
        (folder / name).write_text("x")
        (folder / name).chmod(0o644)
    folder.chmod(0o755)
    fixed = vault.harden(folder)
    assert len(fixed) == 3 and oct(folder.stat().st_mode & 0o777) == "0o700"
    assert oct((folder / "settings.env").stat().st_mode & 0o777) == "0o600"
    assert vault.harden(folder) == []


def test_accounts_have_roles_and_the_first_one_stays_administrator(tmp_path):
    from inventory import account

    path = tmp_path / "account.json"
    account.write_account("alex", "correct horse", path)
    assert account.users(path) == [{"user": "ALEX", "role": "admin", "first": True,
                                    "created_at": account.read_account(path)["created_at"]}]
    account.add_user("marie", "buyer", "marie-password", path)
    account.add_user("paul", "viewer", "paul-password", path)
    with pytest.raises(ValueError):
        account.add_user("Marie", "buyer", "another-pass", path)             # same name
    with pytest.raises(ValueError):
        account.add_user("zed", "boss", "zed-password", path)                # no such role
    with pytest.raises(ValueError):
        account.add_user("zed", "buyer", "short", path)
    assert [(u["user"], u["role"]) for u in account.users(path)] == [("ALEX", "admin"), ("MARIE", "buyer"), ("PAUL", "viewer")]
    assert "hash" not in str(account.users(path))

    assert account.set_role("marie", "approver", path) == "MARIE"
    with pytest.raises(ValueError):
        account.set_role("alex", "viewer", path)                             # the first stays administrator
    with pytest.raises(ValueError):
        account.remove_user("alex", path)
    lock = account.Lock(tmp_path, path)
    assert lock.authenticate("marie", "marie-password") == "MARIE" and lock.role("MARIE") == "approver"
    assert lock.authenticate("marie", "wrong") is None
    assert lock.authenticate("", "correct horse") is None                     # several accounts: a name is needed
    cookie = lock.new_session(user="MARIE")
    assert lock.read_session(cookie) == "MARIE"
    account.remove_user("marie", path)
    assert lock.read_session(cookie) is None and lock.role("MARIE") is None  # a removed account loses its session

    account.reset_password("paul", "a-new-password", path)
    assert lock.authenticate("paul", "a-new-password") == "PAUL"
    with pytest.raises(ValueError):
        account.change_own_password("paul", "not it", "another-pass", path)
    account.change_own_password("paul", "a-new-password", "third-password", path)
    assert lock.authenticate("paul", "third-password") == "PAUL"


def test_the_permission_table_covers_every_write_and_keeps_viewers_out_of_decisions():
    from inventory import access, server

    assert access.required_role("GET", "/api/data") == "viewer"
    assert access.required_role("GET", "/api/users") == "admin"
    assert access.required_role("POST", "/api/notes") == "buyer"                # the default for a write
    assert access.required_role("DELETE", "/api/notes/3") == "approver"         # a delete is a decision
    assert access.required_role("POST", "/api/gate/4/approve") == "approver"
    assert access.required_role("POST", "/api/stockctl/adjust") == "approver"
    assert access.required_role("POST", "/api/wallet/deposit") == "admin"
    assert access.required_role("PUT", "/api/channels") == "admin"
    assert access.required_role("PUT", "/api/charters/plutus/send_orders") == "admin"
    assert access.required_role("POST", "/api/plutus/mode") == "admin"
    assert access.required_role("POST", "/api/plutus/run") == "approver"
    assert access.required_role("POST", "/api/payments/batch/2/executed") == "approver"
    assert access.required_role("POST", "/api/account/password") == "viewer"
    # Anything that moves money or sets the program up needs an approver at least; no write is left to a viewer.
    for route in server.app.routes:
        for method in sorted((getattr(route, "methods", None) or set()) - {"GET", "HEAD", "OPTIONS"}):
            if route.path in ("/unlock", "/lock", "/exit", "/api/account/password"):
                continue
            assert access.required_role(method, route.path) != "viewer", (method, route.path)
            if any(word in route.path for word in ("/wallet", "/bank", "/payer", "/charters", "/channels", "/users")):
                assert access.required_role(method, route.path) == "admin", (method, route.path)


def test_a_role_limits_what_the_browser_may_do_and_the_pages_carry_security_headers(tmp_path, monkeypatch):
    import importlib

    from fastapi.testclient import TestClient

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("STOCK_VAULT_KEYRING", "0")
    monkeypatch.delenv("STOCK_USER", raising=False)
    from inventory import config as config_module
    from inventory import server as server_module

    importlib.reload(config_module)
    server = importlib.reload(server_module)
    mine = {"origin": "http://testserver"}

    def sign_in(client, user, password, again=None):
        data = {"user": user, "password": password}
        if again:
            data["again"] = again
        return client.post("/unlock", data=data, headers=mine, follow_redirects=False)

    with TestClient(server.app) as boss, TestClient(server.app) as buyer, TestClient(server.app) as viewer:
        assert sign_in(boss, "alex", "correct horse", "correct horse").status_code == 303
        home = boss.get("/api/data")
        for header, value in (("x-content-type-options", "nosniff"), ("x-frame-options", "DENY"),
                              ("referrer-policy", "no-referrer"), ("cache-control", "no-store")):
            assert home.headers[header] == value
        assert "frame-ancestors 'none'" in home.headers["content-security-policy"]
        assert "default-src 'self'" in boss.get("/unlock").headers["content-security-policy"]

        made = boss.post("/api/users", json={"user": "marie", "role": "buyer", "password": "marie-password"}, headers=mine)
        assert made.status_code == 200 and [u["user"] for u in made.json()["users"]] == ["ALEX", "MARIE"]
        boss.post("/api/users", json={"user": "paul", "role": "viewer", "password": "paul-password"}, headers=mine)

        assert sign_in(buyer, "marie", "marie-password").status_code == 303
        assert sign_in(viewer, "paul", "paul-password").status_code == 303
        assert sign_in(viewer, "paul", "wrong-password").status_code == 200

        assert viewer.get("/api/data").status_code == 200
        assert viewer.get("/api/users").status_code == 403
        denied = viewer.post("/api/notes", json={}, headers=mine)
        assert denied.status_code == 403 and "viewer may not do this" in denied.json()["detail"]
        assert buyer.post("/api/notes", json={}, headers=mine).status_code != 403          # allowed to try
        assert buyer.delete("/api/notes/1", headers=mine).status_code == 403              # a delete is a decision
        assert buyer.post("/api/gate/1/approve", headers=mine).status_code == 403
        assert buyer.put("/api/channels", json={"supplier": "A", "url": "https://a.example", "token": "x"},
                         headers=mine).status_code == 403
        assert buyer.get("/api/users").status_code == 403

        assert boss.put("/api/users/MARIE/role", json={"role": "approver"}, headers=mine).status_code == 200
        assert buyer.post("/api/gate/1/approve", headers=mine).status_code != 403          # the role is read on each request
        assert boss.delete("/api/users/ALEX", headers=mine).status_code == 422              # not yourself
        assert boss.delete("/api/users/PAUL", headers=mine).status_code == 200
        assert viewer.get("/api/data").status_code == 401                                   # its account is gone

        assert buyer.post("/api/account/password", json={"old": "marie-password", "new": "marie-new-pass"},
                          headers=mine).status_code == 200
        assert buyer.post("/api/account/password", json={"old": "marie-password", "new": "marie-again-1"},
                          headers=mine).status_code == 422


def test_the_audit_log_is_chained_and_an_edit_or_a_removal_shows(vault_home, db_path):
    from inventory import audit, watch

    assert audit.verify(db_path) == {"ok": True, "checked": 0, "broken_at": None, "why": None}
    for number in range(5):
        audit.record(db_path, "alex", "note.save", f"note #{number}", "x")
    assert audit.verify(db_path)["ok"] is True and audit.verify(db_path)["checked"] == 5
    assert audit.verify(db_path, limit=2)["checked"] == 2

    rows = db.read(db_path, "SELECT event_id FROM audit WHERE row_hash IS NOT NULL ORDER BY event_id")
    middle = rows[2][0]
    db.write(db_path, "UPDATE audit SET detail = 'changed after the fact' WHERE event_id = ? RETURNING event_id", [middle])
    broken = audit.verify(db_path)
    assert broken["ok"] is False and broken["broken_at"] == middle and broken["why"] == "this event was edited"
    watch.scan(db_path)
    assert any(n["kind"] == "security" and "tampered" in n["title"] for n in watch.open_notices(db_path))

    db.write(db_path, "UPDATE audit SET detail = 'x' WHERE event_id = ? RETURNING event_id", [middle])      # put it back
    assert audit.verify(db_path)["ok"] is True
    db.write(db_path, "DELETE FROM audit WHERE event_id = ? RETURNING event_id", [middle])                 # take one out
    gone = audit.verify(db_path)
    assert gone["ok"] is False and gone["broken_at"] == rows[3][0] and "changed or removed" in gone["why"]

    # Without the key an edit cannot be re-signed, even by rewriting every later hash.
    from inventory import vault
    vault.key_file().unlink()
    vault.forget_cached_key()
    assert audit.verify(db_path)["ok"] is False


def test_a_licence_is_signed_by_the_vendor_and_judged_on_who_when_and_where(tmp_path, monkeypatch):
    from inventory import licensing

    private, public = _vendor()
    _other_private, other_public = _vendor()
    today = date(2026, 11, 7)
    good = licensing.issue(private, "ACME BUILD", 100, "any", today)
    assert licensing.check(good, today, public)["valid"] is True
    found = licensing.check(good, today, public)
    assert found["licensee"] == "ACME BUILD" and found["days_left"] == 100 and found["warn"] is False
    assert licensing.check(good, date(2027, 2, 1), public)["warn"] is True                  # within 30 days of the end
    assert "ended on 2027-02-15" in licensing.check(good, date(2027, 3, 1), public)["reason"]
    assert "not signed by the vendor" in licensing.check(good, today, other_public)["reason"]
    forged = {**good, "payload": {**good["payload"], "expires": "2099-01-01"}}              # a longer date, same signature
    assert "not signed by the vendor" in licensing.check(forged, today, public)["reason"]
    assert "not a licence" in licensing.check({"hello": 1}, today, public)["reason"]
    mine = licensing.issue(private, "ACME BUILD", 100, licensing.machine_id(), today)
    assert licensing.check(mine, today, public)["valid"] is True
    elsewhere = licensing.issue(private, "ACME BUILD", 100, "0123456789abcdef", today)
    assert "another machine" in licensing.check(elsewhere, today, public)["reason"]
    with monkeypatch.context() as m:
        m.setattr(licensing, "PRODUCT", "something-else")                                    # signed for another product
        other_product = licensing.issue(private, "ACME BUILD", 100, "any", today)
    assert "another product" in licensing.check(other_product, today, public)["reason"]

    monkeypatch.setattr(licensing, "PUBLIC_KEY", public)
    monkeypatch.setenv("STOCK_LICENSE", "on")
    monkeypatch.setenv("STOCK_LICENSE_FILE", str(tmp_path / "license.json"))
    assert licensing.status()["reason"] == "there is no licence file yet"
    with pytest.raises(ValueError):
        licensing.install("not json")
    with pytest.raises(ValueError):
        licensing.install(json.dumps(forged))
    assert licensing.install(json.dumps(good))["valid"] and licensing.status()["licensee"] == "ACME BUILD"
    assert oct((tmp_path / "license.json").stat().st_mode & 0o777) == "0o600"
    monkeypatch.setenv("STOCK_LICENSE", "off")
    assert licensing.status()["off"] is True


def test_changed_added_or_missing_program_files_fail_the_signature(tmp_path):
    from inventory import integrity

    private, public = _vendor()
    root = tmp_path / "pkg"
    (root / "web_ui").mkdir(parents=True)
    (root / "app.py").write_text("print('a')\n")
    (root / "web_ui" / "app.html").write_text("<p>hi</p>")
    (root / "__pycache__").mkdir()
    (root / "__pycache__" / "x.pyc").write_bytes(b"\x00")                                    # never part of the check
    (root / "notes.xyz").write_text("not a program file")
    manifest = tmp_path / "MANIFEST.json"
    manifest.write_text(json.dumps(integrity.build(private, root)))
    assert sorted(json.loads(manifest.read_text())["files"]) == ["app.py", "web_ui/app.html"]

    assert integrity.verify(root, manifest, public)["ok"] is True
    (root / "app.py").write_text("print('b')\n")
    done = integrity.verify(root, manifest, public)
    assert done["ok"] is False and done["changed"] == ["app.py"] and "changed" in done["reason"]
    (root / "app.py").write_text("print('a')\n")
    (root / "extra.py").write_text("import os\n")
    assert integrity.verify(root, manifest, public)["added"] == ["extra.py"]
    (root / "extra.py").unlink()
    (root / "web_ui" / "app.html").unlink()
    assert integrity.verify(root, manifest, public)["missing"] == ["web_ui/app.html"]
    (root / "web_ui" / "app.html").write_text("<p>hi</p>")
    assert integrity.verify(root, manifest, public)["ok"] is True

    document = json.loads(manifest.read_text())
    document["files"]["app.py"] = "0" * 64                                                   # a manifest edited to match
    manifest.write_text(json.dumps(document))
    assert "not signed by the vendor" in integrity.verify(root, manifest, public)["reason"]
    assert "unsigned build" in integrity.verify(root, tmp_path / "none.json", public)["reason"]
    assert integrity.verify(root, manifest, _vendor()[1])["ok"] is False


def test_the_program_refuses_to_serve_without_a_licence_and_takes_one_on_its_page(tmp_path, monkeypatch):
    import importlib

    from fastapi.testclient import TestClient

    private, public = _vendor()
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("STOCK_LICENSE", "on")
    monkeypatch.setenv("STOCK_LICENSE_FILE", str(tmp_path / "license.json"))
    monkeypatch.delenv("STOCK_USER", raising=False)
    from inventory import config as config_module
    from inventory import licensing
    from inventory import server as server_module

    importlib.reload(config_module)
    server = importlib.reload(server_module)
    monkeypatch.setattr(licensing, "PUBLIC_KEY", public)
    mine = {"origin": "http://testserver"}
    with TestClient(server.app) as client:
        assert client.get("/", follow_redirects=False).headers["location"] == "/license-required"
        assert client.get("/unlock", follow_redirects=False).headers["location"] == "/license-required"
        held = client.get("/api/data")
        assert held.status_code == 423 and "no licence file" in held.json()["detail"]
        page = client.get("/license-required").text
        assert licensing.machine_id() in page and "install the licence" in page
        assert client.get("/sap.css").status_code == 200

        bad = client.post("/license/install", data={"text": "{}"}, headers=mine)
        assert "cannot be used" in bad.text or "not a licence" in bad.text
        good = json.dumps(licensing.issue(private, "ACME BUILD", 30))
        done = client.post("/license/install", data={"text": good}, headers=mine, follow_redirects=False)
        assert done.status_code == 303 and done.headers["location"] == "/"
        assert client.get("/unlock", follow_redirects=False).status_code == 200            # now the sign-in page
        assert client.get("/api/license").status_code == 401                                # and the rest wants a sign-in

        monkeypatch.setenv("STOCK_INTEGRITY", "on")
        from inventory import integrity
        integrity._last.update(at=0.0, result=None)
        sealed = client.get("/api/data")
        assert sealed.status_code == 423 and "unsigned build" in sealed.json()["detail"]
        assert "do not match the vendor" in client.get("/license-required").text


def test_the_doctor_reports_each_check_and_fails_on_a_loose_permission(vault_home, db_path, tmp_path):
    from inventory import audit, health

    config = make_config(tmp_path)
    audit.record(db_path, "alex", "note.save")
    config = SimpleNamespace(db_path=db_path, data_dir=tmp_path)
    done = health.report(config)
    names = [c["name"] for c in done["checks"]]
    assert names == ["version", "licence", "program files", "database", "audit log", "permissions", "secrets", "backup"]
    by = {c["name"]: c for c in done["checks"]}
    assert by["database"]["ok"] and by["audit log"]["ok"] and "1 events chained" in by["audit log"]["detail"]
    assert by["backup"]["ok"] is False and by["backup"]["detail"] == "none yet"
    assert done["ok"] is False

    vault_home.mkdir(parents=True, exist_ok=True)
    (vault_home / "settings.env").write_text("IMAP_PASSWORD=plain-secret\n")
    (vault_home / "settings.env").chmod(0o644)
    again = {c["name"]: c for c in health.report(config)["checks"]}
    assert again["permissions"]["ok"] is False and "settings.env" in again["permissions"]["detail"]
    assert again["secrets"]["ok"] is False and "IMAP_PASSWORD" in again["secrets"]["detail"]
