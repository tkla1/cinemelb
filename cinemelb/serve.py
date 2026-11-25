"""Local read-only web UI over the cinemelb database.

Why this exists
---------------
``cinemelb`` answers "what's on, where and when" from a terminal.  The same
query engine is behind this page, so the tool can be opened in a browser and
handed to somebody who does not live in a terminal.

How it is built, and why this way
---------------------------------
* **Server-side rendering, all state in the URL.**  Every view is an ordinary
  GET: the form submits its own query string and the server renders the whole
  page.  So there is no build step, no framework and no JavaScript at all on
  the search path, the back/forward buttons work, and ``?film=dune&near=brunswick``
  is a shareable link.  A ``fetch``-driven page would need this same server work
  *plus* a client-side state layer — this is the smaller thing that does the job.
* **Stdlib HTTP.**  ``http.server.ThreadingHTTPServer`` is enough for a
  single-user local tool; Flask would be a dependency added to serve one page.
* **One payload, two renderers.**  :func:`_build_payload` produces exactly one
  dict per request and both the HTML page and the JSON routes render *that*, so
  the API cannot drift from what the page shows.
* **Read only.**  The database is opened the way the CLI opens it and not one
  row is ever written; every statement is a SELECT.

What a bad request gets
-----------------------
A request the server can still answer usefully — an unparseable date, an
unknown suburb — comes back **200** with the problem in ``errors`` and the
filter simply not applied, because a typo in a URL should not be an exceptional
status when the rest of the query is still worth running.  Unknown *routes* are
404.  Only a genuine bug in the query engine produces a 500, and it is caught
per request so the server stays up.

Routes
------
``GET /``              the page — ``?view=search`` (default) ``| tonight | venues``
``GET /api/search``    JSON, same parameters as the page
``GET /api/tonight``   JSON, "what can I see in the next few hours"
``GET /api/venues``    JSON, the venue catalogue
``GET /favicon.ico``   204, so the browser stops asking

Parameters (all optional): ``film``, ``date`` (``today`` / ``tomorrow`` /
``fri`` / ``2026-11-27``), ``after`` and ``before`` (``19:00`` / ``7pm``),
``near`` (suburb, landmark or ``lat,lon``), ``radius`` (km), ``hours`` (for
tonight), and the switches ``arthouse``, ``accessible``, ``sold_out``.

Usage
-----
    cinemelb serve                  # wired through the CLI
    python -m cinemelb.serve        # or straight from the package
    serve(port=8787)                # or from Python
"""

from __future__ import annotations

import html
import json
import math
import sys
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit

from . import query as Q
from .geo import format_distance, geocode
from .models import MELBOURNE_TZ, normalise_title
from .store import Store

#: Radius choices offered by the page.  10 km is the default: far enough to
#: cover the inner suburbs from anywhere in them, close enough that "nearest"
#: still means something.  (The CLI's --radius defaults vary per command; this
#: page has one radius control, so it needs one sensible number.)
_RADIUS_CHOICES = (1, 2, 5, 10, 25, 50)
_RADIUS_DEFAULT = 10.0
_HOURS_CHOICES = (2, 3, 4, 6, 8)
_HOURS_DEFAULT = 4.0

#: Engine caps, per view: the page groups sessions per venue and shows at most
#: :data:`_SESSIONS_PER_VENUE` of them, so a generous cap costs little — what it
#: buys is a complete "later today / later this week" list for every venue.
_SEARCH_LIMIT = 200
_TONIGHT_LIMIT = 200
_SESSIONS_PER_VENUE = 12

_VIEWS = ("search", "tonight", "venues")

#: Route -> view.  The JSON API is the page minus the HTML, so routing a path
#: to a view is the whole difference between the two.
_API_ROUTES = {"/api/search": "search", "/api/tonight": "tonight", "/api/venues": "venues"}


# --------------------------------------------------------------------------- #
# parsing — same rules as the CLI, reimplemented here because these are input
# syntax decisions, not CLI mechanics
# --------------------------------------------------------------------------- #

def _first(params: Mapping[str, list[str]], name: str) -> str:
    values = params.get(name)
    return values[0].strip() if values else ""


def _parse_flag(params: Mapping[str, list[str]], name: str) -> bool:
    # Checkboxes submit "on" when ticked; anything unrecognised means "off".
    return _first(params, name).lower() in ("1", "on", "true", "yes")


def _parse_day(value: str) -> date | None:
    """Accept ``YYYY-MM-DD``, ``today``, ``tomorrow``, ``fri``, ``+2``."""
    if not value:
        return None
    v = value.strip().lower()
    today = datetime.now(MELBOURNE_TZ).date()
    if v in ("today", "now"):
        return today
    if v in ("tomorrow", "tmr"):
        return today + timedelta(days=1)
    if v.startswith("+"):
        try:
            # OverflowError, not ValueError: "+999999999999" is a well-formed
            # integer that simply is not a date.
            return today + timedelta(days=int(v[1:]))
        except (ValueError, OverflowError):
            return None
    if len(v) >= 3 and v.isalpha():
        weekdays = ("monday", "tuesday", "wednesday", "thursday",
                    "friday", "saturday", "sunday")
        for i, name in enumerate(weekdays):
            if name.startswith(v):
                # The *next* occurrence, today included: "fri" on a Friday is
                # today, which is what a person means at 6pm on a Friday.
                return today + timedelta(days=(i - today.weekday()) % 7)
    try:
        return date.fromisoformat(v)
    except ValueError:
        return None


