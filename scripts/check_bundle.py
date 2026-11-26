"""Validate a built bundle: shape, indices, title hygiene, and DB parity."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cinemelb.export import _validate  # noqa: E402
from cinemelb.store import Store  # noqa: E402

PREFIX = re.compile(
    r"^(?:bff|jiff|miff|jff|sff)\s?\d{0,2}\b|"
    r"^[A-Z]{2,8}FF[A-Z]?\d{0,2}\s*[:\-–—]|"
    r"^(?:astor|palace(?:\s+cinemas)?|cult\s+classix|jodofest|casffa)\s*[:\-–—]",
    re.I,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", nargs="?", default="dist/data/bundle.json")
    parser.add_argument("--titles", action="store_true", help="reject programme prefixes")
    parser.add_argument("--against-db", action="store_true", help="compare with the SQLite store")
    args = parser.parse_args()

    path = Path(args.bundle)
    if not path.exists():
        print(f"FAIL bundle not found: {path}")
        return 1
    bundle = json.loads(path.read_text(encoding="utf-8"))
    problems = []
    try:
        _validate(bundle)
    except SystemExit as exc:
        problems.append(str(exc))

    counts = bundle.get("counts", {})
    for name, key in (("films", "films"), ("cinemas", "cinemas"),
                      ("screenings", "screenings"), ("dates", "dates")):
        if counts.get(name) != len(bundle.get(key, [])):
            problems.append(f"counts.{name} does not match len({key})")

    if args.titles:
        titles = [row[1] for row in bundle.get("films", [])]
        prefixed = [title for title in titles if PREFIX.search(title)]
        if prefixed:
            problems.append("programme prefixes in exported titles: " +
                            "; ".join(prefixed[:8]))
        duplicates = [title for title, n in Counter(t.lower() for t in titles).items() if n > 1]
        if duplicates:
            problems.append("duplicate titles: " + "; ".join(duplicates[:8]))

    if args.against_db:
        db = ROOT / "data" / "cinemelb.sqlite3"
        if not db.exists():
            problems.append(f"database not found: {db}")
        else:
            with Store(db) as store:
                films = len(store.films())
                cinemas = len(store.cinemas())
                screenings = len(store.screenings())
                dates = len({row["local_date"] for row in store.screenings()})
            for name, expected, actual in (
                ("films", films, counts.get("films")),
                ("cinemas", cinemas, counts.get("cinemas")),
                ("screenings", screenings, counts.get("screenings")),
                ("dates", dates, counts.get("dates")),
            ):
                if expected != actual:
                    problems.append(f"{name}: database {expected} != bundle {actual}")

    if problems:
        print("FAIL bundle")
        for problem in problems:
            print("  -", problem)
        return 1
    print(
        f"OK   bundle v{bundle.get('version')} · "
        f"{counts.get('films')} films · {counts.get('screenings')} sessions · "
        f"{counts.get('cinemas')} cinemas · {counts.get('dates')} dates"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
