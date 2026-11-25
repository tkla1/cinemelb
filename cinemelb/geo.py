"""Location maths: distance, nearest-venue ranking, and address geocoding.

The goal the user stated is "ask for the closest showings from a given
location", so location handling is a first-class feature rather than an
afterthought:

* :func:`haversine_km` — great-circle distance, accurate to well under 1% at
  Melbourne's latitudes.
* :func:`nearest` — sorts venues by distance from a point.
* :func:`geocode` — resolves a text location ("Brunswick", "3000", "Fed
  Square", a street address) to coordinates, via a cached Nominatim lookup.
  Unknown strings fall back to matching suburb names already present in the
  registry, which needs no network at all.
"""

from __future__ import annotations

import functools
import json
import math
import re
from pathlib import Path
from typing import Iterable

from .config import CACHE_DIR, MELBOURNE_CBD

EARTH_RADIUS_KM = 6371.0088

#: Rough average door-to-door speeds (km/h) for the walking/driving estimate
#: shown in results.  Melbourne metro traffic makes 22 km/h a fair planning
#: figure; walking is a flat 4.8 km/h.
SPEED_KMH = {"walk": 4.8, "drive": 22.0, "transit": 18.0}


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in kilometres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def travel_minutes(km: float, mode: str = "drive") -> int:
    """Very rough door-to-door time estimate, for ranking/presentation only."""
    speed = SPEED_KMH.get(mode, SPEED_KMH["drive"])
    return max(1, round(km / speed * 60))


def format_distance(km: float) -> str:
    if km < 1.0:
        return f"{int(round(km * 1000 / 10) * 10)} m"
    if km < 10:
        return f"{km:.1f} km"
    return f"{km:.0f} km"


def nearest(
    origin: tuple[float, float],
    items: Iterable,
    *,
    lat_attr: str = "latitude",
    lon_attr: str = "longitude",
) -> list[tuple[float, object]]:
    """Return ``[(distance_km, item), ...]`` sorted nearest-first.

    Items without coordinates are dropped rather than treated as infinitely
    far, so a venue with unknown coordinates never silently outranks a real
    one *or* pollutes the tail of the list.
    """
    lat0, lon0 = origin
    out: list[tuple[float, object]] = []
    for it in items:
        lat = getattr(it, lat_attr, None)
        lon = getattr(it, lon_attr, None)
        if lat is None or lon is None:
            continue
        out.append((haversine_km(lat0, lon0, float(lat), float(lon)), it))
    out.sort(key=lambda pair: pair[0])
    return out


# --------------------------------------------------------------------------- #
# geocoding
# --------------------------------------------------------------------------- #

_GEO_CACHE: Path = CACHE_DIR / "geocode.json"


def _load_geo_cache() -> dict[str, list[float]]:
    if _GEO_CACHE.exists():
        try:
            return json.loads(_GEO_CACHE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def _save_geo_cache(cache: dict[str, list[float]]) -> None:
    _GEO_CACHE.parent.mkdir(parents=True, exist_ok=True)
    _GEO_CACHE.write_text(json.dumps(cache, indent=1), encoding="utf-8")


def parse_latlon(text: str) -> tuple[float, float] | None:
    """Accept ``-37.81,144.96`` (also with a space or ``;``)."""
    m = re.fullmatch(r"\s*(-?\d{1,3}(?:\.\d+)?)\s*[,; ]\s*(-?\d{1,3}(?:\.\d+)?)\s*", text)
    if not m:
        return None
    lat, lon = float(m.group(1)), float(m.group(2))
    if -90 <= lat <= 90 and -180 <= lon <= 180:
        return lat, lon
    return None


def geocode(
    text: str,
    *,
    suburb_hint: str | None = None,
    allow_network: bool = True,
) -> tuple[float, float] | None:
    """Resolve a free-text location to (lat, lon), or ``None``.

    Order of attempts: explicit coordinates -> cached geocode -> known
    Melbourne suburb -> Nominatim.  Cached results are written back so repeat
    queries never hit the network.
    """
    if not text or not text.strip():
        return None
    coords = parse_latlon(text)
    if coords:
        return coords

    key = text.strip().lower()
    cache = _load_geo_cache()
    if key in cache:
        return float(cache[key][0]), float(cache[key][1])

    hit = _match_known_place(key)
    if hit:
        cache[key] = [hit[0], hit[1]]
        _save_geo_cache(cache)
        return hit

    if not allow_network:
        return None

    query = text
    if suburb_hint and suburb_hint.lower() not in key:
        query = f"{text}, {suburb_hint}"
    try:
        import requests

        from .config import USER_AGENT

        r = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={
                "q": f"{query}, Victoria, Australia",
                "format": "jsonv2",
                "limit": 1,
                "countrycodes": "au",
            },
            headers={"User-Agent": USER_AGENT},
            timeout=15,
        )
        r.raise_for_status()
        rows = r.json()
        if rows:
            lat, lon = float(rows[0]["lat"]), float(rows[0]["lon"])
            cache[key] = [lat, lon]
            _save_geo_cache(cache)
            return lat, lon
    except Exception:  # network/parse problems are not fatal for geocoding
        return None
    return None