def _parse_time(value: str) -> time | None:
    """Accept ``20``, ``20:30``, ``8pm``, ``8:30pm``, ``2030``."""
    if not value:
        return None
    v = value.strip().lower().replace(".", "")
    pm, am = v.endswith("pm"), v.endswith("am")
    if pm or am:
        v = v[:-2].strip()
    try:
        if ":" in v:
            hh, mm = v.split(":", 1)
            h, m = int(hh), int(mm)
        elif len(v) == 4 and v.isdigit():
            h, m = int(v[:2]), int(v[2:])
        else:
            h, m = int(v), 0
    except ValueError:
        return None
    if pm and h < 12:
        h += 12
    if am and h == 12:
        h = 0
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return time(h, m)


def _parse_radius(value: str) -> float:
    """Positive kilometres, capped at 500.

    Silently clamped rather than reported: the page offers a fixed list of
    radii, so only a hand-made URL can be out of range, and "nearest 500 km of
    Melbourne" is a sane reading of an absurd number.
    """
    try:
        km = float(value)
    except (TypeError, ValueError):
        return _RADIUS_DEFAULT
    if not math.isfinite(km) or km <= 0:
        return _RADIUS_DEFAULT
    return min(km, 500.0)


def _parse_hours(value: str) -> float:
    try:
        hours = float(value)
    except (TypeError, ValueError):
        return _HOURS_DEFAULT
    if not math.isfinite(hours) or hours <= 0:
        return _HOURS_DEFAULT
    return min(hours, 24.0)


# --------------------------------------------------------------------------- #
# the request
# --------------------------------------------------------------------------- #

@dataclass(slots=True)
class _Query:
    """One request, decoded into the terms the query engine speaks.

    It holds *parsed* values, never the raw text: the form is re-rendered from
    this object, so what the page shows in its boxes is exactly what the server
    ran.  Typing ``fri`` and seeing ``2026-11-27`` afterwards is the point — the
    box, the URL and the results can never disagree.
    """

    view: str = "search"
    film: str = ""
    near: str = ""
    day: date | None = None
    after: time | None = None
    before: time | None = None
    radius_km: float = _RADIUS_DEFAULT
    hours: float = _HOURS_DEFAULT
    arthouse: bool = False
    accessible: bool = False
    sold_out: bool = False
    errors: list[str] = field(default_factory=list)

    @classmethod
    def parse(cls, params: Mapping[str, list[str]], *, view: str | None = None) -> "_Query":
        q = cls()
        # An API route fixes the view; only the page may choose with ?view=.
        chosen = view or _first(params, "view").lower()
        q.view = chosen if chosen in _VIEWS else "search"
        q.film = _first(params, "film")
        q.near = _first(params, "near")
        q.radius_km = _parse_radius(_first(params, "radius"))
        q.hours = _parse_hours(_first(params, "hours"))
        q.arthouse = _parse_flag(params, "arthouse")
        q.accessible = _parse_flag(params, "accessible")
        q.sold_out = _parse_flag(params, "sold_out")

        # Only the search view takes a date or a time window, so only it parses
        # them: a stray ?date= on /api/venues should not invent an error about
        # a filter that view never offers.
        if q.view == "search":
            raw_day = _first(params, "date")
            q.day = _parse_day(raw_day)
            if raw_day and q.day is None:
                q.errors.append(
                    f"Could not read the date {raw_day!r}, so the date filter "
                    "was ignored. Try today, tomorrow, fri or 2026-11-27."
                )
            for name in ("after", "before"):
                raw = _first(params, name)
                parsed = _parse_time(raw)
                if raw and parsed is None:
                    q.errors.append(
                        f"Could not read the time {raw!r}, so the "
                        f"{name} filter was ignored. Try 19:00 or 7pm."
                    )
                setattr(q, name, parsed)
        return q

    @property
    def searched(self) -> bool:
        """Does this request ask for anything?

        A bare ``/`` asks for nothing, so the landing page must not dump the
        whole database; any filter or a non-search tab means "run it".
        """
        if self.view != "search":
            return True
        return bool(self.film or self.near or self.day or self.after
                    or self.before or self.arthouse or self.accessible or self.sold_out)

    def to_dict(self) -> dict:
        return {
            "view": self.view,
            "film": self.film,
            "date": self.day.isoformat() if self.day else "",
            "after": self.after.strftime("%H:%M") if self.after else "",
            "before": self.before.strftime("%H:%M") if self.before else "",
            "near": self.near,
            "radius_km": self.radius_km,
            "hours": self.hours,
            "arthouse": self.arthouse,
            "accessible": self.accessible,
            "sold_out": self.sold_out,
        }


# --------------------------------------------------------------------------- #
# running the query
# --------------------------------------------------------------------------- #

def _origin(q: _Query, errors: list[str]) -> tuple[float, float] | None:
    """Resolve the location box, reporting an unknown place instead of failing.

    A suburb the geocoder has never heard of is a warning, not an error: the
    film and date the user asked for are still worth answering, just not
    distance-ranked.
    """
    if not q.near:
        return None
    coords = geocode(q.near)
    if coords is None:
        errors.append(
            f"Could not resolve the location {q.near!r}, so results are not "
            "ordered by distance. Try a Melbourne suburb, a landmark, or "
            "coordinates like -37.81,144.96."
        )
        return None
    return coords


def _filters(q: _Query, *, limit: int, radius_km: float | None) -> Q.Filters:
    """The filters every view shares.

    ``max_distance_km`` is set only when an origin resolved, never merely
    because a radius was asked for: distances are measured *from* the origin,
    so with no origin every hit is "unknown distance" and the cap would
    silently discard the entire result list.
    """
    f = Q.Filters(
        tags=["arthouse"] if q.arthouse else [],
        accessible=q.accessible,
        include_sold_out=q.sold_out,
        limit=limit,
    )
    if radius_km is not None:
        f.max_distance_km = radius_km
    return f


