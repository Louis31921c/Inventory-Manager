"""Tests: screens."""






def test_downloads_land_somewhere(tmp_path, monkeypatch):
    from inventory import desktop

    monkeypatch.setenv("XDG_DOWNLOAD_DIR", str(tmp_path / "pick-ups"))
    folder = desktop.downloads_dir()
    assert folder.is_dir()

    first = desktop.free_name(folder, "movements-week.csv")
    first.write_text("one")
    second = desktop.free_name(folder, "movements-week.csv")
    second.write_text("two")
    assert first.name == "movements-week.csv"
    assert second.name == "movements-week-2.csv"
    assert desktop.free_name(folder, "").name == "download"
    assert desktop.free_name(folder, "../../escape.csv").parent == folder


def test_every_box_the_page_wires_up_is_on_the_page():
    """The page is edited by hand; a box taken out must not strand its screen."""
    import re

    from inventory import server as server_module

    page = server_module.page_source()
    here = set(re.findall(r'\bid="([^"]+)"', page))
    # Listeners hung at the top level run as the page loads: a missing one used to
    # throw there and leave every screen below it dead.
    wired = re.findall(r'^\$\("([^"]+)"\)(\??)\.addEventListener', page, re.M)
    assert [name for name, optional in wired if not optional and name not in here] == []

    # Nothing is left half-written either.
    assert page.count("<section") == page.count("</section>")
    assert page.count("<label") == page.count("</label>")


def test_the_ddmrp_screen_is_named_on_the_menu_bar():
    from inventory import server as server_module

    page = server_module.page_source()
    bar = page[page.index('<nav class="menubar"'):page.index("</nav>")]
    assert 'id="menu-ddmrp"' in bar and ">DDMRP<" in bar
    # The menu that used to live inside the screen now hangs off that name.
    assert 'id="ddmrp-tools"' not in page
    assert 'id="tools-drop"' in bar


def test_the_menu_bar_ends_with_system_and_help():
    from inventory import server as server_module

    page = server_module.page_source()
    bar = page[page.index('<nav class="menubar"'):page.index("</nav>")]
    # The gap pushes the last two names to the far end, over Remember me and Exit.
    assert bar.index('class="menubar-gap"') < bar.index('id="system-menu"') < bar.index('id="menu-help"')


def test_the_tool_row_keeps_nothing_but_remember_me_and_exit():
    from inventory import server as server_module

    page = server_module.page_source()
    row = page[page.index('<div class="toolbar">'):page.index('<div class="workspace">')]
    for gone in ['id="command"', 'id="tool-refresh"', 'id="tool-find"',
                 'id="tool-export"', 'id="tool-planner"']:
        assert gone not in row
    assert 'id="lock-form"' in row and 'id="quit-form"' in row


def test_the_inventory_name_rolls_its_cards_down():
    from inventory import server as server_module

    page = server_module.page_source()
    bar = page[page.index('<nav class="menubar"'):page.index("</nav>")]
    assert 'id="inv-drop"' in bar
    screen = page[page.index('id="panel-inventory"'):page.index('id="panel-query"')]
    for card in ["In stock", "Partly delivered", "Delivered lines", "To put away", "Count today"]:
        assert f"<h2>{card}</h2>" in screen


def test_the_purchase_orders_card_asks_three_things_and_no_search():
    from inventory import server as server_module

    page = server_module.page_source()
    card = page[page.index('id="stored-po"'):page.index('id="panel-quotes"')]
    assert "<h2>Purchase orders</h2>" in card
    for pick in ['id="o-site"', 'id="o-work"', 'id="o-state"']:
        assert pick in card
    assert 'id="o-search"' not in page
    # The same orders sit behind the Purchase orders line, so the menu drops its double.
    assert '"Stored PO"' not in page


def test_the_tree_folds_and_carries_a_quote_and_a_dots_order():
    from inventory import server as server_module

    page = server_module.page_source()
    tree = page[page.index('<aside class="tree"'):page.index("</aside>")]
    assert 'id="tree-toggle"' in tree and 'id="tree-list"' in tree
    assert 'id="tree-new-quote"' in tree and 'id="tree-plutus"' in tree
    # The sites hang off the Purchase orders menu now, not off the tree.
    assert 'id="tree-sites"' not in page


