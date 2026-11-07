"""Shared builders for the test modules: notes, configs, databases, fake links."""

import json
import pathlib
from datetime import date, datetime, timedelta
from types import SimpleNamespace


from inventory import db, llm
from inventory.config import Config, MailConfig, SmsConfig
from inventory.models import Line, Note


def make_note(**kw):
    base = dict(
        supplier="Acme Supplies",
        delivery_date="2026-09-21",
        order_date="2026-09-15",
        site="Villa Project",
        work_item="Ground slab",
        lines=[Line(article="Cement 35kg", quantity=40, designation="CIM35 - Cement CEM II 35kg"),
               Line(article="Mesh ST25", backorder=True)],
    )
    base.update(kw)
    return Note(**base)


def make_config(tmp_path, **kw):
    sms = kw.pop("sms", None) or SmsConfig(provider="none", to="", sender="", account_sid="",
                                           auth_token="", webhook_url="", webhook_token="",
                                           max_per_hour=6)
    mail = kw.pop("mail", None) or MailConfig(host="", port=993, user="", password="", folder="INBOX",
                      security="ssl", insecure=False, mark_read=True, every_minutes=15)
    base = dict(backend="claude", gemini_api_key="", openai_api_key="", photo_retention_days=42,
                data_dir=tmp_path, model="", user="ALEX", sms=sms, mail=mail)
    base.update(kw)
    return Config(**base)