def _search_filters(q: _Query, limit: int, radius_km: float | None) -> Q.Filters:
    f = _filters(q, limit=limit, radius_km=radius_km)
    f.day, f.after, f.before = q.day, q.after, q.before
    return f


def _browse(store: Store, q: _Query, limit: int, radius_km: float | None,
            origin: tuple[float, float] | None) -> list[Q.Hit]:
    """No film given — show what the other filters leave standing.

    Mirrors ``cinemelb whats-on``: an origin means "everything within reach"
    (:func:`query.nearest`), otherwise "everything on the day"
    (:func:`query.films_at`).
    """
    f = _search_filters(q, limit, radius_km)
    if origin:
        return Q.nearest(store, origin, radius_km=q.radius_km, filters=f)
    day = q.day or datetime.now(MELBOURNE_TZ).date()
    return Q.films_at(store, day, start=q.after, end=q.before, filters=f)


def _popular(store: Store) -> list[dict]:
    """The widest-released films, as a way in when a search finds nothing."""
    return [
        {"title": row["title"], "cinema_count": row["cinema_count"]}
        for row in Q.film_leaderboard(store, limit=12)
    ]


def _session(hit: Q.Hit) -> dict:
    """Flatten a hit for the page and the API, adding what the table needs."""
    d = hit.to_dict()
    d["day"] = f"{date.fromisoformat(d['date']):%a}"
    # The accessible filter means "captioned or audio described" (see
    # query._screen_allowed); the same pair drives the badge, so a filtered
    # list is visibly filtered instead of silently smaller.
    joined = " ".join(d["attributes"]).lower()
    d["cc"] = "cc" in joined or "closed caption" in joined
    d["ad"] = "ad" in joined or "audio description" in joined
    return d


def _group_by_cinema(hits: list[Q.Hit], sessions: list[dict]) -> list[dict]:
    """One card per venue, in the engine's order.

    The order is meaningful: nearest-first when an origin was supplied, soonest
    first otherwise.  Sessions are appended in that same order, so the first
    chip in a card is always the one to aim for.
    """
    groups: list[dict] = []
    index: dict[str, dict] = {}
    for hit, session in zip(hits, sessions):
        cinema = hit.cinema
        group = index.get(cinema["id"])
        if group is None:
            group = {
                "cinema": {
                    "id": cinema["id"],
                    "name": cinema["name"],
                    "suburb": cinema.get("suburb") or "",
                    "chain": cinema.get("chain") or "",
                    "tags": cinema.get("tags") or [],
                },
                "distance_km": session["distance_km"],
                "travel_min": hit.travel_min,
                "sessions": [],
            }
            index[cinema["id"]] = group
            groups.append(group)
        group["sessions"].append(session)

    for group in groups:
        members = group["sessions"]
        # Derived before the display cap, so the "… N more" count and the
        # multi-film/dated switches describe every session at the venue, not
        # just the twelve that fit on a card.
        group["film"] = members[0]["film"]
        group["multi_film"] = len({s["film"] for s in members}) > 1
        group["dated"] = len({s["date"] for s in members}) > 1
        group["total"] = len(members)
        group["hidden"] = max(0, group["total"] - _SESSIONS_PER_VENUE)
        group["sessions"] = members[:_SESSIONS_PER_VENUE]
    return groups


def _relative_age(last: datetime | None) -> str | None:
    """How stale is the data?  ``None`` when nothing has ever been scraped.

    A showtimes page that hides its age is worse than one that admits it: the
    failure mode is driving to a cinema.
    """
    if last is None:
        return None
    minutes = (datetime.now(timezone.utc) - last).total_seconds() / 60
    if minutes < 1:
        return "just now"
    if minutes < 90:
        return f"{minutes:.0f} min ago"
    hours = minutes / 60
    if hours < 36:
        return f"{hours:.0f} h ago"
    return f"{hours / 24:.0f} days ago"


def _coverage(days: list[str]) -> str:
    if not days:
        return ""
    first = f"{date.fromisoformat(days[0]):%d %b %Y}"
    last = f"{date.fromisoformat(days[-1]):%d %b %Y}"
    return first if first == last else f"{first} – {last}"