def test_the_landing_page_carries_the_approval_window():
    from inventory import server as server_module

    page = server_module.page_source()
    home = page[page.index('id="panel-home"'):page.index('id="panel-capture"')]
    assert 'id="gate-card"' in home and "<h2>Baskets needing your approval</h2>" in home
    assert 'id="gate-list"' in home


def test_the_mailbox_card_asks_for_the_microsoft_sign_in():
    from inventory import server as server_module

    page = server_module.page_source()
    card = page[page.index("<h2>Mailbox</h2>"):page.index("<h2>Chat bot</h2>")]
    for box in ['id="mail-oauth"', 'id="mail-client-id"', 'id="mail-tenant"',
                'id="mail-oauth-start"', 'id="mail-oauth-digits"']:
        assert box in card


def test_the_purchase_card_is_plutus_now():
    from inventory import server as server_module

    page = server_module.page_source()
    assert "<h2>Plutus orders</h2>" in page
    assert 'id="ask-agent"' in page and 'id="agent-note"' in page
    # The button asked for a route that does not exist, so it only ever answered 404.
    assert "/api/forecast/dotsDraft" not in page
    assert '"/api/forecast/draft"' in page


def test_the_plutus_card_asks_for_an_article_too():
    from inventory import server as server_module

    page = server_module.page_source()
    card = page[page.index('id="plutus-card"'):page.index('id="ask-agent"')]
    for box in ['id="order-site"', 'id="order-work"', 'id="order-article"']:
        assert box in card


def test_the_bar_carries_the_blockchain_screen():
    from inventory import server as server_module

    page = server_module.page_source()
    bar = page[page.index('<nav class="menubar"'):page.index("</nav>")]
    assert 'id="menu-chain"' in bar and ">Blockchain<" in bar
    screen = page[page.index('id="panel-chain"'):page.index('id="panel-query"')]
    assert "<h2>blockchain</h2>" in screen
    assert "<h2>Plutus draft</h2>" in screen
    # The round is drawn, with somewhere to say which step is live and a way out of it.
    assert 'id="chain-round"' in screen and 'id="chain-now"' in screen
    assert 'id="chain-doing"' in screen and 'id="chain-warn"' in screen


def test_the_blockchain_name_rolls_down_to_what_sits_under_the_round():
    from inventory import server as server_module

    page = server_module.page_source()
    bar = page[page.index('<nav class="menubar"'):page.index("</nav>")]
    assert 'id="chain-drop"' in bar and ">Blockchain<" in bar

    screen = page[page.index('id="panel-chain"'):page.index('id="panel-query"')]
    assert 'id="chain-card"' in screen          # the round, always there
    assert "<h2>Held baskets</h2>" in screen
    assert "<h2>Baskets nobody has bought</h2>" in screen
    assert "<h2>Plutus draft</h2>" in screen
    assert 'id="table-approval"' in screen and 'id="table-unbought"' in screen


def test_every_card_of_the_orders_screen_sits_on_the_screen_and_not_inside_another_card():
    """A missing </div> once swallowed the contact card into a hidden one, so the page it
    belongs to could never be seen. Cards are siblings of each other on their screen."""
    from html.parser import HTMLParser

    from inventory import server as server_module

    class Walker(HTMLParser):
        VOID = {"input", "img", "br", "hr", "meta", "link"}

        def __init__(self):
            super().__init__()
            self.stack, self.parents = [], {}

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if attrs.get("id"):
                self.parents[attrs["id"]] = next(
                    (name for name, _ in reversed(self.stack) if name), None)
            if tag not in self.VOID:
                self.stack.append((attrs.get("id"), tag))

        def handle_endtag(self, tag):
            if tag in self.VOID:
                return
            while self.stack and self.stack.pop()[1] != tag:
                pass

    walker = Walker()
    walker.feed(server_module.page_source())
    for card in ("order-scan", "list-review", "contacts-card", "stored-po"):
        assert walker.parents[card] == "panel-orders", f"{card} is inside {walker.parents[card]}"
