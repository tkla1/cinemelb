"""Adapter for the "Moving Story" platform (Lido Hawthorn, Classic Elsternwick,
Cameo Belgrave).

The platform is a server-rendered PHP site whose *date-addressed* pages are the
authoritative source: ``/now-showing/<YYYY-MM-DD>`` lists every film playing
that day, including festival, repertory and one-off screenings, each with its
session times. The per-film pages (``/movies/<slug>``) show a longer horizon but
only for films linked from the home page, so they silently miss festival
programmes — the date pages are the complete view.

The date is carried in the URL, so there is no tab-index guessing: an earlier
version resolved a bare "Friday" tab by its position in the list and stored
tomorrow's sessions a day early whenever the "Today" tab disappeared. Reading
the date from the URL removes that whole class of error.

Times are wall-clock ("1:30 pm") and read as Melbourne local. Titles and
attributes are HTML-escaped in the source (``Disney&#x2019;s Hexed``) and are
unescaped here.
"""

from __future__ import annotations

import html as _html
import re
from datetime import date, datetime, time, timedelta

from ..http import Http
from ..models import Film, film_key, make_screening
from .base import Adapter, AdapterOutput

#: A date tab: <a href="/now-showing/2026-12-04" ...>Fri 4 Dec</a>
_DAY_LINK = re.compile(r'href="/now-showing/(\d{4}-\d{2}-\d{2})"')

#: Top-level film blocks. Each opens with <li class="Stack"> and contains one
#: film's title and its Sessions list.
_STACK = re.compile(r'<li class="Stack">')

#: The film title inside a block.
_TITLE = re.compile(
    r'<span class="Title">\s*<a[^>]*>\s*(.*?)\s*</a>', re.S
)

#: One session anchor: carries the ticket URL, numeric id, title and genres.
#: The href is OPTIONAL: a sold-out session renders as a bare
#: ``<a class="Link sessions-link" data-id="..." ...>`` with no href at all,
#: so requiring one silently dropped every sold-out performance.
_SESSION = re.compile(
    r'<a class="Link sessions-link"'
    r'(?:\s+href="([^"]*)")?'
    r'(?:\s+data-id="([^"]*)")?'
    r'(?:\s+data-name="([^"]*)")?'
    r'(?:\s+data-genre="([^"]*)")?'
    r'[^>]*>(.*?)</a>',
    re.S,
)

_TIME = re.compile(r'<span class="Time">\s*([^<]+?)\s*</span>')
_ATTR = re.compile(r'<span class="Attribute[^"]*">\s*([^<]+?)\s*</span>')

#: "1:30 pm", "12:45 pm", "6:20 pm" — the site never uses 24-hour times.
_CLOCK = re.compile(r"(\d{1,2}):(\d{2})\s*([ap])\.?m\.?", re.I)

#: How far ahead to scrape. The site's own date tabs reach ~20 days; going
#: further costs requests for pages that usually return nothing.
HORIZON_DAYS = 21


def _parse_clock(text: str) -> time | None:
    m = _CLOCK.search(text)
    if not m:
        return None
    hour = int(m.group(1)) % 12
    if m.group(3).lower() == "p":
        hour += 12
    return time(hour, int(m.group(2)))


def _unescape(text: str) -> str:
    return _html.unescape(text).strip()


class MovingStoryAdapter(Adapter):
    """Scrape the date-addressed pages of a Moving Story venue."""

    name = "movingstory"

    def fetch(self, cinema: dict, http: Http) -> AdapterOutput:
        site = (cinema.get("website") or "").rstrip("/")
        if not site:
            return AdapterOutput(note="no website on record")

        today = date.today()
        out = AdapterOutput()

        # Discover the horizon from the first date page's tab strip. The tabs
        # are the venue's own idea of what is bookable, so follow them rather
        # than assuming a fixed window.
        try:
            first = http.get(f"{site}/now-showing/{today.isoformat()}")
        except Exception as exc:  # noqa: BLE001
            return AdapterOutput(note=f"date page fetch failed: {exc}")

        dates = sorted(set(_DAY_LINK.findall(first.text)))
        # The tab strip often omits today once every session has started, so
        # include today explicitly rather than losing the day's remaining shows.
        if today.isoformat() not in dates:
            dates.insert(0, today.isoformat())
        horizon = today + timedelta(days=HORIZON_DAYS)
        dates = [d for d in dates if d <= horizon.isoformat()]

        seen_films: set[str] = set()
        pages = {today.isoformat(): first.text}

        for day in dates:
            text = pages.get(day)
            if text is None:
                try:
                    text = http.get(f"{site}/now-showing/{day}").text
                except Exception:  # noqa: BLE001
                    continue
            self._parse_day(text, day, cinema, site, out, seen_films)

        if not out.screenings:
            out.note = "no sessions found on the date-addressed pages"
        return out

    # -- parsing ----------------------------------------------------------
    def _parse_day(
        self,
        text: str,
        day: str,
        cinema: dict,
        site: str,
        out: AdapterOutput,
        seen_films: set[str],
    ) -> None:
        try:
            day_date = date.fromisoformat(day)
        except ValueError:
            return

        # Split into film blocks. The first segment is the page preamble.
        segments = _STACK.split(text)[1:]
        for segment in segments:
            title_m = _TITLE.search(segment)
            if not title_m:
                continue
            title = _unescape(title_m.group(1))
            if not title:
                continue

            for sess in _SESSION.finditer(segment):
                href, _sid, _name, genres, body = sess.groups()
                time_m = _TIME.search(body)
                if not time_m:
                    continue
                clock = _parse_clock(time_m.group(1))
                if clock is None:
                    continue

                attrs = [_unescape(a) for a in _ATTR.findall(body) if _unescape(a)]
                sold_out = any("sold out" in a.lower() for a in attrs)
                ticket = ""
                if href:
                    ticket = _unescape(href).replace("&amp;", "&")
                    if ticket.startswith("/"):
                        ticket = site + ticket

                # Genre strings are per-session in the markup; keep the first
                # film's genres as catalogue metadata.
                if title not in seen_films:
                    seen_films.add(title)
                    genres_clean = [g.strip() for g in _unescape(genres or "").split(",") if g.strip()]
                    out.films.append(
                        Film(key=film_key(title), title=title, genres=genres_clean)
                    )

                out.screenings.append(
                    make_screening(
                        cinema_id=cinema["id"],
                        film_title=title,
                        local_naive=datetime.combine(day_date, clock),
                        source=self.name,
                        screen="",
                        attributes=attrs,
                        ticket_url=ticket,
                        sold_out=sold_out,
                    )
                )
