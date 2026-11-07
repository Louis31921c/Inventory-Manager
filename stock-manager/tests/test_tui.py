"""Tests: tui."""



from inventory import db
from inventory.models import Line, Note
from helpers import filled, make_note, temp_config


def test_screen_grid_draws_where_it_is_told():
    from inventory.screen import Screen, columns

    screen = Screen(20, 3)
    screen.put(0, 2, "HELLO", "bright")
    screen.rule(1, 0, 20)
    screen.right(2, 20, "42")
    assert screen.rows_text() == ["  HELLO", "-" * 20, " " * 18 + "42"]
    assert columns([6, -4], ["BOLT", "12"]) == "BOLT      12"
    assert screen.cells[0][2] == ("H", "bright")


def test_render_draws_every_tab(tmp_path):
    from inventory import tui

    app = tui.App(filled(tmp_path))
    app.start()
    for index in range(len(tui.TABS)):
        app.tab = index
        text = str(tui.render(app, 110, 30))
        assert "STOCK MANAGER" in text
        assert "F7=QUIT" in text
        assert f"[{tui.TABS[index]}]" in text
    app.tab = 1
    assert "CEMENT 35KG" in str(tui.render(app, 110, 30))
    app.tab = 3
    assert "HARBOUR POINT" in str(tui.render(app, 110, 30))


def test_search_filter_and_work_item_edit(tmp_path):
    import curses

    from inventory import tui

    app = tui.App(filled(tmp_path))
    app.start()
    app.tab = 1
    app.search = "mesh"
    assert [row["article"] for row in app.filtered()] == ["MESH ST25"]
    app.only_backorders = True
    assert len(app.filtered()) == 1
    app.search = ""
    assert len(app.filtered()) == 1  # only the backorder line survives

    app.only_backorders = False
    app.search_sel = 0
    tui.handle(app, 0, "e")
    assert app.prompt is not None
    assert app.prompt.value == "GROUND SLAB"
    tui.handle(app, 21, None)
    for char in "GARAGE DOOR":
        tui.handle(app, ord(char), char)
    tui.handle(app, curses.KEY_ENTER, None)
    assert app.prompt is None
    assert "GARAGE DOOR" in {row["work_item"] for row in app.rows}


def test_review_keys_edit_and_save_a_note(tmp_path):
    import curses

    from inventory import tui

    app = tui.App(temp_config(tmp_path))
    app.start()
    app.draft = Note(supplier="ACME", delivery_date="2026-09-30",
                     lines=[Line(article="HEX BOLT", quantity=100)])
    app.draft_proposed = app.draft.model_copy(deep=True)
    app.draft_sel = 0

    tui.handle(app, 0, "a")
    assert len(app.draft.lines) == 2
    tui.handle(app, curses.KEY_ENTER, None)
    for char in "WASHER M6":
        tui.handle(app, ord(char), char)
    tui.handle(app, curses.KEY_ENTER, None)
    assert app.draft.lines[1].article == "WASHER M6"

    tui.handle(app, 0, "q")
    for char in "25":
        tui.handle(app, ord(char), char)
    tui.handle(app, curses.KEY_ENTER, None)
    assert app.draft.lines[1].quantity == 25

    tui.handle(app, 0, "b")
    assert app.draft.lines[1].backorder is True

    tui.handle(app, 0, "s")
    assert app.draft is None
    rows = db.run_readonly(app.config.db_path,
                           "SELECT article, quantity, backorder FROM inventory_lines ORDER BY line_no")
    assert rows.rows == [("HEX BOLT", 100.0, False), ("WASHER M6", 25.0, True)]
    assert db.accuracy(app.config.db_path)["fields"] > 0


def test_order_ticks_from_the_keyboard(tmp_path):
    import curses

    from inventory import tui

    app = tui.App(filled(tmp_path))
    app.start()
    app.tab = 3
    app.sheet_cursor = app.line_cursor = 0
    tui.handle(app, 0, "s")
    tui.handle(app, curses.KEY_DOWN, None)
    tui.handle(app, 0, "o")
    assert [line["in_stock"] for line in app.sheets[0]["lines"]] == [True, False]
    tui.handle(app, curses.KEY_UP, None)
    tui.handle(app, 0, "c")
    assert app.sheets[0]["lines"][0]["in_stock"] is None


def test_function_keys_switch_tabs_and_export(tmp_path):
    import curses

    from inventory import tui

    app = tui.App(filled(tmp_path))
    app.start()
    tui.handle(app, curses.KEY_F3, None)
    assert app.tab == 1
    tui.handle(app, curses.KEY_F1, None)
    assert app.tab == 2
    tui.handle(app, curses.KEY_F5, None)
    assert (app.config.data_dir / "exports" / "inventory.csv").exists()
    tui.handle(app, curses.KEY_F7, None)
    assert app.running is False


def test_cli_commands(tmp_path, capsys):
    from inventory import cli

    data = str(tmp_path)
    assert cli.main(["--data", data, "--user", "ALEX", "where"]) == 0
    assert "database" in capsys.readouterr().out

    config = cli.ready(cli.load_config(data, "ALEX"))
    db.save_note(config.db_path, make_note(), "sha-cli", "", "ALEX")

    assert cli.main(["--data", data, "search", "cement"]) == 0
    assert "CEMENT 35KG" in capsys.readouterr().out

    assert cli.main(["--data", data, "sql", "SELECT count(*) FROM inventory_lines"]) == 0
    assert "2" in capsys.readouterr().out

    assert cli.main(["--data", data, "--user", "ALEX", "export", "inventory",
                     "--format", "csv"]) == 0
    assert (tmp_path / "exports" / "inventory.csv").exists()

    assert cli.main(["--data", data, "vocab"]) == 0
    assert "CEMENT 35KG" in capsys.readouterr().out
    assert cli.main(["--data", data, "vocab", "--merge", "CEMENT 35KG", "CEMENT"]) == 0

    assert cli.main(["--data", data, "audit", "--users"]) == 0
    assert "ALEX" in capsys.readouterr().out

    assert cli.main(["--data", data, "errors"]) == 0
    assert cli.main(["--data", data, "sql", "DELETE FROM inventory_lines"]) == 1


def test_backup_snapshot_round_trip(tmp_path):
    from inventory import backup, cli

    config = cli.ready(temp_config(tmp_path))
    db.save_note(config.db_path, make_note(), "sha-snap", "", "ALEX")
    archive = backup.snapshot(config, passphrase="")
    assert archive.exists() and archive.name.startswith("stock-")

    import tarfile

    with tarfile.open(archive) as tar:
        tar.extractall(tmp_path / "unpacked", filter="data")
    counts = backup.restore(tmp_path / "unpacked" / "db", tmp_path / "again.duckdb")
    assert counts["inventory_lines"] == 2
