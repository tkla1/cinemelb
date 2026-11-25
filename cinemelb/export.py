"""Export the database as a static SPA bundle.

The web app is one HTML page plus one JSON file. Rather than re-implement the
query engine over HTTP, the whole dataset ships once and the browser answers
every question in memory. That only works because the payload is compressed
hard, and this module is where the compression happens.

Three decisions carry most of the size:

* **Index tables, not objects.** A screening row is nine integers into shared
  tables instead of nine repeated strings. Measured over the full dataset this
  is ~170 KB gzipped against ~263 KB for the equivalent object form.
* **Canonical attribute sets.** Sources spell the same badge a dozen ways
  ("CC", "Closed Captioning", "Closed Captioning Available"), so spellings are
  collapsed to a shared vocabulary first. That shrinks the table and gives the
  UI one thing to render.
* **A synopsis cap.** Full synopses are the single largest lever: they cost
  ~63 KB gzipped on their own. Truncating at a word boundary keeps the useful
  first sentence or two.

Rows are sorted by date then minute so the browser can slice a day cheaply.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .models import MELBOURNE_TZ
from .geo import known_places
from .store import Store

#: The static shell shipped alongside the bundle.
WEB_DIR = Path(__file__).resolve().parent / "web"

#: Default synopsis length. 0 keeps the full text.
SYNOPSIS_CAP = 200

#: Compound attributes arrive comma-separated ("Limited, NFT").
_ATTR_SPLIT = re.compile(r"\s*[,;]\s*")

#: Spellings that mean the same badge. Keys are lowercased and stripped.
_ATTR_CANON: dict[str, str] = {
    # closed captions
    "cc": "CC",
    "closed caption": "CC",
    "closed captions": "CC",
    "closed captioning": "CC",
    "closed captioning available": "CC",
    "captions": "CC",
    "captioned": "CC",
    "ccap": "CC",
    # open captions: burned in, no device needed. Distinct from CC, and
    # genuinely more useful to a viewer, so it keeps its own label.
    "ocaptions": "Open Captions",
    "open caption": "Open Captions",
    "open captions": "Open Captions",
    "open captioning": "Open Captions",
    "screening with open captions": "Open Captions",
    # audio description
    "ad": "AD",
    "audio description": "AD",
    "audio described": "AD",
    "audio description session": "AD",
    "audio description available": "AD",
    # hearing assistance
    "hi": "HI",
    "hoh": "HI",
    "hearing impaired": "HI",
    "hearing loop": "HI",
    # no free tickets: a booking-policy note, not a feature
    "nft": "NFT",
    "no free tickets": "NFT",
    "no free ticket": "NFT",
    "no free list": "NFT",
    # subtitles
    "subtitled": "Subtitled",
    "subtitles": "Subtitled",
    # both at once
    "ccad": "CC/AD",
    "captions and audio description": "CC/AD",
    "cc/ad": "CC/AD",
}

#: Rendering order, so identical sets always serialise identically.
_ATTR_ORDER = ["CC", "AD", "Open Captions", "HI", "Subtitled", "NFT"]


def _canonical_attrs(raw: Any) -> list[str]:
    """Collapse one row's attribute spellings to the shared vocabulary."""
    if not raw:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = [raw]
    out: list[str] = []
    for item in raw:
        for part in _ATTR_SPLIT.split(str(item)):
            part = " ".join(part.split())
            if not part:
                continue
            badge = _ATTR_CANON.get(part.lower(), part)
            # "CC/AD" means both, so it expands to two badges rather than
            # asking the UI to know about a combined spelling.
            for single in badge.split("/"):
                if single and single not in out:
                    out.append(single)
    out.sort(key=lambda b: (_ATTR_ORDER.index(b) if b in _ATTR_ORDER else len(_ATTR_ORDER), b))
    return out


def _truncate(text: str, cap: int) -> str:
    """Cut to a word boundary at or under ``cap``, marking the cut."""
    text = " ".join(str(text or "").split())
    if not cap or len(text) <= cap:
        return text
    cut = text[:cap].rsplit(" ", 1)[0] or text[:cap]
    return cut.rstrip(" ,;:—–-") + "…"


