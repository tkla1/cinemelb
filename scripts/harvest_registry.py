"""Build ``data/cinemas.json`` — the master venue registry.

Sources, in order of authority:

1. **Flicks.com.au cinema pages.**  Server-rendered, one page per venue,
   carrying the venue's own address, phone, official website and the
   coordinates Flicks uses for its map.  This is the only source that covers
   arthouse/independent venues at all, so it is the backbone.
2. **Chain location APIs** (Hoyts, Village, Palace).  These provide the
   upstream IDs the scrapers need, and act as a cross-check on coordinates.

The script is idempotent: re-running refreshes the registry in place and
preserves any hand-written ``tags`` / ``notes`` already present, so a curator
can annotate a venue without the next refresh wiping it.

Usage::

    python scripts/harvest_registry.py            # full rebuild
    python scripts/harvest_registry.py --check    # report only, no write
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cinemelb.config import MELBOURNE_BOUNDS, REGISTRY_PATH
from cinemelb.http import Http
from cinemelb.models import Cinema, slugify

FLICKS = "https://www.flicks.com.au"

# --------------------------------------------------------------------------- #
# Venue slugs, harvested from Flicks' regional listings.
# Region labels follow Flicks; they are how Melburnians actually think about
# where a cinema is.
# --------------------------------------------------------------------------- #
REGIONS: dict[str, list[str]] = {
    "inner": [
        "acmi-australia-centre-for-the-moving-image-giant",
        "astor-st-kilda",
        "brunswick-picture-house",
        "chinatown-cinema-melbourne",
        "cinema-nova-melbourne",
        "eclipse-cinema",
        "fomo-cinemas-east-brunswick",
        "hoyts-docklands",
        "hoyts-melbourne-central",
        "hoyts-victoria-gardens",
        "imax-melbourne",
        "lido-cinema-hawthorn-melbourne",
        "moonlight-cinema-melbourne",
        "palace-balwyn",
        "palace-cinema-como",
        "palace-kino-cinemas",
        "palace-westgarth",
        "village-cinemas-crown",
        "village-cinemas-rivoli-cinemas",
    ],
    "east": [
        "cameo-cinemas",
        "hoyts-chadstone",
        "hoyts-eastland",
        "hoyts-forest-hill",
        "hoyts-greensborough",
        "metro-cinemas-boronia",
        "reading-burwood-brickworks",
        "reading-chirnside-park",
        "village-cinemas-century-city",
        "village-cinemas-doncaster",
        "village-cinemas-knox",
        "village-m-city",
        "waverley-mount-waverly",
    ],
    "north-west": [
        "hoyts-broadmeadows",
        "hoyts-highpoint",
        "hoyts-northland",
        "hoyts-watergardens",
        "palace-penny-lane-moonee-ponds",
        "pentridge-cinema",
        "reading-epping",
        "reading-melton",
        "reading-millers-junction",
        "reading-sunbury",
        "sun-theatre-yarraville",
        "thornbury-picture-house",
        "united-cinemas-craigieburn",
        "village-cinemas-airport-west",
        "village-cinemas-coburg-drive-in",
        "village-plenty-valley",
        "village-cinemas-sunshine",
        "village-cinemas-werribee",
    ],
    "south": [
        "classic-elsternwick",
        "hoyts-frankston",
        "palace-brighton-bay",
        "palace-dendy-brighton",
        "reading-dandenong",
        "village-cinemas-fountain-gate",
        "village-cinemas-karingal",
        "village-cinemas-southland",
    ],
}

# --------------------------------------------------------------------------- #
# Hand-curated knowledge that no single upstream field expresses.
# Keys are our stable cinema ids.
# --------------------------------------------------------------------------- #
#: Keys are the ids this script derives from the venue name, which is why
#: some look redundant ("imax-melbourne").  ``source`` names the adapter that
#: owns the venue; chain venues get theirs overwritten by enrich_registry.py.
CURATED: dict[str, dict] = {
    # --- independents & art-houses -------------------------------------
    "acmi-australia-centre-for-the-moving-image": {
        "source": "acmi", "tags": ["arthouse", "festival", "free-sessions"]},
    "cinema-nova-melbourne": {
        "source": "nova", "tags": ["arthouse", "independent"]},
    "lido-cinemas-hawthorn": {
        "source": "movingstory", "tags": ["arthouse", "independent", "rooftop"]},
    "classic-elsternwick": {
        "source": "movingstory", "tags": ["arthouse", "independent", "repertory"]},
    "sun-theatre-yarraville": {
        "source": "sun", "tags": ["arthouse", "independent", "heritage"]},
    "imax-melbourne": {"source": "imax", "tags": ["imax", "large-format"]},
    "brunswick-picture-house": {
        "source": "generic_html", "tags": ["arthouse", "single-screen", "independent"]},
    "thornbury-picture-house": {
        "source": "generic_html", "tags": ["arthouse", "single-screen", "independent"]},
    "eclipse-cinema": {
        "source": "generic_html", "tags": ["arthouse", "single-screen", "independent"]},
    "fomo-cinemas-east-brunswick": {
        "source": "generic_html", "tags": ["independent", "boutique", "single-screen"]},
    "chinatown-cinema-melbourne": {
        "source": "generic_html", "tags": ["arthouse", "chinese-language"]},
    # Cameo runs the same "Moving Story" platform as Lido and Classic.
    "cameo-cinemas": {
        "source": "movingstory", "tags": ["arthouse", "independent", "outdoor"]},
    "moonlight-cinema-melbourne": {
        "source": "generic_html", "tags": ["outdoor", "seasonal"]},
    "metro-boronia": {"source": "generic_html", "tags": ["independent"]},
    # --- Reading (its own SPA API) -------------------------------------
    "reading-burwood-brickworks": {"source": "reading"},
    "reading-chirnside-park": {"source": "reading"},
    "reading-dandenong": {"source": "reading"},
    "reading-epping": {"source": "reading"},
    "reading-melton": {"source": "reading"},
    "reading-millers-junction": {"source": "reading"},
    "reading-sunbury": {"source": "reading"},
    "waverley-cinemas": {"source": "generic_html", "tags": ["independent"]},
    "united-craigieburn": {"source": "generic_html", "tags": ["independent"]},
    # --- Palace (arthouse chain) ---------------------------------------
    "astor-theatre-st-kilda": {
        "source": "palace", "source_ids": {"palace": "180"},
        "tags": ["arthouse", "repertory", "35mm", "70mm", "heritage", "single-screen"]},
    "pentridge-cinema": {
        "source": "palace", "source_ids": {"palace": "165"},
        "tags": ["arthouse", "boutique"]},
    "palace-penny-lane": {"source": "palace", "source_ids": {"palace": "190"}},
    "palace-westgarth": {"source": "palace", "source_ids": {"palace": "159"}},
    "palace-balwyn": {"source": "palace", "source_ids": {"palace": "100"}},
    "palace-kino-cinemas": {"source": "palace", "source_ids": {"palace": "300"}},
    "palace-cinema-como": {"source": "palace", "source_ids": {"palace": "155"}},
    "palace-brighton-bay": {"source": "palace", "source_ids": {"palace": "151"}},
    "palace-church-street": {
        "source": "palace", "source_ids": {"palace": "152"},
        "tags": ["arthouse"]},
    # --- suburb corrections ----------------------------------------------
    # Flicks sometimes gives the greater-city name, the building name, or a
    # mis-spelling; the postcode identifies the real locality.
    "cinema-nova-melbourne": {"suburb": "Carlton", "postcode": "3053",
                              "tags": ["arthouse", "independent"]},
    "imax-melbourne": {"suburb": "Carlton", "postcode": "3053",
                       "tags": ["imax", "large-format"]},
    "hoyts-docklands": {"suburb": "Docklands", "postcode": "3008"},
    "waverley-cinemas": {"suburb": "Mount Waverley", "postcode": "3149"},
    "village-m-city-clayton": {"suburb": "Clayton", "postcode": "3168"},
    "village-century-city": {"suburb": "Glen Waverley", "postcode": "3150"},
    "hoyts-eastland-ringwood": {"suburb": "Ringwood", "postcode": "3134"},
    # --- venue facts worth carrying ------------------------------------
    "village-coburg-drive-in": {"tags": ["drive-in", "outdoor"]},
    "hoyts-highpoint-and-imax": {"tags": ["imax", "large-format"]},
    "hoyts-chadstone": {"tags": ["imax", "large-format"]},
    "village-fountain-gate": {"tags": ["imax", "large-format"]},
    "palace-cinema-como": {"tags": ["arthouse", "platinum"]},
    "village-crown": {"tags": ["gold-class"]},
    "acmi": {"source": "acmi"},
}


def _strip_tags(html: str) -> str:
    return re.sub(r"<[^>]+>", " ", html)


def _clean(text: str) -> str:
    text = _strip_tags(text)
    text = text.replace("&amp;", "&").replace("&#39;", "'").replace("&nbsp;", " ")
    text = text.replace("&quot;", '"').replace("&lt;", "<").replace("&gt;", ">")
    return re.sub(r"\s+", " ", text).strip()


def parse_cinema_page(html: str, slug: str) -> dict:
    """Pull address / phone / website / coordinates out of a Flicks page."""
    out: dict = {"slug": slug}

    # <h1>-ish title: the page title is "NAME | Movie Session Times & Tickets..."
    m = re.search(r"<title>(.*?)\s*\|\s*Movie Session", html, re.S)
    if m:
        out["name"] = _clean(m.group(1))
    if not out.get("name"):
        m = re.search(r'<h1[^>]*>(.*?)</h1>', html, re.S)
        if m:
            out["name"] = _clean(m.group(1))

    # Coordinates live on the embedded-map div.
    m = re.search(r'id="map"[^>]*data-lat="(-?\d+\.\d+)"[^>]*data-lon="(-?\d+\.\d+)"', html)
    if m:
        out["latitude"] = float(m.group(1))
        out["longitude"] = float(m.group(2))

    # Contact list: address, phone (tel:), website (http target=_blank).
    m = re.search(r'data-lat=.*?</div></div></div>', html, re.S)
    block = m.group(0) if m else html

    m = re.search(r'contact-item"><span[^>]*aria-hidden="true"><svg><use xlink:href="#pin">'
                  r'</use></svg></span><div class="single-cinema__contact-text">(.*?)</div>', html, re.S)
    if m:
        out["address"] = _clean(m.group(1))

    m = re.search(r'href="tel:([^"]+)"', html)
    if m:
        out["phone"] = m.group(1).strip()

    m = re.search(r'contact-link" href="(https?://[^"]+)" target="_blank"', html)
    if m:
        website = m.group(1).strip()
        if "flicks.com.au" not in website:
            out["website"] = website

    out.update(parse_address(out.get("address", "")))
    return out


_STATE = re.compile(r"\b(VIC|Victoria)\b\.?", re.IGNORECASE)

#: Tokens that mark a string as a street address rather than a locality.
_STREET_SUFFIX = re.compile(
    r"\b(street|st|road|rd|avenue|ave|highway|hwy|lane|drive|parade|boulevard|"
    r"crescent|court|place|terrace|esplanade|close|quay|way|circuit|grove|"
    r"mall|square|pde|cnr|corner)\b",
    re.IGNORECASE,
)


def _looks_like_street(text: str) -> bool:
    return bool(re.search(r"\d", text)) or bool(_STREET_SUFFIX.search(text))


def parse_address(addr: str) -> dict:
    """Extract suburb + postcode from the two address shapes Flicks uses.

    * ``"8 Louis Street, Airport West 3042, Victoria"``  (postcode last)
    * ``"Northland Shopping Centre, 50 Murray Road 3072, Preston, VIC"``
      (postcode mid-string)

    Returns ``{}`` when neither shape matches, so a caller can fall back to a
    previous value rather than overwriting good data with an empty string.
    """
    if not addr:
        return {}
    a = addr.replace("\u00a0", " ").strip().rstrip(",")
    a = _STATE.sub("", a)
    a = re.sub(r"\s*,\s*", ", ", a).strip(" ,")

    # The postcode is the LAST 4-digit token, not the first: street numbers are
    # also four digits ("1628 Burwood Highway, Belgrave 3160"), so matching
    # left-to-right turns Belgrave's postcode into its street number.
    matches = list(re.finditer(r"\b(\d{4})\b", a))
    if not matches:
        return {}
    m = matches[-1]
    postcode = m.group(1)
    before = a[: m.start()].strip(" ,")
    after = a[m.end():].strip(" ,")

    segments = [s.strip() for s in before.split(",") if s.strip()]

    if after:
        suburb = after
    else:
        suburb = segments[-1] if segments else before

    suburb = _STATE.sub("", suburb).strip(" ,")

    # Two localities before the postcode ("Hawthorn, Melbourne, VIC 3122") is
    # the suburb-then-greater-city form; the suburb is the *earlier* one.  But
    # only if that earlier segment is not itself a street ("…Latrobe Street,
    # Melbourne 3000" must stay Melbourne).
    if (
        suburb.lower() in ("melbourne", "victoria", "vic", "australia")
        and len(segments) >= 2
        and not _looks_like_street(segments[-2])
    ):
        suburb = _STATE.sub("", segments[-2]).strip(" ,")

    suburb = re.sub(r"\s{2,}", " ", suburb).strip()
    if not suburb or re.search(r"\d", suburb):
        # "50 Murray Road" is a street, not a suburb
        return {"postcode": postcode}
    return {"suburb": suburb, "postcode": postcode}


def within_metro(lat: float | None, lon: float | None) -> bool:
    if lat is None or lon is None:
        return True  # keep it; a missing coord is a data gap, not a reason to drop
    b = MELBOURNE_BOUNDS
    return (b["min_lat"] <= lat <= b["max_lat"]) and (b["min_lon"] <= lon <= b["max_lon"])


def load_existing() -> dict[str, dict]:
    if REGISTRY_PATH.exists():
        try:
            rows = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
            return {r["id"]: r for r in rows}
        except (json.JSONDecodeError, KeyError):
            return {}
    return {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="report only")
    ap.add_argument("--fresh", action="store_true", help="bypass HTTP cache")
    args = ap.parse_args()

    http = Http(ttl=7 * 86400)
    if args.fresh:
        http.force_refresh = True

    existing = load_existing()
    rows: list[dict] = []
    failures: list[str] = []

    for region, slugs in REGIONS.items():
        for slug in slugs:
            url = f"{FLICKS}/cinema/{slug}/"
            try:
                resp = http.get(url)
            except Exception as exc:                       # noqa: BLE001
                failures.append(f"{slug}: {exc}")
                continue
            info = parse_cinema_page(resp.text, slug)
            cid = slugify(info.get("name") or slug)

            curated = CURATED.get(cid, {})
            # Also allow curation keyed by the flicks slug for the awkward ones
            curated.update({k: v for k, v in CURATED.get(slug, {}).items() if k not in curated})

            prev = existing.get(cid, {})
            row = {
                "id": cid,
                "name": info.get("name") or slug,
                "chain": _infer_chain(info.get("name", "")),
                "address": info.get("address", prev.get("address", "")),
                "suburb": curated.get("suburb")
                or info.get("suburb")
                or prev.get("suburb", ""),
                "postcode": curated.get("postcode")
                or info.get("postcode")
                or prev.get("postcode", ""),
                "state": "VIC",
                "latitude": info.get("latitude", prev.get("latitude")),
                "longitude": info.get("longitude", prev.get("longitude")),
                "phone": info.get("phone", prev.get("phone", "")),
                "website": info.get("website", prev.get("website", "")),
                "booking_url": prev.get("booking_url", ""),
                "screens": prev.get("screens"),
                "source": curated.get("source", prev.get("source") or "generic_html"),
                "source_ids": {**prev.get("source_ids", {}), **curated.get("source_ids", {})},
                "tags": sorted(set(prev.get("tags", [])) | set(curated.get("tags", []))),
                "notes": prev.get("notes", ""),
                "region": region,
                "flicks_slug": slug,
                "active": True,
            }
            if not within_metro(row["latitude"], row["longitude"]):
                row["metro"] = False
                row["notes"] = (row["notes"] + " Outside greater Melbourne.").strip()
            else:
                row["metro"] = True
            rows.append(row)
            print(f"  ok  {row['id']:<38} {row['suburb'] or '?':<18} "
                  f"{row['latitude']}, {row['longitude']}")

    # Cinemas Flicks does not list (rooftops, universities, theatre-screens).
    # These carry hand-checked coordinates rather than a Flicks page.
    for venue in MANUAL_VENUES:
        row = _manual_row(venue, existing)
        rows.append(row)
        print(f"  ok  {row['id']:<38} {row['suburb'] or '?':<18} "
              f"{row['latitude']}, {row['longitude']}  (manual)")

    rows.sort(key=lambda r: (r["suburb"] or "", r["name"]))

    print(f"\nparsed {len(rows)} venues, {len(failures)} failures")
    for f in failures:
        print("  FAIL", f)

    missing_coords = [r["id"] for r in rows if r["latitude"] is None]
    if missing_coords:
        print(f"  {len(missing_coords)} venues without coordinates: {missing_coords}")

    if args.check:
        return 0

    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    REGISTRY_PATH.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {REGISTRY_PATH} ({len(rows)} venues)")
    return 0


def _infer_chain(name: str) -> str:
    low = name.lower()
    for chain in ("hoyts", "village", "palace", "reading", "event", "united",
                  "dendy", "imax", "acmi", "wallis", "metropolitan", "metro "):
        if chain in low:
            return chain.strip().capitalize() if chain != "imax" else "IMAX"
    return ""


#: Venues that are real cinemas but that Flicks does not list, so they cannot
#: be harvested from the region pages.  Coordinates are hand-checked against
#: OpenStreetMap; the address strings are the venues' own.  These are mostly
#: occasional or seasonal screens (a rooftop, a university cinema, a theatre
#: that runs films between live shows), so several will legitimately have no
#: scheduled screenings at any given moment — the registry records them, the
#: adapter layer decides whether a schedule exists.
MANUAL_VENUES: list[dict] = [
    {
        "id": "rooftop-cinema",
        "name": "Rooftop Cinema",
        "chain": "",
        "address": "252 Swanston Street, Melbourne 3000",
        "suburb": "Melbourne",
        "postcode": "3000",
        "latitude": -37.81204,
        "longitude": 144.96530,
        "website": "https://rooftopcinema.com.au",
        "source": "generic_html",
        "tags": ["outdoor", "seasonal", "arthouse"],
        "notes": "Open-air screen on the roof of Curtin House; summer season only.",
    },
    {
        "id": "capitol-theatre",
        "name": "The Capitol",
        "chain": "",
        "address": "113 Swanston Street, Melbourne 3000",
        "suburb": "Melbourne",
        "postcode": "3000",
        "latitude": -37.81499,
        "longitude": 144.96605,
        "website": "https://www.capitoltheatre.com.au",
        "source": "generic_html",
        "tags": ["heritage", "arthouse", "festival"],
        "notes": "RMIT's restored 1924 cinema; screens films and festivals.",
    },
    {
        "id": "forum-theatre",
        "name": "Forum Theatre",
        "chain": "",
        "address": "154 Flinders Street, Melbourne 3000",
        "suburb": "Melbourne",
        "postcode": "3000",
        "latitude": -37.81644,
        "longitude": 144.96938,
        "website": "https://www.forumtheatre.com.au",
        "source": "generic_html",
        "tags": ["heritage", "festival"],
        "notes": "Primarily a live venue; runs film festivals and screenings.",
    },
    {
        "id": "la-trobe-university-cinema",
        "name": "La Trobe University Cinema",
        "chain": "",
        "address": "La Trobe University, Bundoora 3083",
        "suburb": "Bundoora",
        "postcode": "3083",
        "latitude": -37.71694,
        "longitude": 145.04398,
        "website": "https://www.latrobe.edu.au",
        "source": "generic_html",
        "tags": ["university", "arthouse"],
        "notes": "Campus cinema; programme is term-dependent.",
    },
    {
        "id": "thornbury-theatre",
        "name": "Thornbury Theatre",
        "chain": "",
        "address": "859 High Street, Thornbury 3071",
        "suburb": "Thornbury",
        "postcode": "3071",
        "latitude": -37.75466,
        "longitude": 145.00112,
        "website": "https://www.thornburytheatre.com.au",
        "source": "generic_html",
        "tags": ["heritage", "festival"],
        "notes": "Live music venue that also runs film nights.",
    },
]


def _manual_row(venue: dict, existing: dict) -> dict:
    """Shape a MANUAL_VENUES entry into the same row the harvester emits."""
    prev = existing.get(venue["id"], {})
    row = {
        "id": venue["id"],
        "name": venue["name"],
        "chain": venue.get("chain", ""),
        "address": venue.get("address", ""),
        "suburb": venue.get("suburb", ""),
        "postcode": venue.get("postcode", ""),
        "state": "VIC",
        "latitude": venue.get("latitude"),
        "longitude": venue.get("longitude"),
        "phone": venue.get("phone", ""),
        "website": venue.get("website", ""),
        "booking_url": "",
        "screens": prev.get("screens"),
        "source": venue.get("source", "generic_html"),
        "source_ids": prev.get("source_ids", {}),
        "tags": sorted(set(prev.get("tags", [])) | set(venue.get("tags", []))),
        "notes": venue.get("notes", prev.get("notes", "")),
        "region": "melbourne",
        "metro": True,
        "active": True,
        "flicks_slug": "",
    }
    return row



if __name__ == "__main__":
    raise SystemExit(main())