def _build_payload(store: Store, q: _Query) -> dict:
    """Everything one request produces, for both the page and the API."""
    stats = store.stats()
    payload: dict = {
        "view": q.view,
        "query": q.to_dict(),
        "errors": list(q.errors),
        "searched": q.searched,
        "empty_db": stats["screenings"] == 0,
        "stats": {
            "venues": stats["cinemas"],
            "films": stats["films"],
            "screenings": stats["screenings"],
        },
        "freshness": _relative_age(store.last_refresh()),
        "coverage": _coverage(stats["days"]),
        "origin": None,
        "matched_films": [],
        "alternatives": [],
        "results": [],
        "groups": [],
        "films": [],
        "count": 0,
        "truncated": False,
    }
    if payload["empty_db"]:
        # No rows to query and nothing to explain beyond "refresh": return now
        # so no view can start a pointless full-table scan.
        return payload

    if q.view == "venues":
        # The catalogue needs no location and no scoring, so it is answered
        # before anything geocodes or ranks.
        payload["results"] = Q.cinema_catalogue(store)
        payload["count"] = len(payload["results"])
        return payload

    origin = _origin(q, payload["errors"])
    if origin:
        payload["origin"] = {"lat": origin[0], "lon": origin[1], "radius_km": q.radius_km}
    radius_km = q.radius_km if origin else None

    hits: list[Q.Hit] = []
    if q.view == "tonight":
        # upcoming_hits computes its own window from "now", so no day/after/
        # before is passed: those belong to a fixed-date view, not this one.
        filters = _filters(q, limit=_TONIGHT_LIMIT, radius_km=radius_km)
        hits = Q.upcoming_hits(
            store, within_hours=q.hours, filters=filters, origin=origin
        )
        payload["truncated"] = len(hits) >= _TONIGHT_LIMIT
    elif not q.searched:
        # A bare landing page: show a way in rather than the whole database.
        payload["films"] = _popular(store)
        return payload
    elif q.film:
        # A second resolve keeps the neighbouring titles to hand: the top fuzzy
        # hit is not always the one meant ("dune" resolves to the 1984 film, not
        # to "Dune: Part Three"), and a plain-HTML page has no autocomplete to
        # fall back on.  The extra pass is ~75 ms locally — invisible, and worth
        # the escape hatch when the match is a different film altogether.
        found = Q.resolve_film(store, q.film)
        matches, hits = Q.find_film(
            store, q.film, filters=_search_filters(q, _SEARCH_LIMIT, radius_km),
            origin=origin,
        )
        payload["matched_films"] = [{"key": k, "title": t} for k, t in matches]
        # Keep only candidates that are the same title in another cut or
        # re-issue: "Dune" should offer "Dune: Part Three", but a film whose
        # name merely looks similar ("focker" scored "Wicker") is a different
        # film, and offering it is noise rather than a correction.
        wanted = normalise_title(q.film)
        payload["alternatives"] = [
            {"key": k, "title": t} for k, t in found[1:4]
            if wanted and (wanted in normalise_title(t) or normalise_title(t) in wanted)
        ]
        payload["truncated"] = len(hits) >= _SEARCH_LIMIT
    else:
        hits = _browse(store, q, _SEARCH_LIMIT, radius_km, origin)
        payload["truncated"] = len(hits) >= _SEARCH_LIMIT

    sessions = [_session(h) for h in hits]
    payload["results"] = sessions
    payload["groups"] = _group_by_cinema(hits, sessions)
    payload["count"] = len(sessions)
    if not sessions:
        payload["films"] = _popular(store)
    return payload


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #

def _esc(value: object) -> str:
    """Escape for HTML text *and* quoted attributes.

    Film titles, venue names, suburbs and attributes are all scraped from third
    party sites, so nothing here is trusted to be text.
    """
    return html.escape("" if value is None else str(value), quote=True)


def _safe_href(url: str) -> str:
    """Escaped ``http(s)`` URL, or ``""``.

    Ticket and website links come out of the database, which came off the
    internet; a ``javascript:`` href would run in this page's origin, so the
    scheme is checked here rather than trusted.
    """
    candidate = (url or "").strip()
    if candidate.startswith(("http://", "https://")):
        return _esc(candidate)
    return ""


def _query_string(q: dict, *, view: str) -> str:
    """Canonical query string for a tab link.

    The search state is carried on every tab, not just its own: the tabs are
    navigation, and clicking Venues and back should not lose the query.  Each
    view ignores the parameters it does not use.
    """
    pairs: list[tuple[str, str]] = [("view", view)]
    for name in ("film", "date", "after", "before"):
        if q[name]:
            pairs.append((name, q[name]))
    if q["near"]:
        pairs.append(("near", q["near"]))
        pairs.append(("radius", f"{q['radius_km']:g}"))
    for name in ("arthouse", "accessible", "sold_out"):
        if q[name]:
            pairs.append((name, "1"))
    if view == "tonight":
        pairs.append(("hours", f"{q['hours']:g}"))
    return "/?" + urlencode(pairs)


def _link(view: str, **params: str) -> str:
    return _esc("/?" + urlencode({"view": view, **params}))


