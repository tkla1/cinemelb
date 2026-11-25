"""Adapter for the Indy Systems cinema platform.

Brunswick Picture House and Thornbury Picture House are two single-screen
arthouse venues on the same New Zealand-built platform (``indy-systems``): the
site is a Quasar single-page app served from the venue's own domain, and the
timetable only exists after JavaScript runs — which is why the generic HTML
adapter (rightly) returns nothing for them.

Two things in the site's own bundle matter:

* The Apollo client posts to ``/graphql`` **on the venue's own host**, and the
  SPA tags every request with ``client-type: consumer`` plus ``site-id``.  Those
  two headers are what put the API in "consumer" mode where public showings are
  readable; without ``site-id`` the server answers
  ``Consumer Site request with no Site ID (Code: 901)``.
* Each site's numeric id is baked into its bundle (``P=g?"72":null``):
  Brunswick is 72, Thornbury is 12.  The registry carries the id in
  ``source_ids["indy"]``.

The schema is not introspectable (``__schema``/``__type`` are switched off, and
a bad query comes back as ``Code 232``), so the query below was recovered from
the client and verified field by field against the live endpoint.  The site's
*front end* is currently broken — it posts without a query and shows
"Code 232" to visitors — but the API itself is healthy, so this adapter reads
the same data the site would.

Timestamps arrive as real UTC (``2026-11-25T09:15:00Z``); the SPA converts them
with ``inTimeZone(site.timeZone)``, which is ``Australia/Melbourne`` for both
venues.  This adapter performs the same conversion and hands ``make_screening``
a naive wall-clock time, so an 8:15pm session stays 8:15pm rather than landing
at 9:15am.

``showingsForDate`` with no ``date`` argument returns every upcoming session,
so one POST per venue covers the whole run.
"""

from __future__ import annotations

import html as html_lib
import json
import re
from datetime import datetime

from ..http import Http
from ..models import MELBOURNE_TZ, Film, film_key, make_screening
from .base import Adapter, AdapterOutput

SOURCE = "indy"

#: The venue's own host proxies this to the platform; a GET here returns 404.
ENDPOINT = "{site}/graphql"

#: Poster ids are imgix paths on the platform's asset host.
IMGIX = "https://indy-systems.imgix.net"
POSTER_PARAMS = "?fit=crop&w=400&h=600&fm=jpeg&auto=format,compress&cs=origin"

#: Recovered from the consumer bundle and validated against the live schema.
#: ``past`` is the server's own answer to "has this session started?", which is
#: what lets an adapter drop this afternoon's 11am session without guessing.
SHOWINGS_QUERY = """
query ConsumerShowings {
  showingsForDate {
    data {
      id
      time
      published
      past
      movie {
        id
        name
        duration
        rating
        genre
        allGenres
        synopsis
        directedBy
        starring
        posterImage
      }
      screen {
        name
      }
    }
  }
}
"""

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def _text(fragment: str) -> str:
    """Strip tags/entities from a synopsis fragment."""
    plain = _TAG.sub(" ", fragment or "")
    return _WS.sub(" ", html_lib.unescape(plain)).strip()


def _local(iso: str) -> datetime | None:
    """Parse the API's UTC timestamp into naive Melbourne wall-clock time.

    This is the one place the project's usual "Australian APIs emit a bogus Z"
    worry does *not* apply: the SPA itself converts these instants with
    ``inTimeZone(site.timeZone)``, and the ``past`` flags line up with a true
    UTC reading (a 09:15Z session is still to come at 19:00 local).
    """
    text = (iso or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        stamped = datetime.fromisoformat(text)
    except ValueError:
        return None
    if stamped.tzinfo is None:              # defensive: treat as already local
        return stamped
    return stamped.astimezone(MELBOURNE_TZ).replace(tzinfo=None)


def _split(value: object) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in str(value).split(",") if part.strip()]


class IndyAdapter(Adapter):
    """Indy Systems consumer GraphQL (Brunswick / Thornbury Picture House)."""

    name = SOURCE

    def fetch(self, cinema: dict, http: Http) -> AdapterOutput:
        cinema_id = self.venue_id(cinema)
        site = str(cinema.get("website") or "").rstrip("/")
        site_id = self.source_id(cinema)
        if not site_id:
            return AdapterOutput(
                note="no indy site id in source_ids; add {\"indy\": \"<numeric id>\"}"
                     " to data/cinemas.json"
            )
        if not site:
            return AdapterOutput(note="no website on record for this indy-systems venue")

        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "client-type": "consumer",
            "site-id": site_id,
        }
        try:
            resp = http.post_json(ENDPOINT.format(site=site),
                                  {"query": SHOWINGS_QUERY}, headers=headers)
        except Exception as exc:                                # noqa: BLE001
            return AdapterOutput(note=f"indy GraphQL unavailable: {exc}")

        try:
            body = resp.json()
        except json.JSONDecodeError:
            return AdapterOutput(
                note=f"indy GraphQL returned non-JSON (status {resp.status})"
            )
        if not isinstance(body, dict):
            return AdapterOutput(note="indy GraphQL returned an unexpected payload")

        # The platform wraps schema/transport problems in either shape; report
        # them verbatim rather than pretending the venue has no sessions.
        problem = body.get("error") or (body.get("errors") or [None])[0]
        if problem:
            message = problem.get("message_to_log") or problem.get("message") or str(problem)
            return AdapterOutput(note=f"indy GraphQL error: {message}"[:300])

        rows = (((body.get("data") or {}).get("showingsForDate") or {}).get("data")) or []
        if not rows:
            return AdapterOutput(
                note=f"indy GraphQL returned no showings for site {site_id}"
            )

        out = AdapterOutput()
        films: dict[str, Film] = {}
        seen: set[tuple[str, str, str]] = set()

        for row in rows:
            if not row.get("published", True) or row.get("past"):
                continue
            start = _local(str(row.get("time") or ""))
            movie = row.get("movie") or {}
            title = _text(str(movie.get("name") or ""))
            if not (start and title):
                continue

            screen = _text(str((row.get("screen") or {}).get("name") or ""))
            key = film_key(title)
            stamp = start.strftime("%Y-%m-%dT%H:%M")
            if (key, stamp, screen) in seen:
                continue
            seen.add((key, stamp, screen))

            if key not in films:
                poster = str(movie.get("posterImage") or "")
                runtime = movie.get("duration")
                films[key] = Film(
                    key=key,
                    title=title,
                    runtime_minutes=int(runtime) if str(runtime or "").isdigit() else None,
                    rating=str(movie.get("rating") or ""),
                    genres=_split(movie.get("allGenres")) or _split(movie.get("genre")),
                    synopsis=_text(str(movie.get("synopsis") or "")),
                    poster_url=f"{IMGIX}/{poster}{POSTER_PARAMS}" if poster else "",
                    directors=_split(movie.get("directedBy")),
                )

            out.screenings.append(
                make_screening(
                    cinema_id=cinema_id,
                    film_title=title,
                    local_naive=start,
                    source=self.name,
                    screen=screen,
                    film_key_override=key,
                )
            )

        out.films = list(films.values())
        if not out.screenings:
            out.note = (
                f"indy API returned {len(rows)} showings for site {site_id}, "
                "all of them past or unpublished"
            )
        return out
