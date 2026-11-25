"""The query engine — the user-facing half of the tool.

Three shapes of question, plus the filtering/location plumbing they share:

* **"Where is this film showing?"**  — :func:`find_film`
* **"What's on at 8pm Friday?"**     — :func:`films_at`
* **"What's near me?"**              — :func:`nearest`

Every result is a :class:`Hit` carrying the cinema, the exact screening, and
(if an origin was supplied) the straight-line distance and a rough travel
estimate.  Ranking is always "soonest first" unless a location was given, in
which case "nearest first" is what the user actually asked for.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from .geo import haversine_km, travel_minutes
from .models import MELBOURNE_TZ, film_key, normalise_title
from .store import Store


@dataclass(slots=True)
class Hit:
    cinema: dict
    screening: dict
    distance_km: float | None = None
    travel_min: int | None = None

    # -- presentation helpers ---------------------------------------------
    @property
    def when(self) -> str:
        s = self.screening
        d = date.fromisoformat(s["local_date"])
        return f"{d:%a %d %b} {s['local_time']}"

    def to_dict(self) -> dict:
        return {
            "cinema_id": self.cinema["id"],
            "cinema": self.cinema["name"],
            "suburb": self.cinema.get("suburb", ""),
            "chain": self.cinema.get("chain", ""),
            "date": self.screening["local_date"],
            "time": self.screening["local_time"],
            "film": self.screening["film_title"],
            "format": self.screening.get("format", ""),
            "screen": self.screening.get("screen", ""),
            "attributes": self.screening.get("attributes", []),
            "sold_out": self.screening.get("sold_out", False),
            "ticket_url": self.screening.get("ticket_url", ""),
            "distance_km": round(self.distance_km, 2) if self.distance_km is not None else None,
            "travel_min": self.travel_min,
        }


@dataclass(slots=True)
class Filters:
    """Everything that narrows a query.  All fields optional."""

    day: date | None = None
    day_to: date | None = None
    after: time | None = None
    before: time | None = None
    tags: list[str] = field(default_factory=list)      # cinema tags, AND semantics
    chains: list[str] = field(default_factory=list)
    formats: list[str] = field(default_factory=list)   # screening format, OR
    accessible: bool = False                           # CC or AD
    #: Sold-out sessions are retained in the database (that a screening is full
    #: is real information) but hidden by default — a result list full of
    #: sessions you cannot buy is worse than useless.
    include_sold_out: bool = False
    exclude_tags: list[str] = field(default_factory=list)
    max_distance_km: float | None = None
    limit: int = 200
    #: only venues considered "metro" Melbourne (default) or everywhere
    metro_only: bool = True


# --------------------------------------------------------------------------- #
# venue filtering / distance decoration
# --------------------------------------------------------------------------- #

def _cinema_index(store: Store) -> dict[str, dict]:
    return {c["id"]: c for c in store.cinemas()}


def _venue_allowed(cinema: dict, f: Filters) -> bool:
    tags = {t.lower() for t in cinema.get("tags") or []}
    if f.tags and not all(t.lower() in tags for t in f.tags):
        return False
    if f.exclude_tags and tags & {t.lower() for t in f.exclude_tags}:
        return False
    if f.chains:
        chain = (cinema.get("chain") or "").lower()
        if not any(c.lower() in chain for c in f.chains):
            return False
    if f.metro_only and cinema.get("metro") is False:
        return False
    return True


def _screen_allowed(s: dict, f: Filters) -> bool:
    if s.get("sold_out") and not f.include_sold_out:
        return False
    if f.formats:
        fmt = (s.get("format") or "").lower()
        if not any(x.lower() in fmt for x in f.formats):
            return False
    if f.accessible:
        attrs = " ".join(s.get("attributes") or []).lower()
        if "cc" not in attrs and "closed caption" not in attrs \
                and "ad" not in attrs and "audio description" not in attrs:
            return False
    return True


def _decorate(hits: list[Hit], origin: tuple[float, float] | None) -> list[Hit]:
    if not origin:
        return hits
    for h in hits:
        lat, lon = h.cinema.get("latitude"), h.cinema.get("longitude")
        if lat is None or lon is None:
            continue
        h.distance_km = haversine_km(origin[0], origin[1], float(lat), float(lon))
        h.travel_min = travel_minutes(h.distance_km)
    return hits


def _sort(hits: list[Hit], origin: tuple[float, float] | None, f: Filters) -> list[Hit]:
    if origin:
        # `distance_km or 1e9` would be wrong: 0.0 is falsy, so a venue at the
        # query point (distance 0) would sort last instead of first.
        hits.sort(key=lambda h: (
            h.distance_km is None,
            h.distance_km if h.distance_km is not None else 1e9,
            h.screening["local_date"], h.screening["local_time"],
        ))
    else:
        hits.sort(key=lambda h: (h.screening["local_date"], h.screening["local_time"],
                                 h.cinema["name"]))
    return hits


def _apply_distance_cap(hits: list[Hit], f: Filters) -> list[Hit]:
    if f.max_distance_km is None:
        return hits
    return [h for h in hits if h.distance_km is not None and h.distance_km <= f.max_distance_km]


# --------------------------------------------------------------------------- #
# film resolution
# --------------------------------------------------------------------------- #

def resolve_film(store: Store, text: str) -> list[tuple[str, str]]:
    """Map free text to ``[(film_key, title)]``, best match first.

    Exact normalised match wins; otherwise substring; otherwise a fuzzy
    similarity pass.  Chain titles disagree about articles and punctuation,
    so "hunger games" must find "The Hunger Games: Sunrise on the Reaping".
    """
    if not text or not text.strip():
        return []
    wanted = normalise_title(text)
    films = store.films()
    if not films:
        # fall back to titles that actually have screenings
        seen: dict[str, str] = {}
        for s in store.screenings():
            seen.setdefault(s["film_key"], s["film_title"])
        films = [{"key": k, "title": v} for k, v in seen.items()]

    # Popularity (how many sessions exist) is the tie-breaker that makes bare
    # title queries behave: "dune" must find "Dune: Part Three" showing at 20
    # venues, not "Dune - Re-issue" showing at one, even though the re-issue
    # is the shorter (and therefore superficially "closer") string match.
    popularity: dict[str, int] = {}
    for s in store.screenings():
        popularity[s["film_key"]] = popularity.get(s["film_key"], 0) + 1

    scored: list[tuple[int, int, int, str, str]] = []
    for film in films:
        title = film["title"]
        nt = normalise_title(title)
        if nt == wanted:
            tier = 0
        elif nt.startswith(wanted + " ") or nt.startswith(wanted + ":"):
            tier = 1
        elif wanted in nt:
            tier = 2
        else:
            ratio = difflib.SequenceMatcher(None, wanted, nt).ratio()
            if ratio < 0.62:
                continue
            tier = 3
        scored.append((tier, -popularity.get(film["key"], 0), len(nt), film["key"], title))
    scored.sort(key=lambda t: t[:4])

    # Collapse to distinct keys, keeping best score.
    out: list[tuple[str, str]] = []
    seen_keys: set[str] = set()
    for _tier, _pop, _len, key, title in scored:
        if key in seen_keys:
            continue
        seen_keys.add(key)
        out.append((key, title))
        if len(out) >= 8:
            break
    return out


# --------------------------------------------------------------------------- #
# the three queries
# --------------------------------------------------------------------------- #

def find_film(
    store: Store,
    film_text: str,
    *,
    filters: Filters | None = None,
    origin: tuple[float, float] | None = None,
    include_similar: bool = False,
) -> tuple[list[tuple[str, str]], list[Hit]]:
    """Where is this film showing?  Returns (matches, hits).

    Only the single best-matching title is queried by default.  Merging
    "Dune: Part Three" with "Dune - Re-issue" because both contain "dune"
    would produce a result set answering a question nobody asked; pass
    ``include_similar=True`` when the caller does want that.
    """
    f = filters or Filters()
    found = resolve_film(store, film_text)
    if not found:
        return [], []
    # Return only the titles actually queried, so a caller can trust that
    # `matched_films` describes the result set rather than the whole fuzzy
    # neighbourhood.
    matches = found if include_similar else found[:1]
    keys = [k for k, _ in matches]
    venues = _cinema_index(store)

    hits: list[Hit] = []
    for key in keys:
        for s in store.screenings(
            film_key=key,
            day=f.day,
            day_to=f.day_to,
            after=f.after.strftime("%H:%M") if f.after else None,
            before=f.before.strftime("%H:%M") if f.before else None,
        ):
            cinema = venues.get(s["cinema_id"])
            if not cinema or not _venue_allowed(cinema, f) or not _screen_allowed(s, f):
                continue
            hits.append(Hit(cinema=cinema, screening=s))

    _decorate(hits, origin)
    hits = _sort(hits, origin, f)
    hits = _apply_distance_cap(hits, f)
    return matches, hits[: f.limit]


def films_at(
    store: Store,
    day: date,
    *,
    start: time | None = None,
    end: time | None = None,
    filters: Filters | None = None,
    origin: tuple[float, float] | None = None,
) -> list[Hit]:
    """Everything starting in a window on a given day."""
    f = filters or Filters()
    f.day = day
    venues = _cinema_index(store)

    hits: list[Hit] = []
    for s in store.screenings(
        day=day,
        after=start.strftime("%H:%M") if start else None,
        before=end.strftime("%H:%M") if end else None,
    ):
        cinema = venues.get(s["cinema_id"])
        if not cinema or not _venue_allowed(cinema, f) or not _screen_allowed(s, f):
            continue
        hits.append(Hit(cinema=cinema, screening=s))

    _decorate(hits, origin)
    hits = _sort(hits, origin, f)
    return _apply_distance_cap(hits, f)[: f.limit]


def nearest(
    store: Store,
    origin: tuple[float, float],
    *,
    radius_km: float = 5.0,
    filters: Filters | None = None,
) -> list[Hit]:
    """Showings within ``radius_km`` of a point, nearest first."""
    f = filters or Filters()
    f.max_distance_km = radius_km
    venues = _cinema_index(store)
    # restrict to venues inside the radius before hitting the screening table
    near_ids: list[str] = []
    for cid, cinema in venues.items():
        lat, lon = cinema.get("latitude"), cinema.get("longitude")
        if lat is None or lon is None:
            continue
        if haversine_km(origin[0], origin[1], float(lat), float(lon)) <= radius_km:
            if _venue_allowed(cinema, f):
                near_ids.append(cid)
    if not near_ids:
        return []

    hits: list[Hit] = []
    for s in store.screenings(
        cinema_ids=near_ids,
        day=f.day,
        day_to=f.day_to,
        after=f.after.strftime("%H:%M") if f.after else None,
        before=f.before.strftime("%H:%M") if f.before else None,
    ):
        cinema = venues[s["cinema_id"]]
        if not _screen_allowed(s, f):
            continue
        hits.append(Hit(cinema=cinema, screening=s))

    _decorate(hits, origin)
    hits = _sort(hits, origin, f)
    return hits[: f.limit]


# --------------------------------------------------------------------------- #
# extras
# --------------------------------------------------------------------------- #

def upcoming_hits(
    store: Store,
    *,
    within_hours: float = 4.0,
    filters: Filters | None = None,
    origin: tuple[float, float] | None = None,
) -> list[Hit]:
    """"What can I see in the next few hours?" — the most useful default."""
    now = datetime.now(MELBOURNE_TZ)
    start = now.replace(tzinfo=None)
    end = start + timedelta(hours=within_hours)
    f = filters or Filters()
    venues = _cinema_index(store)

    hits: list[Hit] = []
    for day in {start.date(), end.date()}:
        for s in store.screenings(day=day):
            cinema = venues.get(s["cinema_id"])
            if not cinema or not _venue_allowed(cinema, f) or not _screen_allowed(s, f):
                continue
            local = datetime.fromisoformat(f"{s['local_date']}T{s['local_time']}")
            if start <= local <= end:
                hits.append(Hit(cinema=cinema, screening=s))

    _decorate(hits, origin)
    hits = _sort(hits, origin, f)
    return _apply_distance_cap(hits, f)[: f.limit]


def film_leaderboard(
    store: Store,
    *,
    day: date | None = None,
    metro_only: bool = True,
    limit: int = 25,
) -> list[dict]:
    """Films ranked by how many Melbourne venues are showing them."""
    venues = _cinema_index(store)
    counts: dict[str, dict] = {}
    for s in store.screenings(day=day):
        cinema = venues.get(s["cinema_id"])
        if not cinema:
            continue
        if metro_only and cinema.get("metro") is False:
            continue
        row = counts.setdefault(
            s["film_key"],
            {"film_key": s["film_key"], "title": s["film_title"],
             "cinemas": set(), "screenings": 0},
        )
        row["cinemas"].add(s["cinema_id"])
        row["screenings"] += 1
    out = sorted(counts.values(), key=lambda r: (-len(r["cinemas"]), -r["screenings"]))
    for r in out:
        r["cinema_count"] = len(r["cinemas"])
        r["cinemas"] = sorted(r["cinemas"])
    return out[:limit]


def cinema_catalogue(store: Store, *, metro_only: bool = True) -> list[dict]:
    venues = _cinema_index(store)
    counts: dict[str, int] = {}
    for s in store.screenings():
        counts[s["cinema_id"]] = counts.get(s["cinema_id"], 0) + 1
    rows = []
    for cid, c in venues.items():
        if metro_only and c.get("metro") is False:
            continue
        rows.append({**c, "screening_count": counts.get(cid, 0)})
    rows.sort(key=lambda r: (r.get("suburb") or "", r["name"]))
    return rows
