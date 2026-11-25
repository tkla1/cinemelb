"""Adapter for the long tail of Melbourne's single-site cinemas.

Every other adapter owns a *platform*: fourteen Palace venues, twelve Hoyts,
one Village API.  The venues this adapter owns are the opposite problem — a
handful of independent cinemas with completely different websites, so there is
no single parser to write.  What they do share is a pattern of *kinds* of site:

* a WordPress cinema plugin rendering sessions server-side (Metro Boronia);
* a ticketing provider's own public pages (Veezi — Eclipse, Chinatown);
* a small custom JSON API belonging to the venue (FoMo);
* a per-city Next.js site with its timetable in the hydration payload
  (Moonlight, which is outdoor and only lists a season at a time);
* an existing platform we already parse (Cameo Cinemas runs Moving Story's CMS);
* and, for the rest, whatever plain HTML the site happens to publish.

So this is a strategy table, not a branch per venue::

    STRATEGIES: (predicate, extractor) pairs

The predicate decides from the venue record alone — a website domain, or a
``strategy`` key that ``data/overrides/generic_html.json`` can set — and the
first match wins.  :func:`_generic` is the last resort; it sniffs the page for
machine-readable timetables (JSON-LD, ``<time datetime>``) and falls back to the
"date heading plus times" text scan that small cinema sites respond to
surprisingly often.  It also recognises platform fingerprints in the markup, so
a venue that migrates onto Veezi or the wp-cinema plugin starts working without
a code change.

Two rules from :mod:`cinemelb.models` matter doubly here:

* Times are **naive Melbourne wall-clock**.  These sites print "7:00pm" and
  mean seven o'clock at the venue; ``make_screening`` attaches the timezone.
* **Never invent a screening.**  These sites are small enough that a bad guess
  becomes a real person driving to a closed cinema.  Anything this module
  cannot read returns an empty :class:`AdapterOutput` whose ``note`` names the
  obstruction — the orchestrator logs that note, and a blank venue recovers on
  the next refresh while a fabricated 7pm does not.

Sites whose timetable only exists after JavaScript must stay empty here; the
note names the platform (``Indy Systems``, ``Next.js``, ``CRA``) so the gap is a
follow-up rather than a mystery.  As of writing that is Brunswick and Thornbury
(Picture House's Indy Systems SPA), United Craigieburn (a MovieXchange "Lumos"
Next app) and Waverley (a statically exported Next app).

Configuration lives in ``data/overrides/generic_html.json``, which the
orchestrator merges into ``source_ids``:

    {"metro-boronia":  {"strategy": "wpcinema",
                        "generic_html": "https://metroboronia.com.au"},
     "eclipse-cinema": {"strategy": "veezi", "veezi": "vp8p94rb2b46tshes7ac8j70sr"}}

``strategy`` forces a table entry, ``veezi`` holds a Veezi site token, and
``generic_html`` supplies a site URL for a venue whose registry row has none.
"""

from __future__ import annotations

import html as html_lib
import json
import re
from datetime import date, datetime, time, timedelta
from typing import Any, Callable
from urllib.parse import urljoin

from ..http import Http
from ..models import MELBOURNE_TZ, Film, film_key, make_screening
from .base import Adapter, AdapterOutput
from .movingstory import MovingStoryAdapter

#: the registry's ``source`` value; also the label written on every Screening
SOURCE = "generic_html"

Extractor = Callable[[dict, Http], AdapterOutput]
Predicate = Callable[[dict], bool]


# --------------------------------------------------------------------------- #
# shared parsing helpers
# --------------------------------------------------------------------------- #

_MONTHS = (
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
)

#: "8:05 PM", "7:00pm", "19:05" — the three clock styles these sites use.
_CLOCK = re.compile(r"\b(\d{1,2}):(\d{2})\s*(?:([ap])\.?m\.?)?\b", re.I)

