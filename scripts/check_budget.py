"""Check the built site against the gzip budgets in SPEC.md §8."""

from __future__ import annotations

import argparse
import gzip
import sys
from pathlib import Path

CAPS_KB = {"bundle": 260, "js": 28, "css": 12, "html": 6}


def gz(path: Path) -> int:
    return len(gzip.compress(path.read_bytes(), 9))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dist", nargs="?", default="dist")
    args = parser.parse_args()
    dist = Path(args.dist)
    if not dist.exists():
        print(f"FAIL dist not found: {dist}")
        return 1

    assets = dist / "assets"
    js = sorted(assets.glob("app.*.js"))
    css = sorted(assets.glob("app.*.css"))
    files = {
        "bundle": dist / "data" / "bundle.json",
        "js": js[-1] if js else assets / "missing.js",
        "css": css[-1] if css else assets / "missing.css",
        "html": dist / "index.html",
    }
    failures = 0
    for name, path in files.items():
        if not path.exists():
            print(f"FAIL {name}: missing {path}")
            failures += 1
            continue
        size = gz(path)
        cap = CAPS_KB[name] * 1024
        status = "OK  " if size <= cap else "FAIL"
        if size > cap:
            failures += 1
        print(f"{status} {name:7} {size / 1024:7.1f} KB gz  cap {cap / 1024:5.1f} KB  "
              f"({path.relative_to(dist)})")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
