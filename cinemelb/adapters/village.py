"""Village Cinemas adapter.

Village (an EVT brand, ticketing by Vista) runs a Next.js App Router site whose
cinema list is server-rendered but whose *session times are not*: the "Find
sessions" widget queries an Algolia index of sessions through a same-origin
proxy route.  That route is the adapter's whole interface:

* ``GET /api/algolia/sessions/hits``   — session documents, filtered with the
  widget's own facet parameters (``f.c`` = cinema id, ``f.d`` = date,
  ``f.m`` = movie, ``f.x`` = experience, …).  The numeric ids in the registry's
  ``source_ids["village"]`` are exactly the ``cinema.cinemaId`` facet values,
  leading zero included ("004", "023").
* ``GET /api/algolia/sessions/facets`` — facet counts over the same index; used
  only to recover the day-by-day shape of a venue that overflows one page.

The proxy returns at most 1000 hits per response — ``nbHits`` still carries the
true total — while the index holds ~9900 sessions for every Village cinema in
the country.  One unfiltered request would therefore return an arbitrary
thousand and silently drop most of the network, so :meth:`fetch_many` filters
by venue and asks once per venue; a Victoria-wide horizon is ~7000 sessions,
and the busiest single venue carries ~800.  A venue that outgrows the page is
split by day rather than truncated.

``showtime`` reads ``2026-11-25T21:00:00.000000+11:00``: Melbourne wall-clock
wearing the correct offset, which is what :func:`cinemelb.models.make_screening`
wants once the offset is stripped (a 9pm session is 9pm at the venue).

Fully-booked sessions are kept and flagged instead of dropped: the index only
lists sessions that are actually programmed, and both :class:`Screening` and the
store carry ``sold_out`` so a full house stays visible as a full house.
"""

from __future__ import annotations

from datetime import datetime

from ..http import Http
from ..models import Film, film_key, make_screening
from .base import Adapter, AdapterOutput

BASE = "https://villagecinemas.com.au"
HITS_URL = f"{BASE}/api/algolia/sessions/hits"
FACETS_URL = f"{BASE}/api/algolia/sessions/facets"

#: Vista experience label -> the presentation name the model uses.  "Standard"
#: is deliberately absent: the lack of a premium format is not a format.
_FORMATS = {
    "imax": "IMAX",
    "vmax": "Vmax",
    "gold class": "Gold Class",
    "vpremium": "Vpremium",
    "vjunior": "Vjunior",
    "europa": "Europa",
    "drive in": "Drive-in",
    "4dx": "4DX",
}

#: Accessibility codes whose human-readable twin Village also emits as a
#: secondary attribute; the code alone is enough.
_ACCESSIBILITY_LABELS = {"CC": "Closed Captions", "AD": "Audio Description"}