class FakeGenai:
    """Stands in for genai.Client, returning canned structured-output text."""

    def __init__(self, *payloads, status="completed", error=None):
        self.payloads, self.calls = list(payloads), []
        self.status, self.error = status, error
        self.aio = SimpleNamespace(interactions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return SimpleNamespace(status=self.status, output_text=json.dumps(self.payloads.pop(0)))


def gemini(*payloads, **kw):
    backend = llm.GeminiBackend.__new__(llm.GeminiBackend)
    backend.model = "gemini-3.8-flash"
    backend.client = FakeGenai(*payloads, **kw)
    return backend


EMPTY_NOTE = {"supplier": None, "delivery_date": None, "order_date": None,
              "site": None, "work_item": None, "lines": []}


FAKE_CLAUDE = """#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
prompt = sys.stdin.read()
log = {"args": args, "prompt": prompt, "cwd": os.getcwd(), "files": sorted(os.listdir("."))}
open(os.environ["FAKE_CLAUDE_LOG"], "w").write(json.dumps(log))
print(os.environ["FAKE_CLAUDE_OUT"])
"""


def ok_result(output):
    return {"type": "result", "subtype": "success", "is_error": False, "structured_output": output}


def webhook_config(tmp_path, **kw):
    sms = SmsConfig(provider="webhook", to="+33600000000", sender="", account_sid="",
                    auth_token="", webhook_url="https://sms.example/send", webhook_token="t",
                    max_per_hour=kw.pop("max_per_hour", 6))
    return make_config(tmp_path, sms=sms)


def _ok():
    class R:
        def read(self):
            return b""

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    return R()


def temp_config(tmp_path):
    from inventory.config import Config, MailConfig, SmsConfig

    sms = SmsConfig(provider="none", to="", sender="", account_sid="", auth_token="",
                    webhook_url="", webhook_token="", max_per_hour=6)
    return Config(backend="claude", gemini_api_key="", openai_api_key="",
                  photo_retention_days=42, data_dir=tmp_path, model="", user="ALEX",
                  sms=sms, mail=MailConfig(host="", port=993, user="", password="", folder="INBOX",
                      security="ssl", insecure=False, mark_read=True, every_minutes=15))


def filled(tmp_path):
    config = temp_config(tmp_path)
    db.init(config.db_path)
    db.save_note(config.db_path, make_note(), "sha-tui", "", "ALEX")
    db.save_purchase_order(config.db_path, {"site": "HARBOUR POINT", "lines": [
        {"article": "HEX BOLT", "quantity": 10}, {"article": "WASHER M6", "quantity": 20}]},
        "sha-list", "", "ALEX")
    return config


def legacy_database(path):
    import duckdb as ddb

    with ddb.connect(str(path)) as con:
        con.execute("CREATE TABLE bons (bon_id INTEGER, image_sha256 VARCHAR, image_path VARCHAR, "
                    "confirmed_at TIMESTAMP)")
        con.execute("CREATE TABLE livraisons (article VARCHAR, quantite DOUBLE, "
                    "fournisseur VARCHAR, date_livraison DATE, date_commande DATE, "
                    "chantier VARCHAR, ouvrage VARCHAR, reliquat BOOLEAN, designation VARCHAR, "
                    "bon_id INTEGER, ligne_no INTEGER)")
        con.execute("CREATE TABLE listes (liste_id INTEGER, date_liste DATE, chantier VARCHAR, "
                    "ouvrage VARCHAR, dessinateur VARCHAR, image_sha256 VARCHAR, "
                    "image_path VARCHAR, cree_le TIMESTAMP)")
        con.execute("CREATE TABLE liste_lignes (liste_id INTEGER, ligne_no INTEGER, "
                    "article VARCHAR, designation VARCHAR, reference VARCHAR, quantite DOUBLE, "
                    "stock DOUBLE, en_stock BOOLEAN)")
        con.execute("CREATE TABLE catalogue (fournisseur VARCHAR, designation VARCHAR, "
                    "reference VARCHAR, article VARCHAR, vu INTEGER, maj_le TIMESTAMP)")
        con.execute("CREATE TABLE lectures (mesure_id INTEGER, mesure_le TIMESTAMP, "
                    "document VARCHAR, champs INTEGER, corriges INTEGER, lignes INTEGER)")

        con.execute("INSERT INTO bons VALUES (7, 'sha-old', '/server/images/sha-old.jpg', now())")
        con.execute("INSERT INTO livraisons VALUES "
                    "('BOLT M6', 1200, 'NORTHGATE', DATE '2026-09-21', DATE '2026-09-14', "
                    "'HARBOUR POINT', 'PARAPET', false, '550110 - BOLT M6X25', 7, 1), "
                    "('WASHER M6', 400, 'NORTHGATE', DATE '2026-09-21', NULL, 'HARBOUR POINT', NULL, "
                    "true, '550220 - WASHER SERIES M 6', 7, 2)")
        con.execute("INSERT INTO listes VALUES (3, DATE '2026-09-17', 'HARBOUR POINT', 'RAILING', "
                    "'N.T', 'sha-list-old', '/server/images/sha-list-old.jpg', now())")
        con.execute("INSERT INTO liste_lignes VALUES (3, 1, 'BOLT M6X30', 'Bolt M6x30', NULL, 110, "
                    "110, true)")
        con.execute("INSERT INTO catalogue VALUES ('NORTHGATE', '550110 - BOLT M6X25', '550110', "
                    "'BOLT M6', 4, now())")
        con.execute("INSERT INTO lectures VALUES (1, now(), 'bon', 20, 2, 5)")
    return path


def planning_database(path):
    from inventory.models import Line, Note

    db.init(path)
    for week, (bolts, sheets) in enumerate([(700, 2), (500, 3), (600, 1)]):
        day = date(2026, 9, 7) + timedelta(days=7 * week)
        note = Note(supplier="NORTHGATE", delivery_date=day.isoformat(), site="PARAPET",
                    lines=[Line(article="BOLT M6", quantity=bolts, designation="BOLT M6 ZINC"),
                           Line(article="SHEET ALU", quantity=sheets)])
        db.save_note(path, note, f"sha-{week}", "", "ALEX")
    return path


def buffer_database(path):
    from inventory.models import Line, Note

    db.init(path)
    weeks = [(300, 2), (0, 0), (900, 3), (300, 1)]
    for index, (bolts, sheets) in enumerate(weeks):
        day = date(2026, 9, 7) + timedelta(days=7 * index)
        lines = [Line(article="BOLT M6", quantity=bolts)] if bolts else []
        if sheets:
            lines.append(Line(article="SHEET ALU", quantity=sheets))
        if not lines:
            continue
        note = Note(supplier="NORTHGATE", delivery_date=day.isoformat(),
                    order_date=(day - timedelta(days=10)).isoformat(),
                    site="PARAPET", lines=lines)
        db.save_note(path, note, f"buf-{index}", "", "ALEX")
    return path


def a_basket(**kw):
    base = dict(article="CHEVILLE SPIT FIX", quantity=100, unit_price=0.62,
                supplier="FOUSSIER")
    base.update(kw)
    return base


def _ticked_list(db_path, states):
    lines = [{"article": f"ART{n}", "designation": "d", "quantity": 2.0 + n, "in_stock": state}
             for n, state in enumerate(states, 1)]
    return db.save_purchase_order(db_path, {"list_date": "2026-11-01", "site": "NORD",
                                            "work_item": "W1", "drafter": "X",
                                            "lines": lines}, None, None, "T")


def _outbox_world(db_path):
    from inventory import contacts, prices

    prices.remember(db_path, "BOLT", 2.0, supplier="ACME", seen_on=date(2026, 9, 1))
    prices.remember(db_path, "NUT", 1.0, supplier="ACME", seen_on=date(2026, 9, 1))
    prices.remember(db_path, "PIPE", 9.0, supplier="TUBES SA", seen_on=date(2026, 9, 1))
    contacts.save(db_path, {"company": "ACME", "email": "sales@acme.example"}, "T")
    return db.save_purchase_order(db_path, {
        "list_date": date(2026, 10, 30), "site": "NORD", "work_item": "W1", "drafter": "X",
        "lines": [{"article": "BOLT", "quantity": 10.0, "in_stock": False},
                  {"article": "NUT", "quantity": 4.0, "in_stock": False},
                  {"article": "PIPE", "quantity": 2.0, "in_stock": False},
                  {"article": "ODDITY", "quantity": 1.0, "in_stock": False},
                  {"article": "SHELF", "quantity": 7.0, "in_stock": True}]}, None, None, "T")


def tmp_path_for(db_path):
    return db_path.parent


def _ready_mail():
    return MailConfig(host="imap.gmail.com", port=993, user="buyer@example.com",
                      password="app-password", folder="INBOX", security="ssl", insecure=False,
                      mark_read=True, every_minutes=15, provider="gmail")


def _policy_world(db_path):
    """ACME has delivered four times, has prices and an address; an order for it is drafted."""
    from inventory import outbox

    list_id = _outbox_world(db_path)
    for number in range(4):
        day = date(2026, 9, 5) + timedelta(days=7 * number)
        db.save_note(db_path, make_note(supplier="ACME", delivery_date=day.isoformat(),
                                        order_date=(day - timedelta(days=5)).isoformat(),
                                        lines=[Line(article="BOLT", quantity=10)]), f"pw{number}", "", "T")
    mail = next(r for r in outbox.draft_order(db_path, list_id)["drafts"] if r["supplier"] == "ACME")
    return list_id, mail


class _Link:
    sent = []

    def send_message(self, message):
        _Link.sent.append(message)

    def quit(self):
        pass


def _ddmrp_shelf(db_path):
    """Four weeks of use for three articles. BOLT is counted at almost nothing (red), NUT is counted
    comfortably (green), WASHER sits between (yellow)."""
    for number in range(1, 5):
        day = date(2026, 10, 1) + timedelta(days=number * 5)
        for article, quantity in (("BOLT", 100), ("NUT", 100), ("WASHER", 100)):
            db.save_note(db_path, make_note(supplier="ACME", delivery_date=day.isoformat(),
                                            order_date=(day - timedelta(days=5)).isoformat(),
                                            lines=[Line(article=article, quantity=quantity)]),
                         f"dd-{article}-{number}", "", "T")
    return {"BOLT": 5, "NUT": 5000}


def _purchasing_world(db_path, funded=2000.0):
    """ACME has delivered four times and has prices and an address; Plutus has written an order for it."""
    from inventory import wallet

    list_id, mail = _policy_world(db_path)
    db.write(db_path, "UPDATE purchase_orders SET drafter = 'PLUTUS', created_at = now() WHERE list_id = ? RETURNING 1",
             [list_id])
    db.write(db_path, "DELETE FROM purchase_order_lines WHERE list_id = ? AND article = 'ODDITY' RETURNING 1", [list_id])
    if funded:
        wallet.deposit(db_path, funded, "float", "ALEX")
    return list_id, mail


def policies_log(db_path):
    from inventory import policies

    return [r for r in policies.log_recent(db_path, 40) if r["policy"] == "PLUTUS"]


def _sent_acme_order(db_path):
    from inventory import outbox

    _Link.sent = []
    list_id, mail = _policy_world(db_path)
    outbox.send(db_path, mail["mail_id"], _ready_mail(), "T", connect=lambda m, f: _Link())
    db.write(db_path, "UPDATE supplier_mail SET sent_at = ? WHERE mail_id = ? RETURNING mail_id",
             [datetime(2026, 11, 2, 9, 0), mail["mail_id"]])
    return list_id, mail["mail_id"]


def _hook(status=200, answer=b'{"confirmed": true, "reference": "SO-77", "promised_date": "2026-11-18"}'):
    import http.server
    import threading

    seen = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            seen.append((self.path, dict(self.headers), body))
            self.send_response(status)
            self.end_headers()
            self.wfile.write(answer)

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, seen


def _fake_sftp(files=None, fail=False):
    """A stand-in for subprocess.run(['sftp', ...]) that records what was asked of it."""
    calls = []

    class Done:
        def __init__(self, out="", code=0, err=""):
            self.stdout, self.returncode, self.stderr = out, code, err

    def run(command, input="", **kwargs):
        calls.append((command, input))
        if fail:
            return Done(code=1, err="Host key verification failed.")
        if input.startswith("ls"):
            return Done("\n".join(files or []) + "\n")
        if input.startswith("get"):
            target = input.split('"')[3]
            pathlib.Path(target).write_text(files_data["xml"], encoding="utf-8")
        return Done()
    files_data = {"xml": ""}
    run.calls, run.data = calls, files_data
    return run


def _vendor():
    import base64

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.generate()
    private = base64.b64encode(key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                                 serialization.NoEncryption())).decode()
    public = base64.b64encode(key.public_key().public_bytes(serialization.Encoding.Raw,
                                                            serialization.PublicFormat.Raw)).decode()
    return private, public
