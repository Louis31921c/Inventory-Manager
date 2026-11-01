"""The verification agent: Claude, reading what Dots wants to buy against what we know.

It answers one question and one only — *is this the article we already have under another
name?* Dots writes "CHEVILLE SPIT FIX 10x100"; the shelf knows "343546 - CHEVILLE SPIT FIX
3 10X100". Same thing, written twice. That is a reading job, and it is the one the agent
is for.

It never answers the other question — *do we want this, at this price, from this
supplier?* That one is the person's, and the agent's answer is evidence on their card, not
a recommendation to accept. When the agent cannot be reached, or is not sure, the gate
holds: it never falls open.
"""

import re

from . import agents, llm, vocabulary
from .models import normalize_name

# How many known names a line is held up against. More than this and the prompt is noise.
SHORTLIST = 12
WORD = re.compile(r"[A-Z0-9]+")

SAME = "same"
NEW = "new"
UNSURE = "unsure"

SCHEMA = {
    "type": "object",
    "properties": {
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "asked": {"type": "string"},
                    "verdict": {"type": "string", "enum": [SAME, NEW, UNSURE]},
                    "match": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                    "confidence": {"type": "number"},
                    "reason": {"type": "string"},
                },
                "required": ["asked", "verdict", "match", "confidence", "reason"],
                "additionalProperties": False,
            },
        },
        "note": {"type": "string"},
    },
    "required": ["lines", "note"],
    "additionalProperties": False,
}

INSTRUCTIONS = """You are checking a shopping basket against a workshop's own article names.

For each line, say whether the article asked for is one the workshop already knows under a
different wording, or something genuinely new.

- "same" only when it is the same physical article: the same part, the same size, the same
  material. A different size or a different finish is a different article, however close
  the wording. Put the known name in "match", exactly as it is written in the list.
- "new" when nothing in the list is that article.
- "unsure" when the line is too vague to tell, or two known names fit equally well.

Trade names, supplier codes and the order of the words all move about; sizes, grades and
materials do not. Judge on what the thing is, not on how the string looks.

You are not deciding whether to buy anything. Someone else does that with your answer in
front of them, so give the reason you would want to read."""


def _words(name):
    return set(WORD.findall(normalize_name(name) or ""))


def shortlist(article, known, limit=SHORTLIST):
    """The known names worth holding this one up against, closest first."""
    mine = _words(article)
    if not mine:
        return []
    scored = []
    for name in known:
        theirs = _words(name)
        if not theirs:
            continue
        shared = len(mine & theirs)
        if not shared:
            continue
        scored.append((shared / len(mine | theirs), shared, name))
    scored.sort(reverse=True)
    return [name for _, _, name in scored[:limit]]


def _question(lines, known):
    out = []
    for line in lines:
        article = normalize_name(line.get("article")) or ""
        near = shortlist(article, known)
        out.append(f"- asked for: {article}\n  known names that come near: "
                   + (", ".join(near) if near else "none"))
    return "The basket:\n" + "\n".join(out) + "\n\n" + INSTRUCTIONS


def overall(answers):
    """One word for the whole basket, taking the most doubtful line as the answer."""
    verdicts = {answer.get("verdict") for answer in answers}
    if not verdicts or UNSURE in verdicts:
        return UNSURE
    if NEW in verdicts:
        return NEW
    return SAME


async def check(db_path, lines, model=None):
    """Hold a basket up against the names we know.

    Gives back {verdict, note, lines}. Never raises: a backend that is missing, refusing
    or slow comes back as "unsure", which leaves the gate shut.
    """
    known = vocabulary.known_articles(db_path)
    try:
        answer = await agents.ask(_question(lines, known), SCHEMA, model=model)
    except llm.ExtractionError as e:
        return {"verdict": UNSURE, "note": f"The verification agent could not answer: {e}",
                "lines": []}
    except Exception as e:  # a backend that breaks must not open the gate
        return {"verdict": UNSURE, "note": f"The verification agent failed: {e}", "lines": []}

    answers = answer.get("lines") or []
    return {"verdict": overall(answers), "note": answer.get("note") or "", "lines": answers}


def learn(db_path, asked, match, who=None):
    """Write down a match the person agreed with, so the same name is never asked twice."""
    import duckdb

    alias, article = normalize_name(asked), normalize_name(match)
    if not alias or not article or alias == article:
        return False
    with duckdb.connect(str(db_path)) as con:
        con.execute("""
                INSERT INTO article_aliases (alias, article, created_by)
                VALUES (?, ?, ?) ON CONFLICT (alias) DO NOTHING
                """, [alias, article, who])
    return True