#: weekday-optional day/month pairs: "Wednesday 25, November", "Sat 28 Nov".
_DAY_MONTH = re.compile(
    r"(?:(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*\.?,?\s+)?"
    r"(\d{1,2})(?:st|nd|rd|th)?\s*,?\s*"
    r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*",
    re.I,
)
_ISO_DAY = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def _host(cinema: dict) -> str:
    """Bare hostname of the venue's website (``www.`` stripped)."""
    site = str(cinema.get("website") or "").strip()
    m = re.match(r"(?:https?://)?([^/\s]+)", site, re.I)
    return (m.group(1) if m else site).lower().removeprefix("www.")


def _sids(cinema: dict) -> dict[str, str]:
    return dict(cinema.get("source_ids") or {})


def _sid(cinema: dict, key: str) -> str:
    return str(_sids(cinema).get(key) or "").strip()


def _site(cinema: dict) -> str:
    """The venue's site, preferring an override over a registry blank."""
    site = (cinema.get("website") or "").strip() or _sid(cinema, "generic_html")
    return site.rstrip("/")


def _month(name: str) -> int | None:
    key = name.strip().lower()[:3]
    for index, full in enumerate(_MONTHS, start=1):
        if full.startswith(key):
            return index
    return None


def _date_near(day: int, month: int, today: date) -> date | None:
    """Resolve a day/month with no year, preferring the upcoming occurrence.

    A cinema timetable only ever runs forward, so a date that lands more than a
    month in the past is next year's ("February" read in November).
    """
    try:
        cand = date(today.year, month, day)
    except ValueError:
        return None                     # e.g. 31 February in a bad template
    if cand < today - timedelta(days=31):
        try:
            cand = date(today.year + 1, month, day)
        except ValueError:
            return None
    return cand


def _clock(text: str) -> time | None:
    """Read one wall-clock time out of ``text`` (12h preferred, 24h accepted)."""
    for m in _CLOCK.finditer(text):
        hour, minute, meridiem = int(m.group(1)), int(m.group(2)), m.group(3)
        if minute > 59:
            continue
        if meridiem:
            if not 1 <= hour <= 12:
                continue
            hour = hour % 12 + (12 if meridiem.lower() == "p" else 0)
        elif hour > 23:
            continue
        try:
            return time(hour, minute)
        except ValueError:
            continue
    return None


def _naive_dt(value: str) -> datetime | None:
    """Parse an ISO-ish timestamp as *naive local* time.

    Australian cinema APIs tag local timestamps with ``Z`` or an offset that
    does not match the advertised wall clock; dropping the suffix is the only
    reading that keeps a 7pm session at 7pm.
    """
    if not value:
        return None
    text = str(value).strip().replace("Z", "")
    if "T" in text and "." in text:
        text = text.split(".", 1)[0]
    elif " " in text and "." in text:
        text = text.split(".", 1)[0]
    text = text.replace(" ", "T")
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:19], fmt)
        except ValueError:
            continue
    return None


def _clean(text: str) -> str:
    """Strip tags + entities from a fragment of markup."""
    plain = re.sub(r"<[^>]+>", " ", text or "")
    return " ".join(html_lib.unescape(plain).split())


def _today() -> date:
    return datetime.now(MELBOURNE_TZ).date()


def _film_rows(titles: dict[str, dict[str, Any]]) -> list[Film]:
    """Build Film rows from whatever metadata an extractor managed to collect."""
    rows: list[Film] = []
    for title, meta in titles.items():
        rows.append(
            Film(
                key=film_key(title),
                title=title,
                runtime_minutes=meta.get("runtime"),
                rating=str(meta.get("rating") or ""),
                genres=list(meta.get("genres") or []),
                synopsis=str(meta.get("synopsis") or ""),
                poster_url=str(meta.get("poster") or ""),
                trailer_url=str(meta.get("trailer") or ""),
            )
        )
    return rows


def _emit(
    out: AdapterOutput,
    *,
    cinema_id: str,
    title: str,
    start: datetime,
    seen: set[tuple[str, str, str]],
    screen: str = "",
    fmt: str = "",
    attributes: list[str] | None = None,
    ticket_url: str = "",
    sold_out: bool = False,
) -> None:
    """Append one screening, dropping exact repeats across page panels."""
    stamp = start.strftime("%Y-%m-%dT%H:%M")
    key = (film_key(title), stamp, screen)
    if key in seen:
        return
    seen.add(key)
    out.screenings.append(
        make_screening(
            cinema_id=cinema_id,
            film_title=title,
            local_naive=start,
            source=SOURCE,
            screen=screen,
            fmt=fmt,
            attributes=attributes or [],
            ticket_url=ticket_url,
            sold_out=sold_out,
        )
    )


