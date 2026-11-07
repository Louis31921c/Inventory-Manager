"""Refuses a commit that carries a key, a token or a private key. Used by pre-commit and by CI.

    python3 tools/check_secrets.py FILE...
"""

import re
import sys
from pathlib import Path

PATTERNS = {
    "Slack token": r"xox[bap]-[0-9A-Za-z-]{10,}",
    "OpenAI-style key": r"sk-[A-Za-z0-9]{20,}",
    "AWS key id": r"AKIA[0-9A-Z]{16}",
    "Google API key": r"AIza[0-9A-Za-z_-]{30,}",
    "GitHub token": r"gh[pousr]_[0-9A-Za-z]{30,}",
    "private key": r"BEGIN (RSA |OPENSSH |EC |)PRIVATE KEY",
    "sealed secret": r"enc:v1:gAAAA[0-9A-Za-z_=-]{20,}",
}
SKIP = {".png", ".jpg", ".woff2", ".duckdb", ".pyc"}


def main(paths):
    bad = []
    for name in paths:
        path = Path(name)
        if not path.is_file() or path.suffix in SKIP or path.name == "check_secrets.py":
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for label, pattern in PATTERNS.items():
            if re.search(pattern, text) and "tests/" not in path.as_posix():
                bad.append(f"{path}: looks like a {label}")
    for line in bad:
        print(line, file=sys.stderr)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
