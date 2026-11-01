"""The two agents the app runs, both of them Claude, both of them answerable.

  buying        reads the planner's own numbers and drafts a purchase order
  verification  reads a basket against the names we already know

They are deliberately two, and deliberately narrow. The buying agent proposes what to
order and signs nothing; the verification agent settles what an article is and approves
nothing. Neither can spend anything: a draft is a draft until a person saves it, and a
held basket waits for the gate however sure an agent sounds.

Both go out through `ask`, so there is one place where this app talks to Claude, one
timeout, and one way for it to fail. There is no API key here: the agents run the Claude
Code CLI that is already signed in on this machine.
"""

import logging
import shutil

from . import llm

log = logging.getLogger(__name__)

BUYING = "buying"
VERIFICATION = "verification"

# A draft of twenty lines is a long think; the default of five minutes is not enough when
# the shelf is big.
TIMEOUT = 420


def available():
    return bool(shutil.which("claude") or shutil.which("claude-code"))


def status():
    """What the screens say about the agents, without naming a key that does not exist."""
    ready = available()
    return {
        "ready": ready,
        "detail": ("the Claude command line is signed in on this machine" if ready
                   else "install the Claude Code CLI, then run claude and /login"),
        "agents": [
            {"name": BUYING, "does": "drafts a purchase order from the planner's numbers"},
            {"name": VERIFICATION, "does": "says whether an article is one we already know"},
        ],
    }


async def ask(prompt, schema, system=None, model=None, timeout=TIMEOUT):
    """One question to Claude, one structured answer. The only door either agent uses."""
    backend = llm.ClaudeCodeBackend(model=model, timeout=timeout)
    return await backend.answer(prompt, schema, system=system)


# ---- the buying agent -----------------------------------------------------

def order_question(planner):
    """What the buyer is handed: the planner's numbers, and nothing else to go on."""
    import json

    return ("Here are the planner's numbers for the articles it watches.\n\n"
            + json.dumps(planner, ensure_ascii=False, indent=1, default=str)
            + "\n\nWrite the purchase order they call for.")


async def draft_order(planner, model=None):
    """The buying agent. Gives back {lines, note}; the person signs it, or does not."""
    if not available():
        raise llm.ExtractionError(
            "The buying agent needs the Claude command line: install it, then run "
            "claude and /login.")
    return await ask(order_question(planner), llm.ORDER_SCHEMA,
                     system=llm.prompt_text("order_draft.md"), model=model)
