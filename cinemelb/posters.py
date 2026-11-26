"""Resolve a proper cover image for every film in the catalogue.

The venue feeds are the only place many arthouse titles have a cover, and some
of those feeds are worse than useless: Palace's ``/poster/<name>`` endpoint
answers *every* filename with the same Palace Cinemas logo, so a third of the
catalogue was showing a house advert as its poster. IMDb's own search
suggestion endpoint (``v2.sg.media-imdb.com``) is public, keyless, and returns a
poster URL with each candidate, which makes it the reliable fallback.

Matching is deliberately strict. A wrong poster is worse than no poster: the
rules accept an exact normalised-title match, or a near-exact one with a
release year within a year, and prefer the IMDb id the ratings pass already
resolved. Everything else is recorded as a miss and the venue's own poster (if
it is not a known placeholder) is kept.

Build-time and cached, exactly like :mod:`cinemelb.ratings`: the browser never
calls IMDb, and ``data/posters.json`` is a local artefact.
"""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import quote

import requests

from .ratings import title_year_hint

#: IMDb's search suggestion host, the one its own search box calls.
SUGGEST = "https://v2.sg.media-imdb.com/suggestion"

#: Poster size requested from the IMDb image CDN. 300x444 is ~16 KB per cover;
#: the full-size originals are 100-250 KB and the cards never render wider.
POSTER_WIDTH = 300
POSTER_HEIGHT = 444

#: Default cache lifetime. Posters change rarely; re-run with --refresh when a
#: wrong match is spotted.
TTL_DAYS = 30.0

#: Placeholder images that must never be presented as a film's cover.
_PLACEHOLDER_PATTERNS = (
    re.compile(r"palacecinemas\.com\.au/poster/", re.I),
    re.compile(r"empty-poster", re.I),
    re.compile(r"no[-_]?poster", re.I),
    re.compile(r"placeholder", re.I),
    re.compile(r"coming[-_]?soon", re.I),
    re.compile(r"/assets/pizza-pie/", re.I),
)

#: Only these IMDb title kinds are films. A podcast episode or TV series with a
#: similar name is not a cover for tonight's screening.
_FILM_KINDS = {"movie", "short", "tvMovie", "video", "tvSpecial"}

#: Search-string noise. Parentheticals are removed wholesale; these are the
#: trailing qualifiers that tickets feeds append to a title.
_NOISE = re.compile(
    r"(?:^|\s[-–—:]\s*)"
    r"(?:imax(?:\s*\d+mm)?|70mm|35mm|4k|3d|4dx|dolby(?:\s+atmos)?|"
    r"encore(?:\s+screening)?|live(?:\s+from.*)?|on\s+stage|q\s*(?:&|and)\s*a|"
    r"tickets?\s+on\s+sale!?|restored|restoration|"
    r"re[-\s]?issue|re[-\s]?release|"
    r"anniversary(?:\s+\d+th)?|season\s+\d+|"
    r"special\s+(?:event|screening)|final\s+screening|last\s+chance|"
    r"subbed|dubbed|"
    r"presented\s+by.*|with\s+subtitles)\s*$",
    re.I,
)

#: Programme and venue prefixes used by Melbourne listings. Festival codes can
#: be followed by a colon ("JFF26: Look Back") or just a space ("BFF26 Hill").
#: Any *FF code with an explicit colon is treated as a festival prefix too, so
#: CASFFA and the next niche festival do not need a code change.
_PREFIX = re.compile(
    r"^(?:(?:bff|jiff|miff|jff|sff)\d{2}\s*[:\-–—]?\s+|"
    r"[A-Za-z]{2,8}FF[A-Za-z]?\d{0,2}\s*[:\-–—]\s+|"
    r"cult\s+classix\s*[:\-–—]?\s+|"
    r"(?:astor|the\s+astor(?:\s+theatre)?|classic\s+elsternwick|lido|cameo|"
    r"cinema\s+nova|palace(?:\s+cinemas)?)\s*[:\-–—]\s*)",
    re.I,
)

#: A bare presentation format at the end ("Australia 3D", "Dune IMAX").
_TRAILING_FORMAT = re.compile(r"(?:^|\s)(?:3d|4dx|imax(?:\s*\d+mm)?)\s*$", re.I)

_LEAD_ARTICLE = re.compile(r"^(?:the|a|an)\s+")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_TRAILING = re.compile(r"\s*(?:\.{3,}|…)\s*$")

#: Number words and digits mean the same thing in a title ("Part 3").
_NUMBER_WORDS = {
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10",
}


def _ascii(text: str) -> str:
    """Fold accents and typographic punctuation for comparison only."""
    text = unicodedata.normalize("NFKC", str(text or ""))
    text = text.replace("’", "'").replace("‘", "'")
    text = text.replace("–", "-").replace("—", "-")
    text = text.replace("&", " and ")
    return text