def _format_of(attributes: list[str]) -> str:
    joined = " ".join(attributes).lower()
    for token in ("70mm", "35mm", "16mm", "4k", "imax", "dolby"):
        if token in joined:
            return token.upper() if token.endswith("mm") else token
    return ""


# --------------------------------------------------------------------------- #
# Veezi — public sessions page (Eclipse Cinema, Chinatown Cinema)
# --------------------------------------------------------------------------- #

VEEZI_SESSIONS = "https://ticketing.oz.veezi.com/sessions/?siteToken={token}"

#: A Veezi site token, as it appears on any page that links to the box office.
_VEEZI_TOKEN = re.compile(r"siteToken=([A-Za-z0-9%+=_\-]{8,})")

#: The sessions page renders the same timetable twice, once grouped by date and
#: once by film; parsing the second panel only keeps every session exactly once.
_VEEZI_PANEL = re.compile(r"id=[\"']sessionsByFilmConent[\"']")
_VEEZI_SECTION = re.compile(
    r'<div class="date">\s*<h3 class="date-title[^"]*">(?P<date>[^<]+)</h3>'
    r'|<h3 class="title">\s*(?P<title>.*?)\s*</h3>'
    r'|<h4 class="date">\s*(?P<day>[^<]+)\s*</h4>'
    r"|<span class=\"censor\">\s*(?P<censor>[^<]*)\s*</span>"
    r"|<a[^>]+href=\"(?P<ticket>https://ticketing[^\"]*?/purchase/[^\"]*)\""
    r"|<time>\s*(?P<time>[^<]+?)\s*</time>",
    re.I | re.S,
)


def _veezi_token(cinema: dict, http: Http) -> str:
    """Site token from config, else discovered from the venue's own site.

    Discovery matters because a token is not a secret but it *is* opaque, and
    several venues embed it only inside a "buy tickets" link.
    """
    token = _sid(cinema, "veezi") or _sid(cinema, "generic_html")
    if token:
        return token
    site = _site(cinema)
    if not site:
        return ""
    try:
        page = http.get(site + "/")
    except Exception:                                       # noqa: BLE001
        return ""
    found = _VEEZI_TOKEN.findall(page.text)
    # The most-linked token on the page is the live box office (stale links to
    # an old site layout are common in menus).
    if not found:
        return ""
    return max(set(found), key=found.count)


def _veezi(cinema: dict, http: Http) -> AdapterOutput:
    cinema_id = str(cinema.get("id", ""))
    token = _veezi_token(cinema, http)
    if not token:
        return AdapterOutput(note="no Veezi siteToken found for this venue")

    page = http.get(VEEZI_SESSIONS.format(token=token))
    text = page.text

    # Restrict to the by-film panel so the by-date panel cannot double-count.
    panel = _VEEZI_PANEL.search(text)
    if panel:
        text = text[panel.start() :]

    today = _today()
    out = AdapterOutput()
    seen: set[tuple[str, str, str]] = set()
    films: dict[str, dict[str, Any]] = {}

    title = ""
    day: date | None = None
    rating = ""
    ticket = ""
    for m in _VEEZI_SECTION.finditer(text):
        if m.group("date") or m.group("day"):
            label = m.group("date") or m.group("day") or ""
            dm = _DAY_MONTH.search(label)
            month = _month(dm.group(2)) if dm else None
            day = _date_near(int(dm.group(1)), month, today) if (dm and month) else None
            # A sold-out session renders without its purchase link; dropping the
            # previous link here stops it being inherited by the next session.
            ticket = ""
        elif m.group("title") is not None:
            title = _clean(m.group("title"))
            rating = ""
            ticket = ""
            if title:
                films.setdefault(title, {})
        elif m.group("censor") is not None:
            rating = _clean(m.group("censor"))
        elif m.group("ticket"):
            ticket = m.group("ticket")
        elif m.group("time") is not None:
            clock = _clock(m.group("time"))
            if not (title and day and clock):
                continue
            if rating:
                films[title]["rating"] = rating
            _emit(
                out,
                cinema_id=cinema_id,
                title=title,
                start=datetime.combine(day, clock),
                seen=seen,
                ticket_url=html_lib.unescape(ticket),
            )
            ticket = ""

    out.films = _film_rows(films)
    if not out.screenings:
        out.note = (
            "Veezi timetable page contained no sessions "
            f"(siteToken {token[:6]}…, {page.status})"
        )
    return out


