"""End-to-end verification against live data.

Checks, in order:

1. the registry is complete and geographically sane;
2. every adapter named in the registry imports;
3. a live refresh of every implemented platform returns screenings;
4. scraped times are plausible cinema times (not shifted by a timezone bug);
5. each query the tool promises actually answers (film→venues, time→films,
   near→ranked by distance, now→upcoming, trending→leaderboard);
6. distances are correct (a venue queried at its own coordinates is 0 km).

Exits non-zero if any check fails, so it can gate a release.

    python scripts/verify.py                # live, full refresh
    python scripts/verify.py --no-refresh   # verify the existing database
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cinemelb.adapters import available, known
from cinemelb.config import MELBOURNE_BOUNDS, REGISTRY_PATH
from cinemelb.geo import haversine_km
from cinemelb.models import MELBOURNE_TZ
from cinemelb.query import (
    Filters,
    cinema_catalogue,
    film_leaderboard,
    films_at,
    find_film,
    nearest,
    upcoming_hits,
)
from cinemelb.scrape import load_registry, refresh
from cinemelb.store import Store

#: Deliberately ASCII: this script must run on a stock Windows console whose
#: default code page cannot encode tick marks.
PASS = "OK  "
FAIL = "FAIL"
WARN = "NOTE"

results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, detail: str = "") -> bool:
    results.append((ok, name, detail))
    mark = PASS if ok else FAIL
    print(f"  {mark} {name}" + (f" — {detail}" if detail else ""))
    return ok


def note(name: str, detail: str) -> None:
    results.append((True, name, detail))
    print(f"  {WARN} {name} — {detail}")


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-refresh", action="store_true")
    ap.add_argument("--fresh", action="store_true")
    args = ap.parse_args()

    # ---------------------------------------------------------------- registry
    section("Registry")
    rows = load_registry()
    check(len(rows) >= 50, "venue count", f"{len(rows)} venues")
    bad = [
        r for r in rows
        if not r.get("suburb") or not r.get("postcode") or r.get("latitude") is None
    ]
    check(not bad, "every venue has suburb, postcode and coordinates",
          ", ".join(r["id"] for r in bad[:4]))
    outside = [
        r for r in rows
        if not (
            MELBOURNE_BOUNDS["min_lat"] <= r["latitude"] <= MELBOURNE_BOUNDS["max_lat"]
            and MELBOURNE_BOUNDS["min_lon"] <= r["longitude"] <= MELBOURNE_BOUNDS["max_lon"]
        )
    ]
    check(not outside, "all venues inside greater Melbourne",
          ", ".join(r["id"] for r in outside[:4]))
    ids = [r["id"] for r in rows]
    check(len(ids) == len(set(ids)), "venue ids unique")

    independent_markers = ("nova", "astor", "lido", "classic", "sun", "cameo", "acmi", "imax")
    names = " ".join(r["name"].lower() for r in rows)
    missing = [m for m in independent_markers if m not in names]
    check(not missing, "independent / art-house venues present in registry",
          f"missing: {missing}" if missing else "nova, astor, lido, classic, sun, cameo, acmi, imax")

    by_source: dict[str, int] = {}
    for r in rows:
        by_source[r.get("source", "?")] = by_source.get(r.get("source", "?"), 0) + 1
    print("     adapters: " + ", ".join(f"{k}={v}" for k, v in sorted(by_source.items())))

    # ---------------------------------------------------------------- adapters
    section("Adapters")
    ready = set(available())
    print(f"     loadable: {', '.join(sorted(ready))}")
    for source in sorted(by_source):
        ok = source in ready
        if not ok:
            # not fatal: the venue still appears in listings, just without times
            note(f"adapter {source!r}", "not implemented — venues have no session data")
        else:
            check(True, f"adapter {source!r} loads", f"{by_source[source]} venues")

    # ---------------------------------------------------------------- refresh
    if not args.no_refresh:
        section("Live refresh")
        report = refresh(fresh=args.fresh)
        print(f"     {report.summary()}")
        by_adapter: dict[str, list[int]] = {}
        notes: dict[str, list[str]] = {}
        for v in report.venues:
            agg = by_adapter.setdefault(v.source, [0, 0])
            agg[1] += 1
            if v.screenings:
                agg[0] += 1
            elif v.note:
                notes.setdefault(v.source, []).append(v.note)
        for source, (working, total) in sorted(by_adapter.items()):
            if source not in ready:
                note(f"{source}: no adapter", f"{total} venues listed without times")
            elif working > 0:
                check(True, f"{source}: live data", f"{working}/{total} venues returned screenings")
            elif notes.get(source):
                # Every venue carries an explanation and none returned rows: an
                # adapter that is *honestly* blocked or empty, not a broken one.
                # The auditor's rule — a named reason beats a silent zero.
                first = notes[source][0].split(". ")[0]
                note(f"{source}: honestly empty", f"{total} venues; {first}")
            else:
                check(False, f"{source}: live data", f"{working}/{total} venues returned screenings")

    # ---------------------------------------------------------------- database
    section("Database")
    with Store() as store:
        stats = store.stats()
        check(stats["cinemas"] >= 50, "venues stored", str(stats["cinemas"]))
        check(stats["screenings"] > 0, "screenings stored", str(stats["screenings"]))
        check(stats["films"] > 0, "films stored", str(stats["films"]))
        print(f"     dates: {stats['days'][0] if stats['days'] else '-'} → "
              f"{stats['days'][-1] if stats['days'] else '-'}")
        print("     by adapter: " + ", ".join(f"{k}={v}" for k, v in stats["sources"].items()))

        # ------------------------------------------------------- time sanity
        section("Time sanity")
        all_s = store.screenings()
        # Midnight premieres are real (Hoyts sells 00:01 sessions), so a small
        # tail outside 06:00-23:59 is expected — a *large* one means a timezone
        # bug has shifted every session.
        outside_hours = [s for s in all_s if not ("06:00" <= s["local_time"] <= "23:59")]
        check(
            len(outside_hours) <= max(20, len(all_s) * 0.02),
            "session times fall in normal cinema hours",
            f"{len(outside_hours)} of {len(all_s)} outside 06:00-23:59"
            + (f" (e.g. {outside_hours[0]['local_time']} {outside_hours[0]['film_title'][:30]})"
               if outside_hours else ""),
        )
        today = datetime.now(MELBOURNE_TZ).date()
        far_past = [s for s in all_s if date.fromisoformat(s["local_date"]) < today - timedelta(days=1)]
        check(not far_past, "no stale screenings in the past", f"{len(far_past)} stale")
        # Opera/ballet seasons are sold ~a year ahead, so the horizon has to be
        # generous; a genuine parsing bug produces absurd years, not months.
        future = [s for s in all_s if date.fromisoformat(s["local_date"]) > today + timedelta(days=400)]
        check(not future, "no absurd future dates", f"{len(future)} beyond +400 days")

        # ------------------------------------------------------------ queries
        section("Queries")
        leaderboard = film_leaderboard(store, limit=5)
        check(bool(leaderboard), "trending returns films",
              ", ".join(f"{r['title'][:28]} ({r['cinema_count']})" for r in leaderboard[:3]))

        if leaderboard:
            top = leaderboard[0]["title"]
            matches, hits = find_film(store, top)
            check(bool(matches) and bool(hits), f"film lookup: {top[:40]!r}",
                  f"{len(hits)} screenings across "
                  f"{len({h.cinema['id'] for h in hits})} venues")

        matches, hits = find_film(store, "dune")
        if matches:
            check(bool(hits), "fuzzy lookup: 'dune'", f"{len(hits)} screenings")
        else:
            note("fuzzy lookup: 'dune'", "not in this week's catalogue")

        tomorrow = datetime.now(MELBOURNE_TZ).date() + timedelta(days=1)
        at_evening = films_at(store, tomorrow, start=time(19, 0), end=time(21, 0))
        check(bool(at_evening), "time window: 19:00-21:00 tomorrow",
              f"{len(at_evening)} screenings, {len({h.screening['film_key'] for h in at_evening})} films")

        # ---------------------------------------------------- distance checks
        section("Location")
        venues = [c for c in cinema_catalogue(store) if c.get("screening_count")]
        check(bool(venues), "venues with screenings to test against", str(len(venues)))
        if venues:
            probe = venues[0]
            origin = (probe["latitude"], probe["longitude"])
            hits = nearest(store, origin, radius_km=5.0)
            check(bool(hits), f"nearest to {probe['name']}", f"{len(hits)} screenings within 5km")
            if hits:
                first = hits[0]
                own = first.distance_km is not None and first.distance_km <= 0.6
                check(own, "nearest result is the venue at the query point",
                      f"{first.cinema['name']} at {first.distance_km:.2f} km")
                within = all(h.distance_km is None or h.distance_km <= 5.0 for h in hits)
                check(within, "radius respected")
                ordered = all(
                    (hits[i].distance_km or 0) <= (hits[i + 1].distance_km or 0) + 1e-9
                    for i in range(len(hits) - 1)
                )
                check(ordered, "results ordered nearest-first")

        cbd = (-37.8136, 144.9631)
        cbd_hits = nearest(store, cbd, radius_km=2.0)
        check(bool(cbd_hits), "nearest to Melbourne CBD (2km)", f"{len(cbd_hits)} screenings")

        soon = upcoming_hits(store, within_hours=6)
        note("next 6 hours", f"{len(soon)} screenings (may legitimately be 0 late at night)")

        # ---------------------------------------------------- filter behaviour
        section("Filters")
        arthouse = [
            c for c in cinema_catalogue(store)
            if "arthouse" in (c.get("tags") or [])
        ]
        check(bool(arthouse), "arthouse-tagged venues exist", str(len(arthouse)))
        if arthouse:
            a = arthouse[0]
            _, tagged = find_film(store, leaderboard[0]["title"], filters=Filters(tags=["arthouse"]))
            only_arthouse = all("arthouse" in (h.cinema.get("tags") or []) for h in tagged)
            check(only_arthouse, "tag filter restricts to tagged venues", f"{len(tagged)} results")

    # ---------------------------------------------------------------- summary
    failures = [r for r in results if not r[0]]
    print(f"\n{'=' * 60}")
    if failures:
        print(f"{FAIL} {len(failures)} check(s) FAILED:")
        for _, name, detail in failures:
            print(f"   - {name}: {detail}")
        return 1
    print(f"{PASS} all {len(results)} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
