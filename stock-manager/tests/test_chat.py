"""Tests: chat."""


import pytest

from inventory import db
from helpers import make_config


def test_chat_basket_becomes_a_purchase_order(db_path):
    from inventory import baskets, discord_bot

    items = baskets.parse_items("12 vis th 6x25 inox\n3x rondelle m8; ecrou borgne d6 x 100")
    # A line carries a price now, empty when the shop page gave none.
    assert items == [{"article": "VIS TH 6X25 INOX", "quantity": 12.0, "unit_price": None},
                     {"article": "RONDELLE M8", "quantity": 3.0, "unit_price": None},
                     {"article": "ECROU BORGNE D6", "quantity": 100.0, "unit_price": None}]
    assert baskets.parse_items("   ") == []
    assert baskets.parse_items("silicone noir")[0]["quantity"] == 1.0

    baskets.add(db_path, 777, items, shop="foussier.fr", site="ariane",
                work_item="ecran acoustique", who="LOUIS")
    held = baskets.lines(db_path, 777)
    assert len(held) == 3 and held[0]["shop"] == "foussier.fr" and held[0]["site"] == "ARIANE"
    assert baskets.shops(db_path, 777) == ["foussier.fr"]

    open_now = baskets.open_baskets(db_path)
    assert open_now[0]["channel"] == "777" and open_now[0]["lines"] == 3

    assert baskets.drop_line(db_path, 777, held[0]["line_id"]) is True
    assert baskets.drop_line(db_path, 777, held[0]["line_id"]) is False

    answer = baskets.to_purchase_order(db_path, 777, "LOUIS")
    assert answer["list_id"] and answer["site"] == "ARIANE" and len(answer["lines"]) == 2
    assert baskets.lines(db_path, 777) == []
    assert baskets.to_purchase_order(db_path, 777, "LOUIS") is None

    stored = db.purchase_orders(db_path)[0]
    assert {line["article"] for line in stored["lines"]} == {"RONDELLE M8", "ECROU BORGNE D6"}
    assert stored["drafter"] == "LOUIS"

    assert "does not pay" in discord_bot.purchase_text(answer)
    assert "#" + str(answer["list_id"]) in discord_bot.purchase_text(answer)
    assert discord_bot.purchase_text(None) == "Nothing in the basket to order."
    assert "empty" in discord_bot.basket_text([])
    assert "foussier.fr" in discord_bot.basket_text(held)
    assert discord_bot.allowed(777, []) is True
    assert discord_bot.allowed(777, ["42"]) is False
    assert discord_bot.allowed(777, ["777"]) is True