def _minute_of_day(value: str) -> int:
    """Parse "HH:MM" (or "H:MM:SS") into minutes past midnight."""
    hh, _, mm = str(value).partition(":")
    return int(hh) * 60 + int(mm or 0)


def _hashed(name: str, content: bytes) -> str:
    """Content-addressed filename, so assets can be cached immutably."""
    stem, _, ext = name.rpartition(".")
    digest = hashlib.sha256(content).hexdigest()[:8]
    return f"{stem}.{digest}.{ext}"


def build_bundle(
    store: Store,
    *,
    days: int | None = None,
    synopsis_cap: int = SYNOPSIS_CAP,
    include_urls: bool = True,
) -> dict[str, Any]:
    """Serialise the store into the index-table bundle the SPA consumes.

    ``days`` limits the export to the next N days from today (None = full
    coverage, which is what "all times for a film" needs).
    """
    today = datetime.now(MELBOURNE_TZ).date()
    cutoff = (today + timedelta(days=days - 1)).isoformat() if days else None

    cinemas = store.cinemas()
    cinema_ids = {c["id"] for c in cinemas}

    rows: list[dict[str, Any]] = []
    for r in store.screenings():
        if not r.get("local_time") or r["cinema_id"] not in cinema_ids:
            continue
        if cutoff and r["local_date"] > cutoff:
            continue
        rows.append(r)

    if not rows:
        raise SystemExit("no screenings to export — run `cinemelb refresh` first")

    # Only films that actually appear in the exported window. A film whose run
    # has ended should not be searchable, and its row would be dead weight.
    used_films = {r["film_key"] for r in rows}
    films = [f for f in store.films() if f["key"] in used_films]

    dates = sorted({r["local_date"] for r in rows})
    date_index = {d: i for i, d in enumerate(dates)}

    formats = sorted({r.get("format") or "" for r in rows})
    format_index = {f: i for i, f in enumerate(formats)}

    screens = sorted({r.get("screen") or "" for r in rows})
    screen_index = {s: i for i, s in enumerate(screens)}

    attr_sets: list[list[str]] = []
    attr_index: dict[str, int] = {}
    for r in rows:
        badges = _canonical_attrs(r.get("attributes"))
        key = "|".join(badges)
        if key not in attr_index:
            attr_index[key] = len(attr_sets)
            attr_sets.append(badges)

    urls: list[str] = []
    url_index: dict[str, int] = {}
    if include_urls:
        for r in rows:
            url = (r.get("ticket_url") or "").strip()
            if url and url not in url_index:
                url_index[url] = len(urls)
                urls.append(url)

    cinema_rows = [
        [
            c["name"],
            c.get("chain") or "",
            c.get("suburb") or "",
            c.get("address") or "",
            c.get("website") or "",
            c.get("tags") or [],
            round(float(c["latitude"]), 5) if c.get("latitude") is not None else None,
            round(float(c["longitude"]), 5) if c.get("longitude") is not None else None,
        ]
        for c in cinemas
    ]

    film_rows = [
        [
            f["key"],
            f.get("title") or "",
            f.get("year"),
            f.get("rating") or "",
            f.get("runtime_minutes"),
            f.get("genres") or [],
            f.get("poster_url") or "",
            _truncate(f.get("synopsis") or "", synopsis_cap),
            f.get("trailer_url") or "",
        ]
        for f in films
    ]

    film_index = {f["key"]: i for i, f in enumerate(films)}
    cinema_index = {c["id"]: i for i, c in enumerate(cinemas)}

    screening_rows: list[list[int]] = []
    for r in rows:
        screening_rows.append([
            date_index[r["local_date"]],
            film_index[r["film_key"]],
            _minute_of_day(r["local_time"]),
            cinema_index[r["cinema_id"]],
            format_index[r.get("format") or ""],
            attr_index["|".join(_canonical_attrs(r.get("attributes")))],
            1 if r.get("sold_out") else 0,
            url_index.get((r.get("ticket_url") or "").strip(), -1),
            screen_index[r.get("screen") or ""],
        ])

    # Date first, then minute: a day's slice is contiguous and time-ordered.
    screening_rows.sort(key=lambda row: (row[0], row[2], row[3]))

    # Gazetteer for the SPA's "near" box. The hard-coded landmarks are the
    # places people type ("fed square", "brunswick"), and the registry's own
    # suburbs cover anywhere a cinema sits. Anything else is looked up live.
    places: dict[str, tuple[float, float]] = dict(known_places())
    for c in cinemas:
        suburb = (c.get("suburb") or "").strip().lower()
        if suburb and c.get("latitude") is not None and suburb not in places:
            places[suburb] = (float(c["latitude"]), float(c["longitude"]))
    place_rows = [
        [name, round(lat, 5), round(lon, 5)]
        for name, (lat, lon) in sorted(places.items())
    ]

    bundle: dict[str, Any] = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "counts": {
            "cinemas": len(cinemas),
            "films": len(films),
            "screenings": len(screening_rows),
            "dates": len(dates),
            "urls": len(urls),
        },
        "cinemas": cinema_rows,
        "films": film_rows,
        "screenings": screening_rows,
        "dates": dates,
        "formats": formats,
        "attrs": attr_sets,
        "screens": screens,
        "urls": urls,
        "places": place_rows,
    }
    _validate(bundle)
    return bundle


