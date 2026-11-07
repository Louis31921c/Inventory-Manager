"""Builds the wheel to send to a customer, with the program files signed inside it.

    python3 tools/release.py [--out dist]

It builds the wheel, signs a manifest of the package files as the wheel will install them, adds the manifest and
its line in RECORD, and checks the result. Needs the vendor key (see tools/vendor.py).
"""

import argparse
import base64
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from inventory import integrity  # noqa: E402
from vendor import key  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(ROOT / "dist"))
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        built, work = Path(tmp) / "built", Path(tmp) / "unpacked"
        subprocess.run([sys.executable, "-m", "pip", "wheel", str(ROOT), "--no-deps", "-q", "-w", str(built)], check=True)
        wheel = next(built.glob("*.whl"))
        with zipfile.ZipFile(wheel) as archive:
            archive.extractall(work)
        document = integrity.build(key(), work / "inventory")
        text = json.dumps(document, indent=1) + "\n"
        (work / "inventory" / "MANIFEST.json").write_text(text, encoding="utf-8")
        record = next(work.glob("*.dist-info/RECORD"))
        digest = base64.urlsafe_b64encode(hashlib.sha256(text.encode()).digest()).rstrip(b"=").decode()
        record.write_text(record.read_text().rstrip("\n") + f"\ninventory/MANIFEST.json,sha256={digest},{len(text.encode())}\n")
        check = integrity.verify(work / "inventory")
        if not check["ok"]:
            raise SystemExit(f"The signed wheel does not verify: {check['reason']}")
        target = out / wheel.name
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(work.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(work).as_posix())
        shutil.copystat(wheel, target)
    print("built", target, f"({len(document['files'])} files signed)")


if __name__ == "__main__":
    main()