# --------------------------------------------------------------------------- #
# FoMo — the venue's own JSON API
# --------------------------------------------------------------------------- #

FOMO_SESSIONS = "https://api.fomocinemas.com.au/api/front/film/with-show"


def _fomo(cinema: dict, http: Http) -> AdapterOutput:
    payload = http.get_json(FOMO_SESSIONS) or {}
    rows = payload.get("data") or []
    cinema_id = str(cinema.get("id", ""))

    out = AdapterOutput()
    seen: set[tuple[str, str, str]] = set()
    films: dict[str, dict[str, Any]] = {}

    for row in rows:
        info = row.get("info") or {}
        title = _clean(str(info.get("Title") or row.get("Title") or ""))
        start = _naive_dt(str(row.get("Showtime") or ""))
        if not (title and start):
            continue
        films.setdefault(
            title,
            {
                "rating": info.get("Rating"),
                "runtime": info.get("RunTime"),
                "synopsis": (info.get("Synopsis") or "").strip(),
                "poster": info.get("GraphicUrl") or "",
                "trailer": info.get("TrailerUrl") or "",
            },
        )
        attrs = [str(a) for a in (row.get("SessionAttributesNames") or []) if a]
        _emit(
            out,
            cinema_id=cinema_id,
            title=title,
            start=start,
            seen=seen,
            screen=str(row.get("ScreenName") or ""),
            fmt=_format_of(attrs),
            attributes=attrs,
            sold_out=bool(row.get("SoldoutStatus")),
        )

    out.films = _film_rows(films)
    if not out.screenings:
        out.note = f"FoMo API returned {len(rows)} rows but none had a title and showtime"
    return out


# --------------------------------------------------------------------------- #
# WordPress "wp-cinema" plugin (Metro Boronia)
# --------------------------------------------------------------------------- #

WPCINEMA_DAYS = 7                       # the plugin's day switcher covers a week

_WPCINEMA_PAGE_DATE = re.compile(r"<h2 class='wpcinema'>\s*Movies for ([^<]+)</h2>", re.I)
_WPCINEMA_MOVIE = re.compile(r"<div class='wpcinema_movie'>", re.I)
#: The rating lives inside the same anchor as the title, so stop at the first
#: tag rather than at ``</a>`` — otherwise every title ends in " (M)".
_WPCINEMA_TITLE = re.compile(
    r"<h3 class='movietitle'>\s*<a[^>]*>(?P<title>[^<]*)", re.I
)
_WPCINEMA_RATING = re.compile(r"<span class='rating'>\((?P<rating>[^)]*)\)</span>", re.I)
_WPCINEMA_SESSION = re.compile(
    r"<span title='(?P<title>[^']*)'[^>]*?class=[\"']wpcinema_session_(?:available|closed)"
    r"[^\"']*[\"'][^>]*>"
    r"\s*(?:<a href='(?P<ticket>[^']*)'>)?\s*"
    r"(?P<time>\d{1,2}:\d{2}\s*[ap]m)\s*"
    r"(?:</a>)?\s*</span>",
    re.I | re.S,
)
_WPCINEMA_SCREEN = re.compile(r"\[(?:in\s+)?([^\]]+)\]", re.I)
_WPCINEMA_POSTER = re.compile(
    r"<img src='(?P<poster>[^']*)' title='(?P<title>[^']*)'", re.I
)