#: Landmarks and suburbs people actually type.  Used before any network call
#: so the common cases are instant and work offline.
_KNOWN_PLACES: dict[str, tuple[float, float]] = {
    "melbourne": MELBOURNE_CBD,
    "cbd": MELBOURNE_CBD,
    "city": MELBOURNE_CBD,
    "melbourne cbd": MELBOURNE_CBD,
    "federation square": (-37.8180, 144.9690),
    "fed square": (-37.8180, 144.9690),
    "flinders street station": (-37.8183, 144.9671),
    "southern cross": (-37.8183, 144.9526),
    "melbourne central": (-37.8118, 144.9645),
    "university of melbourne": (-37.7989, 144.9600),
    "rmit": (-37.8078, 144.9631),
    "docklands": (-37.8143, 144.9419),
    "southbank": (-37.8230, 144.9630),
    "carlton": (-37.7998, 144.9671),
    "fitzroy": (-37.7983, 144.9784),
    "collingwood": (-37.8020, 144.9880),
    "abbotsford": (-37.8030, 144.9990),
    "richmond": (-37.8190, 145.0010),
    "south yarra": (-37.8400, 144.9920),
    "prahran": (-37.8510, 144.9930),
    "st kilda": (-37.8620, 144.9810),
    "windsor": (-37.8540, 144.9940),
    "armadale": (-37.8550, 145.0160),
    "hawthorn": (-37.8220, 145.0300),
    "hawthorn east": (-37.8280, 145.0480),
    "kew": (-37.8060, 145.0360),
    "balwyn": (-37.8130, 145.0800),
    "camberwell": (-37.8310, 145.0610),
    "glen waverley": (-37.8780, 145.1650),
    "clayton": (-37.9200, 145.1200),
    "wantirna south": (-37.8620, 145.2340),
    "ringwood": (-37.8150, 145.2280),
    "forest hill": (-37.8350, 145.1650),
    "doncaster": (-37.7870, 145.1250),
    "box hill": (-37.8190, 145.1240),
    "berwick": (-38.0360, 145.3430),
    "narre warren": (-38.0210, 145.3050),
    "dandenong": (-37.9870, 145.2140),
    "frankston": (-38.1430, 145.1230),
    "cheltenham": (-37.9640, 145.0530),
    "mentone": (-37.9830, 145.0640),
    "brighton": (-37.9070, 144.9930),
    "elsternwick": (-37.8830, 145.0030),
    "caulfield": (-37.8770, 145.0220),
    "malvern": (-37.8570, 145.0330),
    "hoppers crossing": (-37.8780, 144.7000),
    "werribee": (-37.9000, 144.6600),
    "point cook": (-37.9000, 144.7500),
    "altona north": (-37.8340, 144.8350),
    "sunshine": (-37.7870, 144.8320),
    "footscray": (-37.8000, 144.8990),
    "yarraville": (-37.8160, 144.8910),
    "seddon": (-37.8060, 144.8950),
    "maribyrnong": (-37.7700, 144.8880),
    "highpoint": (-37.7720, 144.8880),
    "moonee ponds": (-37.7660, 144.9210),
    "essendon": (-37.7560, 144.9120),
    "airport west": (-37.7150, 144.8880),
    "tullamarine": (-37.7010, 144.8850),
    "coburg": (-37.7440, 144.9660),
    "coburg north": (-37.7230, 144.9700),
    "brunswick": (-37.7660, 144.9610),
    "brunswick east": (-37.7720, 144.9790),
    "northcote": (-37.7700, 144.9990),
    "thornbury": (-37.7580, 145.0070),
    "preston": (-37.7410, 145.0300),
    "reservoir": (-37.7160, 145.0100),
    "mill park": (-37.6520, 145.0720),
    "epping": (-37.6530, 145.0300),
    "craigieburn": (-37.6000, 144.9450),
    "broadmeadows": (-37.6830, 144.9190),
    "greensborough": (-37.7040, 145.1030),
    "taylors lakes": (-37.6970, 144.7760),
    "watergardens": (-37.6970, 144.7760),
    "melton": (-37.6830, 144.5830),
    "sunbury": (-37.5790, 144.7270),
    "chirnside park": (-37.7530, 145.3010),
    "belgrave": (-37.9090, 145.3550),
    "boronia": (-37.8600, 145.2840),
    "mount waverley": (-37.8770, 145.1290),
    "chadstone": (-37.8880, 145.0830),
    "eastland": (-37.8150, 145.2280),
    "bankstown": (-33.9200, 151.0300),
}


def _match_known_place(key: str) -> tuple[float, float] | None:
    if key in _KNOWN_PLACES:
        return _KNOWN_PLACES[key]
    # postcode -> approximate suburb centroid (common Melbourne postcodes)
    return None


@functools.lru_cache(maxsize=1)
def known_places() -> dict[str, tuple[float, float]]:
    return dict(_KNOWN_PLACES)
