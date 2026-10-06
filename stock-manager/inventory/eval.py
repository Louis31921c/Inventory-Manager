import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

from .config import load_config
from .llm import make_backend

CORPUS = Path(__file__).resolve().parent.parent / "tests" / "corpus"
HISTORY_NAME = "eval-history.json"
HISTORY_KEEP = 50


def history_path(data_dir):
    return data_dir / HISTORY_NAME


def read_history(data_dir):
    try:
        return json.loads(history_path(data_dir).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def record_run(data_dir, summary):
    runs = read_history(data_dir)[-(HISTORY_KEEP - 1):] + [summary]
    history_path(data_dir).parent.mkdir(parents=True, exist_ok=True)
    history_path(data_dir).write_text(json.dumps(runs, ensure_ascii=False), encoding="utf-8")


def _compare(expected, got, path=""):
   
    if isinstance(expected, dict) and set(expected) == {"$or"}:
        if not any(not list(_compare(option, got, path)) for option in expected["$or"]):
            yield (path, " or ".join(repr(o) for o in expected["$or"]), got)
        return

    if isinstance(expected, dict):
        for key, value in expected.items():
            yield from _compare(value, (got or {}).get(key), f"{path}.{key}" if path else key)
        return

    if isinstance(expected, list):
        if not isinstance(got, list) or len(got) != len(expected):
            yield (path, f"{len(expected)} items", f"{len(got) if isinstance(got, list) else got}")
            return
        for i, value in enumerate(expected):
            yield from _compare(value, got[i], f"{path}[{i + 1}]")
        return

    same = expected == got
    if isinstance(expected, (int, float)) and isinstance(got, (int, float)):
        same = abs(expected - got) < 1e-9
    if isinstance(expected, str) and isinstance(got, str):
        same = expected.strip().casefold() == got.strip().casefold()
    if not same:
        yield (path, expected, got)


def _count_leaves(value):
    if isinstance(value, dict):
        return sum(_count_leaves(v) for v in value.values())
    if isinstance(value, list):
        return sum(_count_leaves(v) for v in value)
    return 1


async def run_case(backend, case):
    spec = json.loads(case.read_text(encoding="utf-8"))
    data = case.with_suffix(".jpg").read_bytes()
    if spec.get("type") == "list":
        got = await backend.extract_list(data, "image/jpeg")
    else:
        got = (await backend.extract(data, "image/jpeg")).model_dump()

    mismatches = list(_compare(spec["expected"], got))
    return {
        "case": case.stem,
        "fields": _count_leaves(spec["expected"]),
        "errors": len(mismatches),
        "details": [{"field": f, "expected": e, "got": g} for f, e, g in mismatches],
        "got": got,
    }


async def run_corpus(backend, cases):
    results = await asyncio.gather(*(run_case(backend, case) for case in cases))
    fields = sum(r["fields"] for r in results)
    errors = sum(r["errors"] for r in results)
    return {
        "date": datetime.now().isoformat(timespec="seconds"),
        "score": round(100 * (fields - errors) / fields, 1) if fields else 0.0,
        "fields": fields,
        "errors": errors,
        "documents": len(results),
        "cases": [{k: r[k] for k in ("case", "fields", "errors", "details")} for r in results],
    }


async def main_async(args):
    cases = sorted(CORPUS.glob("*.json"))
    if args.only:
        cases = [c for c in cases if args.only in c.stem]
    if not cases:
        print(f"No case in {CORPUS}. Add <name>.jpg + <name>.json.", file=sys.stderr)
        return 1

    config = load_config()
    summary = await run_corpus(make_backend(config), cases)
    if args.save:
        record_run(config.data_dir, summary)

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=1))
    else:
        for case in summary["cases"]:
            mark = "ok  " if not case["errors"] else "FAIL"
            print(f"{mark} {case['case']:<24} {case['fields'] - case['errors']}/{case['fields']} fields")
            for detail in case["details"]:
                print(f"      {detail['field']}: expected {detail['expected']!r}, "
                      f"got {detail['got']!r}")
        print(f"\nscore: {summary['score']:.1f}%  "
              f"({summary['fields'] - summary['errors']}/{summary['fields']} fields "
              f"over {summary['documents']} document(s))")

    return 0 if summary["errors"] == 0 else 2


def main():
    parser = argparse.ArgumentParser(description="Measure reading accuracy on a known corpus")
    parser.add_argument("--only", help="only the cases whose name contains this")
    parser.add_argument("--json", action="store_true", help="JSON output")
    parser.add_argument("--save", action="store_true", help="append the run to the history file")
    raise SystemExit(asyncio.run(main_async(parser.parse_args())))


if __name__ == "__main__":
    main()