def _wpcinema(cinema: dict, http: Http) -> AdapterOutput:
    site = _site(cinema)
    if not site:
        return AdapterOutput(
            note="no website on record; add data/overrides/generic_html.json "
                 "with a site URL for this venue"
        )
    cinema_id = str(cinema.get("id", ""))
    today = _today()

    out = AdapterOutput()
    seen: set[tuple[str, str, str]] = set()
    films: dict[str, dict[str, Any]] = {}
    errors: list[str] = []

    for offset in range(WPCINEMA_DAYS):
        url = site + "/" if offset == 0 else f"{site}/?wp-cinema-day={offset}"
        try:
            page = http.get(url)
        except Exception as exc:                            # noqa: BLE001
            errors.append(f"day {offset}: {exc}")
            continue

        # The page prints its own date; the ticket links carry it explicitly,
        # so either is enough and the heading is the fallback for closed
        # sessions (which have no link left to read).
        page_day: date | None = None
        heading = _WPCINEMA_PAGE_DATE.search(page.text)
        if heading:
            dm = _DAY_MONTH.search(heading.group(1))
            month = _month(dm.group(2)) if dm else None
            page_day = _date_near(int(dm.group(1)), month, today) if (dm and month) else None

        # Each opening <div class='wpcinema_movie'> starts a film block that
        # runs to the next one, so sessions inside it belong to that title.
        starts = [m.start() for m in _WPCINEMA_MOVIE.finditer(page.text)]
        if not starts:
            continue
        starts.append(len(page.text))
        for begin, end in zip(starts, starts[1:]):
            block = page.text[begin:end]
            title_m = _WPCINEMA_TITLE.search(block)
            title = _clean(title_m.group("title")) if title_m else ""
            if not title:
                continue
            rating_m = _WPCINEMA_RATING.search(block)
            poster_m = _WPCINEMA_POSTER.search(block)
            meta = films.setdefault(title, {})
            if rating_m:
                meta["rating"] = rating_m.group("rating")
            if poster_m:
                meta.setdefault("poster", poster_m.group("poster"))

            for s in _WPCINEMA_SESSION.finditer(block):
                clock = _clock(s.group("time"))
                if not clock:
                    continue
                ticket = s.group("ticket") or ""
                stamp_m = _ISO_DAY.search(ticket)
                day = None
                if stamp_m:
                    try:
                        day = date(int(stamp_m.group(1)), int(stamp_m.group(2)),
                                   int(stamp_m.group(3)))
                    except ValueError:
                        day = None
                day = day or page_day
                if not day:
                    continue
                screen_m = _WPCINEMA_SCREEN.search(s.group("title") or "")
                _emit(
                    out,
                    cinema_id=cinema_id,
                    title=title,
                    start=datetime.combine(day, clock),
                    seen=seen,
                    screen=_clean(screen_m.group(1)) if screen_m else "",
                    attributes=[],
                    ticket_url=html_lib.unescape(ticket),
                )

    out.films = _film_rows(films)
    if not out.screenings:
        out.note = "; ".join(errors) or "wp-cinema day pages held no sessions"
    return out


# --------------------------------------------------------------------------- #
# Cameo — the same CMS Moving Story runs, so reuse that parser
# --------------------------------------------------------------------------- #

def _movingstory(cinema: dict, http: Http) -> AdapterOutput:
    """Delegate rather than re-parse: Cameo's markup is Moving Story's markup.

    The film-page parse (day tabs + ``<ul class="Sessions">``) is already
    written and tested in :mod:`.movingstory`; duplicating it here would mean
    two places to fix when the CMS changes.  The adapter's ``name`` is re-labelled
    so the resulting rows carry this registry source.
    """
    adapter = MovingStoryAdapter()
    adapter.name = SOURCE
    return adapter.fetch(cinema, http)


# --------------------------------------------------------------------------- #
# Moonlight Cinema — per-city Next.js site with its own timetable payload
# --------------------------------------------------------------------------- #

MOONLIGHT_HOST = "https://{subsite}.moonlight.com.au"
_MOONLIGHT_SUB = re.compile(r"^([a-z]+)\.moonlight\.com\.au$", re.I)
_NEXT_DATA = re.compile(
    r'id="__NEXT_DATA__"[^>]*>(?P<body>.*?)</script>', re.I | re.S
)


