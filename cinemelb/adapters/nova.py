"""Adapter for Cinema Nova, Carlton.

Nova runs a hand-written PHP site, but — usefully — it exposes the whole week
through a *date* URL family: ``/films-by-date-<epoch>``, where ``<epoch>`` is
local (Melbourne) midnight as a Unix timestamp.  The seven epochs are listed in
the "Search by Day" ``<select>`` on any page, so we read them from the home
page rather than computing them (that also picks up the site's own idea of
"today").

Each date page renders one ``<div class="panel-film">`` per title, and each
session is an ``<a class="showtime">`` whose ``href`` carries the ticketing
``txtSessionId``; the time is the ``<p>HH:MM</p>`` inside it (24-hour, already
Melbourne local) and accessibility notes such as ``CC & AD`` sit in a
``<small>``.  The site does not publish screen names anywhere on these pages.

Seven date pages — plus the home page, which carries the epoch list — yield
roughly 80 sessions per day, enough for the whole week without touching the
individual film pages.
"""

from __future__ import annotations

import html as html_lib
import re
from datetime import datetime, time
from urllib.parse import urljoin

from ..http import Http
from ..models import Film, MELBOURNE_TZ, film_key, make_screening
from .base import Adapter, AdapterOutput

BASE = "https://www.cinemanova.com.au"

_EPOCH = re.compile(r"films-by-date-(\d+)")
_PANEL_SPLIT = re.compile(r'<div class="panel-film[^"]*">')
_TITLE = re.compile(r'<a class="film-title"[^>]*>\s*<h4>(.*?)</h4>', re.S)
_POSTER = re.compile(r'<img src="([^"]+)"')
_SYNOPSIS = re.compile(r"<p>(.*?)</p>", re.S)
_SHOWTIME = re.compile(r'<a class="showtime" href="([^"]+)"[^>]*>(.*?)</a>', re.S)
_CLOCK = re.compile(r"<p>\s*(\d{1,2}):(\d{2})\s*</p>")
_NOTE = re.compile(r"<small>\s*(.*?)\s*</small>", re.S)
_TAG = re.compile(r"<[^>]+>")
_FORMATS = {"70mm": "70mm", "35mm": "35mm", "4k": "4K"}


def _text(fragment: str) -> str:
    return html_lib.unescape(re.sub(r"\s+", " ", _TAG.sub(" ", fragment))).strip()


class NovaAdapter(Adapter):
    name = "nova"

    def fetch(self, cinema: dict, http: Http) -> AdapterOutput:
        site = (cinema.get("website") or BASE).rstrip("/")
        cinema_id = self.venue_id(cinema)
        out = AdapterOutput()

        try:
            home = http.get(site + "/")
        except Exception as exc:                                 # noqa: BLE001
            return AdapterOutput(note=f"home page unavailable: {exc}")
        epochs = list(dict.fromkeys(_EPOCH.findall(home.text)))
        if not epochs:
            return AdapterOutput(note="no films-by-date links on home page")

        films: dict[str, Film] = {}
        for epoch in epochs:
            day = datetime.fromtimestamp(int(epoch), MELBOURNE_TZ).date()
            try:
                page = http.get(f"{site}/films-by-date-{epoch}")
            except Exception:                                    # noqa: BLE001
                continue

            # Split on the panel opener and keep the index-0 preamble out.
            for panel in _PANEL_SPLIT.split(page.text)[1:]:
                title_match = _TITLE.search(panel)
                if not title_match:
                    continue
                title = _text(title_match.group(1))
                if not title:
                    continue
                key = film_key(title)
                if key not in films:
                    # Keep the search above the sessions block, otherwise a
                    # panel without a teaser would "find" the time in <p>.
                    body = panel[title_match.end():]
                    cut = body.find('class="showtime"')
                    if cut >= 0:
                        body = body[:cut]
                    synopsis = _SYNOPSIS.search(body)
                    poster = _POSTER.search(panel)
                    blurb = _text(synopsis.group(1)) if synopsis else ""
                    # The teaser paragraph ends with a "more" link we don't want.
                    blurb = re.sub(r"\s*more\s*$", "", blurb).rstrip(" .…")
                    films[key] = Film(
                        key=key,
                        title=title,
                        synopsis=blurb,
                        # Nova's teaser images are site-relative in places
                        # ("/media/Webtiles/…"), which only resolve on its host.
                        poster_url=urljoin(
                            site + "/", poster.group(1)
                        ) if poster else "",
                    )

                for href, inner in _SHOWTIME.findall(panel):
                    clock = _CLOCK.search(inner)
                    if not clock:
                        continue
                    try:
                        start = time(int(clock.group(1)), int(clock.group(2)))
                    except ValueError:
                        continue
                    notes = [_text(n) for n in _NOTE.findall(inner) if _text(n)]
                    attributes = [
                        part.strip()
                        for note in notes
                        for part in re.split(r"\s*&\s*", note)
                        if part.strip()
                    ]
                    out.screenings.append(
                        make_screening(
                            cinema_id=cinema_id,
                            film_title=title,
                            local_naive=datetime.combine(day, start),
                            source=self.name,
                            fmt=self._format_from(title + " " + " ".join(notes)),
                            attributes=attributes,
                            ticket_url=html_lib.unescape(href),
                            film_key_override=key,
                        )
                    )

        out.films = list(films.values())
        if not out.screenings:
            out.note = "films-by-date pages contained no sessions"
        return out

    @staticmethod
    def _format_from(text: str) -> str:
        low = text.lower()
        for token, label in _FORMATS.items():
            if token in low:
                return label
        return ""