def search_title(raw: str) -> str:
    """The string to send IMDb: the title without listing noise.

    This is about retrieval, not matching, so it stays close to what a person
    would type. Matching runs through :func:`ratings.normalise_title` on both
    sides instead.
    """
    text = _ascii(raw)
    text = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", text)
    text = _PREFIX.sub("", text)
    text = _TRAILING.sub("", text)
    # Peel stacked qualifiers ("- IMAX 70MM - 3D").
    for _ in range(3):
        before = text
        text = _NOISE.sub("", text)
        text = _TRAILING_FORMAT.sub("", text)
        if text == before:
            break
    text = text.replace("…", " ")
    return " ".join(text.split()).strip(" -:,")


def _match_key(text: str) -> str:
    """Normalise for comparison, without the semantic peeling ratings uses.

    The ratings normaliser drops ``live`` as presentation noise, which turns
    "A Time to Live and a Time to Die" into "time to" and would match almost
    anything. Covers need a literal comparison, so only case, punctuation,
    accents, a leading article and number words are folded here.
    """
    text = _ascii(text)
    text = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", text)
    text = text.replace("'", "")
    text = _NON_ALNUM.sub(" ", text.casefold())
    words = [_NUMBER_WORDS.get(word, word) for word in text.split()]
    key = " ".join(words)
    return _LEAD_ARTICLE.sub("", key)


def is_placeholder(url: str) -> bool:
    """True for cover URLs known to be venue branding rather than a film."""
    if not url:
        return True
    return any(pattern.search(url) for pattern in _PLACEHOLDER_PATTERNS)


def poster_url(image_url: str, width: int = POSTER_WIDTH,
               height: int = POSTER_HEIGHT) -> str:
    """Resize an IMDb CDN image to a card-sized cover.

    IMDb image URLs look like ``.../MV5B...@._V1_.jpg``; the ``_V1_`` block is
    a transform spec, so replacing it asks the CDN for a smaller crop instead
    of downloading the 1600x2048 original.
    """
    url = str(image_url or "").strip()
    if not url:
        return ""
    spec = f"._V1_UX{width}_CR0,0,{width},{height}_AL_.jpg"
    cut = url.find("._V1_")
    if cut > 0:
        return url[:cut] + spec
    if url.endswith(".jpg"):
        return url[: -len(".jpg")] + spec
    return url


def _similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _time(value: Any) -> datetime | None:
    try:
        text = str(value or "").replace("Z", "+00:00")
        stamp = datetime.fromisoformat(text)
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return stamp
    except ValueError:
        return None


def _fresh(entry: Mapping[str, Any], ttl_days: float) -> bool:
    if ttl_days <= 0:
        return False
    stamp = _time(entry.get("fetched_at"))
    if stamp is None:
        return False
    age = (datetime.now(timezone.utc) - stamp).total_seconds() / 86400
    return age <= ttl_days