def test_mail_and_chat_settings_routes(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    for key in ("IMAP_HOST", "IMAP_USER", "IMAP_PASSWORD", "MAIL_PROVIDER",
                "DISCORD_BOT_TOKEN", "DISCORD_CHANNELS"):
        monkeypatch.delenv(key, raising=False)

    import importlib

    from inventory import config as config_module
    from inventory import server as server_module

    importlib.reload(config_module)
    server = importlib.reload(server_module)

    mine = {"origin": "http://testserver"}
    with TestClient(server.app) as client:
        client.post("/unlock", data={"user": "alex", "password": "correct horse",
                                     "again": "correct horse"}, headers=mine)

        start = client.get("/api/mail/settings").json()
        assert start["current"]["configured"] is False
        assert start["current"]["password_set"] is False
        assert "gmail" in {row["id"] for row in start["providers"]}

        saved = client.post("/api/mail/settings", headers=mine, json={
            "provider": "gmail", "user": "stock@company.com", "password": "hunter2-xyzzy",
            "folder": "Suppliers", "every_minutes": 5,
        }).json()
        assert saved["current"]["host"] == "imap.gmail.com"
        assert saved["current"]["folder"] == "Suppliers"
        assert saved["current"]["configured"] is True
        assert saved["current"]["password_set"] is True
        assert "hunter2-xyzzy" not in client.get("/api/mail/settings").text

        kept = client.post("/api/mail/settings", headers=mine, json={
            "provider": "gmail", "user": "stock@company.com", "every_minutes": 5,
        }).json()
        assert kept["current"]["password_set"] is True

        assert client.post("/api/mail/settings", headers=mine,
                           json={"provider": "nope"}).status_code == 422

        # Two doors now, so the answer names them: Discord and Slack side by side.
        chat = client.post("/api/chat/settings", headers=mine,
                           json={"token": "a-bot-token", "channels": "777, 888"}).json()
        assert chat["discord"]["token_set"] is True
        assert chat["discord"]["channels"] == ["777", "888"]
        assert "a-bot-token" not in client.get("/api/chat/settings").text

        gone = client.post("/api/chat/settings", headers=mine,
                           json={"channels": "777", "forget": True}).json()
        assert gone["discord"]["token_set"] is False

        slack = client.post("/api/slack/settings", headers=mine,
                            json={"bot_token": "xoxb-secret", "app_token": "xapp-secret",
                                  "channels": "C01"}).json()
        assert slack["slack"]["bot_token_set"] is True
        assert slack["slack"]["app_token_set"] is True
        assert slack["slack"]["channels"] == ["C01"]
        assert "xoxb-secret" not in client.get("/api/chat/settings").text

        dropped = client.post("/api/slack/settings", headers=mine,
                              json={"channels": "C01", "forget": True}).json()
        assert dropped["slack"]["bot_token_set"] is False
        assert dropped["slack"]["app_token_set"] is False

        assert client.get("/api/baskets").json()["baskets"] == []
        assert client.post("/api/baskets/777/order", headers=mine).status_code == 404
        assert client.delete("/api/baskets/777", headers=mine).status_code == 404

        page = client.get("/").text
        assert 'id="mail-provider"' in page and 'id="chat-token"' in page
        assert 'id="slack-bot-token"' in page and 'id="slack-app-token"' in page


def test_the_bot_registers_its_commands(db_path, tmp_path):
    """Only runs where the extra is installed: pip install 'stock-manager[discord]'."""
    discord = pytest.importorskip("discord")
    from discord import app_commands

    from inventory import discord_bot

    held = []

    class Spy(app_commands.CommandTree):
        def __init__(self, *args, **kw):
            super().__init__(*args, **kw)
            held.append(self)

    config = make_config(tmp_path)
    original = app_commands.CommandTree
    app_commands.CommandTree = Spy
    try:
        client = discord_bot.build_client(config, ["777"])
    finally:
        app_commands.CommandTree = original

    assert isinstance(client, discord.Client)
    names = {command.name: [p.name for p in command.parameters]
             for command in held[0].get_commands()}
    assert set(names) == {"basket", "basket_show", "basket_clear", "purchase", "stock", "quote"}
    assert names["quote"] == ["supplier", "items", "site", "work_item"]
    assert names["basket"] == ["shop", "items", "site", "work_item", "supplier"]


def test_a_basket_line_can_carry_the_price_off_the_shop_page():
    from inventory import baskets

    read = baskets.parse_items("12 vis th 6x25 @ 0.11, 4 rondelle m8, 3 cheville @ 0,62 EUR")
    assert [(row["article"], row["quantity"], row["unit_price"]) for row in read] == [
        ("VIS TH 6X25", 12.0, 0.11),
        ("RONDELLE M8", 4.0, None),
        ("CHEVILLE", 3.0, 0.62),
    ]


def test_the_same_words_come_out_in_slack_markup():
    from inventory import chat

    assert chat.to_slack("**Basket** (2 line(s)) for **foussier.fr**") == \
        "*Basket* (2 line(s)) for *foussier.fr*"
    # Discord's italics are Slack's italics, and must not be eaten by the bold pass.
    assert chat.to_slack("· 12 × VIS  _ALEX_") == "· 12 × VIS  _ALEX_"
    assert chat.to_slack("nothing to do here") == "nothing to do here"


def test_both_doors_say_the_same_thing(db_path):
    from inventory import chat, discord_bot

    # The formatters live in one place now; the Discord module still answers for them.
    assert discord_bot.basket_text is chat.basket_text
    assert discord_bot.purchase_text is chat.purchase_text
    assert discord_bot.allowed is chat.allowed


def test_slack_needs_both_tokens_before_it_is_ready(monkeypatch):
    from inventory import slack_bot

    monkeypatch.setenv("SLACK_BOT_TOKEN", "")
    monkeypatch.setenv("SLACK_APP_TOKEN", "")
    assert slack_bot.status()["ready"] is False

    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    assert slack_bot.status()["ready"] is False
    assert slack_bot.status()["bot_token_set"] is True

    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
    monkeypatch.setenv("SLACK_CHANNELS", "C01, C02")
    now = slack_bot.status()
    assert now["channels"] == ["C01", "C02"]
    assert now["ready"] is now["installed"]


def test_the_slack_door_builds_its_commands(tmp_path, monkeypatch):
    pytest.importorskip("slack_bolt")
    from inventory import slack_bot

    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
    app = slack_bot.build_app(make_config(tmp_path), ["C01"])
    # Building must not reach out to Slack: no network, no token check.
    assert app is not None


def test_a_slack_dialog_carries_the_fields_discord_had(monkeypatch):
    pytest.importorskip("slack_bolt")
    from inventory import slack_bot

    view = slack_bot.basket_dialog("C01")
    assert view["callback_id"] == slack_bot.BASKET_VIEW
    assert view["private_metadata"] == "C01"
    assert [block["block_id"] for block in view["blocks"]] == \
        ["shop", "items", "site", "work_item", "supplier"]
    # Only the shop and the items are asked for; the rest may be left alone.
    assert [block["optional"] for block in view["blocks"]] == [False, False, True, True, True]


def test_the_slack_test_says_which_token_is_wrong(monkeypatch):
    from inventory import slack_bot

    monkeypatch.setenv("SLACK_BOT_TOKEN", "")
    monkeypatch.setenv("SLACK_APP_TOKEN", "")
    assert slack_bot.check() == (False, "No token stored yet.")

    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
    ok, said = slack_bot.check()
    assert ok is False and "bot token" in said

    # The two fail in different ways, and each is answered for on its own.
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_APP_TOKEN", "")
    monkeypatch.setattr(slack_bot, "available", lambda: True)

    import slack_sdk

    monkeypatch.setattr(slack_sdk.WebClient, "auth_test",
                        lambda self: {"user": "plutus", "team": "the workshop"})
    ok, said = slack_bot.check()
    assert ok is False and "app token" in said


def test_a_slack_test_that_works_names_the_bot_and_never_the_token(monkeypatch):
    from inventory import slack_bot

    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-secret")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-secret")
    monkeypatch.setenv("SLACK_CHANNELS", "C01")
    monkeypatch.setattr(slack_bot, "available", lambda: True)

    import slack_sdk

    monkeypatch.setattr(slack_sdk.WebClient, "auth_test",
                        lambda self: {"user": "plutus", "team": "the workshop"})
    opened = {}

    def connections_open(self, app_token=None, **kw):
        # Slack wants the app token as an argument here, not as the client's own token.
        opened["app_token"] = app_token
        return {"url": "wss://nowhere"}

    monkeypatch.setattr(slack_sdk.WebClient, "apps_connections_open", connections_open)

    ok, said = slack_bot.check()
    assert opened["app_token"] == "xapp-secret"
    assert ok is True
    assert "plutus in the workshop" in said and "C01" in said
    assert "xoxb-secret" not in said and "xapp-secret" not in said


