"""Hoyts adapter.

Hoyts (an EVT brand) exposes a clean JSON API used by its own website:

* ``/api/cinemas``  — every venue, with address + coordinates
* ``/api/movies``   — every title, keyed by ``vistaId`` (e.g. ``HO00019379``)
* ``/api/sessions`` — **all** sessions for **all** venues in one ~9 MB array

The sessions endpoint is the reason this adapter overrides
:meth:`Adapter.fetch_many`: one download covers all twelve Victorian venues,
instead of twelve identical multi-megabyte requests.

Times come back as local wall-clock without a timezone (``2026-11-25T13:30:00``)
while ``utcDate`` is a null sentinel (``0001-01-01``) — so the local field is
the only trustworthy one, which is exactly what ``make_screening`` expects.
"""

from __future__ import annotations

from datetime import datetime

from ..http import Http
from ..models import Film, film_key, make_screening
from .base import Adapter, AdapterOutput

BASE = "https://apim-aea.hoyts.com.au/cinemaapi-au-live/api"

#: Hoyts tags that describe the *presentation* rather than ticketing detail.
_FORMAT_TAGS = {
    "IMAX": "IMAX",
    "XTREMESCREEN": "Xtremescreen",
    "VMAX": "Vmax",
    "GOLDCLASS": "Gold Class",
    "3D": "3D",
    "4DX": "4DX",
    "DOLBY": "Dolby Atmos",
    "LUX": "LUX",
    "TITAN": "Titan",
}


def _movie_metadata(http: Http) -> tuple[dict[str, dict], list[Film]]:
    """Map vistaId -> movie record, plus the Film rows to persist."""
    movies = http.get_json(f"{BASE}/movies")
    by_vista: dict[str, dict] = {}
    films: list[Film] = []
    for m in movies:
        vid = m.get("vistaId")
        if vid:
            by_vista[vid] = m
        name = (m.get("name") or "").strip()
        if not name:
            continue
        runtime = (m.get("runtime") or {}).get("minutes") or None
        rating = (m.get("rating") or {}).get("id") or ""
        genres = []
        for g in m.get("genres") or []:
            if isinstance(g, dict):
                genres.append(g.get("name", ""))
            elif isinstance(g, str):
                genres.append(g)
        films.append(
            Film(
                key=film_key(name),
                title=name,
                runtime_minutes=runtime,
                rating=str(rating),
                genres=[g for g in genres if g],
                synopsis=(m.get("summary") or "").strip(),
            )
        )
    return by_vista, films


class HoytsAdapter(Adapter):
    name = "hoyts"

    def fetch(self, cinema: dict, http: Http) -> AdapterOutput:
        return self.fetch_many([cinema], http).get(self.venue_id(cinema), AdapterOutput())

    def fetch_many(self, cinemas: list[dict], http: Http) -> dict[str, AdapterOutput]:
        out = {self.venue_id(c): AdapterOutput() for c in cinemas}
        by_code = {self.source_id(c, "hoyts"): self.venue_id(c) for c in cinemas}

        try:
            by_vista, films = _movie_metadata(http)
        except Exception as exc:                                 # noqa: BLE001
            for o in out.values():
                o.note = f"movies fetch failed: {exc}"
            return out
        for o in out.values():
            o.films.extend(films)

        try:
            sessions = http.get_json(f"{BASE}/sessions")
        except Exception as exc:                                 # noqa: BLE001
            for o in out.values():
                o.note = f"sessions fetch failed: {exc}"
            return out

        for s in sessions:
            code = s.get("cinemaId")
            venue = by_code.get(code)
            if not venue:
                continue
            if s.get("disabled"):
                continue

            raw_date = (s.get("date") or "").strip()
            if not raw_date:
                continue
            try:
                local = datetime.strptime(raw_date[:19], "%Y-%m-%dT%H:%M:%S")
            except ValueError:
                continue

            movie_id = s.get("movieId") or ""
            meta = by_vista.get(movie_id) or {}
            title = (meta.get("name") or "").strip()
            if not title:
                # Sessions occasionally reference a title the catalogue omits
                # (private hire, one-off events).  Skipping keeps the data
                # honest rather than inventing a name.
                continue

            tags = [str(t) for t in (s.get("originalTags") or [])]
            fmt = ""
            upper_tags = {t.upper() for t in tags}
            for tag, label in _FORMAT_TAGS.items():
                if tag in upper_tags:
                    fmt = label
                    break

            attrs = [t for t in tags if t.upper() in ("CC", "AD", "OCAPTIONS", "AUDIO")]
            ticket = ""
            link = s.get("link") or ""
            if link:
                ticket = "https://www.hoyts.com.au" + link

            out[venue].screenings.append(
                make_screening(
                    cinema_id=venue,
                    film_title=title,
                    local_naive=local,
                    source=self.name,
                    screen=str(s.get("screenName") or ""),
                    fmt=fmt,
                    attributes=attrs,
                    ticket_url=ticket,
                )
            )

        for venue_id, o in out.items():
            if not o.screenings:
                o.note = o.note or "no upcoming sessions in the Hoyts feed"
        return out
