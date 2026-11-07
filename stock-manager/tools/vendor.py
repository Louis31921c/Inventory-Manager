"""Vendor tools: make a licence, sign a release. They need the vendor's private key and nothing else does.

    python3 tools/vendor.py license "Customer name" --days 365 [--machine ID|any]
    python3 tools/vendor.py manifest [--root DIR]   # sign the package files as they are now (ship.sh signs the installed copy)

The key is read from ~/.stock-manager-vendor/vendor.key (or STOCK_VENDOR_KEY). Keep it off every machine
that is not yours, and keep a backup: without it no licence can be issued and no release signed.
"""

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from inventory import integrity, licensing  # noqa: E402


def key():
    path = Path(os.environ.get("STOCK_VENDOR_KEY") or Path.home() / ".stock-manager-vendor" / "vendor.key")
    try:
        return path.read_text().strip()
    except OSError:
        raise SystemExit(f"No vendor key at {path}.")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="what", required=True)
    lic = sub.add_parser("license")
    lic.add_argument("licensee")
    lic.add_argument("--days", type=int, default=365)
    lic.add_argument("--machine", default="any")
    lic.add_argument("--out")
    sign = sub.add_parser("manifest")
    sign.add_argument("--root", help="the package folder to sign (default: the source tree)")
    args = parser.parse_args()
    if args.what == "license":
        document = licensing.issue(key(), args.licensee, args.days, args.machine)
        text = json.dumps(document, indent=1)
        if args.out:
            Path(args.out).write_text(text + "\n")
            Path(args.out).chmod(0o600)
            print("written", args.out, "expires", document["payload"]["expires"])
        else:
            print(text)
    else:
        root = Path(args.root) if args.root else integrity.PACKAGE
        document = integrity.build(key(), root)
        (root / "MANIFEST.json").write_text(json.dumps(document, indent=1) + "\n")
        print("signed", len(document["files"]), "files ->", root / "MANIFEST.json")


if __name__ == "__main__":
    main()
