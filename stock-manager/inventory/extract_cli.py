import asyncio
import mimetypes
import sys
import time

from .config import load_config
from .llm import make_backend
from .vocabulary import known_articles


async def main(path):
    media_type = mimetypes.guess_type(path)[0] or "image/jpeg"
    with open(path, "rb") as f:
        data = f.read()

    config = load_config()
    known = known_articles(config.db_path) if config.db_path.exists() else []

    started = time.monotonic()
    note = await make_backend(config).extract(data, media_type, known)
    print(f"[{config.backend}, {time.monotonic() - started:.0f} s, {len(known)} known names]")
    print(note.model_dump_json(indent=2))
    print()
    for problem in note.problems():
        print(f"  check: {problem}")
    for warning in note.warnings:
        print(f"  warning: {warning}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    asyncio.run(main(sys.argv[1]))
