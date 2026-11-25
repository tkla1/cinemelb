"""Reading Cinemas adapter.

Reading publishes no server-rendered pages: readingcinemas.com.au is a
create-react-app shell and every session arrives from a JSON API on its own
host, ``prod-api.readingcinemas.com.au``.

That API is not open, but it does not need an account: it hands out a token
to anyone who asks, and the SPA does exactly that on first paint.

* ``GET /settings/1`` returns ``data.settings.token``, a Cognito JWT with
  scope ``prod/Read`` and a one-hour life.  The SPA keeps it in
  ``sessionStorage["token"]`` (``GET_SETTINGS_SUCCESS``) and every later
  request carries ``Authorization: Bearer <token>``.  Without that header
  ``/getcinemas`` and ``/films`` answer ``401 Unauthorized``.
* ``GET /getcinemas?countryId=1`` lists every venue; its ``slug`` field is
  the ``cinemaId`` the rest of the API uses.
* ``GET /films?countryId=1&cinemaId=<slug>&status=nowShowing&flag=`` returns
  every title at one venue *with its sessions*, so a venue costs one request.

Two field quirks are load-bearing:

* ``date_time`` is ``2026-11-25T21:15:00+11:00``, a real Melbourne offset
  around a wall-clock time.  We keep the wall-clock part and let
  :func:`cinemelb.models.make_screening` attach the timezone, because the
  cinema advertises the clock time on the ticket.
* ``ratingName`` holds the consumer advice ("Mature themes and violence"),
  not the M/MA15+ code: Reading renders the classification as a rating
  *icon* image, so the advice text is the most authoritative rating field
  the API exposes.
"""

from __future__ import annotations

import html
import json
import re
from datetime import datetime
from typing import Any

from ..http import Http
from ..models import Film, film_key, make_screening
from .base import Adapter, AdapterOutput

BASE = "https://prod-api.readingcinemas.com.au"
SITE = "https://readingcinemas.com.au"

#: Reading's Australian storefront.  The same API serves New Zealand and the
#: Angelika/State brands under other ids, none of which are Melbourne.
COUNTRY_ID = "1"

#: ``showtypes[].type`` -> presentation label.  "Standard" is the default and
#: says nothing worth storing, so it is deliberately absent.
_FORMATS = {"Premium": "Premium", "TitanLuxe": "Titan Luxe", "TitanXc": "Titan XC"}

#: session flags of interest; the API packs each one as JSON into flag1..flag3.
_FLAGS = ("flag1", "flag2", "flag3")

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def _text(value: Any) -> str:
    """Flatten Reading's inline HTML (paragraph tags, entities) to plain text."""
    if not value:
        return ""
    txt = html.unescape(_TAG.sub(" ", str(value)))
    return _WS.sub(" ", txt).strip()


