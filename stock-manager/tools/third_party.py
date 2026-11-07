"""Writes THIRD-PARTY.md: every package the program needs, with its version and licence.

    python3 tools/third_party.py

Run it with the program's own virtual environment, after a dependency changes.
"""

import importlib.metadata as md
import re
from pathlib import Path

ROOTS = ["duckdb", "pydantic", "python-dotenv", "openpyxl", "fastapi", "uvicorn", "python-multipart",
         "pillow", "reportlab", "cryptography"]


def closure():
    seen, todo = {}, list(ROOTS)
    while todo:
        name = todo.pop()
        key = name.lower().replace("_", "-")
        if key in seen:
            continue
        try:
            dist = md.distribution(name)
        except md.PackageNotFoundError:
            continue
        seen[key] = dist
        for requirement in dist.requires or []:
            if "extra ==" in requirement:
                continue
            found = re.match(r"[A-Za-z0-9_.-]+", requirement)
            if found:
                todo.append(found.group(0))
    return seen.values()


def licence(dist):
    meta = dist.metadata
    text = (meta.get("License-Expression") or meta.get("License") or "").strip()
    first = text.splitlines()[0][:60] if text else ""
    if not first or len(first) > 40:
        named = [c.split("::")[-1].strip() for c in meta.get_all("Classifier") or [] if c.startswith("License ::")]
        first = named[0] if named else (first or "see package")
    return first


def main():
    rows = sorted((d.metadata["Name"], d.version, licence(d)) for d in closure())
    lines = ["# Third-party components", "",
             "The program uses the packages below, each under its own licence. This list is generated from",
             "the installed metadata (`python3 tools/third_party.py`); none of them changes the terms of the",
             "Stock Manager licence for the program's own code.", "",
             "| Package | Version | Licence |", "|---|---|---|"]
    lines += [f"| {n} | {v} | {lic} |" for n, v, lic in rows]
    Path(__file__).resolve().parent.parent.joinpath("THIRD-PARTY.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(len(rows), "packages")


if __name__ == "__main__":
    main()