def _next_data(page_text: str) -> dict[str, Any]:
    """The Next.js hydration payload, or ``{}`` when the page is not Next."""
    m = _NEXT_DATA.search(page_text)
    if not m:
        return {}
    try:
        data = json.loads(m.group("body").strip())
    except (json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _moonlight_base(cinema: dict) -> str:
    """The venue's own subsite host.

    Moonlight runs one Next.js site per city and the registry points at the
    location *selector*, which carries no timetable at all — so the subsite has
    to be derived from the venue name when the row does not already name it.
    """
    host = _host(cinema)
    sub = _MOONLIGHT_SUB.match(host)
    if sub:
        return f"https://{sub.group(1)}.moonlight.com.au"
    name = str(cinema.get("name") or "").replace("Cinema", " ")
    for token in reversed(name.split()):
        if token.lower() in {"melbourne", "sydney", "brisbane", "perth", "adelaide"}:
            return MOONLIGHT_HOST.format(subsite=token.lower())
    return _site(cinema)


def _moonlight(cinema: dict, http: Http) -> AdapterOutput:
    cinema_id = str(cinema.get("id", ""))
    base = _moonlight_base(cinema)
    page = http.get(base + "/program")
    page_props = (_next_data(page.text).get("props") or {}).get("pageProps") or {}
    edges = ((page_props.get("movies") or {}).get("edges")) or []

    out = AdapterOutput()
    seen: set[tuple[str, str, str]] = set()
    films: dict[str, dict[str, Any]] = {}

    for edge in edges:
        node = edge.get("node") or {}
        acf = node.get("acfCptMovie") or {}
        title = _clean(str(acf.get("movieName") or node.get("title") or ""))
        sessions = acf.get("movieSessions") or []
        if not title or not sessions:
            # Placeholder cards ("TO BE ANNOUNCED") carry a date but no time;
            # publishing them would mean inventing the start time.
            continue
        poster = (node.get("featuredImage") or {}).get("node", {}).get("sourceUrl", "")
        runtime = acf.get("movieDuration")
        films.setdefault(
            title,
            {
                "rating": acf.get("movieRating"),
                "runtime": int(runtime) if str(runtime or "").isdigit() else None,
                "synopsis": _clean(str(acf.get("movieSynopsis") or node.get("content") or "")),
                "poster": acf.get("moviePoster") or poster,
            },
        )
        for session in sessions:
            day = _naive_dt(str(session.get("date") or ""))
            clock = _clock(str(session.get("startTime") or ""))
            if not (day and clock):
                continue
            ribbon = _clean(str(session.get("overrideRibbonTitle") or ""))
            _emit(
                out,
                cinema_id=cinema_id,
                title=title,
                start=datetime.combine(day.date(), clock),
                seen=seen,
                attributes=[ribbon] if ribbon else [],
                sold_out=str(session.get("status") or "").lower() not in ("", "available"),
            )

    out.films = _film_rows(films)
    if not out.screenings:
        out.note = (
            "no sessions on sale in the program page payload "
            "(out of season, or tickets not yet released)"
        )
    return out


# --------------------------------------------------------------------------- #
# generic fallback — structured data first, text scan last
# --------------------------------------------------------------------------- #

_JSONLD = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(?P<body>.*?)</script>',
    re.I | re.S,
)
_TIME_TAG = re.compile(
    r"<time[^>]+datetime=[\"'](?P<dt>[^\"']+)[\"'][^>]*>(?P<label>[^<]*)</time>", re.I
)
_HEADING = re.compile(r"<h([1-6])[^>]*>(?P<text>.*?)</h\1>", re.I | re.S)
_ANCHOR = re.compile(r"<a[^>]+href=[\"'](?P<href>[^\"']+)[\"'][^>]*>(?P<text>.*?)</a>", re.I | re.S)
_PROGRAM_LINK = re.compile(
    r"sessions?|whats?-?on|what-s-on|now[-_]?showing|showtimes?|program|times", re.I
)

