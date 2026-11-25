"""Adapter for IMAX Melbourne — MovieXchange "Lumos" + Vista Connect OCAPI.

IMAX runs the same Lumos platform as United Cinemas, so the shape is familiar
(``__NEXT_DATA__`` environment -> ``gasToken`` -> OCAPI ``/ocapi/v1``), but the
details differ in ways that matter:

* The page's ``cmsConfig.apiUrl`` points at ``cms-api-web.imaxmelbourne.com.au``,
  which answers **Cloudflare 403** to anything but the site's own browser
  session.  The Lumos adapter reads the Vista Connect host from that
  configuration endpoint; here that route is unusable, so the Connect host is a
  constant (:data:`CONNECT`) taken from the page's own API calls.
* ``Connect-Region-Code`` — which the United adapter sends — makes this tenant
  answer **400**, so the OCAPI headers carry the bearer token and nothing else.
* The deprecated GAS route ``/v2/experiences/get`` on the same host is also
  Cloudflare-403.  It is *not* the source of the timetable; the OCAPI routes
  below are, and they answer plain HTTP 200 with the page's token.  An earlier
  version of this adapter concluded the venue was unscrapable from that 403
  alone; that was wrong.

Routes used (all ``GET``, ``Authorization: Bearer <gasToken>``):

* ``/ocapi/v1/sites`` — sanity check + the site id (``IMAX``).
* ``/ocapi/v1/film-screening-dates?siteIds=IMAX`` — the business dates that
  have sessions.  Today this is ~38 days ahead.
* ``/ocapi/v1/showtimes/by-business-date/<date>?siteIds=IMAX`` — one day's
  sessions plus ``relatedData`` (films, screens, attributes, censor ratings),
  so a day costs one request.

Titles arrive in **upper case** upstream (``THE HUNGER GAMES: SUNRISE ON THE
REAPING``).  They are stored verbatim: matching runs on the normalised title so
the case is irrelevant to queries, and silently re-casing would corrupt titles
that are genuinely upper case (``WALL-E``, ``SE7EN``, festival acronyms).

Times arrive as offset-bearing ISO (``2026-11-25T21:00:00+11:00``) and are
converted to naive Melbourne wall-clock for :func:`make_screening`.
"""

from __future__ import annotations

import base64
import json
import re
from datetime import datetime, timezone

from ..http import FetchError, Http
from ..models import MELBOURNE_TZ, Film, film_key, make_screening
from .base import Adapter, AdapterOutput

SOURCE = "imax"

#: The venue's own marketing host.  302s from ``imaxmelbourne.com.au``; the
#: SPA is what carries the hydration blob with the token.
SITE = "https://web.imaxmelbourne.com.au"

#: Vista Connect host for this tenant.  Normally read from the CMS
#: configuration endpoint, but that host is Cloudflare-403 for non-browser
#: clients; this value is the one the page's own OCAPI calls go to.
CONNECT = "https://digital-api.imaxmelbourne.com.au"

#: Upstream site id.  ``/ocapi/v1/sites`` returns exactly one: ``IMAX``.
SITE_ID = "IMAX"

#: Cap on days crawled.  The feed currently advertises ~38 business dates; the
#: cap only exists so a venue that starts selling a year ahead cannot turn one
#: refresh into hundreds of requests.
MAX_BUSINESS_DAYS = 42