_CSS = """
:root{color-scheme:dark;--bg:#12141a;--panel:#171a21;--card:#1c2029;--line:#262a35;
--text:#e8eaf0;--muted:#8b93a5;--violet:#9d82ff;--green:#4ade80;--amber:#ffd479}
*,*::before,*::after{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
a{color:#a9c2ff}a:hover{color:#cadbff}
.wrap{max-width:1000px;margin:0 auto;padding:20px 16px 64px}
header.site h1{margin:0;font-size:18px;font-weight:650;letter-spacing:-.01em}
header.site h1 span{color:var(--violet)}
.sub{margin:4px 0 0;color:var(--muted);font-size:12.5px}
nav{display:flex;flex-wrap:wrap;gap:6px;margin:18px 0 16px;
border-bottom:1px solid var(--line);padding-bottom:12px}
nav a{padding:6px 14px;border-radius:999px;border:1px solid var(--line);
text-decoration:none;color:var(--muted);font-size:13px}
nav a:hover{color:var(--text);border-color:#3a4152}
nav a.on{background:#2a2340;border-color:#4b3a7a;color:#dbcaff}
form{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:15px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(165px,1fr));gap:11px}
label.cap{display:block;font-size:11px;text-transform:uppercase;letter-spacing:.06em;
color:var(--muted);margin-bottom:5px}
input[type=text],select{width:100%;background:#0e1015;border:1px solid var(--line);
border-radius:8px;color:var(--text);padding:9px 10px;font:inherit;font-size:14px}
input[type=text]::placeholder{color:#5c6373}
input[type=text]:focus,select:focus{outline:2px solid #4b3a7a;outline-offset:1px;
border-color:#4b3a7a}
.toggles{display:flex;flex-wrap:wrap;gap:8px;margin-top:13px}
.toggles label{display:flex;align-items:center;gap:7px;border:1px solid var(--line);
border-radius:999px;padding:6px 13px;font-size:13px;color:var(--muted);cursor:pointer}
.toggles label:has(input:checked){border-color:#4b3a7a;background:#221d33;color:#dbcaff}
.toggles input{accent-color:#7c5cff;margin:0}
.actions{display:flex;flex-wrap:wrap;align-items:center;gap:12px;margin-top:15px}
button{background:#6d4fe0;color:#fff;border:0;border-radius:8px;padding:10px 20px;
font:inherit;font-weight:600;cursor:pointer}
button:hover{background:#7c5cff}
.hint{color:var(--muted);font-size:12px}
.notice{margin:14px 0 0;padding:10px 13px;border-radius:9px;background:#2a1c1c;
border:1px solid #57302f;color:#ffbcb8;font-size:13.5px}
.head{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:baseline;
gap:8px;margin:24px 0 11px}
.head h2{margin:0;font-size:16px;font-weight:650}
.head span{color:var(--muted);font-size:12.5px}
.alts{margin:0 0 12px;font-size:13px;color:var(--muted)}
.alts a{display:inline-block;margin-left:6px;padding:2px 9px;border-radius:999px;
background:#232838;border:1px solid var(--line);text-decoration:none;color:#d5dcea;
font-size:12.5px}
.alts a:hover{border-color:#4b3a7a;background:#2a2438}
.card{background:var(--card);border:1px solid var(--line);border-radius:11px;
padding:13px 15px;margin-bottom:11px}
.card header{display:flex;justify-content:space-between;align-items:baseline;gap:10px}
.card h2{margin:0;font-size:14.5px;font-weight:600}
.dist{color:var(--violet);font-size:12.5px;font-weight:600;white-space:nowrap}
.meta{color:var(--muted);font-size:11.5px;margin-top:2px}
.filmline{margin:7px 0 0;font-size:13px;color:#cdd5e6}
.sessions{list-style:none;margin:8px 0 0;padding:0;display:flex;flex-wrap:wrap;gap:6px}
.sessions li{display:flex;align-items:center;gap:6px;background:#242a36;
border-radius:7px;padding:5px 9px;font-size:13px}
.t{font-variant-numeric:tabular-nums;font-weight:600}
.film{color:#c3cbdd;font-size:12.5px}
.chip{font-size:10.5px;border-radius:4px;padding:1px 5px;background:#2f3543;
color:#c3cbdd;white-space:nowrap}
.chip.fmt{background:#302b1c;color:var(--amber)}
.chip.cc{background:#1c2a22;color:var(--green)}
.chip.sold{background:#3a1f1f;color:#ff9c9c}
.chip.book{background:#243055;color:#bdd0ff;text-decoration:none}
.chip.book:hover{background:#2f3f6e;color:#e2ecff}
.tags{margin:8px 0 0;display:flex;flex-wrap:wrap;gap:6px}
.more{margin:9px 0 0;color:var(--muted);font-size:11.5px}
.empty{border:1px dashed var(--line);border-radius:11px;padding:32px 20px;
text-align:center;color:var(--muted)}
.empty b{display:block;color:var(--text);font-size:15px;margin-bottom:5px}
.intro{margin:20px 0 0;color:#cdd5e6;font-size:14px}
.intro p{margin:0}
.popular-wrap h3{margin:24px 0 10px;font-size:12px;text-transform:uppercase;
letter-spacing:.07em;color:var(--muted);font-weight:600}
.popular{display:flex;flex-wrap:wrap;gap:8px}
.popular a{background:#232838;border:1px solid var(--line);border-radius:999px;
padding:7px 13px;text-decoration:none;color:#dfe4f0;font-size:13px}
.popular a:hover{border-color:#4b3a7a;background:#2a2438}
.popular small{color:var(--muted);font-size:11px}
footer{margin-top:34px;padding-top:16px;border-top:1px solid var(--line);
color:var(--muted);font-size:11.5px}
code{background:#232838;border-radius:4px;padding:1px 5px;font-size:11.5px}
@media (max-width:560px){.wrap{padding:16px 12px 48px}header.site h1{font-size:16px}
.grid{grid-template-columns:1fr}.head{margin-top:20px}}
"""


def _field(name: str, label: str, value: str, placeholder: str = "",
           *, autofocus: bool = False) -> str:
    focus = " autofocus" if autofocus else ""
    return (
        f'<div><label class="cap" for="f-{name}">{_esc(label)}</label>'
        f'<input id="f-{name}" type="text" name="{name}" value="{_esc(value)}"'
        f' placeholder="{_esc(placeholder)}"{focus}></div>'
    )


def _select(name: str, label: str, value: str, choices: list[tuple[str, str]]) -> str:
    options = "".join(
        f'<option value="{_esc(opt)}"{" selected" if opt == value else ""}>{_esc(text)}</option>'
        for opt, text in choices
    )
    return (
        f'<div><label class="cap" for="f-{name}">{_esc(label)}</label>'
        f'<select id="f-{name}" name="{name}">{options}</select></div>'
    )


def _toggle(name: str, label: str, checked: bool) -> str:
    mark = " checked" if checked else ""
    return (f'<label><input type="checkbox" name="{name}" value="1"{mark}>'
            f'<span>{_esc(label)}</span></label>')