def _parse_showtime(value: str) -> datetime | None:
    """Read a Village showtime as a naive Melbourne wall-clock datetime."""
    if not value:
        return None
    try:
        return datetime.strptime(value[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None


def _film(key: str, title: str, movie: dict) -> Film:
    """Build the film catalogue row from a session's embedded movie record."""
    duration = str(movie.get("duration") or "")
    classification = movie.get("classification") or {}
    genres = [
        str(item.get("text") or item.get("id") or "")
        for item in (movie.get("genreCollection") or {}).get("items") or []
        if isinstance(item, dict)
    ]
    poster = movie.get("posterOverride") or movie.get("poster") or {}
    trailer = str(movie.get("youTubeVideoId") or "")
    return Film(
        key=key,
        title=title,
        runtime_minutes=int(duration) if duration.isdigit() else None,
        rating=str(classification.get("description") or "").strip(),
        genres=[genre for genre in genres if genre],
        synopsis=str(
            movie.get("shortSynopsisOverride") or movie.get("shortSynopsis") or ""
        ).strip(),
        poster_url=str((poster.get("image") or {}).get("url") or ""),
        trailer_url=f"https://www.youtube.com/watch?v={trailer}" if trailer else "",
    )


def _format(hit: dict) -> str:
    """The premium format a session runs in, if it runs in one."""
    label = str((hit.get("experience") or {}).get("label") or "").strip().lower()
    fmt = _FORMATS.get(label, "")
    if not fmt:
        events = {
            str(event.get("name") or "").strip().lower()
            for event in hit.get("events") or []
            if isinstance(event, dict)
        }
        if "3d" in events:
            fmt = "3D"
    return fmt


def _attributes(hit: dict) -> list[str]:
    """Accessibility, presentation and seating labels for one session."""
    attrs = [str(code) for code in hit.get("accessibility") or [] if code]
    # Village repeats CC/AD as long labels; keep the code form the other
    # adapters use and drop the duplicate text.
    redundant = {_ACCESSIBILITY_LABELS.get(code, "") for code in attrs}
    for group in ("secondaryAttributes", "seatingAttributes"):
        for attribute in hit.get(group) or []:
            label = str((attribute or {}).get("label") or "").strip()
            if label and label not in redundant and label not in attrs:
                attrs.append(label)
    return attrs


def _cinema_dates(http: Http, cinema_id: str) -> list[str]:
    """Every date this venue has sessions on, ascending."""
    payload = http.get_json(FACETS_URL, params={"f.c": cinema_id})
    for block in payload.get("facets") or []:
        dates = (block.get("facets") or {}).get("date") or {}
        if dates:
            return sorted(dates)
    return []


def _cinema_hits(http: Http, cinema_id: str) -> list[dict]:
    """Every upcoming session at one venue.

    One filtered request normally covers the whole horizon, which is the point
    of filtering by venue.  When a venue does overflow the proxy's page, its
    days are fetched one at a time: a half-delivered feed is indistinguishable
    from a venue with quiet weeks, and that is the kind of error worth paying a
    facet call to avoid.
    """
    payload = http.get_json(HITS_URL, params={"f.c": cinema_id})
    hits = payload.get("hits") or []
    if int(payload.get("nbHits") or 0) <= len(hits):
        return hits

    all_hits: list[dict] = []
    for day in _cinema_dates(http, cinema_id):
        page = http.get_json(HITS_URL, params={"f.c": cinema_id, "f.d": day})
        all_hits.extend(page.get("hits") or [])
    return all_hits


class VillageAdapter(Adapter):
    name = "village"

    def fetch(self, cinema: dict, http: Http) -> AdapterOutput:
        # Village is queried one venue at a time anyway, so a single-venue call
        # is just the general case with a list of one.
        return self.fetch_many([cinema], http).get(self.venue_id(cinema), AdapterOutput())

    def fetch_many(self, cinemas: list[dict], http: Http) -> dict[str, AdapterOutput]:
        results = {self.venue_id(cinema): AdapterOutput() for cinema in cinemas}
        for cinema in cinemas:
            venue_id = self.venue_id(cinema)
            out = results[venue_id]
            cinema_id = self.source_id(cinema, "village")
            if not cinema_id:
                out.note = "no village cinema id in source_ids"
                continue
            try:
                hits = _cinema_hits(http, cinema_id)
            except Exception as exc:                             # noqa: BLE001
                out.note = f"session query failed: {exc}"
                continue
            self._collect(hits, venue_id, cinema_id, out)
        return results

    def _collect(
        self, hits: list[dict], venue_id: str, cinema_id: str, out: AdapterOutput
    ) -> None:
        films: dict[str, Film] = {}
        for hit in hits:
            movie = hit.get("movie") or {}
            # The site shows the override when marketing has replaced a title;
            # the catalogue key must follow the title a user can search for.
            title = str(
                movie.get("titleOverride") or movie.get("title") or ""
            ).strip()
            local = _parse_showtime(str(hit.get("showtime") or ""))
            if not title or not local:
                continue

            key = film_key(title)
            films.setdefault(key, _film(key, title, movie))
            out.screenings.append(
                make_screening(
                    cinema_id=venue_id,
                    film_title=title,
                    local_naive=local,
                    source=self.name,
                    # The index carries no auditorium number, only the format.
                    fmt=_format(hit),
                    attributes=_attributes(hit),
                    ticket_url=(
                        f"{BASE}/order/tickets"
                        f"?cinemaId={cinema_id}&sessionId={hit.get('sessionId')}"
                    ),
                    film_key_override=key,
                    # A full house still screens, so flag it rather than drop it.
                    sold_out=hit.get("seatsAvailable") == 0,
                )
            )

        out.films.extend(films.values())
        if not out.screenings:
            out.note = "no upcoming sessions in the Village index"
