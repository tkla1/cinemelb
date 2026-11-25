"""Adapter for the Flicks-built white-label site Waverley Cinemas runs.

Waverley's site is a statically exported Next.js app (``"nextExport": true``)
whose hydration payload is empty — every page is a shell, which is why the
generic HTML adapter finds nothing to read.  The browser fills it from a small
JSON API served *by the site itself*:

* ``GET /api/movie/playing-now`` — every film on sale with its whole
  ``sessionTimes`` list.  One request covers the entire run (about 200
  sessions over four weeks at the time of writing), so there is no per-film
  or per-day fan-out.  This is the only call the adapter makes.

(The neighbouring ``/api/cinema/info`` carries the venue record; its
``timezone`` is ``Australia/Sydney``, but that only matters for the site's
clock, since session times here are wall-clock strings.)

Sessions are published as naive local wall-clock, which is exactly what
:func:`cinemelb.models.make_screening` wants: ``{"date": "2026-11-26",
"time": "2:30pm"}``.  Nothing is converted, so the session reads on the ticket
exactly as the cinema prints it.

Two details of the payload shape:

* A session's ``attributes`` mix the *screen* ("Cinema 1") with badges
  ("NEW", "FinalWeeks", "A@Kids", …).  Only the screen-looking ones become
  ``screen``; the rest are kept as attributes.
* ``bookingLink`` points at the venue's Veezi box office and is carried
  straight through as ``ticket_url``.

``director`` is a list in the film API but a nested film record in this feed,
so both shapes are accepted.
"""

from __future__ import annotations

import html as html_lib
import re
from datetime import date, datetime, time

from ..http import Http
from ..models import Film, film_key, make_screening
from .base import Adapter, AdapterOutput

SOURCE = "waverley"

#: The whole timetable in one document.
PLAYING_NOW = "{site}/api/movie/playing-now"

#: "2:30pm", "11:45am" — the site publishes 12-hour times; a bare 24-hour
#: clock is accepted too so a style change cannot silently empty the venue.
_CLOCK = re.compile(r"(\d{1,2}):(\d{2})\s*(?:([ap])\.?m\b)?", re.I)

#: Screen names in the attribute list look like "Cinema 3" / "Screen 2".
_SCREEN = re.compile(r"^(?:cinema|screen|theatre)\s*\d+\b", re.I)

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def _text(fragment: object) -> str:
    plain = _TAG.sub(" ", str(fragment or ""))
    return _WS.sub(" ", html_lib.unescape(plain)).strip()


def _clock(value: object) -> time | None:
    match = _CLOCK.search(str(value or ""))
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    meridiem = (match.group(3) or "").lower()
    if minute > 59:
        return None
    if meridiem:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if meridiem == "p" else 0)
    elif hour > 23:
        return None
    return time(hour, minute)


def _directors(film: dict) -> list[str]:
    """The feed nests a whole film record under ``director``; unwrap it."""
    value = film.get("director")
    if isinstance(value, dict):
        value = value.get("director")
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, list):
        return [str(part).strip() for part in value if str(part).strip()]
    return []


def _minutes(value: object) -> int | None:
    text = str(value or "").strip()
    return int(text) if text.isdigit() else None


class WaverleyAdapter(Adapter):
    """Flicks/vwassets white-label cinema site API (Waverley Cinemas)."""

    name = SOURCE

    def fetch(self, cinema: dict, http: Http) -> AdapterOutput:
        cinema_id = self.venue_id(cinema)
        site = str(cinema.get("website") or "").strip().rstrip("/")
        if not site:
            # The upstream identifier for this platform *is* the venue's host.
            host = self.source_id(cinema).strip().rstrip("/")
            site = host if host.startswith("http") else (f"https://{host}" if host else "")
        if not site:
            return AdapterOutput(note="no website on record for this venue")

        try:
            feed = http.get_json(PLAYING_NOW.format(site=site))
        except Exception as exc:                                 # noqa: BLE001
            return AdapterOutput(note=f"playing-now feed unavailable: {exc}")

        movies = feed if isinstance(feed, list) else (feed.get("movies") or [])
        if not movies:
            return AdapterOutput(
                note=f"{site}/api/movie/playing-now returned no films"
            )

        out = AdapterOutput()
        films: dict[str, Film] = {}
        seen: set[tuple[str, str, str]] = set()

        for movie in movies:
            title = _text(movie.get("title"))
            if not title:
                continue
            key = film_key(title)
            if key not in films:
                films[key] = Film(
                    key=key,
                    title=title,
                    runtime_minutes=_minutes(movie.get("duration")),
                    rating=_text(movie.get("classification")),
                    synopsis=_text(movie.get("synopsisShort")),
                    poster_url=str(movie.get("imageVerticalUrl") or ""),
                    trailer_url=self._trailer(movie),
                    directors=_directors(movie),
                )

            for session in movie.get("sessionTimes") or []:
                day_text = str(session.get("date") or "").strip()
                clock = _clock(session.get("time"))
                if not (day_text and clock):
                    continue
                try:
                    day = date.fromisoformat(day_text[:10])
                except ValueError:
                    continue

                badges = [str(a.get("shortName") or "").strip()
                          for a in session.get("attributes") or []]
                badges = [b for b in badges if b]
                screens = [b for b in badges if _SCREEN.match(b)]
                screen = screens[0] if screens else ""
                extras = [b for b in badges if b not in screens]

                stamp = datetime.combine(day, clock).strftime("%Y-%m-%dT%H:%M")
                if (key, stamp, screen) in seen:
                    continue
                seen.add((key, stamp, screen))

                out.screenings.append(
                    make_screening(
                        cinema_id=cinema_id,
                        film_title=title,
                        local_naive=datetime.combine(day, clock),
                        source=self.name,
                        screen=screen,
                        attributes=extras,
                        ticket_url=str(session.get("bookingLink") or ""),
                        sold_out=bool(session.get("isSoldOut")),
                        film_key_override=key,
                    )
                )

        out.films = list(films.values())
        if not out.screenings:
            out.note = (
                f"{len(movies)} films in the playing-now feed carried no session times"
            )
        return out

    @staticmethod
    def _trailer(movie: dict) -> str:
        value = str(movie.get("trailer") or "").strip()
        if not value:
            return ""
        if value.startswith("http"):
            return value
        provider = str(movie.get("trailerProvider") or "").lower()
        if provider in ("youtube", "yt") or len(value) == 11:
            return f"https://www.youtube.com/watch?v={value}"
        return ""