def _form(q: dict) -> str:
    """The one input surface.  It edits the *current* tab's query, so
    submitting never silently switches views."""
    view = q["view"]
    if view == "venues":
        return ""
    fields: list[str] = []
    if view == "search":
        fields.append(_field("film", "Film title", q["film"],
                             "e.g. dune, the fox", autofocus=True))
        fields.append(_field("date", "Date", q["date"], "today · fri · 2026-11-27"))
        fields.append(_field("after", "Starts after", q["after"], "19:00"))
        fields.append(_field("before", "Starts before", q["before"], "22:30"))
    else:
        fields.append(_select(
            "hours", "Within", f"{q['hours']:g}",
            [(f"{h:g}", f"next {h:g} hours") for h in _HOURS_CHOICES],
        ))
    fields.append(_field("near", "Location", q["near"],
                         "brunswick · fed square · -37.81,144.96"))
    fields.append(_select(
        "radius", "Radius", f"{q['radius_km']:g}",
        [(f"{r:g}", f"{r:g} km") for r in _RADIUS_CHOICES],
    ))
    toggles = "".join((
        _toggle("arthouse", "Arthouse only", q["arthouse"]),
        _toggle("accessible", "Captioned / audio described", q["accessible"]),
        _toggle("sold_out", "Include sold out", q["sold_out"]),
    ))
    if view == "search":
        submit, hint = "Search", "leave the film box empty to browse the day"
    else:
        submit, hint = "Show sessions", f"counting from {datetime.now(MELBOURNE_TZ):%H:%M} today"
    return (
        f'<form method="get" action="/">'
        f'<input type="hidden" name="view" value="{_esc(view)}">'
        f'<div class="grid">{"".join(fields)}</div>'
        f'<div class="toggles">{toggles}</div>'
        f'<div class="actions"><button type="submit">{submit}</button>'
        f'<span class="hint">{_esc(hint)}</span></div>'
        "</form>"
    )


def _session_row(session: dict, *, multi_film: bool, dated: bool) -> str:
    label = f"{session['day']} {session['time']}" if dated else session["time"]
    chips = [f'<span class="t">{_esc(label)}</span>']
    if multi_film:
        chips.append(f'<span class="film">{_esc(session["film"])}</span>')
    if session["format"]:
        chips.append(f'<span class="chip fmt">{_esc(session["format"])}</span>')
    if session["cc"]:
        chips.append('<span class="chip cc">CC</span>')
    if session["ad"]:
        chips.append('<span class="chip cc">AD</span>')
    if session["sold_out"]:
        chips.append('<span class="chip sold">sold out</span>')
    href = _safe_href(session["ticket_url"])
    if href:
        caption = _esc(f"Book {session['film']} at {session['cinema']} "
                       f"{session['date']} {session['time']}")
        chips.append(
            f'<a class="chip book" href="{href}" target="_blank" '
            f'rel="noopener noreferrer" title="{caption}">tickets</a>'
        )
    return f'<li>{"".join(chips)}</li>'


def _card(group: dict) -> str:
    cinema = group["cinema"]
    meta = " · ".join(
        _esc(part) for part in (cinema["suburb"], cinema["chain"], *cinema["tags"]) if part
    )
    distance = ""
    if group["distance_km"] is not None:
        distance = format_distance(group["distance_km"])
        if group["travel_min"]:
            distance += f" · ~{group['travel_min']} min"
    dist = f'<span class="dist">{_esc(distance)}</span>' if distance else ""
    film_line = "" if group["multi_film"] else f'<p class="filmline">{_esc(group["film"])}</p>'
    rows = "".join(
        _session_row(session, multi_film=group["multi_film"], dated=group["dated"])
        for session in group["sessions"]
    )
    more = (f'<p class="more">… {group["hidden"]} more at this venue</p>'
            if group["hidden"] else "")
    return (
        f'<article class="card"><header><h2>{_esc(cinema["name"])}</h2>{dist}</header>'
        f'<div class="meta">{meta}</div>{film_line}'
        f'<ul class="sessions">{rows}</ul>{more}</article>'
    )


def _empty_block(head: str, detail: str) -> str:
    return f'<div class="empty"><b>{_esc(head)}</b>{_esc(detail)}</div>'


def _popular_block(payload: dict) -> str:
    films = payload["films"]
    if not films:
        return ""
    chips = "".join(
        f'<a href="{_link("search", film=film["title"])}">{_esc(film["title"])} '
        f'<small>{film["cinema_count"]}</small></a>'
        for film in films
    )
    return (f'<section class="popular-wrap"><h3>Most screens this week</h3>'
            f'<div class="popular">{chips}</div></section>')


def _day_label(iso: str) -> str:
    if not iso:
        return ""
    return f"{date.fromisoformat(iso):%a %d %b}"


def _results_block(payload: dict) -> str:
    q = payload["query"]
    count, venues = payload["count"], len(payload["groups"])
    tally = f"{count} session{'s' if count != 1 else ''} · {venues} venue{'s' if venues != 1 else ''}"
    if payload["view"] == "tonight":
        title = f"Next {q['hours']:g} hours"
    elif q["film"] and payload["matched_films"]:
        # The heading names the film, so the day belongs in the tally.
        title = payload["matched_films"][0]["title"]
        if q["date"]:
            tally += f" · {_day_label(q['date'])}"
    elif q["film"]:
        title = q["film"]
    else:
        # Browsing: the heading *is* the day, so don't repeat it in the tally.
        title = f"Everything {_day_label(q['date'])}" if q["date"] else "Everything"
    head = (f'<div class="head"><h2>{_esc(title)}</h2><span>{_esc(tally)}</span></div>')

    notes: list[str] = []
    if payload["matched_films"]:
        best = payload["matched_films"][0]["title"]
        if q["film"] and normalise_title(best) != normalise_title(q["film"]):
            notes.append(f"Matched <b>{_esc(best)}</b>")
    if payload["alternatives"]:
        links = "".join(
            f'<a href="{_link("search", film=alt["title"])}">{_esc(alt["title"])}</a>'
            for alt in payload["alternatives"]
        )
        notes.append(f"Also showing {links}")
    alts = f'<p class="alts">{" · ".join(notes)}</p>' if notes else ""

    if payload["truncated"]:
        # Say so rather than quietly showing a prefix: a list that looks
        # complete but is not is the failure this tool exists to avoid.
        alts += (f'<p class="alts">Showing the first {count} sessions — '
                 "narrow the filters to see the rest.</p>")
    cards = "".join(_card(group) for group in payload["groups"])
    return head + alts + cards