def _validate(bundle: dict[str, Any]) -> None:
    """Fail loudly on a dangling index rather than shipping a broken deploy."""
    if not bundle.get("places"):
        raise SystemExit("bundle has no places table — the SPA cannot geocode")
    for name, lat, lon in bundle["places"]:
        if not name or not -90 <= lat <= 90 or not -180 <= lon <= 180:
            raise SystemExit(f"bad place row: {name!r} {lat!r} {lon!r}")
    limits = {
        0: len(bundle["dates"]),
        1: len(bundle["films"]),
        3: len(bundle["cinemas"]),
        4: len(bundle["formats"]),
        5: len(bundle["attrs"]),
        8: len(bundle["screens"]),
    }
    n_urls = len(bundle["urls"])
    for i, row in enumerate(bundle["screenings"]):
        if len(row) != 9:
            raise SystemExit(f"screening row {i} has {len(row)} fields, expected 9")
        for pos, limit in limits.items():
            if not 0 <= row[pos] < limit:
                raise SystemExit(
                    f"screening row {i} field {pos} = {row[pos]} out of range 0..{limit - 1}"
                )
        if not -1 <= row[7] < n_urls:
            raise SystemExit(f"screening row {i} url index {row[7]} out of range")
        if not 0 <= row[2] < 24 * 60:
            raise SystemExit(f"screening row {i} minute {row[2]} out of range")
        if row[6] not in (0, 1):
            raise SystemExit(f"screening row {i} sold flag {row[6]} is not 0/1")


def write_site(bundle: dict[str, Any], out_dir: Path | str) -> Path:
    """Write the deployable static site into ``out_dir``."""
    out = Path(out_dir).resolve()
    cwd = Path.cwd().resolve()
    if out == cwd or out in cwd.parents:
        raise SystemExit(f"refusing to write the site over {out}")

    if out.exists():
        shutil.rmtree(out)
    (out / "assets").mkdir(parents=True)
    (out / "data").mkdir(parents=True)

    css = (WEB_DIR / "app.css").read_bytes()
    js = (WEB_DIR / "app.js").read_bytes()
    css_name = _hashed("app.css", css)
    js_name = _hashed("app.js", js)
    (out / "assets" / css_name).write_bytes(css)
    (out / "assets" / js_name).write_bytes(js)

    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    html = html.replace("./assets/app.css", f"./assets/{css_name}")
    html = html.replace("./assets/app.js", f"./assets/{js_name}")
    (out / "index.html").write_text(html, encoding="utf-8")

    headers = WEB_DIR / "_headers"
    if headers.exists():
        shutil.copyfile(headers, out / "_headers")

    payload = json.dumps(bundle, separators=(",", ":"), ensure_ascii=False)
    (out / "data" / "bundle.json").write_text(payload, encoding="utf-8")

    (out / "robots.txt").write_text(
        "User-agent: *\nAllow: /\n", encoding="utf-8"
    )
    return out
