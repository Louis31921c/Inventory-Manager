"""The two agents the app runs, both of them answerable, each with a charter (see charters.py).

  Plutus    the buyer: writes the purchase orders, on a rhythm, from the DDMRP buffers
  Terminus  the gate keeper: reads a basket against the names we know, then approves or cancels it

They are deliberately two, and deliberately narrow. Plutus decides what to buy and writes it down
as a purchase order, one big order each cycle and a small urgent one between; it stops there,
because sending an order to a supplier is a person's, or a policy's inside its limits. Terminus
decides the baskets the gate holds, inside limits, and never touches an order or a price. Each
has numbers a person sets, and the limits are enforced in code on whatever the model answers,
so an agent that ignores its instructions changes nothing.

Both go out through `ask`, so there is one place where this app talks to Claude, one
timeout, and one way for it to fail. There is no API key here: the agents run the Claude
Code CLI that is already signed in on this machine.
"""

import logging
import os
import shutil
from pathlib import Path

from . import llm

log = logging.getLogger(__name__)

# Plutus had the purse; Terminus was the boundary stone nobody was allowed to move.
PLUTUS = "plutus"
TERMINUS = "terminus"

# A draft of twenty lines is a long think; the default of five minutes is not enough when
# the shelf is big.
TIMEOUT = 420


# A desktop launcher does not get the shell's PATH, so the command line can be installed
# and still be invisible to the app. These are where it usually is when PATH misses it.
LIKELY = ("~/.local/bin/claude", "~/.npm-global/bin/claude", "/usr/local/bin/claude",
          "~/.local/bin/claude-code")


def claude_binary():
    """Where the Claude command line is, PATH or not."""
    found = shutil.which("claude") or shutil.which("claude-code")
    if found:
        return found
    for guess in LIKELY:
        path = Path(guess).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return None


def available():
    return claude_binary() is not None


def status():
    """What the screens say about the agents, without naming a key that does not exist."""
    found = claude_binary()
    return {
        "ready": bool(found),
        "found_at": found,
        "detail": (f"the Claude command line is at {found}" if found
                   else "install the Claude Code CLI, then run claude and /login"),
        "agents": [
            {"name": PLUTUS, "does": "writes the purchase orders from the DDMRP buffers, once a cycle"},
            {"name": TERMINUS, "does": "reads the names, then approves or cancels held baskets inside its limits"},
        ],
    }


async def ask(prompt, schema, system=None, model=None, timeout=TIMEOUT):
    """One question to Claude, one structured answer. The only door either agent uses."""
    backend = llm.ClaudeCodeBackend(model=model, timeout=timeout,
                                    binary=claude_binary() or "claude")
    return await backend.answer(prompt, schema, system=system)


# ---- Plutus, who holds the purse ------------------------------------------

def order_question(planner):
    """What the buyer is handed: the planner's numbers, and nothing else to go on."""
    import json

    return ("Here are the planner's numbers for the articles it watches.\n\n"
            + json.dumps(planner, ensure_ascii=False, indent=1, default=str)
            + "\n\nWrite the purchase order they call for.")


async def draft_order(planner, model=None, charter=None):
    """Plutus. Gives back {lines, note}; the person signs it, or does not. `charter` is the
    text of its charter, freedom and limits, added to its instructions."""
    if not available():
        raise llm.ExtractionError(
            "Plutus needs the Claude command line: install it, then run "
            "claude and /login.")
    system = llm.prompt_text("order_draft.md")
    if charter:
        system += "\n\n" + charter
    return await ask(order_question(planner), llm.ORDER_SCHEMA, system=system, model=model)
