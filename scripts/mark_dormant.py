"""Mark the 5 non-cinema / dormant venues inactive with a precise reason.

Evidence gathered 2026-11-25:
- rooftop-cinema: stale WordPress site; no /whats-on or /movies (404), no booking
  links, "Mother's Day" content — no film program advertised.
- forum-theatre: forumtheatre.com.au does not resolve; the real venue
  (forummelbourne.com.au) is a live-music venue — no film sessions.
- capitol-theatre: the registered URL is the *Sydney* Capitol Theatre; the
  Melbourne Capitol (113 Swanston St) is now a retail arcade, not a cinema.
- thornbury-theatre: website is a 114-byte empty shell; live-music venue.
- la-trobe-university-cinema: 403 to non-browser clients; no public program.
"""

import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

REGISTRY = Path("data/cinemas.json")

REASONS = {
    "rooftop-cinema": (
        "Dormant: stale WordPress site with no film program, no booking links "
        "and no what's-on page (/whats-on and /movies both 404). Last content "
        "is a Mother's Day event. Re-check in summer season."
    ),
    "forum-theatre": (
        "Not a cinema: forumtheatre.com.au does not resolve; the venue now runs "
        "as Forum Melbourne (forummelbourne.com.au), a live-music venue with no "
        "film sessions."
    ),
    "capitol-theatre": (
        "Not a cinema: the registered URL is the Sydney Capitol Theatre. The "
        "Melbourne Capitol at 113 Swanston St is now a retail arcade, not a "
        "screening venue. Website field is wrong."
    ),
    "thornbury-theatre": (
        "Not a cinema: the website is a 114-byte empty shell; the venue is a "
        "live-music venue with no film program."
    ),
    "la-trobe-university-cinema": (
        "Not a public cinema: the university site returns 403 to non-browser "
        "clients and advertises no public film program."
    ),
}


def main() -> int:
    rows = json.loads(REGISTRY.read_text(encoding="utf-8"))
    changed = 0
    for row in rows:
        if row["id"] in REASONS:
            row["active"] = False
            row["notes"] = REASONS[row["id"]]
            changed += 1
            print(f"  marked inactive: {row['id']}")
    REGISTRY.write_text(
        json.dumps(rows, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"\n{changed} venues marked inactive; {len(rows)} rows total")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
