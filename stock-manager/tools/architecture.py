"""Writes the module map into docs/ARCHITECTURE.md from the first line of each module's docstring.

    python3 tools/architecture.py

Every module must be listed under a domain below; the test suite fails when a new one is not, so the map
cannot fall behind the code.
"""

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "inventory"

DOMAINS = {
    "Entry points": ["cli", "desktop", "tui", "server", "screen", "windows", "shortcuts"],
    "Documents in": ["intake", "mailbox", "mail_oauth", "extract_cli", "llm", "models", "models_info", "catalog",
                     "vocabulary", "photos", "archive", "sheets", "documents", "units", "prompts"],
    "Stock": ["stock", "stockctl", "receiving", "counting", "backorders", "requisitions", "reorder"],
    "Planning": ["forecast", "ddmrp", "replenish", "leadtimes", "feedback", "workdays", "pricing", "prices", "matching"],
    "Buying and suppliers": ["baskets", "gate", "verify", "judge", "coherence", "purchasing", "policies", "charters",
                             "agents", "wallet", "outbox", "channels", "replies", "quotes", "contacts", "outbound"],
    "Money": ["bills", "payments", "bankdrop", "disputes"],
    "Chat and alerts": ["chat", "discord_bot", "slack_bot", "alerts", "watch", "dashboard", "autonomy"],
    "Routing": ["routing"],
    "Platform": ["db", "migrations", "config", "account", "access", "vault", "audit", "licensing", "integrity", "health",
                 "backup", "sync", "pg", "eval", "simulation", "simulation_catalogue"],
}


def modules():
    return sorted(p.stem for p in PACKAGE.glob("*.py") if p.stem != "__init__")


def unlisted():
    known = {name for names in DOMAINS.values() for name in names}
    return [name for name in modules() if name not in known]


def first_line(name):
    doc = ast.get_docstring(ast.parse((PACKAGE / f"{name}.py").read_text(encoding="utf-8"))) or ""
    line = doc.strip().splitlines()[0] if doc.strip() else ""
    return line.replace("|", "/")


def table():
    out = []
    for domain, names in DOMAINS.items():
        out += [f"### {domain}", "", "| Module | What it does |", "|---|---|"]
        for name in names:
            if (PACKAGE / f"{name}.py").exists():
                out.append(f"| `{name}` | {first_line(name)} |")
        out.append("")
    return "\n".join(out)


def main():
    path = ROOT / "docs" / "ARCHITECTURE.md"
    text = path.read_text(encoding="utf-8")
    start, end = "<!-- modules:start -->", "<!-- modules:end -->"
    head, rest = text.split(start, 1)
    _old, tail = rest.split(end, 1)
    path.write_text(f"{head}{start}\n\n{table()}\n{end}{tail}", encoding="utf-8")
    print("written", path, len(modules()), "modules")


if __name__ == "__main__":
    if unlisted():
        sys.exit(f"Not in any domain: {', '.join(unlisted())}")
    main()