def _venues_block(payload: dict) -> str:
    venues = payload["results"]
    if not venues:
        return _empty_block("No venues in the registry",
                            "The cinema registry is empty — run cinemelb refresh.")
    head = (f'<div class="head"><h2>All venues</h2>'
            f'<span>{len(venues)} cinemas</span></div>')
    cards = []
    for venue in venues:
        meta = " · ".join(
            _esc(part) for part in (venue.get("suburb"), venue.get("chain"),
                                    venue.get("address")) if part
        )
        tags = "".join(f'<span class="chip">{_esc(tag)}</span>'
                       for tag in venue.get("tags") or [])
        site = _safe_href(venue.get("website") or venue.get("booking_url") or "")
        if site:
            tags += (f'<a class="chip book" href="{site}" target="_blank" '
                     f'rel="noopener noreferrer">website</a>')
        tag_line = f'<p class="tags">{tags}</p>' if tags else ""
        count = venue.get("screening_count") or 0
        cards.append(
            f'<article class="card"><header><h2>{_esc(venue["name"])}</h2>'
            f'<span class="dist">{count} session{"s" if count != 1 else ""}</span></header>'
            f'<div class="meta">{meta}</div>{tag_line}</article>'
        )
    return head + "".join(cards)


def _body(payload: dict) -> str:
    """Pick the block the view needs: catalogue, landing, or results."""
    if payload["view"] == "venues":
        return _venues_block(payload)
    if payload["empty_db"]:
        return _empty_block("The database is empty",
                            "Run cinemelb refresh to scrape session times "
                            "into the local database.")
    if not payload["searched"]:
        return (
            '<section class="intro"><p>Search every cinema in Melbourne — chains '
            "and independents — by film title, with an optional date, start-time "
            "window and distance from a suburb you choose.</p></section>"
            + _popular_block(payload)
        )
    if not payload["count"]:
        return _no_results_block(payload) + _popular_block(payload)
    return _results_block(payload)


def _no_results_block(payload: dict) -> str:
    q = payload["query"]
    if payload["view"] == "tonight":
        return _empty_block(
            f"Nothing starts in the next {q['hours']:g} hours",
            "Try a longer window, or clear a filter.",
        )
    if q["film"] and not payload["matched_films"]:
        return _empty_block(
            f"No film called “{q['film']}” in the database",
            "Matching is fuzzy: two or three words from the title works best.",
        )
    if payload["matched_films"]:
        return _empty_block(
            "No sessions matched those filters",
            "Try another date, a later start time, or a wider radius.",
        )
    return _empty_block(
        "No sessions matched those filters",
        "Try a later start time or a wider radius.",
    )


def _header(payload: dict) -> str:
    stats = payload["stats"]
    status = (f"data from {payload['freshness']}" if payload["freshness"]
              else "no successful refresh yet")
    bits = [
        status,
        f"{stats['venues']} venues",
        f"{stats['screenings']:,} screenings",
    ]
    if payload["coverage"]:
        bits.append(f"coverage {payload['coverage']}")
    return ('<header class="site"><h1><span>cinemelb</span> Melbourne cinema '
            f'showtimes</h1><p class="sub">{_esc(" · ".join(bits))}</p></header>')


def _tabs(q: dict) -> str:
    links = []
    for view, label in (("search", "Search"), ("tonight", "Tonight"), ("venues", "Venues")):
        current = ' class="on"' if view == q["view"] else ""
        links.append(f'<a href="{_esc(_query_string(q, view=view))}"{current}>{label}</a>')
    return f'<nav>{"".join(links)}</nav>'


def _render_page(payload: dict) -> str:
    q = payload["query"]
    title = f"{q['film']} · cinemelb" if q["film"] else "cinemelb · Melbourne showtimes"
    notices = "".join(f'<div class="notice" role="alert">{_esc(e)}</div>'
                      for e in payload["errors"])
    parts = [
        "<!DOCTYPE html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        '<meta name="color-scheme" content="dark">',
        f"<title>{_esc(title)}</title>",
        f"<style>{_CSS}</style>",
        "</head>",
        "<body>",
        '<div class="wrap">',
        _header(payload),
        _tabs(q),
        _form(q),
        notices,
        _body(payload),
        # The counts are in the header; this is the "where did this come from
        # and how do I refresh it" line.
        '<footer>read-only local copy of the database · '
        "<code>cinemelb refresh</code> to update</footer>",
        "</div>",
        "</body>",
        "</html>",
    ]
    return "\n".join(part for part in parts if part)


def _error_page(message: str) -> str:
    """Last-resort page for a bug in the query engine."""
    return (
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        "<title>cinemelb · error</title>"
        f'<style>{_CSS}</style></head><body><div class="wrap">'
        '<header class="site"><h1><span>cinemelb</span> Melbourne cinema '
        f'showtimes</h1></header><div class="notice" role="alert">{_esc(message)}</div>'
        '<p><a href="/">back to search</a></p></div></body></html>'
    )


