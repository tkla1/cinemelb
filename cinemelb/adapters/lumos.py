"""Adapter for United Cinemas — the MovieXchange "Lumos" / Vista stack.

United Craigieburn is the one United venue inside greater Melbourne (the chain
also runs Avalon, Collaroy, Eldorado, Narellan, Opera Quays, Rockingham, The
Edge and Warriewood, all outside the metro box).  The website is a Next.js app
whose timer is *not* in the hydration payload — the timetable comes from two
services behind it:

1. ``__NEXT_DATA__.props.pageProps.environment.gasToken`` is a short-lived
   (12 h) RSA JWT identifying the chain to its CMS API.  It is public: the
   server puts it in the page HTML for every visitor.
2. ``/api/v1/sales-channels/web/shared/configuration`` names the chain's Vista
   Connect host (``https://digital-api.unitedcinemas.com.au``) and region
   (``AU``).  That is the API the SPA calls as ``ocapi/v1``.

The OCAPI routes used here are the same ones the site's own booking flow uses:

* ``GET /ocapi/v1/film-screening-dates?siteIds=<id>`` — which business dates
  have sessions at all, so the adapter does not guess a window.
* ``GET /ocapi/v1/showtimes/by-business-date/<date>?siteIds=<id>`` — the day's
  sessions *plus* a ``relatedData`` block carrying the film titles, censor
  ratings, genres, screen names and attributes they reference.  One request per
  day therefore needs no follow-up lookups.

Two OCAPI quirks worth knowing:

* ``siteId`` (singular) is rejected with ``siteIds field cannot be empty``.
* Sending a ``Vista-Tenant`` header — the constant is in the bundle — makes the
  API answer 400; ``Authorization`` plus ``Connect-Region-Code`` is what it
  actually wants.

Times arrive as real offset-bearing ISO (``2026-11-25T19:15:00+11:00``) and are
converted to naive Melbourne wall-clock for ``make_screening``.
"""

from __future__ import annotations

import base64
import json
import re
from datetime import datetime, timezone

from ..http import FetchError, Http
from ..models import MELBOURNE_TZ, Film, film_key, make_screening
from .base import Adapter, AdapterOutput

SOURCE = "lumos"

#: Used when the CMS configuration cannot be read; the value is in the
#: chain's own bundle, so it is stable but not discoverable from a bare URL.
DEFAULT_CONNECT = "https://digital-api.unitedcinemas.com.au"
DEFAULT_REGION = "AU"

#: Stop before this many business days so one refresh stays a handful of
#: requests; United sells about four weeks ahead and the tail is sparse.
MAX_BUSINESS_DAYS = 28

_NEXT_DATA = re.compile(
    r'id="__NEXT_DATA__"[^>]*>(?P<body>.*?)</script>', re.I | re.S
)
_WS = re.compile(r"\s+")