def test_the_slack_card_can_test_the_connection():
    from inventory import server as server_module

    page = server_module.page_source()
    assert 'id="slack-check"' in page


def test_the_landing_page_offers_the_five_doors_in_the_order_they_are_drawn(tmp_path,
                                                                            monkeypatch):
    from inventory import shortcuts

    monkeypatch.delenv("CONTACTS_CSV", raising=False)
    doors = shortcuts.listing(tmp_path)
    assert [door["key"] for door in doors] == ["slack", "discord", "outlook", "proton",
                                               "contacts"]
    assert [door["name"] for door in doors] == ["Slack", "Discord", "Outlook", "Proton Mail",
                                                "Supplier contacts"]
    # The four outside ones are always there; the sheet is only there when the file is.
    assert all(door["ready"] for door in doors[:4])
    assert doors[-1]["ready"] is False


def test_the_contact_sheet_is_the_setting_first_and_the_data_folder_after(tmp_path,
                                                                          monkeypatch):
    from inventory import shortcuts

    beside = tmp_path / "supplier-contacts.csv"
    beside.write_text("company,person\n", encoding="utf-8")
    monkeypatch.delenv("CONTACTS_CSV", raising=False)
    assert shortcuts.contacts_path(tmp_path) == beside

    named = tmp_path / "elsewhere" / "contacts.csv"
    named.parent.mkdir()
    named.write_text("company,person\n", encoding="utf-8")
    monkeypatch.setenv("CONTACTS_CSV", str(named))
    assert shortcuts.contacts_path(tmp_path) == named
    assert shortcuts.listing(tmp_path)[-1]["ready"] is True