def _parse_local(value: str) -> datetime | None:
    """``2026-11-25T21:15:00+11:00`` -> naive 21:15 wall clock.

    The offset is dropped rather than applied: it describes the same clock
    the ticket prints, and ``make_screening`` re-attaches Melbourne itself.
    """
    if not value or len(value) < 19:
        return None
    try:
        return datetime.strptime(value[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None


def _attributes(performance: dict) -> list[str]:
    """Session flags in the API's own short codes ("CCAD", "REC", ...)."""
    tags: list[str] = []
    for field in _FLAGS:
        raw = performance.get(field)
        if not raw:
            continue
        try:
            tag = str(json.loads(raw).get("short") or "")
        except (TypeError, ValueError, AttributeError):
            tag = ""
        if tag and tag not in tags:
            tags.append(tag)
    return tags


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _token(http: Http) -> str:
    """Mint the anonymous bearer token the SPA uses for every call.

    Fetched fresh every time on purpose: the token inside this payload
    expires after an hour, so a cached copy would eventually be handed back
    dead and every venue would fail its request.
    """
    payload = http.get_json(f"{BASE}/settings/1", fresh=True)
    try:
        return str(payload["data"]["settings"]["token"])
    except (KeyError, TypeError):
        raise RuntimeError("settings payload carried no token") from None


def _venues(http: Http, token: str) -> list[dict]:
    """Every venue the API serves, across every state and country."""
    payload = http.get_json(
        f"{BASE}/getcinemas", headers=_auth(token), params={"countryId": COUNTRY_ID}
    )
    rows = payload.get("data") if isinstance(payload, dict) else payload
    return [row for row in rows or [] if isinstance(row, dict)]


class ReadingAdapter(Adapter):
    name = "reading"

    def fetch(self, cinema: dict, http: Http) -> AdapterOutput:
        return self.fetch_many([cinema], http).get(self.venue_id(cinema), AdapterOutput())

    def fetch_many(self, cinemas: list[dict], http: Http) -> dict[str, AdapterOutput]:
        """One token for the batch, one ``/films`` request per venue.

        Reading prices a venue as a single request, so only the token and the
        venue list are shared; fetching them once means a seven-venue refresh
        mints one token instead of seven.
        """
        out = {self.venue_id(c): AdapterOutput() for c in cinemas}
        try:
            token = _token(http)
            venues = _venues(http, token)
        except Exception as exc:                                 # noqa: BLE001
            for result in out.values():
                result.note = f"Reading API unavailable: {exc}"
            return out

        for cinema in cinemas:
            venue_id = self.venue_id(cinema)
            try:
                out[venue_id] = self._fetch_venue(cinema, http, token, venues)
            except Exception as exc:                             # noqa: BLE001
                out[venue_id] = AdapterOutput(note=f"error: {exc}")
        return out

    # -- internals ---------------------------------------------------------
    def _slug(self, cinema: dict, venues: list[dict]) -> str:
        """The API's ``cinemaId`` for a registry venue.

        ``source_ids.reading`` is authoritative (data/overrides/reading.json
        lands there).  Without it we match names, because the two naming
        schemes genuinely differ: the registry says "Reading Burwood
        Brickworks" where the API says "Burwood".
        """
        explicit = self.source_id(cinema, "reading")
        if explicit:
            return explicit

        wanted = _text(cinema.get("name") or "").lower()
        if wanted.startswith("reading "):
            wanted = wanted[len("reading "):]
        for venue in venues:
            slug = str(venue.get("slug") or "")
            name = _text(venue.get("name") or "").lower()
            if not slug or not name:
                continue
            # An API name may be a prefix of ours ("Burwood" against "Burwood
            # Brickworks") but never the other way round, so match forwards.
            if wanted == name or wanted.startswith(name + " "):
                return slug
        return ""

    def _fetch_venue(
        self, cinema: dict, http: Http, token: str, venues: list[dict]
    ) -> AdapterOutput:
        venue_id = self.venue_id(cinema)
        slug = self._slug(cinema, venues)
        if not slug:
            return AdapterOutput(note="no Reading cinema id in source_ids and no name match")

        payload = http.get_json(
            f"{BASE}/films",
            headers=_auth(token),
            params={
                "countryId": COUNTRY_ID,
                "cinemaId": slug,
                # "nowShowing" still returns pre-sale dates for coming-soon
                # titles, which is exactly the bookable window we want.
                "status": "nowShowing",
                "flag": "",
            },
        )
        movies = payload.get("data") if isinstance(payload, dict) else payload
        out = AdapterOutput()

        for movie in movies or []:
            title = _text(movie.get("name"))
            if not title:
                continue
            key = film_key(title)

            release = str(movie.get("release_date") or "")
            length = _text(movie.get("length"))
            director = _text(movie.get("director"))
            out.films.append(
                Film(
                    key=key,
                    title=title,
                    year=int(release[:4]) if release[:4].isdigit() else None,
                    runtime_minutes=int(length) if length.isdigit() else None,
                    rating=_text(movie.get("ratingName")),
                    synopsis=_text(movie.get("synopsis")),
                    poster_url=str(
                        movie.get("film_image_large_size")
                        or movie.get("poster_image")
                        or ""
                    ),
                    trailer_url=str(movie.get("youtube_id") or ""),
                    directors=[director] if director else [],
                )
            )

            for showdate in movie.get("showdates") or []:
                for group in showdate.get("showtypes") or []:
                    fmt = _FORMATS.get(str(group.get("type") or ""), "")
                    for performance in group.get("showtimes") or []:
                        local = _parse_local(str(performance.get("date_time") or ""))
                        if not local:
                            continue
                        out.screenings.append(
                            make_screening(
                                cinema_id=venue_id,
                                film_title=title,
                                local_naive=local,
                                source=self.name,
                                fmt=fmt,
                                attributes=_attributes(performance),
                                ticket_url=self._ticket_url(slug, performance),
                                film_key_override=key,
                                sold_out=bool(performance.get("soldout")),
                            )
                        )

        if not out.screenings:
            out.note = "films feed contained no sessions"
        return out

    @staticmethod
    def _ticket_url(slug: str, performance: dict) -> str:
        """The SPA's own session route: /cinemas/<cinema>/sessions/<id>/<film>."""
        session_id = str(performance.get("id") or "")
        if not session_id:
            return ""
        movie_id = str(performance.get("ScheduledFilmId") or "")
        return f"{SITE}/cinemas/{slug}/sessions/{session_id}/{movie_id}"
