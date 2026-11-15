import asyncio

from inventory import agents, charters, db, verify


def _fresh(tmp_path):
    path = tmp_path / "t.duckdb"
    db.init(path)
    return path


def test_guardrails_are_kept_per_bot_and_reach_the_prompt(tmp_path):
    path = _fresh(tmp_path)
    assert charters.guardrails(path, "plutus", "limits") == ""
    charters.set_guardrails(path, "plutus", "limits", "  Never order from ACME.  ", "me")
    charters.set_guardrails(path, "plutus", "rights", "May reorder the red zone.", "me")
    assert charters.guardrails(path, "plutus", "limits") == "Never order from ACME."
    block = charters.prompt_block(path, "plutus")
    assert "Never order from ACME." in block and "May reorder the red zone." in block
    assert "ACME" not in charters.prompt_block(path, "terminus")
    assert charters.all_guardrails(path)["plutus"]["rights"] == "May reorder the red zone."
    charters.set_guardrails(path, "plutus", "limits", "", "me")
    assert charters.guardrails(path, "plutus", "limits") == ""


def test_guardrails_refuse_a_wrong_bot_and_a_wall_of_text(tmp_path):
    path = _fresh(tmp_path)
    for bot, kind, text in (("nobody", "limits", "x"), ("plutus", "wishes", "x"), ("plutus", "limits", "x" * 4001)):
        try:
            charters.set_guardrails(path, bot, kind, text)
        except ValueError:
            continue
        raise AssertionError("accepted")


def test_review_asks_only_when_there_is_something_to_check(tmp_path, monkeypatch):
    path = _fresh(tmp_path)
    asked = []

    async def fake(prompt, schema, **kw):
        asked.append(prompt)
        return {"breaches": [{"item": 2, "rule": "Never order from ACME", "why": "it is ACME"},
                             {"item": 9, "rule": "r", "why": "out of range"}]}

    monkeypatch.setattr(agents, "ask", fake)
    assert asyncio.run(charters.review(path, "plutus", ["a", "b"])) is None and not asked
    charters.set_guardrails(path, "plutus", "limits", "Never order from ACME.")
    assert asyncio.run(charters.review(path, "plutus", [])) is None
    got = asyncio.run(charters.review(path, "plutus", ["BOLT from X", "NUT from ACME"]))
    assert [b["item"] for b in got] == [2] and "Never order from ACME." in asked[0]


def test_a_broken_second_look_changes_nothing(tmp_path, monkeypatch):
    path = _fresh(tmp_path)
    charters.set_guardrails(path, "plutus", "limits", "Never order from ACME.")

    async def boom(*a, **k):
        raise RuntimeError("no model")

    monkeypatch.setattr(agents, "ask", boom)
    assert asyncio.run(charters.review(path, "plutus", ["x"])) is None


def test_terminus_leaves_a_guardrail_breach_to_a_person(tmp_path, monkeypatch):
    path = _fresh(tmp_path)
    charters.set_guardrails(path, "terminus", "limits", "Always leave baskets for site MARTIN to a person.")
    db.save_purchase_order(path, {"lines": [{"article": "RONDELLE M8", "quantity": 1}]}, None, None, "u")

    async def fake(prompt, schema, **kw):
        if "breaches" in schema["properties"]:
            return {"breaches": [{"item": 1, "rule": "site MARTIN", "why": "it is for MARTIN"}]}
        return {"note": "", "lines": [{"asked": "RONDELLE M 8", "verdict": "same", "match": "RONDELLE M8",
                                       "confidence": 0.99, "reason": "spacing"}]}

    monkeypatch.setattr(agents, "ask", fake)
    out = asyncio.run(verify.check(path, [{"article": "RONDELLE M 8"}]))
    assert out["verdict"] == verify.UNSURE and "guardrail" in out["lines"][0]["reason"]
