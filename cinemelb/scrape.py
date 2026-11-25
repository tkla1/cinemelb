"""Refresh orchestrator.

Ties the registry, the adapters and the store together:

    registry (data/cinemas.json)
        -> group venues by adapter
        -> adapter.fetch_many(...)          (one call per platform where possible)
        -> normalise + de-duplicate
        -> store.upsert_films / replace_screenings
        -> scrape_runs log

A refresh is *per venue* authoritative: whatever a venue's source says is
live replaces that venue's future screenings.  A platform that fails leaves
the previous rows alone rather than blanking them, so a transient network
error cannot silently empty the user's "what's on tonight".
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .adapters import get_adapter
from .config import REGISTRY_PATH
from .http import Http
from .models import MELBOURNE_TZ, Cinema, Film, Screening
from .store import Store


@dataclass(slots=True)
class VenueReport:
    cinema_id: str
    source: str
    ok: bool
    screenings: int = 0
    films: int = 0
    note: str = ""
    seconds: float = 0.0


@dataclass(slots=True)
class RefreshReport:
    started: datetime
    venues: list[VenueReport] = field(default_factory=list)
    total_screenings: int = 0

    def ok_count(self) -> int:
        return sum(1 for v in self.venues if v.ok)

    def failed(self) -> list[VenueReport]:
        return [v for v in self.venues if not v.ok]

    def summary(self) -> str:
        return (
            f"{self.ok_count()}/{len(self.venues)} venues, "
            f"{self.total_screenings} screenings"
        )


def load_registry(path: Path | None = None, *, include_inactive: bool = False) -> list[dict]:
    """Registry rows, active venues only unless ``include_inactive``.

    The default is what a refresh should scrape.  ``include_inactive`` exists so
    the store can be kept in sync: the registry's ``active`` flag has to reach
    the database, otherwise a venue marked dormant stays visible in
    ``cinemelb cinemas`` forever because the row was never updated.
    """
    p = Path(path or REGISTRY_PATH)
    if not p.exists():
        raise FileNotFoundError(
            f"registry not found at {p} — run scripts/harvest_registry.py first"
        )
    rows = json.loads(p.read_text(encoding="utf-8"))
    if include_inactive:
        return rows
    return [r for r in rows if r.get("active", True)]


def apply_overrides(rows: list[dict]) -> int:
    """Merge ``data/overrides/*.json`` into registry rows.

    Sub-adapter metadata (Veezi site tokens, numeric venue ids for a platform
    whose list API is blocked) lives in these files so that re-harvesting the
    registry — which rewrites it from scratch — cannot lose it.
    """
    override_dir = Path(REGISTRY_PATH).parent / "overrides"
    if not override_dir.exists():
        return 0
    merged = 0
    by_id = {r["id"]: r for r in rows}
    for path in sorted(override_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        for cinema_id, patch in data.items():
            row = by_id.get(cinema_id)
            if not row:
                continue
            ids = dict(row.get("source_ids") or {})
            for key, value in patch.items():
                if key == "source":
                    row["source"] = value
                elif key == "strategy":
                    ids["strategy"] = value
                else:
                    ids[key] = value
            row["source_ids"] = ids
            merged += 1
    return merged


def to_cinema(row: dict) -> Cinema:
    known = set(Cinema.__dataclass_fields__)          # type: ignore[attr-defined]
    return Cinema(**{k: v for k, v in row.items() if k in known})


def refresh(
    *,
    only: list[str] | None = None,
    sources: list[str] | None = None,
    registry_path: Path | None = None,
    db_path: Path | str | None = None,
    ttl: int = 900,
    fresh: bool = False,
    prune: bool = True,
    progress=None,
) -> RefreshReport:
    """Fetch sessions for every (selected) venue and write them to the store."""
    started = datetime.now(timezone.utc)
    # ``all_rows`` includes inactive venues so the store's ``active`` flag stays
    # in sync with the registry; ``rows`` is what actually gets scraped.
    all_rows = load_registry(registry_path, include_inactive=True)
    apply_overrides(all_rows)
    rows = [r for r in all_rows if r.get("active", True)]

    if only:
        def matches(row: dict) -> bool:
            haystack = {
                row["id"].lower(),
                (row.get("suburb") or "").lower(),
                (row.get("name") or "").lower(),
            }
            return any(w.lower() in h or h in w.lower() for w in only for h in haystack if h)

        rows = [r for r in rows if matches(r)]
    if sources:
        rows = [r for r in rows if r.get("source") in set(sources)]

    by_source: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_source[row.get("source") or "generic_html"].append(row)

    http = Http(ttl=ttl)
    http.force_refresh = fresh
    report = RefreshReport(started=started)

    with Store(db_path) as store:
        # Always store the *whole* registry, even when refreshing a subset:
        # `cinemelb cinemas` should list every venue from day one, and a
        # targeted refresh must not make the other 40 venues vanish.
        store.upsert_cinemas([to_cinema(r) for r in all_rows])

        # Platforms are independent of each other, so fetch them concurrently.
        # SQLite writes stay on this thread; only the network work is pooled.
        sourced = [
            (source, group, get_adapter(source))
            for source, group in sorted(by_source.items())
        ]
        fetched: dict[str, tuple[dict[str, AdapterOutput] | None, str, float]] = {}

        def run_platform(item) -> tuple[str, dict[str, AdapterOutput] | None, str, float]:
            source, group, adapter = item
            if adapter is None:
                return source, None, f"no adapter for source {source!r}", 0.0
            t0 = time.monotonic()
            try:
                results = adapter.fetch_many(group, http)
            except Exception as exc:                             # noqa: BLE001
                return source, None, f"adapter crashed: {exc}", time.monotonic() - t0
            return source, results, "", time.monotonic() - t0

        usable = [item for item in sourced if item[2] is not None]
        with ThreadPoolExecutor(max_workers=min(6, max(1, len(usable)))) as pool:
            for source, results, err, secs in pool.map(run_platform, sourced):
                fetched[source] = (results, err, secs)

        for source, group in sorted(by_source.items()):
            results, err, elapsed = fetched[source]
            if results is None:
                for row in group:
                    report.venues.append(VenueReport(row["id"], source, False, note=err))
                    store.log_run(source=source, cinema_id=row["id"], ok=False, detail=err)
                continue

            # one shared film catalogue per platform call
            films: dict[str, Film] = {}
            for out in results.values():
                for f in out.films:
                    films.setdefault(f.key, f)
            if films:
                store.upsert_films(films.values())

            for row in group:
                cid = row["id"]
                out = results.get(cid)
                if out is None:
                    note = "adapter returned nothing for this venue"
                    report.venues.append(VenueReport(cid, source, False, note=note))
                    store.log_run(source=source, cinema_id=cid, ok=False, detail=note)
                    continue

                ok = bool(out.screenings) or not out.note
                if out.screenings:
                    # Some feeds list a session twice (e.g. once per sales
                    # channel).  Collapse identical rows rather than showing
                    # the user the same 7pm screening twice.
                    unique = dedupe_identical(out.screenings)
                    store.replace_screenings(cid, unique)
                    report.total_screenings += len(unique)
                    out.screenings = unique
                elif ok:
                    # A *successful* fetch that found nothing means the venue
                    # genuinely has no upcoming sessions.  Clear the previous
                    # rows, or the tool keeps advertising screenings that are
                    # no longer on sale.
                    store.replace_screenings(cid, [])
                else:
                    # The fetch failed.  Keep this adapter's previous rows — a
                    # transient error must not blank the schedule — but drop
                    # rows written by a different adapter, which belong to a
                    # platform that no longer owns this venue.
                    dropped = store.drop_foreign_screenings(cid, source)
                    if dropped:
                        out.note = (out.note + f" ({dropped} stale rows from another "
                                    "adapter removed)").strip()
                report.venues.append(
                    VenueReport(
                        cinema_id=cid,
                        source=source,
                        ok=ok,
                        screenings=len(out.screenings),
                        films=len(out.films),
                        note=out.note,
                        seconds=round(elapsed / max(1, len(group)), 2),
                    )
                )
                store.log_run(
                    source=source, cinema_id=cid, ok=ok, detail=out.note,
                    n_screenings=len(out.screenings),
                )
                if progress:
                    progress(report.venues[-1])

        if prune:
            store.prune_before(datetime.now(MELBOURNE_TZ).date() - timedelta(days=1))
            # only safe once every venue has been revisited; a partial refresh
            # would otherwise delete films whose screenings come from the
            # platforms that were skipped this time
            if not only and not sources:
                store.prune_orphan_films()

    return report


def dedupe_identical(screenings: list[Screening]) -> list[Screening]:
    """Collapse the same film/time/screen reported twice (e.g. merged feeds)."""
    seen: set[tuple] = set()
    out: list[Screening] = []
    for s in screenings:
        key = (s.cinema_id, s.film_key, s.local_date, s.local_time, s.screen)
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out