_NEXT_DATA = re.compile(r'id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.I | re.S)
_WS = re.compile(r"\s+")


def _next_data(page_text: str) -> dict:
    """The Next.js hydration blob, or ``{}`` when the page is not Next."""
    match = _NEXT_DATA.search(page_text or "")
    if not match:
        return {}
    try:
        blob = json.loads(match.group(1).strip())
    except json.JSONDecodeError:
        return {}
    return blob if isinstance(blob, dict) else {}


def _environment(blob: dict) -> dict:
    """The ``environment`` object carrying ``gasToken``, if present."""
    env = ((blob.get("props") or {}).get("pageProps") or {}).get("environment")
    if isinstance(env, dict) and env.get("gasToken"):
        return env
    return {}


def _jwt_expiry(token: str) -> datetime | None:
    """``exp`` from a JWT, without verifying it.  ``None`` when unreadable."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        return datetime.fromtimestamp(int(claims["exp"]), timezone.utc)
    except Exception:                                            # noqa: BLE001
        return None


def _local(stamped: str) -> datetime | None:
    """Offset-bearing ISO instant -> naive Melbourne wall-clock."""
    text = (stamped or "").strip()
    if not text:
        return None
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(MELBOURNE_TZ).replace(tzinfo=None)


def _text(value: object) -> str:
    """Read one of OCAPI's ``{"text": …}`` localised strings."""
    if isinstance(value, dict):
        value = value.get("text")
    return _WS.sub(" ", str(value or "")).strip()


def _format_of(attributes: list[str], glasses: bool) -> str:
    """Presentation format from the session's own attribute names.

    ``requires3dGlasses`` is ``false`` even for the 3D documentaries — the
    venue's own attribute list is what actually says "3D" / "IMAX70MM" — so the
    attribute text is authoritative and the boolean is only a fallback.
    """
    text = " ".join(attributes).lower()
    if "70mm" in text:
        return "IMAX 70mm"
    if "laser" in text:
        return "IMAX Laser"
    if glasses or "3d" in text:
        return "IMAX 3D"
    return "IMAX"


#: Presentation wording IMAX appends to a film's title.  These are *not* part of
#: the film's name: "DUNE: PART THREE - IMAX 70MM" is the same film as "Dune:
#: Part Three" at every other venue, and keying it separately would hide IMAX
#: from a "where is Dune: Part Three showing?" query — the venue's whole reason
#: for being in the tool.  The suffix is still shown in the screening's title
#: and in its ``format``, so nothing is lost.
_PRESENTATION_SUFFIX = re.compile(
    r"\s+[-\u2013\u2014]\s+"
    r"(?:IMAX(?:\s*70MM)?|4K(?:\s*LASER)?)"
    r"(?:\s+PRESENTATION)?"
    r"\s*$",
    re.IGNORECASE,
)


def _base_title(title: str) -> str:
    """Strip IMAX's presentation suffix, leaving the film's own name."""
    stripped = _PRESENTATION_SUFFIX.sub("", title or "").strip()
    return stripped or title


def _fetch_day(http: Http, base: str, day: str, site_id: str, headers: dict,
               failures: list[str]) -> dict | None:
    """One business date's showtimes, or ``None`` after recording a failure.

    A 429 here is Cloudflare's "1015 rate limited" rather than a real absence.
    :class:`Http` retries it with a short backoff; if it still fails the day is
    recorded as a failure so a partial week is never silently stored as if it
    were the whole week.  ``401``/``403`` are re-raised so the caller can refresh
    the token and retry the crawl.
    """
    try:
        payload = http.get_json(
            f"{base}/showtimes/by-business-date/{day}?siteIds={site_id}",
            headers=headers,
        )
    except FetchError as exc:
        if exc.status in (401, 403):
            raise
        failures.append(f"{day}: {exc}")
        return None
    if not isinstance(payload, dict):
        failures.append(f"{day}: unexpected payload")
        return None
    # A 200 carrying a Cloudflare error envelope is not an empty day.
    if payload.get("error_code") or "showtimes" not in payload:
        failures.append(f"{day}: {payload.get('detail') or 'no showtimes field'}")
        return None
    return payload


class ImaxAdapter(Adapter):
    """IMAX Melbourne — Lumos SPA + Vista Connect OCAPI."""

    name = SOURCE

    def fetch(self, cinema: dict, http: Http) -> AdapterOutput:
        cinema_id = self.venue_id(cinema)
        site_id = self.source_id(cinema) or SITE_ID
        try:
            return self._crawl(cinema_id, site_id, http)
        except FetchError as exc:
            # The token is short-lived and the page is cached on disk, so a
            # 401/403 usually means "stale token", not "blocked".  Re-read the
            # page bypassing the cache and try once more.
            if exc.status not in (401, 403):
                return AdapterOutput(note=f"IMAX OCAPI failed: {exc}")
            try:
                return self._crawl(cinema_id, site_id, http, force_token=True)
            except Exception as retry_exc:                       # noqa: BLE001
                return AdapterOutput(note=f"IMAX OCAPI failed: {retry_exc}")
        except Exception as exc:                                 # noqa: BLE001
            return AdapterOutput(note=f"IMAX OCAPI failed: {exc}")

    # -- token -------------------------------------------------------------
    def _token(self, http: Http, *, force: bool = False) -> str:
        """The ``gasToken`` from the page's hydration blob.

        Cached by :class:`Http` like any other page, so a refresh inside the
        token's life costs no extra request.  ``force`` bypasses the cache for
        the stale-token retry path.
        """
        page = http.get(SITE + "/", fresh=True) if force else http.get(SITE + "/")
        env = _environment(_next_data(page.text))
        token = str(env.get("gasToken") or "")
        if not token:
            raise FetchError(
                "IMAX page carried no __NEXT_DATA__ gasToken "
                f"(HTTP {page.status}, {len(page.text)} bytes)"
            )
        return token

    # -- crawl -------------------------------------------------------------
    def _crawl(self, cinema_id: str, site_id: str, http: Http,
               *, force_token: bool = False) -> AdapterOutput:
        token = self._token(http, force=force_token)
        base = f"{CONNECT}/ocapi/v1"
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

        dates = self._business_dates(base, site_id, headers, http)
        if not dates:
            return AdapterOutput(
                note=f"IMAX site {site_id} publishes no business dates"
            )

        out = AdapterOutput()
        films: dict[str, Film] = {}
        seen: set[tuple[str, str, str]] = set()
        failures: list[str] = []

        for day in dates[:MAX_BUSINESS_DAYS]:
            payload = _fetch_day(http, base, day, site_id, headers, failures)
            if payload is None:
                continue

            related = payload.get("relatedData") or {}
            screens = {row.get("id"): _text(row.get("name"))
                       for row in related.get("screens") or []}
            attributes = {row.get("id"): _text(row.get("name"))
                          for row in related.get("attributes") or []}
            ratings = {row.get("id"): _text(row.get("classification"))
                       for row in related.get("censorRatings") or []}
            genres = {row.get("id"): _text(row.get("name"))
                      for row in related.get("genres") or []}
            films_by_id = {row.get("id"): row for row in related.get("films") or []}

            for showtime in payload.get("showtimes") or []:
                schedule = showtime.get("schedule") or {}
                start = _local(str(schedule.get("startsAt") or ""))
                film = films_by_id.get(showtime.get("filmId")) or {}
                title = _text(film.get("title"))
                if not (start and title):
                    continue

                # "DUNE: PART THREE - IMAX 70MM" is the same film as "Dune: Part
                # Three"; key it that way so a query finds IMAX too.  The full
                # upstream title stays on the screening row.
                base_title = _base_title(title)
                key = film_key(base_title)
                screen = screens.get(showtime.get("screenId"), "")
                stamp = start.strftime("%Y-%m-%dT%H:%M")
                if (key, stamp, screen) in seen:
                    continue
                seen.add((key, stamp, screen))

                if key not in films:
                    runtime = film.get("runtimeInMinutes")
                    release = str(film.get("releaseDate") or "")
                    films[key] = Film(
                        key=key,
                        title=base_title,
                        year=int(release[:4]) if release[:4].isdigit() else None,
                        runtime_minutes=int(runtime) if str(runtime or "").isdigit() else None,
                        rating=ratings.get(film.get("censorRatingId"), ""),
                        genres=[genres[g] for g in film.get("genreIds") or [] if g in genres],
                        synopsis=_text(film.get("synopsis")),
                        trailer_url=str(film.get("trailerUrl") or ""),
                        # Keep the upstream presentation wording searchable.
                        aka=[title] if title != base_title else [],
                    )

                names = [attributes[a] for a in showtime.get("attributeIds") or []
                         if a in attributes]
                fmt = _format_of(names, bool(showtime.get("requires3dGlasses")))
                out.screenings.append(
                    make_screening(
                        cinema_id=cinema_id,
                        film_title=title,
                        local_naive=start,
                        source=self.name,
                        screen=screen,
                        fmt=fmt,
                        attributes=names,
                        sold_out=bool(showtime.get("isSoldOut")),
                        film_key_override=key,
                    )
                )

        out.films = list(films.values())
        if not out.screenings:
            out.note = (
                f"IMAX dates {dates[0]}..{dates[-1]} held no showtimes"
                + (f" ({len(failures)} day(s) failed: {failures[0]})" if failures else "")
            )
        elif failures:
            out.note = f"{len(failures)} day(s) failed: {failures[0]}"
        return out

    @staticmethod
    def _business_dates(base: str, site_id: str, headers: dict, http: Http) -> list[str]:
        payload = http.get_json(
            f"{base}/film-screening-dates?siteIds={site_id}", headers=headers
        )
        if not isinstance(payload, dict) or "filmScreeningDates" not in payload:
            raise FetchError(
                f"{base}/film-screening-dates?siteIds={site_id}",
                None,
                f"unexpected payload: {str(payload)[:160]}",
            )
        return [str(row.get("businessDate") or "")
                for row in payload.get("filmScreeningDates") or []
                if row.get("businessDate")]