def test_a_door_hands_its_own_target_to_the_desktop_and_nothing_else(tmp_path, monkeypatch):
    from inventory import shortcuts

    handed = []
    monkeypatch.setattr(shortcuts, "_opener", lambda: ["xdg-open"])
    monkeypatch.setattr(shortcuts.subprocess, "Popen",
                        lambda command, **kw: handed.append(command))
    monkeypatch.setattr(shortcuts.os, "name", "posix")

    # The link itself is the workshop's own and gets edited; what is pinned here is that
    # the door hands over exactly what it holds, and nothing it was passed.
    assert shortcuts.open_it("slack", tmp_path)["name"] == "Slack"
    assert handed == [["xdg-open", shortcuts.DOORS["slack"]["url"]]]

    # Nothing but the five names can be asked for, so a path cannot be smuggled in.
    with pytest.raises(ValueError):
        shortcuts.open_it("/etc/passwd", tmp_path)
    assert len(handed) == 1


def test_a_contact_sheet_that_is_not_there_says_so_rather_than_opening_nothing(tmp_path,
                                                                               monkeypatch):
    from inventory import shortcuts

    monkeypatch.setenv("CONTACTS_CSV", str(tmp_path / "gone.csv"))
    monkeypatch.setattr(shortcuts.subprocess, "Popen",
                        lambda *a, **kw: pytest.fail("nothing should be opened"))
    with pytest.raises(FileNotFoundError) as refused:
        shortcuts.open_it("contacts", tmp_path)
    assert "CONTACTS_CSV" in str(refused.value)


def test_a_basket_turned_into_a_purchase_order_is_an_order_the_chat_placed(db_path):
    from inventory import baskets, gate

    baskets.add(db_path, "chan", [{"article": "RONDELLE M8", "quantity": 40}],
                shop="BRICO", site="ARTIGUES", who="SAM")
    made = baskets.to_purchase_order(db_path, "chan", "SAM")

    rows = gate.step_history(db_path, 7)["rows"]
    assert [row["title"] for row in rows] == [f"purchase order #{made['list_id']} from the chat"]
    assert rows[0]["who"] == "SAM" and "ARTIGUES" in rows[0]["detail"]
    # The basket is emptied by the order, so it has left the first step.
    assert not any("RONDELLE" in row["title"] for row in gate.step_history(db_path, 1)["rows"])


def test_a_click_on_a_wedge_opens_its_history_in_a_window_of_its_own():
    from inventory import server as server_module

    page = server_module.page_source()
    screen = page[page.index('id="panel-chain"'):page.index('id="panel-query"')]
    assert 'id="step-pop"' in screen and 'id="table-step"' in screen
    # The wheel is handed the way to open one, and every wedge is one button.
    assert "roundDiagram(steps, now.step ?? 0, faults, openStepAudit)" in page
    assert '`/api/chain/step/${step.number}`' in page
    assert 'class: "step", role: "button"' in page


def test_a_door_is_an_address_the_desktop_can_open(tmp_path, monkeypatch):
    from inventory import shortcuts

    monkeypatch.delenv("CONTACTS_CSV", raising=False)
    name, uri, ready = shortcuts.door_uri("discord", tmp_path)
    assert (name, uri.startswith("https://"), ready) == ("Discord", True, True)
    (tmp_path / "supplier-contacts.csv").write_text("company\nA\n")
    name, uri, ready = shortcuts.door_uri("contacts", tmp_path)
    assert uri == (tmp_path / "supplier-contacts.csv").resolve().as_uri() and ready
    with pytest.raises(ValueError):
        shortcuts.door_uri("evil", tmp_path)


def test_only_the_doors_that_are_pages_have_a_window_title_to_go_back_to():
    from inventory import shortcuts

    assert set(shortcuts.WINDOW_TITLES) == {key for key, door in shortcuts.DOORS.items()
                                            if "url" in door}
