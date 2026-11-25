"""Palace Cinemas adapter.

Palace operates its own Next.js site and — helpfully — ships the *entire*
session list for a venue inside the page's data payload
(``/_next/data/<buildId>/cinemas/<slug>.json``).  That payload is richer than
most chains expose: film title, rating, synopsis, genres, runtime, trailer,
and every session with its id, screen attributes, closed-caption and
audio-description flags.

The only wrinkle is the rotating ``buildId`` in the data URL, which we read
from any Palace page and cache for the life of the process.
"""

from __future__ import annotations

from datetime import datetime

from ..http import Http
from ..models import Film, film_key, make_screening
from .base import Adapter, AdapterOutput

BASE = "https://www.palacecinemas.com.au"
_BUILD_ID: str | None = None


def _build_id(http: Http) -> str:
    """Discover the current Next.js build id (it changes on each deploy)."""
    global _BUILD_ID
    if _BUILD_ID:
        return _BUILD_ID
    resp = http.get(f"{BASE}/cinemas/")
    import re

    m = re.search(r'/_next/static/([A-Za-z0-9_\-]+)/_buildManifest\.js', resp.text)
    if not m:
        raise RuntimeError("Palace buildId not found — site layout changed?")
    _BUILD_ID = m.group(1)
    return _BUILD_ID


def _parse_local(value: str) -> datetime | None:
    """Palace emits ``2026-11-25T19:00:00.000Z`` — local time wearing a UTC hat.

    The trailing ``.000Z`` is load-bearing for parsing (it must be dropped
    before ``strptime``) and meaningless for meaning (a 7pm session is 7pm
    Melbourne, not 7pm UTC).
    """
    if not value:
        return None
    txt = value.strip().replace("Z", "")
    if "." in txt:
        txt = txt.split(".", 1)[0]
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(txt[:19], fmt)
        except ValueError:
            continue
    return None


class PalaceAdapter(Adapter):
    name = "palace"

    def fetch(self, cinema: dict, http: Http) -> AdapterOutput:
        # The data endpoint is keyed by slug ("palace-balwyn"), not by the
        # numeric cinemaId that the API's cinema list uses.
        ids = cinema.get("source_ids") or {}
        slug = ids.get("palace_slug") or cinema.get("flicks_slug") or ""
        if not slug:
            return AdapterOutput(note="no palace slug in source_ids")

        build = _build_id(http)
        url = f"{BASE}/_next/data/{build}/cinemas/{slug}.json"
        payload = http.get_json(url, params={"slug": slug})

        page = (payload or {}).get("pageProps") or {}
        cinema_id = self.venue_id(cinema)
        out = AdapterOutput()

        for movie in page.get("sessions") or []:
            title = (movie.get("title") or "").strip()
            if not title:
                continue
            key = film_key(title)

            runtime_txt = str(movie.get("runTime") or "")
            runtime = None
            digits = "".join(ch if ch.isdigit() else " " for ch in runtime_txt).split()
            if digits:
                runtime = int(digits[0])

            out.films.append(
                Film(
                    key=key,
                    title=title,
                    runtime_minutes=runtime,
                    rating=(movie.get("rating") or "").strip(),
                    genres=[g for g in (movie.get("genreNames") or []) if g],
                    synopsis=(movie.get("synopsis") or "").strip(),
                    trailer_url=(movie.get("trailerUrl") or ""),
                    poster_url=(movie.get("posterOverride") or ""),
                )
            )

            for s in movie.get("sessions") or []:
                local = _parse_local(s.get("date") or "")
                if not local:
                    continue
                attrs: list[str] = []
                if s.get("displayAttributeText"):
                    attrs.append(str(s["displayAttributeText"]))
                for info in s.get("sessionInfo") or []:
                    if info:
                        attrs.append(str(info))

                fmt = ""
                for token in ("35mm", "70mm", "4K", "IMAX", "3D"):
                    if token.lower() in " ".join(attrs).lower():
                        fmt = token
                        break

                # Palace's payload carries no booking URL, and their ticketing
                # host is not part of the public API — so link to the venue's
                # own session page rather than inventing a deep link that 404s.
                ticket = f"{BASE}/cinemas/{slug}/"
                out.screenings.append(
                    make_screening(
                        cinema_id=cinema_id,
                        film_title=title,
                        local_naive=local,
                        source=self.name,
                        screen=str(s.get("screen") or ""),
                        fmt=fmt,
                        attributes=attrs,
                        ticket_url=ticket,
                        film_key_override=key,
                    )
                )

        if not out.screenings:
            out.note = "payload contained no sessions"
        return out