#: Fingerprints of sites whose timetable only ever exists after JavaScript runs.
#: Naming the platform in the note turns "empty" into an actionable follow-up.
#: Attributes here are unquoted in the wild (``<div id=q-app>``), so allow both.
_JS_ONLY_HINTS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r'id=["\']?q-app'), "an Indy Systems single-page app"),
    (re.compile(r'"nextExport":true'), "a statically exported Next.js site"),
    (re.compile(r'id="__NEXT_DATA__"'), "a Next.js app that loads sessions client-side"),
    (re.compile(r'<div id=["\']?root["\']?>\s*</div>'), "a Create-React-App shell"),
)


def _jsonld_events(page_text: str) -> list[dict[str, Any]]:
    """Flatten every schema.org ScreeningEvent/Movie in the page's JSON-LD."""
    found: list[dict[str, Any]] = []
    for block in _JSONLD.finditer(page_text):
        try:
            data = json.loads(block.group("body").strip())
        except (json.JSONDecodeError, ValueError):
            continue
        queue: list[Any] = [data]
        while queue:
            node = queue.pop()
            if isinstance(node, list):
                queue.extend(node)
                continue
            if not isinstance(node, dict):
                continue
            queue.extend(node.get("@graph") or [])
            kind = node.get("@type")
            kinds = kind if isinstance(kind, list) else [kind]
            if any(str(k).lower() in ("screeningevent", "event", "movie", "theatererevent")
                   for k in kinds if k):
                found.append(node)
    return found


def _generic_jsonld(cinema_id: str, page_text: str) -> AdapterOutput:
    out = AdapterOutput()
    seen: set[tuple[str, str, str]] = set()
    films: dict[str, dict[str, Any]] = {}
    for node in _jsonld_events(page_text):
        title = _clean(str(node.get("name") or node.get("headline") or ""))
        start = _naive_dt(str(node.get("startDate") or ""))
        if not (title and start):
            continue
        films.setdefault(title, {"poster": node.get("image") if isinstance(node.get("image"), str) else ""})
        place = node.get("location") or {}
        screen = ""
        if isinstance(place, dict):
            screen = _clean(str(place.get("name") or ""))
        _emit(out, cinema_id=cinema_id, title=title, start=start, seen=seen, screen=screen)
    out.films = _film_rows(films)
    return out


def _generic_text(cinema_id: str, page_text: str, today: date) -> AdapterOutput:
    """Last resort: date headings plus time tokens.

    Only rows with *both* a date heading and a title are emitted.  A time with
    no film name beside it is left out rather than attached to the heading —
    that is exactly the shape of a fabricated screening.
    """
    out = AdapterOutput()
    seen: set[tuple[str, str, str]] = set()

    # Node boundaries: headings and times, in document order.
    marks: list[tuple[int, str, str]] = []
    for m in _HEADING.finditer(page_text):
        marks.append((m.start(), "heading", _clean(m.group("text"))))
    for m in _TIME_TAG.finditer(page_text):
        marks.append((m.start(), "tag", m.group("dt")))
    for m in _CLOCK.finditer(page_text):
        marks.append((m.start(), "clock", m.group(0)))
    marks.sort()

    day: date | None = None
    title = ""
    for _, kind, payload in marks:
        if kind == "heading":
            dm = _DAY_MONTH.search(payload)
            month = _month(dm.group(2)) if dm else None
            if dm and month:
                day = _date_near(int(dm.group(1)), month, today)
                title = _clean(payload[dm.end():]) or ""
                continue
            title = payload
        elif kind == "tag":
            start = _naive_dt(payload)
            if start and title:
                _emit(out, cinema_id=cinema_id, title=title, start=start, seen=seen)
        else:
            clock = _clock(payload)
            if not (clock and day and title):
                continue
            _emit(
                out,
                cinema_id=cinema_id,
                title=title,
                start=datetime.combine(day, clock),
                seen=seen,
            )

    out.films = _film_rows({s.film_title: {} for s in out.screenings})
    return out