def load_cache(path: Path | str) -> dict[str, dict]:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def save_cache(path: Path | str, cache: Mapping[str, Mapping[str, Any]]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    ordered = {key: cache[key] for key in sorted(cache)}
    p.write_text(json.dumps(ordered, indent=2, ensure_ascii=False) + "\n",
                 encoding="utf-8")


def _normalise_candidates(payload: Any) -> list[dict]:
    if not isinstance(payload, Mapping):
        return []
    out = []
    for item in payload.get("d") or []:
        if not isinstance(item, Mapping):
            continue
        imdb_id = str(item.get("id") or "")
        if not imdb_id.startswith("tt"):
            continue
        image = item.get("i") or {}
        url = image.get("imageUrl") if isinstance(image, Mapping) else ""
        out.append({
            "id": imdb_id,
            "title": str(item.get("l") or ""),
            "year": item.get("y"),
            "kind": str(item.get("qid") or ""),
            "url": str(url or ""),
        })
    return out


def suggest(title: str, session: requests.Session,
            timeout: float = 20.0) -> list[dict]:
    """Ask IMDb's suggestion endpoint for this title."""
    query = search_title(title)
    if not query:
        return []
    head = next((ch for ch in query.lower() if ch.isalnum()), "x")
    url = f"{SUGGEST}/{quote(head)}/{quote(query.replace(' ', '_'))}.json"
    reply = session.get(url, timeout=timeout)
    reply.raise_for_status()
    return _normalise_candidates(reply.json())


def choose(candidates: Iterable[dict], title: str, year: int | None,
           imdb_id: str = "") -> dict | None:
    """Pick the candidate that is the same film, or nothing.

    Order of trust: the id the ratings pass resolved, then an exact normalised
    title, then a near-exact title with a matching year. A year mismatch of
    more than one year vetoes the fuzzy branch, which is what keeps a repertory
    title from borrowing a remake's cover.
    """
    wanted = _match_key(search_title(title))
    if not wanted:
        return None
    best: tuple[float, dict] | None = None
    exact: list[tuple[int, dict]] = []

    for candidate in candidates:
        if candidate.get("kind") not in _FILM_KINDS:
            continue
        if not candidate.get("url"):
            continue
        key = _match_key(candidate.get("title") or "")
        if not key:
            continue
        cand_year = candidate.get("year")
        if imdb_id and candidate.get("id") == imdb_id:
            return {**candidate, "confidence": "high", "method": "ratings-id"}
        if key == wanted:
            distance = abs(int(cand_year) - year) if (year and cand_year) else 0
            exact.append((distance, candidate))
            continue
        ratio = _similarity(wanted, key)
        if ratio < 0.92:
            continue
        if year and cand_year and abs(int(cand_year) - year) > 1:
            continue
        score = ratio + (0.1 if not (year and cand_year) else 0.05)
        if best is None or score > best[0]:
            best = (score, candidate)

    if exact:
        exact.sort(key=lambda pair: pair[0])
        return {**exact[0][1], "confidence": "high", "method": "exact"}
    if best:
        return {**best[1], "confidence": "medium", "method": "fuzzy"}
    return None


def resolve(film: Mapping[str, Any], session: requests.Session,
            imdb_id: str = "") -> dict:
    """One film -> a cache entry (a hit or a recorded miss)."""
    title = str(film.get("title") or "")
    year = film.get("year") or title_year_hint(title)
    entry: dict[str, Any] = {
        "url": "",
        "source": "",
        "imdb_id": "",
        "matched_title": "",
        "matched_year": None,
        "confidence": "",
        "match_method": "",
        "status": "miss",
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    try:
        candidates = suggest(title, session)
    except Exception as exc:                                     # noqa: BLE001
        entry["error"] = f"{type(exc).__name__}: {exc}"[:200]
        return entry

    pick = choose(candidates, title, year, imdb_id=imdb_id)
    if pick is None:
        return entry
    # An exact or id match can occasionally carry no image; keep it a miss.
    url = poster_url(pick.get("url") or "")
    if not url:
        return entry
    entry.update({
        "url": url,
        "source": "imdb",
        "imdb_id": pick.get("id") or "",
        "matched_title": pick.get("title") or "",
        "matched_year": pick.get("year"),
        "confidence": pick.get("confidence") or "",
        "match_method": pick.get("method") or "",
        "status": "ok",
    })
    return entry


def enrich(
    films: list[Mapping[str, Any]],
    *,
    cache_path: Path | str,
    imdb_ids: Mapping[str, str] | None = None,
    limit: int | None = None,
    ttl_days: float = TTL_DAYS,
    pace: float = 0.35,
    retry_misses: bool = False,
    progress=None,
) -> dict[str, Any]:
    """Fill ``data/posters.json`` for the catalogue.

    ``imdb_ids`` maps film key -> id resolved by the ratings pass. It is used
    both to break ties and to skip the search for films whose id is already
    known: a suggestion call is still made (the endpoint returns the poster),
    but any candidate with that id wins outright.
    """
    import time

    cache = load_cache(cache_path)
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/120.0 Safari/537.36",
        "Accept-Language": "en-AU,en;q=0.9",
        "Accept": "application/json,text/plain,*/*",
    })

    stats = {"films": len(films), "cached": 0, "looked": 0, "ok": 0, "miss": 0,
             "failed": 0, "skipped": 0}
    worked = 0
    stopped: str | None = None

    for film in films:
        key = str(film.get("key") or "")
        title = str(film.get("title") or "")
        if not key or not title:
            stats["skipped"] += 1
            continue
        entry = cache.get(key)
        stale_miss = bool(retry_misses and entry and entry.get("status") != "ok")
        if entry and _fresh(entry, ttl_days) and not stale_miss:
            stats["cached"] += 1
            if entry.get("status") == "ok":
                stats["ok"] += 1
            else:
                stats["miss"] += 1
            if progress:
                progress(key, entry)
            continue
        if limit is not None and stats["looked"] >= limit:
            stats["skipped"] += 1
            if progress:
                progress(key, entry or {"status": "skipped"})
            continue
        if stopped:
            stats["skipped"] += 1
            continue

        imdb_id = (imdb_ids or {}).get(key, "")
        entry = resolve(film, session, imdb_id=imdb_id)
        cache[key] = entry
        stats["looked"] += 1
        worked += 1
        if entry.get("status") == "ok":
            stats["ok"] += 1
        elif entry.get("error"):
            stats["failed"] += 1
            # A hard block is not per-title; stop instead of hammering IMDb.
            if "429" in entry["error"] or "403" in entry["error"]:
                stopped = entry["error"]
        else:
            stats["miss"] += 1
        if progress:
            progress(key, entry)
        if worked % 25 == 0:
            save_cache(cache_path, cache)
        if pace > 0:
            time.sleep(pace)

    save_cache(cache_path, cache)
    return {"cache": cache, "stats": stats}


def poster_for(film: Mapping[str, Any],
               cache: Mapping[str, Mapping[str, Any]]) -> tuple[str, bool]:
    """The cover to ship, and whether it came from the resolver.

    A cache hit wins. Otherwise the venue's own poster is kept unless it is a
    known placeholder, so an unr matched opera broadcast still shows something
    rather than the house logo.
    """
    entry = cache.get(str(film.get("key") or "")) or {}
    url = str(entry.get("url") or "")
    if url:
        return url, True
    original = str(film.get("poster_url") or "")
    if original and not is_placeholder(original):
        return original, False
    return "", False
