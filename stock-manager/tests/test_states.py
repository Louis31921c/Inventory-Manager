"""The life-cycle map has to match the code and the database, or it is worse than nothing."""

import re
from pathlib import Path

import pytest

from inventory import db, states

CODE = Path(states.__file__).parent


def _fresh(tmp_path):
    path = tmp_path / "t.duckdb"
    db.init(path)
    return path


def test_every_key_column_and_start_state_matches_the_database(tmp_path):
    path = _fresh(tmp_path)
    for table, cycle in states.MACHINES.items():
        columns = {row[0]: row[1] for row in db.read(
            path, "SELECT column_name, column_default FROM information_schema.columns WHERE table_name = ?", [table])}
        assert cycle.key in columns and "state" in columns, table
        default = columns["state"]
        if default:                                   # a table without a default (supplier_mail) sets it itself
            assert default.strip("'") == cycle.start, table
        assert cycle.start in cycle.steps


def test_every_move_goes_to_a_known_step():
    for cycle in states.MACHINES.values():
        for now, nexts in cycle.moves.items():
            assert now in cycle.steps, (cycle.table, now)
            assert set(nexts) <= set(cycle.steps), (cycle.table, now)
        assert cycle.by_hand or cycle.ends, f"{cycle.table} never finishes"


def test_the_code_writes_only_states_the_map_knows():
    """Any 'state = <word>' in the code must be a step of some life cycle."""
    known = {state for cycle in states.MACHINES.values() for state in cycle.steps}
    found = set()
    for path in CODE.glob("*.py"):
        if path.name in ("migrations.py", "states.py"):
            continue
        for chunk in re.findall(r"\bstate\s*(?:=|IN|<>|!=)\s*\(?([^)\n]*)", path.read_text()):
            found |= set(re.findall(r"'([a-z][a-z ]*)'", chunk))
        found |= set(re.findall(r"state=\"([a-z ]+)\"", path.read_text()))
    found -= {"in", "out"}                              # a mail's direction, not a state
    assert found <= known, f"not in the map: {sorted(found - known)}"


def test_a_wrong_move_is_refused_in_plain_words():
    with pytest.raises(ValueError, match="from 'loaded' it can go to"):
        states.check("dispatches", "loaded", "planned")
    with pytest.raises(ValueError, match="is the end"):
        states.check("gate_requests", "ordered", "approved")
    with pytest.raises(ValueError, match="not a step"):
        states.check("bills", "paid", "flying")
    states.check("bills", "paid", "disputed")          # a drop-down may set any step


def test_move_changes_only_a_row_in_a_state_that_may_take_it(tmp_path):
    path = _fresh(tmp_path)
    db.write(path, "INSERT INTO count_tasks (article, due_on) VALUES ('X', current_date) RETURNING article")
    assert states.move(path, "count_tasks", "X", "done") is True
    assert states.move(path, "count_tasks", "X", "done") is False    # already done: nothing moves


def test_the_markdown_file_is_up_to_date():
    written = (CODE.parent / "STATES.md").read_text()
    assert written.strip() == states.markdown().strip(), "run: python -m inventory.states > STATES.md"


def test_the_map_is_served_to_the_screen_and_to_agents():
    from inventory import server
    served = server.read_states()
    assert {m["table"] for m in served} == set(states.MACHINES)
    assert all(m["steps"] and "about" in m for m in served)
