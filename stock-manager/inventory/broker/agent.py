"""Hermes, the dispatcher: an AI that reviews the optimizer's day and may change it inside limits.

The optimizer is greedy and sees only today. Hermes is shown what the optimizer passed over and
what is due in the next days, and may pull an order onto a vehicle or drop an early rider. Every
answer is checked again in code: an id that is not in the brief, a detour over the limit, a full
vehicle or one change too many is refused and counted, never applied.
"""

import asyncio
import json
import os
import time
from pathlib import Path

from .. import charters
from .engine import best_slot

DEFAULT_LIMITS = {"max_changes_per_day": 5, "max_extra_km": 15.0, "max_pull_days": 3}

SCHEMA = {
    "type": "object",
    "properties": {
        "actions": {"type": "array", "items": {
            "type": "object",
            "properties": {"id": {"type": "string"},
                           "action": {"type": "string", "enum": ["pull_in", "drop"]},
                           "reason": {"type": "string"}},
            "required": ["id", "action", "reason"]}},
        "note": {"type": "string"},
    },
    "required": ["actions", "note"],
}


def charter():
    """(text for the model, limits) from bot-rules/hermes.md, or the defaults."""
    candidates = [Path(os.environ["BOT_RULES_DIR"]) / "hermes.md"] if os.environ.get("BOT_RULES_DIR") else []
    candidates.append(Path(__file__).resolve().parents[3] / "bot-rules" / "hermes.md")
    for path in candidates:
        if path.is_file():
            text = path.read_text(encoding="utf-8")
            limits = dict(DEFAULT_LIMITS)
            for key, (value, _what) in charters._parse(text)[2].items():
                if key in limits:
                    limits[key] = type(limits[key])(value)
            return text, limits
    return "You are Hermes, a freight dispatcher.", dict(DEFAULT_LIMITS)


class Hermes:
    def __init__(self, backend, days=5, ask=None):
        self.backend = backend
        self.days_left = days
        self.text, self.limits = charter()
        self._ask = ask or self._ask_backend

    def _ask_backend(self, brief):
        prompt = ("Here is today's plan. Decide the changes, if any, then answer in the schema.\n\n"
                  + json.dumps(brief, ensure_ascii=False))
        return asyncio.run(self.backend.answer(prompt, SCHEMA, system=self.text))

    def review(self, brief, today, tours, taken, passed, base, km, at, fleet):
        """Ask, then apply what the limits allow. Gives back what happened, for the log."""
        out = {"asked": False, "applied": 0, "refused": 0, "changes": [], "note": ""}
        if self.days_left <= 0 or not (brief["not_taken"] or brief["riding_early"]):
            return out
        self.days_left -= 1
        out["asked"] = True
        try:
            answer = self._ask(brief)
        except Exception as error:  # noqa: BLE001 - any failure of the model keeps the optimizer's plan
            out["note"] = f"no answer: {error}"[:300]
            return out
        out["note"] = str(answer.get("note", ""))[:500]
        waiting = {o.id: (o, extra) for o, extra, _i in passed}
        riding = {o.id: o for o in taken}
        for action in answer.get("actions", []):
            kind, ident, why = action.get("action"), action.get("id"), action.get("reason", "")
            verdict = self._check(kind, ident, today, waiting, riding, tours, base, km, at, fleet, out)
            if verdict is None:
                if kind == "pull_in":
                    order = waiting.pop(ident)[0]
                    _extra, i = best_slot(tours, order, base, km, at, fleet)
                    tours[i].append(order)
                    taken.append(order)
                else:
                    order = riding.pop(ident)
                    for tour in tours:
                        if order in tour:
                            tour.remove(order)
                    taken.remove(order)
                out["applied"] += 1
            else:
                out["refused"] += 1
            out["changes"].append({"id": ident, "action": kind, "reason": why[:200],
                                   "result": verdict or "applied"})
        return out

    def _check(self, kind, ident, today, waiting, riding, tours, base, km, at, fleet, out):
        if out["applied"] >= self.limits["max_changes_per_day"]:
            return "refused: too many changes today"
        if kind == "drop":
            return None if ident in riding else "refused: not an early rider today"
        if kind != "pull_in" or ident not in waiting:
            return "refused: not an order the brief offered"
        order, _ = waiting[ident]
        due_in = max(0, order.latest - today) if order.by != "pull-in" else 0
        if due_in > self.limits["max_pull_days"]:
            return "refused: leaves too far ahead"
        extra, _i = best_slot(tours, order, base, km, at, fleet)
        if extra is None:
            return "refused: no vehicle has room"
        if extra > self.limits["max_extra_km"]:
            return f"refused: adds {extra:.1f} km, over the limit"
        return None


class Desk:
    """Hermes answered by hand, or by an assistant: each brief is written to a folder as
    `brief-NN.json`, and the run waits for `answer-NN.json` ({"actions": [...], "note": "..."})
    beside it. The answer goes through the same checks as the model's."""

    def __init__(self, folder, wait_seconds=1800):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.wait_seconds = wait_seconds
        self.count = 0

    def __call__(self, brief):
        self.count += 1
        name = f"{self.count:02d}"
        (self.folder / f"brief-{name}.json").write_text(json.dumps(brief, indent=1, ensure_ascii=False))
        answer = self.folder / f"answer-{name}.json"
        deadline = time.monotonic() + self.wait_seconds
        while time.monotonic() < deadline:
            if answer.exists():
                time.sleep(0.3)                     # let the writer finish
                return json.loads(answer.read_text(encoding="utf-8"))
            time.sleep(1)
        raise TimeoutError(f"no answer-{name}.json within {self.wait_seconds} s")