def _generic(cinema: dict, http: Http) -> AdapterOutput:
    cinema_id = str(cinema.get("id", ""))
    site = _site(cinema)
    if not site:
        return AdapterOutput(note="no website on record; needs a site URL override")

    home = http.get(site + "/")
    text = home.text

    # A page can advertise a platform even when the registry does not: prefer a
    # specialised extractor over the heuristic whenever the fingerprint is there.
    token = _VEEZI_TOKEN.findall(text)
    if token:
        return _veezi(cinema, http)
    if "wp-cinema" in text.lower() or "wpcinema" in text.lower():
        return _wpcinema(cinema, http)

    today = _today()
    out = _generic_jsonld(cinema_id, text)
    if not out.screenings:
        out = _generic_text(cinema_id, text, today)

    tried: list[str] = []
    if not out.screenings:
        # Walk one level of navigation: small cinema sites often keep the
        # timetable on a child page ("What's on", "Sessions", "Program").
        for anchor in _ANCHOR.finditer(text):
            label = _clean(anchor.group("text"))
            href = html_lib.unescape(anchor.group("href"))
            if not _PROGRAM_LINK.search(label) and not _PROGRAM_LINK.search(href):
                continue
            url = urljoin(site + "/", href)
            if url in tried or not url.startswith("http"):
                continue
            tried.append(url)
            if len(tried) > 3:
                break
            try:
                child = http.get(url)
            except Exception:                               # noqa: BLE001
                continue
            found = _generic_jsonld(cinema_id, child.text)
            if not found.screenings:
                found = _generic_text(cinema_id, child.text, today)
            if found.screenings:
                out = found
                break

    if not out.screenings:
        # Say *why* it is empty: a named platform is a follow-up task, an
        # unnamed one is a bug report.
        reason = next((label for pattern, label in _JS_ONLY_HINTS if pattern.search(text)), "")
        where = f" ({len(tried)} linked pages also checked)" if tried else ""
        out.note = (
            f"no session times in the server HTML of {site}{where}"
            + (f"; it is {reason}, so the timetable only exists after "
               "JavaScript runs — needs a browser-based scrape" if reason else "")
        )
    return out


# --------------------------------------------------------------------------- #
# dispatch
# --------------------------------------------------------------------------- #

def _domain_is(*domains: str) -> Predicate:
    return lambda cinema: any(_host(cinema).endswith(d) for d in domains)


def _strategy_is(*keys: str) -> Predicate:
    return lambda cinema: _sid(cinema, "strategy").lower() in keys


#: (predicate, extractor).  Order matters: explicit configuration wins, then
#: the platform fingerprints, and the heuristic last.
STRATEGIES: list[tuple[Predicate, Extractor]] = [
    (_strategy_is("veezi"), _veezi),
    (_strategy_is("fomo"), _fomo),
    (_strategy_is("wpcinema", "wp-cinema"), _wpcinema),
    (_strategy_is("movingstory"), _movingstory),
    (_strategy_is("moonlight"), _moonlight),
    (_domain_is("eclipsecinema.com.au", "eclipse-cinema.com.au", "chinatowncinema.com.au"), _veezi),
    (_domain_is("fomocinemas.com.au"), _fomo),
    (_domain_is("metroboronia.com.au"), _wpcinema),
    (_domain_is("cameocinemas.com.au"), _movingstory),
    (_domain_is("moonlight.com.au"), _moonlight),
    (lambda cinema: True, _generic),
]


def strategy_for(cinema: dict) -> str:
    """Name of the extractor this venue will use (for the refresh report)."""
    for predicate, extractor in STRATEGIES:
        if predicate(cinema):
            return extractor.__name__.lstrip("_")
    return "generic"


class GenericHtmlAdapter(Adapter):
    """Per-venue extractor for cinemas without a shared platform."""

    name = SOURCE

    def fetch(self, cinema: dict, http: Http) -> AdapterOutput:
        cinema_id = self.venue_id(cinema)
        for predicate, extractor in STRATEGIES:
            try:
                matched = predicate(cinema)
            except Exception:                               # noqa: BLE001
                matched = False
            if not matched:
                continue
            try:
                return extractor(cinema, http)
            except Exception as exc:                        # noqa: BLE001
                # One venue's broken site must never abort a refresh; the
                # orchestrator records this note against the venue.
                return AdapterOutput(
                    note=f"{extractor.__name__.lstrip('_')} failed: {type(exc).__name__}: {exc}"
                )
        return AdapterOutput(note=f"no strategy matched venue {cinema_id}")
