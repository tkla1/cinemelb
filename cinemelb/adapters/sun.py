"""Adapter for the Sun Theatre Yarraville (and its Lounge screens).

The Sun runs WordPress with the commercial ``wp-cinema`` plugin.  WordPress
*looks* like it should offer a REST route, but the plugin registers none:
``/wp-json/`` lists only core namespaces, and the sessions are rendered by a
shortcode straight into the page.  So this is an HTML adapter against a very
regular template — cheaper than the 78 film pages the home page links to.

Two forms of page carry the same markup:

* the home page, which shows **today** only;
* ``https://suntheatre.com.au/?wp-cinema-day=N`` for N=0..6, one per tab of
  the day bar.

Each day page renders one ``<div class='wpcinema_movie'>`` per title, and each
session is an ``<a>`` to ``movietkts.com.au`` whose path is
``.../<MOVIECODE>/<YYYY-MM-DD>/<time>/<screen>``.  That path is the most
reliable carrier of the date, time *and* screen name, so sessions are read
from the links themselves rather than from the surrounding text.

Sessions whose online booking has closed (``wpcinema_session_closed``) render
as bare ``<span>``s with no href — they are today's already-started
screenings, so skipping anything without a booking link also keeps past
sessions out of the store without a clock comparison.

Times are wall-clock Melbourne ("7:00pm"), as the cinema advertises them.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time

from ..http import Http
from ..models import Film, film_key, make_screening
from .base import Adapter, AdapterOutput

BASE = "https://suntheatre.com.au"

#: ``.../Sun-Theatre-Yarraville/S26THEFOX/2026-11-26/12:10pm/La-Scala``
_SESSION_LINK = re.compile(
    r"movietkts\.com\.au/Sun-Theatre-Yarraville/([A-Z0-9]+)/"
    r"(\d{4}-\d\d-\d\d)/(\d{1,2}:\d\d[ap]m)/([A-Za-z0-9\-]+)"
)
_HREF = re.compile(r"href='([^']+)'")
_MOVIE_BLOCK = re.compile(
    r"<div class='wpcinema_movie'>(.*?)<div style='clear:both;'></div>", re.S
)
_TITLE = re.compile(r"<h3 class='movietitle'>\s*<a[^>]*>(.*?)</a>", re.S)
_CLOCK = re.compile(r"(\d{1,2}):(\d\d)([ap])m", re.I)
_RATING = re.compile(r"\(([A-Z0-9+]+)\)")
_TAG = re.compile(r"<[^>]+>")
#: ``<span title='Lounge: Click to book for 12:10pm session [in La Scala]' …>``
_SESSION_SPAN = re.compile(
    r"<span title='([^']*)' class='(wpcinema_session_[^']*)'>(.*?)</span>", re.S
)
_FORMATS = {
    "70mm": "70mm",
    "35mm": "35mm",
    "4k": "4K",
    "vistavision": "VistaVision",
    "infinity vision": "Infinity Vision",
    "imax": "IMAX",
}


def _clean(html: str) -> str:
    return re.sub(r"\s+", " ", _TAG.sub(" ", html)).strip()


class SunAdapter(Adapter):
    name = "sun"

    def fetch(self, cinema: dict, http: Http) -> AdapterOutput:
        site = (cinema.get("website") or BASE).rstrip("/")
        cinema_id = self.venue_id(cinema)
        out = AdapterOutput()
        films: dict[str, Film] = {}
        pages_read = 0

        for day_index in range(7):
            url = site + "/" if day_index == 0 else f"{site}/?wp-cinema-day={day_index}"
            try:
                page = http.get(url)
            except Exception:                                    # noqa: BLE001
                continue
            pages_read += 1
            for block in _MOVIE_BLOCK.findall(page.text):
                title_match = _TITLE.search(block)
                if not title_match:
                    continue
                title, rating, flag = self._title_parts(title_match.group(1))
                if not title:
                    continue
                key = film_key(title)

                for span_title, span_class, inner in _SESSION_SPAN.findall(block):
                    link = _HREF.search(inner)
                    if not link or "movietkts" not in link.group(1):
                        continue                    # booking closed / already started
                    parsed = _SESSION_LINK.search(link.group(1))
                    when = self._datetime(parsed.group(2), parsed.group(3)) if parsed else None
                    if not when:
                        continue
                    screen = parsed.group(4).replace("-", " ")
                    # The prefix before "Click to book" holds pricing/screen
                    # labels ("Lounge", "Adults at Kids Prices"); a lowercase
                    # tail such as "onfilm" is a plugin category slug, not a label.
                    prefix = span_title.split("Click to book", 1)[0]
                    labels = [p.strip() for p in prefix.split(":") if p.strip()]
                    labels = [p for p in labels if not p.islower()]
                    if "cat-gold-icon" in inner:
                        labels.append("Lounge")
                    attributes = list(dict.fromkeys(a for a in [flag, *labels] if a))
                    if key not in films:
                        # "Coming soon" blocks have no sessions; only titles we
                        # can actually show are worth a Film row.
                        films[key] = Film(key=key, title=title, rating=rating)
                    out.screenings.append(
                        make_screening(
                            cinema_id=cinema_id,
                            film_title=title,
                            local_naive=when,
                            source=self.name,
                            screen=screen,
                            fmt=self._format_from(title + " " + " ".join(attributes)),
                            attributes=attributes,
                            ticket_url=link.group(1),
                            sold_out="soldout" in span_class,
                            film_key_override=key,
                        )
                    )

        out.films = list(films.values())
        if not pages_read:
            out.note = "could not read any wp-cinema day page"
        elif not out.screenings:
            out.note = "wp-cinema day pages contained no bookable sessions"
        return out

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _title_parts(html: str) -> tuple[str, str, str]:
        """Split ``THE FOX <span class='rating'>(MA15+)</span> <span…>NFT``."""
        title = _clean(html.split("<span", 1)[0])
        rating = ""
        flags: list[str] = []
        for span in re.findall(r"<span[^>]*>(.*?)</span>", html, re.S):
            text = _clean(span)
            if not text:
                continue
            found = _RATING.search(text)
            if found and not rating:
                rating = found.group(1)
            elif not found:
                flags.append(text)              # e.g. "NFT" — No Free Tickets
        return title, rating, " ".join(flags)

    @staticmethod
    def _datetime(day: str, clock: str) -> datetime | None:
        m = _CLOCK.match(clock)
        if not m:
            return None
        hour = int(m.group(1)) % 12 + (12 if m.group(3).lower() == "p" else 0)
        try:
            return datetime.combine(date.fromisoformat(day), time(hour, int(m.group(2))))
        except ValueError:
            return None

    @staticmethod
    def _format_from(text: str) -> str:
        low = text.lower()
        for token, label in _FORMATS.items():
            if token in low:
                return label
        return ""