def _next_data(page_text: str) -> dict:
    """The Next.js hydration payload, or ``{}`` when the page is not Next."""
    match = _NEXT_DATA.search(page_text or "")
    if not match:
        return {}
    try:
        data = json.loads(match.group("body").strip())
    except (json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _jwt_expiry(token: str) -> datetime | None:
    """``exp`` claim of a JWT, as an aware datetime (no signature check)."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        return datetime.fromtimestamp(int(claims["exp"]), timezone.utc)
    except Exception:                                            # noqa: BLE001
        return None


def _local(stamped: str) -> datetime | None:
    """Convert an offset-bearing ISO instant to naive Melbourne wall-clock."""
    text = (stamped or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
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


def _format_of(attributes: list[str], screen: str, glasses: bool) -> str:
    text = " ".join(attributes + [screen]).lower()
    if glasses or "3d" in text:
        return "3D"
    for token in ("gmax", "vmax", "imax"):
        if token in text:
            return token.upper()
    return ""


class LumosAdapter(Adapter):
    """MovieXchange Lumos website + Vista Connect OCAPI (United Cinemas)."""

    name = SOURCE

    def fetch(self, cinema: dict, http: Http) -> AdapterOutput:
        cinema_id = self.venue_id(cinema)
        site_id = self.source_id(cinema)
        if not site_id:
            return AdapterOutput(
                note="no United site id in source_ids; add {\"lumos\": \"1004\"} "
                     "to data/cinemas.json"
            )

        site = str(cinema.get("website") or "").rstrip("/")
        if not site:
            return AdapterOutput(note="no website on record for this United venue")

        environment = self._environment(site, http)
        if not environment:
            return AdapterOutput(
                note=f"could not read __NEXT_DATA__ environment from {site}"
            )

        try:
            return self._crawl(cinema_id, site_id, environment, http)
        except FetchError as exc:
            # A cached page can hold a token that expired while it sat on disk;
            # refresh the token and try the whole crawl once more.
            if exc.status not in (401, 403):
                return AdapterOutput(note=f"united OCAPI failed: {exc}")
            environment = self._environment(site, http, force=True)
            if not environment:
                return AdapterOutput(note="united gasToken could not be refreshed")
            try:
                return self._crawl(cinema_id, site_id, environment, http)
            except Exception as retry_exc:                       # noqa: BLE001
                return AdapterOutput(note=f"united OCAPI failed: {retry_exc}")
        except Exception as exc:                                 # noqa: BLE001
            return AdapterOutput(note=f"united OCAPI failed: {exc}")

    # -- discovery ---------------------------------------------------------
    def _environment(self, site: str, http: Http, *, force: bool = False) -> dict:
        """Page props carrying the CMS API URL and the gasToken JWT."""
        url = site + "/"
        if not force:
            try:
                cached = _next_data(http.get(url).text)
            except Exception:                                    # noqa: BLE001
                cached = {}
            env = self._environment_of(cached)
            if env:
                expiry = _jwt_expiry(str(env.get("gasToken") or ""))
                # Refresh five minutes early rather than race a 12-hour token.
                if expiry is None or (expiry - datetime.now(timezone.utc)).total_seconds() > 300:
                    return env
        try:
            return self._environment_of(_next_data(http.get(url, fresh=True).text))
        except Exception:                                        # noqa: BLE001
            return {}

    @staticmethod
    def _environment_of(data: dict) -> dict:
        environment = (
            ((data.get("props") or {}).get("pageProps") or {}).get("environment")
            or {}
        )
        return environment if isinstance(environment, dict) and environment.get("gasToken") else {}

    def _connect(self, http: Http, environment: dict) -> tuple[str, dict]:
        """Vista Connect base URL and the headers it expects."""
        api = str((environment.get("cmsConfig") or {}).get("apiUrl") or "").rstrip("/")
        token = str(environment.get("gasToken") or "")
        headers = {"Authorization": f"Bearer {token}"}
        connect, region = DEFAULT_CONNECT, DEFAULT_REGION
        if api:
            try:
                config = http.get_json(
                    f"{api}/api/v1/sales-channels/web/shared/configuration",
                    headers=headers,
                )
                services = ((config.get("initial") or {}).get("services")) or {}
                vista = services.get("vistaConnect") or {}
                connect = str(vista.get("url") or "").rstrip("/") or DEFAULT_CONNECT
                region = str(vista.get("regionCode") or DEFAULT_REGION)
            except Exception:                                    # noqa: BLE001
                pass                                             # keep the defaults
        ocapi = {
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "Connect-Region-Code": region,
        }
        return f"{connect}/ocapi/v1", ocapi

    # -- crawl -------------------------------------------------------------
    def _crawl(self, cinema_id: str, site_id: str, environment: dict,
               http: Http) -> AdapterOutput:
        base, headers = self._connect(http, environment)

        dates = self._business_dates(base, site_id, headers, http)
        if not dates:
            return AdapterOutput(
                note=f"United site {site_id} publishes no business dates"
            )

        out = AdapterOutput()
        films: dict[str, Film] = {}
        seen: set[tuple[str, str, str]] = set()
        failures: list[str] = []

        for day in dates[:MAX_BUSINESS_DAYS]:
            try:
                payload = http.get_json(
                    f"{base}/showtimes/by-business-date/{day}?siteIds={site_id}",
                    headers=headers,
                )
            except FetchError as exc:
                if exc.status in (401, 403):
                    raise
                failures.append(f"{day}: {exc}")
                continue
            if not isinstance(payload, dict):
                failures.append(f"{day}: unexpected payload")
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

                screen = screens.get(showtime.get("screenId"), "")
                key = film_key(title)
                stamp = start.strftime("%Y-%m-%dT%H:%M")
                if (key, stamp, screen) in seen:
                    continue
                seen.add((key, stamp, screen))

                if key not in films:
                    runtime = film.get("runtimeInMinutes")
                    films[key] = Film(
                        key=key,
                        title=title,
                        runtime_minutes=int(runtime) if str(runtime or "").isdigit() else None,
                        rating=ratings.get(film.get("censorRatingId"), ""),
                        genres=[genres[g] for g in film.get("genreIds") or [] if g in genres],
                        synopsis=_text(film.get("synopsis")),
                        trailer_url=str(film.get("trailerUrl") or ""),
                    )

                names = [attributes[a] for a in showtime.get("attributeIds") or []
                         if a in attributes]
                out.screenings.append(
                    make_screening(
                        cinema_id=cinema_id,
                        film_title=title,
                        local_naive=start,
                        source=self.name,
                        screen=screen,
                        fmt=_format_of(names, screen,
                                       bool(showtime.get("requires3dGlasses"))),
                        attributes=names,
                        sold_out=bool(showtime.get("isSoldOut")),
                        film_key_override=key,
                    )
                )

        out.films = list(films.values())
        if not out.screenings:
            out.note = (
                f"United site {site_id} dates {dates[0]}..{dates[-1]} held no "
                f"showtimes" + (f" ({len(failures)} day(s) failed: {failures[0]})"
                                if failures else "")
            )
        elif failures:
            out.note = f"{len(failures)} day(s) could not be read: {failures[0]}"
        return out

    @staticmethod
    def _business_dates(base: str, site_id: str, headers: dict, http: Http) -> list[str]:
        payload = http.get_json(
            f"{base}/film-screening-dates?siteIds={site_id}", headers=headers
        )
        dates = [
            str(row.get("businessDate") or "")
            for row in payload.get("filmScreeningDates") or []
            if row.get("businessDate")
        ]
        return dates