def _not_found_page(path: str) -> str:
    return (
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        "<title>cinemelb · not found</title>"
        f'<style>{_CSS}</style></head><body><div class="wrap">'
        '<header class="site"><h1><span>cinemelb</span> Melbourne cinema '
        "showtimes</h1></header>"
        f'<div class="empty"><b>No such page</b>{_esc(path)}</div>'
        '<p><a href="/">back to search</a></p></div></body></html>'
    )


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

class _App:
    """URL in, response out.

    The sqlite connection is shared by every thread ``ThreadingHTTPServer``
    spawns (``Store`` opens it with ``check_same_thread=False``), so all access
    goes through one lock.  A query takes milliseconds, so serialising them
    costs nothing next to the class of intermittent corruption bug it removes.
    """

    def __init__(self, store: Store) -> None:
        self.store = store
        self.lock = threading.Lock()

    def handle(self, raw_path: str) -> tuple[int, str, bytes]:
        """Return ``(status, content_type, body)``; never raises for bad input."""
        parts = urlsplit(raw_path)
        params = parse_qs(parts.query)

        if parts.path in ("/", "/index.html"):
            with self.lock:
                payload = _build_payload(self.store, _Query.parse(params))
            return 200, "text/html; charset=utf-8", _render_page(payload).encode("utf-8")

        view = _API_ROUTES.get(parts.path)
        if view:
            with self.lock:
                payload = _build_payload(self.store, _Query.parse(params, view=view))
            return 200, "application/json; charset=utf-8", _json_bytes(payload)

        if parts.path == "/favicon.ico":
            return 204, "image/x-icon", b""

        if parts.path.startswith("/api/"):
            return 404, "application/json; charset=utf-8", _json_bytes(
                {"error": "unknown route", "routes": ["/", *_API_ROUTES]}
            )
        return 404, "text/html; charset=utf-8", _not_found_page(parts.path).encode("utf-8")


def _json_bytes(payload: dict) -> bytes:
    # Not indented: these payloads are for scripts, and 200 sessions of pretty
    # printing is megabytes of whitespace.  `curl ... | jq` is one keystroke.
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


class _Handler(BaseHTTPRequestHandler):
    #: Keep-alive: a page load is one request, but the API and any browser
    #: prefetch benefit, and every response carries a Content-Length.
    protocol_version = "HTTP/1.1"
    server_version = "cinemelb"
    sys_version = ""  # don't advertise the Python build to the local network
    server: "_Server"

    def do_GET(self) -> None:
        try:
            status, content_type, body = self.server.app.handle(self.path)
        except Exception as exc:  # one broken view must not take the server down
            status, content_type, body = (
                500, "text/html; charset=utf-8",
                _error_page(f"The query failed: {exc}").encode("utf-8"),
            )
        self._send(status, content_type, body)

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        if status not in (204, 304):  # these must carry no length and no body
            self.send_header("Content-Length", str(len(body)))
        # Showtimes go stale by the hour and the page is generated per request;
        # any cache between here and the screen can only make it wrong.
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        # The stdlib line repeats the request line and fills it with dashes.
        sys.stderr.write(f"  {self.requestline}  {code}\n")


class _Server(ThreadingHTTPServer):
    """A ``ThreadingHTTPServer`` that carries the app the handler needs."""

    daemon_threads = True

    def __init__(self, address: tuple[str, int], app: _App) -> None:
        self.app = app
        super().__init__(address, _Handler)


def make_server(host: str = "127.0.0.1", port: int = 8787,
                db: str | None = None) -> _Server:
    """Build (and bind) the server without serving.

    Split out of :func:`serve` so a caller can pick an ephemeral port
    (``port=0``) and drive ``serve_forever`` itself — which is how the
    verification script exercises the real request path.
    """
    store = Store(db)
    try:
        return _Server((host, port), _App(store))
    except OSError:
        # The bind failed, so nothing will ever use this connection.
        store.close()
        raise


def serve(host: str = "127.0.0.1", port: int = 8787, db: str | None = None) -> None:
    """Serve the web UI until interrupted.

    Binds to ``127.0.0.1`` by default: this is a local tool looking at a local
    database, not a service to expose.  Pass a host explicitly to open it to a
    LAN.
    """
    try:
        server = make_server(host, port, db)
    except OSError as exc:
        # A busy port is the common case (8787 is a popular default) and it is
        # worth naming, because a bare traceback reads like the tool is broken.
        raise SystemExit(
            f"Could not listen on {host}:{port} — {exc}. Another program is "
            f"probably using that port; try --port {port + 1}."
        ) from exc
    bound_host, bound_port = server.server_address[:2]
    # 0.0.0.0 is a bind address, not a destination: print something clickable.
    shown = "127.0.0.1" if bound_host in ("0.0.0.0", "::", "") else bound_host
    print(f"cinemelb web UI  http://{shown}:{bound_port}/", flush=True)
    print(f"database         {server.app.store.path}", flush=True)
    print("read-only · Ctrl-C to stop", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped", flush=True)
    finally:
        server.server_close()
        server.app.store.close()


if __name__ == "__main__":
    # A by-hand entry point for testing this module on its own; the real CLI
    # front end is `cinemelb serve`, wired in cinemelb/cli.py.
    import argparse

    _parser = argparse.ArgumentParser(description="Serve the cinemelb web UI.")
    _parser.add_argument("--host", default="127.0.0.1")
    _parser.add_argument("--port", type=int, default=8787)
    _parser.add_argument("--db", default=None)
    _args = _parser.parse_args()
    serve(host=_args.host, port=_args.port, db=_args.db)
