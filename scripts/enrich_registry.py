"""Attach upstream platform identities to the venue registry.

The registry knows *where* each cinema is; the scrapers need *what that
platform calls it*.  Rather than hand-mapping 58 venues (which rots the moment
a chain opens or renames a site), this matches each registry row against the
live chain location feeds by geographic proximity plus name agreement.

``source_ids`` ends up like::

    {"palace": "100", "palace_slug": "palace-balwyn"}

Run after ``harvest_registry.py``.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cinemelb.config import REGISTRY_PATH
from cinemelb.geo import haversine_km
from cinemelb.http import Http
from cinemelb.models import normalise_title

#: A venue is considered the same site if it is this close.  1.0 km rather
#: than a few hundred metres because chains geocode their own sites
#: inconsistently: Flicks puts Hoyts Docklands 770 m from where Hoyts does,
#: and both are "right" (the centre entrance vs the loading dock).
MAX_KM = 1.0


def to_float(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def fetch_hoyts(http: Http) -> list[dict]:
    rows = http.get_json(
        "https://apim-aea.hoyts.com.au/cinemaapi-au-live/api/cinemas"
    )
    out = []
    for r in rows:
        if r.get("state") != "VIC":
            continue
        out.append(
            {
                "platform": "hoyts",
                "id": r["id"],
                "name": r.get("name", ""),
                "lat": to_float(r.get("latitude")),
                "lon": to_float(r.get("longitude")),
                "extra": {},
            }
        )
    return out


def fetch_palace(http: Http) -> list[dict]:
    rows = http.get_json("https://prod-api.palace-cinemas.workers.dev/cinemas")
    out = []
    for r in rows:
        if r.get("region") != "VIC":
            continue
        out.append(
            {
                "platform": "palace",
                "id": str(r["cinemaId"]),
                "name": r.get("title", ""),
                "lat": to_float(r.get("latitude")),
                "lon": to_float(r.get("longitude")),
                "extra": {"palace_slug": r.get("slug", "")},
            }
        )
    return out


def fetch_village(http: Http) -> list[dict]:
    """Village embeds its cinema list in the Next.js flight payload."""
    resp = http.get("https://villagecinemas.com.au/cinemas")
    html = resp.text
    out = []

    # The flight payload is escaped JSON.  Field order inside each `cinema`
    # object varies by venue (Werribee, for one, serialises differently), so
    # anchor on the slug and search a forward window rather than assuming a
    # fixed sequence.
    anchor = re.compile(r'\\"slug\\":\\"(?P<slug>[\w\-]+)\\",\\"cinema\\":\{')
    for m in anchor.finditer(html):
        window = html[m.end() : m.end() + 4000]
        cid = re.search(r'\\"cinemaId\\":\\"(\d+)\\"', window)
        name = re.search(r'\\"name\\":\\"([^\\]*)\\"', window)
        lat = re.search(r'\\"lat\\":(-?\d+\.\d+)', window)
        lon = re.search(r'\\"lon\\":(-?\d+\.\d+)', window)
        state = re.search(r'\\"state\\":\\"(\w+)\\"', window)
        if not (cid and name and lat and lon and state):
            continue
        if state.group(1) != "VIC":
            continue
        out.append(
            {
                "platform": "village",
                "id": cid.group(1),
                "name": name.group(1),
                "lat": to_float(lat.group(1)),
                "lon": to_float(lon.group(1)),
                "extra": {"village_slug": m.group("slug")},
            }
        )
    return out


#: Words that carry no identifying information.  Melbourne's CBD has half a
#: dozen cinemas within a kilometre of each other, so treating "Melbourne" or
#: "Cinema" as evidence of a match pairs ACMI with the Kino and IMAX with
#: Hoyts Melbourne Central — both wrong.
_GENERIC_TOKENS = {
    "melbourne", "cinema", "cinemas", "theatre", "theatres", "theater",
    "theaters", "the", "and", "of", "at", "in", "picture", "pictures",
    "house", "movies", "screens", "complex", "shopping", "centre", "center",
    "city", "street", "road", "drive", "in", "megaplex",
}


def _tokens(name: str) -> set[str]:
    return {
        t
        for t in normalise_title(name).split()
        if len(t) > 2 and t not in _GENERIC_TOKENS
    }


def name_agrees(a: str, b: str) -> bool:
    """True when two venue names plausibly denote the same place."""
    na, nb = normalise_title(a), normalise_title(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    ta, tb = _tokens(a), _tokens(b)
    if ta and tb:
        # any distinctive token shared is strong evidence ("knox", "pentridge")
        if ta & tb:
            return True
        # "watergardens" vs "water gardens"
        if any(x in y or y in x for x in ta for y in tb if len(x) > 4 and len(y) > 4):
            return True
    # one name contained in the other, ignoring generic filler
    return bool(na in nb or nb in na) and bool(ta & tb)


def main() -> int:
    rows = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    http = Http(ttl=86400)

    chains: list[dict] = []
    for fn in (fetch_hoyts, fetch_palace, fetch_village):
        try:
            got = fn(http)
            print(f"  {fn.__name__}: {len(got)} VIC venues")
            chains.extend(got)
        except Exception as exc:                             # noqa: BLE001
            print(f"  {fn.__name__}: FAILED {exc}")

    matched = 0
    ambiguous = 0
    for row in rows:
        lat, lon = row.get("latitude"), row.get("longitude")
        if lat is None or lon is None:
            continue

        # Rank every candidate by distance, then decide with a rule that
        # separates "same site" from "another cinema in the same mall".
        cands: list[tuple[float, dict, bool]] = []
        for c in chains:
            if c["lat"] is None or c["lon"] is None:
                continue
            d = haversine_km(lat, lon, c["lat"], c["lon"])
            if d > MAX_KM:
                continue
            cands.append((d, c, name_agrees(row["name"], c["name"])))
        if not cands:
            continue
        cands.sort(key=lambda t: t[0])

        dist, chain, agree = cands[0]
        rival = cands[1][0] if len(cands) > 1 else None

        # Accept when the names agree, or when nothing else is anywhere near
        # (a venue geocoded 80 m away is the same venue even if the name is
        # written "Water Gardens" upstream and "Watergardens" here).
        confident = agree or dist < 0.35
        # Two chain sites in one shopping centre differ by ~100 m; if the
        # names disagree we cannot tell them apart.
        too_close_to_disambiguate = rival is not None and (rival - dist) < 0.12
        if not confident or (too_close_to_disambiguate and not agree):
            ambiguous += 1
            print(
                f"  ?  {row['id']:<34} nearest {chain['platform']}:{chain['name']!r}"
                f" at {dist:.2f}km (rival {rival if rival is None else round(rival, 2)}km)"
            )
            continue

        ids = dict(row.get("source_ids") or {})
        ids[chain["platform"]] = chain["id"]
        ids.update(chain["extra"])
        row["source_ids"] = ids
        # Only auto-assign the adapter for chain venues; indie sources are
        # curated in harvest_registry.py.
        if chain["platform"] in ("hoyts", "palace", "village"):
            row["source"] = chain["platform"]
        matched += 1

    print(f"\nmatched {matched} venues to a chain platform ({ambiguous} ambiguous, skipped)")

    REGISTRY_PATH.write_text(
        json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    by_source: dict[str, int] = {}
    for r in rows:
        by_source[r.get("source", "?")] = by_source.get(r.get("source", "?"), 0) + 1
    print("adapter assignment:", dict(sorted(by_source.items())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
