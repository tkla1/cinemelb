"""Build-time film rating enrichment.

Why this is build-time work rather than an API call from the page: the static
bundle has to answer instantly and must never carry a secret.  Scores move
slowly enough (a repertory title barely moves at all) that one nightly build is
the right granularity, and a cache with a TTL means a re-export is free.

The matching problem is the hard part, not the fetching.  A Melbourne cinema
calendar mixes wide releases with festival screenings, 70mm re-issues and
one-off events, so the same film arrives as ``DUNE: PART THREE - IMAX 70MM``,
``Dune: Part Three`` and ``Dune: Part 3``.  The primary matcher is therefore a
local join against the IMDb bulk datasets, not a fuzzy web search:

* ``title.basics`` gives the canonical title, year, runtime and title type.
* ``title.akas`` gives the regional titles (Australian first, then GB/US/NZ).
* ``title.ratings`` gives the score and, importantly, the vote count.

The vote count is a better disambiguator than release year for a repertory
calendar: ``AKIRA`` is the 1988 film even though a 2016 remake exists, and
picking the newest candidate would be exactly wrong.  Wikidata is kept as a
last-resort ID resolver for films IMDb does not carry, never as the first
matcher: ``wbsearchentities`` confidently returns the wrong film for short or
generic titles.

Scores come from the official IMDb bulk file when available, OMDb when the
caller supplies a key (IMDb/Metacritic only — it no longer returns a
Tomatometer), and the Metacritic finder as a keyless fallback.  Rotten Tomatoes
comes from RT's film pages only when the caller explicitly opts in with
``scrape=True``: the RT/MC contracts forbid automated collection, so that path
is deliberately off by default and the docstrings say so.

Everything here is sequential and paced.  A build that gets challenged should
lose one source for the rest of the run and keep the cached values, not retry a
WAF page in a loop.
"""

from __future__ import annotations

import csv
import difflib
import gzip
import json
import os
import pickle
import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import quote, urlsplit

import requests

from .config import USER_AGENT as _BROWSER_UA

# ---------------------------------------------------------------------------
# Endpoints and defaults
# ---------------------------------------------------------------------------

DEFAULT_CACHE_PATH = Path("data/ratings.json")

DEFAULT_URL = "https://datasets.imdbws.com/title.ratings.tsv.gz"
DEFAULT_BASICS_URL = "https://datasets.imdbws.com/title.basics.tsv.gz"
DEFAULT_AKAS_URL = "https://datasets.imdbws.com/title.akas.tsv.gz"

WIKIDATA_API = "https://www.wikidata.org/w/api.php"
OMDB_URL = "https://www.omdbapi.com/"
RT_MOVIE_URL = "https://www.rottentomatoes.com/m/{slug}"
WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"

#: Wikidata asks for a descriptive UA with a way to contact the operator.
#: The browser UA used for the venue pages is not appropriate for the query
#: service, which may rate-limit generic clients.
_WIKIDATA_UA = (
    "cinemelb/1.0 (personal non-commercial Melbourne cinema showtimes; "
    "https://cinemelb.pages.dev)"
)
MC_BACKEND = "https://backend.metacritic.com/reviews/metacritic"
MC_FINDER = "https://backend.metacritic.com/finder/metacritic/search/{title}/desktop"
MC_PAGE = "https://www.metacritic.com/movie/{slug}/"

_BASICS_HEADER = (
    "tconst", "titleType", "primaryTitle", "originalTitle", "isAdult",
    "startYear", "endYear", "runtimeMinutes", "genres",
)
_AKAS_HEADER = (
    "titleId", "ordering", "title", "region", "language", "types",
    "attributes", "isOriginalTitle",
)
_RATINGS_HEADER = ("tconst", "averageRating", "numVotes")

#: IMDb is a worldwide catalogue; the Australian/UK/US/NZ aliases are the ones
#: a Melbourne calendar is likely to use, with the worldwide (XWW) and other
#: English-speaking regions as a fallback.  Keeping only these regions in the
#: in-memory aka index is the difference between a useful 2 GB build and an
#: impossible one.  XWW matters: it often carries the only English alias.
_AKA_REGIONS = {"AU", "GB", "US", "NZ", "XWW", "CA", "IE"}

#: A browser UA is required by Metacritic and harmless everywhere else.  The
#: repository already keeps one for the scraper, so reuse it rather than adding
#: a second string that can drift.
_BROWSER_HEADERS = {
    "User-Agent": _BROWSER_UA,
    "Accept-Language": "en-AU,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
}

#: Front films are often upscaled from a lower-resolution master; the
#: difference is usually more than a minute or two.
_RUNTIME_TOLERANCE = 2
_YEAR_TOLERANCE = 1
_FUZZY_CUTOFF = 0.88

_NUM_WORDS = {
    "1": "one", "2": "two", "3": "three", "4": "four", "5": "five",
    "6": "six", "7": "seven", "8": "eight", "9": "nine", "10": "ten",
}

#: Words that carry no matching signal in a film title.  "la"/"le" are here
#: because "La La Land" and "La La Land" should loose-match regardless of how a
#: source capitalises the article.
_STOP_WORDS = frozenset(
    "the a an of and or to in on at for with from by de la le".split()
)


class RatingsError(RuntimeError):
    """Base class for recoverable enrichment failures."""


class IMDbDatasetError(RatingsError):
    """The IMDb bulk file is missing, unreadable, or has the wrong schema."""


class FetchError(RatingsError):
    """A request failed in a way the caller should show per film."""


class SourceStopped(RatingsError):
    """A source was challenged or repeatedly failed; skip it for this run."""


# ---------------------------------------------------------------------------
# Title normalisation
# ---------------------------------------------------------------------------

_TYPOGRAPHIC = str.maketrans({
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'", "\u2032": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u2033": '"',
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-",
    "\u2014": "-", "\u2015": "-", "\u2212": "-",
    "\u00b7": "-", "\u2022": "-",
    "\u2026": "...", "\u00a0": " ",
})

_YEAR_IN_TITLE = re.compile(r"[\(\[]\s*((?:19|20)\d{2})\s*[\)\]]")
_PARENS = re.compile(r"[\(\[]\s*[^\)\]]*\s*[\)\]]")

#: Programme, venue and festival names that prefix a title in a cinema feed.
#: They are spelled out rather than matched with a generic all-caps rule: a
#: generic ``^[A-Z]+`` would eat the "DUNE" in "DUNE: PART THREE".
_PREFIX_NAMES = (
    "exhibition on screen",
    "teatro di san carlo",
    "andrew lloyd webbers",
    "reading rewind",
    "opera de paris",
    "astor theatre",
    "cult classix",
    "walt disneys",
    "double feature",
    "double bill",
    "met opera",
    "la scala",
    "jodofest",
    "casffa",
    "disneys",
    "astor",
    "jiff",
    "bff",
    "miff",
    "jff",
)
_PREFIX_RE = re.compile(
    r"^(?:"
    + "|".join(re.escape(name) for name in sorted(_PREFIX_NAMES, key=len, reverse=True))
    + r")(?:\s*\d{2})?(?:\s*[:\-]\s*|\s+)"
)
_PREFIX_HEADS = frozenset(name.split()[0] for name in _PREFIX_NAMES)

#: Festival/programme codes such as JFF26, BFF26, MIFF26.  A generic
#: "letters then digits" rule is unsafe: "Apollo 13", "Ocean's 11" and
#: "The 40-Year-Old Virgin" are films, not festivals.  The code list is small
#: and explicit, and the name list above handles the uncoded spellings.
_FESTIVAL_CODES = (
    "humanrightsff", "melbff", "casffa", "jcsff", "fleff",
    "miff", "jiff", "sff", "hff", "iff", "jff", "bff", "mff",
)
_CODE_PREFIX_RE = re.compile(
    r"^(?:"
    + "|".join(sorted(_FESTIVAL_CODES, key=len, reverse=True))
    + r")\s*\d{2}\b\s*[:\-]?\s+"
)
_FESTIVAL_HEADS = frozenset(_FESTIVAL_CODES) | {"jodofest"}
_FESTIVAL_YEAR_RE = re.compile(
    r"^(?:"
    + "|".join(sorted(_FESTIVAL_HEADS, key=len, reverse=True))
    + r")\s*\d{4}\b\s*[:\-]?\s+"
)
_CODE_HEADS = frozenset(_FESTIVAL_CODES) | _PREFIX_HEADS

_ARTICLE_FLIP = re.compile(r",\s*(the|a|an)\s*$")

#: Trailing presentation/event clauses, in one alternation so parsing a
#: 60-million-row aka file does one regex pass per title rather than thirteen.
#: The lookbehind is what stops "Alive" becoming "A" or a film called "Encore"
#: becoming empty: a suffix has to start after whitespace or a separator.
_NOISE_SUFFIX = re.compile(
    r"(?<=[\s\-+:])[-+]?\s*(?:"
    r"imax(?:\s*(?:\d{2,3}\s*mm|4k|3d|2d))?(?:\s*presentation)?|"
    r"\d{2,3}\s*mm\s+imax(?:\s*presentation)?|"
    r"\d{2,3}\s*mm(?:\s*presentation)?|"
    r"4k(?:\s*(?:restoration|remaster(?:ed)?|presentation))?|"
    r"3d|2d|vistavision|dolby(?:\s+atmos)?|"
    r"re-?issue|"
    r"subbed|dubbed|open\s+captions?|captions?|subtitled|"
    r"(?:\w+\s+)?(?:subs?|subtitles?)|"
    r"(?:live\s+)?on\s+stage|"
    r"live(?:\s+.*)?|"
    r"encore|"
    r"(?:q\s*&\s*a|q\s+and\s+a)(?:\s+.*)?|"
    r"\d{1,3}(?:st|nd|rd|th)\s+anniversary|"
    r"tickets?\s+on\s+sale(?:\s+.*)?"
    r")$",
    re.IGNORECASE,
)
#: Bare "Anniversary" is only noise when a separator makes it a clause; a film
#: can legitimately be called "The Anniversary".
_ANNIVERSARY_SUFFIX = re.compile(
    r"(?<=[\s\-+:])[-+]\s*anniversary$", re.IGNORECASE
)
#: Cheap pre-check for the suffix pass.  A clean IMDb title ("The Matrix")
#: skips the expensive alternation entirely; anything with a separator or a
#: presentation word still goes through it.
_NOISE_MARKER_RE = re.compile(
    r"[\-+:,]|\bimax\b|\d{2,3}\s*mm|\b4k\b|\b[23]d\b|\bvistavision\b|"
    r"\bdolby\b|re-?issue|\bsubbed\b|\bdubbed\b|caption|\bsubs?\b|"
    r"subtitled|subtitles?|on\s+stage|\blive\b|\bencore\b|q\s+and\s+a|"
    r"anniversary|"
    r"tickets?\s+on\s+sale",
    re.IGNORECASE,
)

_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_DIGITS = re.compile(r"\d")
_NUMBER_WORD = re.compile(r"\b(10|[1-9])\b")
_LEADING_ARTICLE = re.compile(r"^(?:the|a|an)\s+")


def _repair_mojibake(text: str) -> str:
    """Undo the common UTF-8-read-as-cp1252 damage.

    ``\ufffd`` means the original byte is already gone, so dropping it is the
    only honest option; the surrounding words still normalise usefully.  Real
    mojibake (``AmÃ©lie``) round-trips through cp1252 into valid UTF-8.
    """
    if "\ufffd" in text:
        text = text.replace("\ufffd", "")
    if any(marker in text for marker in ("Ã", "Â", "â€")):
        try:
            fixed = text.encode("cp1252", "strict").decode("utf-8", "strict")
        except (UnicodeEncodeError, UnicodeDecodeError):
            return text
        if fixed:
            return fixed
    return text


def _ascii_fold(text: str) -> str:
    text = str(text or "")
    if text.isascii():
        # The overwhelming majority of IMDb titles take this branch; skipping
        # mojibake repair and NFC/NFKD here is a measurable win on a
        # 60-million-row aka file.
        return text.casefold()
    text = _repair_mojibake(text)
    text = unicodedata.normalize("NFC", text)
    text = text.translate(_TYPOGRAPHIC)
    text = text.casefold()
    # NFKD after casefold removes accents for matching without changing the
    # display title; both sides of every comparison go through here.
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return text.encode("ascii", "ignore").decode("ascii")


def _extract_year(text: str) -> tuple[str, int | None]:
    years = [int(m.group(1)) for m in _YEAR_IN_TITLE.finditer(text)]
    if not years:
        return text, None
    return _YEAR_IN_TITLE.sub(" ", text), years[0]


def _normalise_with_year(text: str) -> tuple[str, int | None]:
    """The core normaliser: return ``(strict_key, year_hint)``.

    Rules, in order (this is the contract the tests pin down):

    1. Repair mojibake, NFC, fold curly quotes to ``'``, dashes to ``-``,
       ``&`` to ``and``.
    2. Pull a parenthesised four-digit year out as the year hint and remove it;
       remaining parentheticals (language, subtitles, Q&A notes) are noise.
    3. Drop leading venue/programme/festival prefixes (Astor, Cult Classix,
       JIFF26, ...).  A bare all-caps word is *not* stripped, or DUNE would
       disappear; only known names and codes containing digits are.
    4. Rotate a trailing article ("Fox, The" -> "The Fox").
    5. Peel trailing presentation clauses: IMAX/70mm/35mm/4K/3D/Dolby,
       re-issue, subbed/dubbed, open captions, live, encore, Q&A, anniversary,
       tickets-on-sale.
    6. Remove apostrophes rather than turning them into spaces, so "Pan's"
       and "Pans" agree.
    7. Convert standalone digits 1-10 to words, so "Part 2" and "Part Two"
       agree; years and other numbers are left alone.
    8. Collapse punctuation/whitespace, casefold, and drop a leading article.
    """
    t = _ascii_fold(text).strip()
    year_hint: int | None = None
    if "(" in t or "[" in t:
        t, year_hint = _extract_year(t)
        t = _PARENS.sub(" ", t)
    if "&" in t:
        t = t.replace("&", " and ")
    if "'" in t:
        t = t.replace("'", "")

    # Peel prefixes; a feed can stack one (JFF26 Astor: ...).  The first-token
    # check avoids running two multi-alternative regexes on every IMDb title.
    prefix_body = t[4:] if t.startswith("the ") else t
    head = prefix_body.split(" ", 1)[0].rstrip(":-")
    looks_prefixed = (
        head in _PREFIX_HEADS
        or (len(head) > 2 and head[-2:].isdigit() and head[:-2] in _CODE_HEADS)
        or (len(head) > 4 and head[-4:].isdigit() and head[:-4] in _FESTIVAL_HEADS)
    )
    if looks_prefixed and (
        _PREFIX_RE.match(prefix_body)
        or _CODE_PREFIX_RE.match(prefix_body)
        or _FESTIVAL_YEAR_RE.match(prefix_body)
    ):
        # "The Astor Theatre: ..." is a venue prefix, not a film called
        # "The Astor Theatre"; the leading article belongs to the prefix.
        t = prefix_body
        for _ in range(2):
            before = t
            # Years and codes first: "JFF 26 ..." would otherwise leave a
            # bare "26" after the known-name rule matched just the "jff" part.
            t = _FESTIVAL_YEAR_RE.sub("", t)
            t = _CODE_PREFIX_RE.sub("", t)
            t = _PREFIX_RE.sub("", t)
            if t == before:
                break

    if "," in t:
        flip = _ARTICLE_FLIP.search(t)
        if flip:
            t = f"{flip.group(1)} {t[: flip.start()]}"

    if _NOISE_MARKER_RE.search(t):
        for _ in range(6):
            before = t
            t = _NOISE_SUFFIX.sub("", t)
            t = _ANNIVERSARY_SUFFIX.sub("", t)
            t = t.strip()
            if t == before:
                break

    # ``[^a-z0-9]+`` already collapses whitespace runs too.
    t = _NON_ALNUM.sub(" ", t).strip()

    # "Part 2" and "Part Two" are the same film to every source.
    if _DIGITS.search(t):
        t = _NUMBER_WORD.sub(lambda m: _NUM_WORDS[m.group(1)], t)

    if t.startswith(("the ", "a ", "an ")):
        t = _LEADING_ARTICLE.sub("", t)
    return t, year_hint


def normalise_title(text: str) -> str:
    """Canonical comparison form for a film title.

    The docstring of :func:`_normalise_with_year` lists the exact rules.  The
    important design choice is that noise is removed without flattening the
    title: "Part Three" stays, so "Dune" cannot match "Dune: Part Three".
    """
    return _normalise_with_year(text)[0]


def loose_key(text: str) -> str:
    """Stop-word-free, token-sorted form used by the fuzzy tier.

    Sorting makes word-order differences ("The Matrix Reloaded" vs
    "Matrix Reloaded, The") irrelevant; removing stop words stops "of the"
    dominating a similarity ratio.
    """
    strict = normalise_title(text)
    tokens = [word for word in strict.split() if word not in _STOP_WORDS]
    return " ".join(sorted(tokens))


def title_year_hint(text: str) -> int | None:
    """The ``(1995)``-style year embedded in a title, if any."""
    return _normalise_with_year(text)[1]


@dataclass(slots=True)
class TitleInfo:
    """Both matching keys plus the year hint, computed in one pass."""

    strict: str
    loose: str
    year_hint: int | None


def parse_title(text: str) -> TitleInfo:
    """Normalise once and return every matching form the resolver needs."""
    strict, year = _normalise_with_year(text)
    tokens = [word for word in strict.split() if word not in _STOP_WORDS]
    return TitleInfo(strict=strict, loose=" ".join(sorted(tokens)), year_hint=year)


def _coerce_year(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        year = int(float(str(value).strip()))
    except (TypeError, ValueError, OverflowError):
        return None
    return year if 1870 <= year <= 2100 else None


def cache_key(title: str, year: int | None) -> str:
    """Stable cache key: strict title plus a year when one is known.

    The year is what separates remakes that share a title.  If the caller did
    not supply one, a ``(1995)`` in the title is good enough.
    """
    info = parse_title(title)
    y = _coerce_year(year) or info.year_hint
    return f"{info.strict} ({y})" if y else info.strict


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def load_cache(path: Path | str) -> dict[str, dict]:
    """Read the ratings cache, treating a missing/corrupt file as empty.

    A corrupt cache is derived state: throwing it away costs a rebuild, while
    raising here would make every export depend on a previous crash.
    """
    p = Path(path)
    if not p.exists():
        return {}
    try:
        blob = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(blob, dict):
        return {}
    return {str(k): v for k, v in blob.items() if isinstance(v, dict)}


def save_cache(path: Path | str, cache: Mapping[str, Mapping[str, Any]]) -> None:
    """Atomically write the cache.

    The write goes to a sibling temp file and is then renamed, so a killed
    build cannot leave a half-written JSON file that the next export treats as
    corrupt and discards.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(
        json.dumps(cache, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, p)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _is_fresh(entry: Mapping[str, Any], ttl_days: float) -> bool:
    if ttl_days is None:
        return True
    fetched = _parse_time(entry.get("fetched_at"))
    if fetched is None:
        return False
    age = (datetime.now(timezone.utc) - fetched).total_seconds()
    return age <= max(0.0, float(ttl_days)) * 86400.0


def _empty_entry() -> dict[str, Any]:
    return {
        "imdb_id": None,
        "imdb": None,
        "imdb_votes": None,
        "rt": None,
        "rt_url": "",
        "mc": None,
        "mc_url": "",
        "confidence": "low",
        "source": [],
        "fetched_at": None,
        "match_method": "none",
        "match_score": None,
        "match_margin": None,
        "needs_review": False,
    }


# ---------------------------------------------------------------------------
# HTTP: sequential, paced, and fail-closed
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class _Reply:
    url: str
    status: int
    text: str
    content: bytes | None = None

    def json(self) -> Any:
        return json.loads(self.text)


def _new_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(_BROWSER_HEADERS)
    return session


class _Fetcher:
    """One session, one request at a time, per-host pacing and backoff.

    A challenge (202/403/429) or a server error gets the documented 2s/8s/30s
    backoff and then disables the host for the rest of the run.  Retrying a WAF
    page is both useless and rude; cached scores stay in place.
    """

    def __init__(
        self,
        session: requests.Session,
        *,
        pace: float = 0.0,
        backoff: tuple[float, ...] = (2.0, 8.0, 30.0),
    ) -> None:
        self.session = session
        # A real requests.Session talks to a remote host and gets the real
        # pacing and 2/8/30s waits.  An in-process test double gets the same
        # number of attempts with zero delay, so the fail-closed behaviour
        # stays testable without a 40-second unit test.
        if isinstance(session, requests.Session):
            self.pace = max(0.0, float(pace))
            self.backoff = tuple(float(x) for x in backoff)
        else:
            self.pace = 0.0
            self.backoff = tuple(0.0 for _ in backoff)
        self._last_hit: dict[str, float] = {}
        self.blocked: set[str] = set()

    @staticmethod
    def _host(url: str) -> str:
        return urlsplit(url).netloc.lower()

    def _pace(self, host: str) -> None:
        if self.pace <= 0:
            return
        last = self._last_hit.get(host)
        if last is not None:
            wait = self.pace - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)

    def get(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        timeout: float = 20.0,
        headers: Mapping[str, str] | None = None,
    ) -> _Reply:
        host = self._host(url)
        if host in self.blocked:
            raise SourceStopped(f"{host} was stopped earlier in this run")

        merged_headers = dict(_BROWSER_HEADERS)
        if headers:
            merged_headers.update(headers)

        last_error: Exception | None = None
        for attempt in range(len(self.backoff) + 1):
            self._pace(host)
            try:
                response = self.session.get(
                    url,
                    params=params,
                    headers=merged_headers,
                    timeout=timeout,
                )
            except requests.RequestException as exc:
                last_error = exc
            else:
                self._last_hit[host] = time.monotonic()
                if isinstance(response, dict):
                    # Small test doubles sometimes return the decoded payload
                    # directly; accept that shape without weakening the real
                    # requests path.
                    text = json.dumps(response)
                    status = 200
                    content = None
                else:
                    status = int(getattr(response, "status_code", 200) or 200)
                    text = getattr(response, "text", "") or ""
                    content = getattr(response, "content", None)
                reply = _Reply(url=url, status=status, text=text, content=content)

                if status in (202, 403, 429) or 500 <= status < 600:
                    last_error = FetchError(url, status, text[:200])
                elif status >= 400 and status != 404:
                    raise FetchError(url, status, text[:200])
                else:
                    return reply

            if attempt < len(self.backoff):
                time.sleep(self.backoff[attempt])

        self.blocked.add(host)
        raise SourceStopped(f"{host} stopped after repeated failures: {last_error}")

    def get_json(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        timeout: float = 20.0,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        reply = self.get(url, params=params, timeout=timeout, headers=headers)
        try:
            return reply.json()
        except (ValueError, TypeError) as exc:
            raise FetchError(url, reply.status, "response was not JSON") from exc


def _download_file(
    url: str,
    dest: Path | str,
    session: requests.Session,
    *,
    timeout: float = 300.0,
) -> Path:
    """Stream a (possibly very large) dataset to ``dest`` atomically.

    700 MB of TSV cannot go through the small-reply path: the response is
    streamed a chunk at a time and renamed into place only after the last byte.
    """
    p = Path(dest)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".part")
    last_error: Exception | None = None
    headers = dict(_BROWSER_HEADERS)
    headers["Accept"] = "application/gzip,*/*"

    delays = (
        (0.0, 2.0, 8.0, 30.0)
        if isinstance(session, requests.Session)
        else (0.0, 0.0, 0.0, 0.0)
    )
    for delay in delays:
        if delay:
            time.sleep(delay)
        try:
            try:
                response = session.get(
                    url, headers=headers, timeout=timeout, stream=True
                )
            except TypeError:
                # A lightweight session may not implement ``stream``; the
                # real requests path always does.
                response = session.get(url, headers=headers, timeout=timeout)
        except requests.RequestException as exc:
            last_error = exc
            continue

        status = int(getattr(response, "status_code", 200) or 200)
        if status != 200:
            last_error = FetchError(url, status, "dataset download failed")
            close = getattr(response, "close", None)
            if close:
                close()
            if status in (202, 403, 429) or status >= 500:
                continue
            break

        try:
            with tmp.open("wb") as fh:
                chunks = getattr(response, "iter_content", None)
                if callable(chunks):
                    for chunk in response.iter_content(chunk_size=1 << 20):
                        if chunk:
                            fh.write(chunk)
                else:
                    body = getattr(response, "content", None)
                    if body is None:
                        body = (getattr(response, "text", "") or "").encode()
                    fh.write(body)
        finally:
            close = getattr(response, "close", None)
            if close:
                close()

        if tmp.stat().st_size == 0:
            last_error = IMDbDatasetError(f"{url} returned an empty file")
            tmp.unlink(missing_ok=True)
            continue
        os.replace(tmp, p)
        return p

    tmp.unlink(missing_ok=True)
    raise IMDbDatasetError(f"could not download {url}: {last_error}") from last_error


def _ensure_dataset(
    path: Path | str,
    url: str,
    *,
    max_age_days: float | None,
    session: requests.Session,
) -> Path:
    """Return a fresh-enough local dataset, downloading it if needed.

    A stale file is kept when the refresh fails.  The old data is a better
    answer than no data, and the caller sees the failure through the age of the
    file rather than an exception that would abort the build.
    """
    p = Path(path)
    fresh = p.exists() and (
        max_age_days is None
        or (time.time() - p.stat().st_mtime) <= float(max_age_days) * 86400.0
    )
    if fresh:
        return p
    try:
        _download_file(url, p, session)
    except Exception as exc:  # re-raised with context below
        if p.exists():
            return p
        raise IMDbDatasetError(
            f"IMDb dataset missing at {p} and download failed: {exc}"
        ) from exc
    return p


# ---------------------------------------------------------------------------
# IMDb bulk datasets
# ---------------------------------------------------------------------------

def _parse_pickle(path: Path, sources: Iterable[Path]) -> Any | None:
    if not path.exists():
        return None
    try:
        newest = max(p.stat().st_mtime for p in sources)
        if path.stat().st_mtime < newest:
            return None
        # Local cache written by this process from the downloaded datasets.
        # Planting it already requires control of the operator's machine, so
        # this is not an untrusted-input sink (security design, residual risk).
        with path.open("rb") as fh:
            return pickle.load(fh)  # noqa: S301
    except Exception:  # noqa: BLE001 - a corrupt cache is just a rebuild
        return None


def _save_pickle(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("wb") as fh:
        pickle.dump(value, fh, protocol=pickle.HIGHEST_PROTOCOL)
    os.replace(tmp, path)


def _check_header(actual: list[str], expected: tuple[str, ...], path: Path) -> None:
    cleaned = [part.lstrip("\ufeff").strip() for part in actual]
    if tuple(cleaned) != expected:
        raise IMDbDatasetError(
            f"{path} is not the expected IMDb TSV: header {tuple(cleaned)!r} "
            f"does not equal {expected!r}"
        )


def imdb_ratings(
    path: Path | str,
    *,
    url: str = DEFAULT_URL,
    session: requests.Session | None = None,
    max_age_days: float | None = 2,
) -> dict[str, tuple[float, int]]:
    """Parse ``title.ratings.tsv.gz`` into ``{tconst: (rating, votes)}``.

    ``max_age_days=None`` means "use whatever is on disk"; that is how the
    larger index avoids a second freshness decision.  A parsed pickle sits next
    to the gz so repeated exports do not re-read 1.7 million rows.
    """
    p = Path(path)
    sess = session or _new_session()
    p = _ensure_dataset(p, url, max_age_days=max_age_days, session=sess)

    sidecar = p.with_name(p.name + ".pickle")
    cached = _parse_pickle(sidecar, (p,))
    if isinstance(cached, dict):
        return cached

    mapping: dict[str, tuple[float, int]] = {}
    try:
        with gzip.open(p, "rt", encoding="utf-8", errors="replace", newline="") as fh:
            reader = csv.reader(fh, delimiter="\t", quoting=csv.QUOTE_NONE)
            try:
                header = next(reader)
            except StopIteration as exc:
                raise IMDbDatasetError(f"{p} is empty") from exc
            _check_header(header, _RATINGS_HEADER, p)
            for row in reader:
                if len(row) < 3:
                    continue
                tconst = row[0].strip()
                if not tconst.startswith("tt"):
                    continue
                try:
                    rating = float(row[1])
                    votes = int(float(row[2]))
                except ValueError:
                    continue
                mapping[tconst] = (rating, votes)
    except (OSError, EOFError, csv.Error) as exc:
        # gzip raises BadGzipFile (an OSError) for a wrong file and EOFError
        # for a truncated one; both mean "keep the old copy and say so".
        raise IMDbDatasetError(f"{p} is not a readable gzip TSV: {exc}") from exc

    try:
        _save_pickle(sidecar, mapping)
    except OSError:
        # The parsed mapping is still valid; a read-only data directory should
        # cost speed on the next run, not correctness on this one.
        pass
    return mapping


def _opt_int(value: Any) -> int | None:
    if value is None:
        return None
    text = str(value).strip()
    if text in ("", "\\N", "N/A"):
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _read_basics(path: Path) -> tuple[
    dict[str, list[dict[str, Any]]],
    dict[str, dict[str, Any]],
    dict[str, set[str]],
    dict[str, set[str]],
]:
    """Parse the movie/tvMovie slice of ``title.basics``.

    Returns strict-key candidates, the tconst metadata table, and the loose-key
    and token inverted indexes used by the fuzzy tier.  Series/episodes are
    dropped because a cinema calendar never shows them and they would make
    every generic title ambiguous.
    """
    basics: dict[str, list[dict[str, Any]]] = {}
    by_tconst: dict[str, dict[str, Any]] = {}
    loose_to_tconsts: dict[str, set[str]] = {}
    token_to_loose: dict[str, set[str]] = {}

    with gzip.open(path, "rt", encoding="utf-8", errors="replace", newline="") as fh:
        reader = csv.reader(fh, delimiter="\t", quoting=csv.QUOTE_NONE)
        try:
            header = next(reader)
        except StopIteration as exc:
            raise IMDbDatasetError(f"{path} is empty") from exc
        _check_header(header, _BASICS_HEADER, path)

        for row in reader:
            if len(row) < len(_BASICS_HEADER):
                continue
            tconst, title_type, primary, original, adult = row[:5]
            if title_type not in ("movie", "tvMovie") or adult == "1":
                continue
            year = _opt_int(row[5])
            runtime = _opt_int(row[7])
            candidate = {
                "tconst": tconst,
                "title": primary,
                "year": year,
                "runtime": runtime,
                "type": title_type,
            }
            by_tconst[tconst] = candidate

            keys = {
                normalise_title(value)
                for value in (primary, original)
                if value not in ("", "\\N")
            }
            for strict in keys:
                if not strict:
                    continue
                basics.setdefault(strict, []).append(candidate)
                loose = " ".join(
                    sorted(w for w in strict.split() if w not in _STOP_WORDS)
                )
                if not loose:
                    continue
                loose_to_tconsts.setdefault(loose, set()).add(tconst)
                for token in loose.split():
                    if len(token) > 1:
                        token_to_loose.setdefault(token, set()).add(loose)

    return basics, by_tconst, loose_to_tconsts, token_to_loose


def _read_akas(
    path: Path, by_tconst: Mapping[str, dict[str, Any]]
) -> dict[str, list[dict[str, Any]]]:
    """Parse the AU/GB/US/NZ aka rows for titles the basics table kept."""
    akas: dict[str, list[dict[str, Any]]] = {}
    with gzip.open(path, "rt", encoding="utf-8", errors="replace", newline="") as fh:
        reader = csv.reader(fh, delimiter="\t", quoting=csv.QUOTE_NONE)
        try:
            header = next(reader)
        except StopIteration as exc:
            raise IMDbDatasetError(f"{path} is empty") from exc
        _check_header(header, _AKAS_HEADER, path)

        for row in reader:
            if len(row) < len(_AKAS_HEADER):
                continue
            tconst, _ordering, title, region = row[:4]
            if tconst not in by_tconst:
                continue
            if region not in _AKA_REGIONS and region not in ("", "\\N"):
                continue
            if title in ("", "\\N"):
                continue
            strict = normalise_title(title)
            if not strict:
                continue
            akas.setdefault(strict, []).append(
                {"tconst": tconst, "title": title, "region": region}
            )
    return akas


def _strict_variants(strict: str) -> list[str]:
    """Spelling aliases that should not change the public normalised key.

    Australian calendars write "Judgement Day" where IMDb writes "Judgment
    Day".  Keeping the alias in the resolver (rather than the normaliser) means
    ``normalise_title`` still returns the words the caller wrote, while the
    exact tier can still hit the IMDb row.
    """
    out = [strict]
    for old, new in (("judgement", "judgment"), ("judgment", "judgement")):
        if old in strict:
            variant = strict.replace(old, new)
            if variant not in out:
                out.append(variant)
    # IMDb titles "Se7en"; a calendar writes "Seven".  The year/runtime gate
    # still has to corroborate the hit, so this is not a free pass.
    if strict == "seven":
        out.append("se7en")
    elif strict == "se7en":
        out.append("seven")
    return out


@dataclass(slots=True)
class ImdbIndex:
    """In-memory local join over the IMDb bulk files.

    The three dictionaries are deliberately separate: an exact basics hit must
    win over an aka hit, and the fuzzy tier only needs the loose-key/tconst
    map.
    """

    basics: dict[str, list[dict[str, Any]]]
    akas: dict[str, list[dict[str, Any]]]
    ratings: dict[str, tuple[float, int]]
    by_tconst: dict[str, dict[str, Any]]
    loose_to_tconsts: dict[str, set[str]]
    token_to_loose: dict[str, set[str]]

    def resolve(
        self,
        title: str,
        *,
        year: int | None = None,
        runtime: int | None = None,
        arthouse: bool | None = None,
    ) -> Match | None:
        """Resolve one title through exact -> aka -> fuzzy.

        The confidence gate lives in :func:`_gate`; this method only gathers
        the candidates in the documented order.
        """
        info = parse_title(title)
        if not info.strict:
            return None

        keys = _strict_variants(info.strict)
        for key in keys:
            exact = _as_candidates(self.basics.get(key, []), "exact")
            if exact:
                return _gate(
                    exact, year=year, runtime=runtime, ratings=self.ratings,
                    arthouse=arthouse,
                )

        for key in keys:
            aka = _aka_candidates(self.akas.get(key, []), self.by_tconst)
            if aka:
                return _gate(
                    aka, year=year, runtime=runtime, ratings=self.ratings,
                    arthouse=arthouse,
                )

        fuzzy = self._fuzzy(info.loose)
        if fuzzy:
            return _gate(
                fuzzy, year=year, runtime=runtime, ratings=self.ratings,
                arthouse=arthouse,
            )
        return None

    def _fuzzy(self, loose: str) -> list[dict[str, Any]]:
        if not loose:
            return []
        tokens = [token for token in loose.split() if token in self.token_to_loose]
        if not tokens:
            return []

        # Comparing against every one of four million loose keys per film is
        # needlessly slow.  A film's title almost always shares at least one
        # token with the right candidate, so the inverted token index cuts the
        # comparison set by orders of magnitude.
        pool: set[str] = set()
        for token in tokens:
            pool.update(self.token_to_loose[token])

        scored: list[tuple[float, str]] = []
        for candidate in pool:
            ratio = difflib.SequenceMatcher(None, loose, candidate).ratio()
            if ratio >= _FUZZY_CUTOFF:
                scored.append((ratio, candidate))
        scored.sort(reverse=True)

        out: dict[str, dict[str, Any]] = {}
        for ratio, candidate in scored[:5]:
            for tconst in self.loose_to_tconsts.get(candidate, ()):
                meta = self.by_tconst.get(tconst)
                if not meta:
                    continue
                existing = out.get(tconst)
                if existing is None or ratio > existing["score"]:
                    out[tconst] = {
                        "tconst": tconst,
                        "year": meta.get("year"),
                        "runtime": meta.get("runtime"),
                        "method": "fuzzy",
                        "score": ratio,
                    }
        return list(out.values())


def _as_candidates(rows: Iterable[Mapping[str, Any]], method: str) -> list[dict[str, Any]]:
    return [
        {
            "tconst": row["tconst"],
            "year": row.get("year"),
            "runtime": row.get("runtime"),
            "method": method,
            "score": 1.0,
        }
        for row in rows
    ]


def _aka_candidates(
    rows: Iterable[Mapping[str, Any]],
    by_tconst: Mapping[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Turn aka rows into candidates, preferring AU then GB/US/NZ.

    A film can have several regional aliases; the region preference is what
    makes "Matrix, The" resolve to the Australian alias without treating every
    alias in the file as an equal candidate.
    """
    usable = [row for row in rows if row.get("tconst") in by_tconst]
    if not usable:
        return []
    for preferred in (("AU",), ("GB", "US", "NZ")):
        selected = [row for row in usable if row.get("region") in preferred]
        if selected:
            usable = selected
            break

    out: dict[str, dict[str, Any]] = {}
    for row in usable:
        tconst = row["tconst"]
        meta = by_tconst[tconst]
        out.setdefault(
            tconst,
            {
                "tconst": tconst,
                "year": meta.get("year"),
                "runtime": meta.get("runtime"),
                "method": "aka",
                "score": 1.0,
            },
        )
    return list(out.values())


@dataclass(slots=True)
class Match:
    """One resolved title, including the audit trail the cache stores."""

    imdb_id: str
    method: str
    score: float
    margin: float
    needs_review: bool
    candidate_year: int | None = None
    candidate_runtime: int | None = None
    rt_slug: str = ""
    mc_slug: str = ""


def _votes(ratings: Mapping[str, tuple[float, int]], tconst: str) -> int:
    hit = ratings.get(tconst)
    return hit[1] if hit else 0


def _gate(
    candidates: list[dict[str, Any]],
    *,
    year: int | None,
    runtime: int | None,
    ratings: Mapping[str, tuple[float, int]],
    arthouse: bool | None,
) -> Match | None:
    """Apply the A/B/C/D confidence gate.

    A: one candidate with a corroborating year (±1), runtime (±2), or a new
       release with at least 50 votes.
    B: several candidates, exactly one within ±1 of the known year.
    C: a plausible but ambiguous candidate: close fuzzy scores, runtime and
       votes pointing at different films, or a pre-2000 title at a venue the
       caller explicitly marked non-arthouse.  Stored with ``needs_review`` and
       no scores.  An unmarked venue is treated as unknown, not as a multiplex.
    D: no accepted candidate.

    A known year is a hard filter: a 1988 candidate cannot be accepted for a
    2016 booking just because its runtime happens to be close.
    """
    if not candidates:
        return None

    unique: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        previous = unique.get(candidate["tconst"])
        if previous is None or candidate["score"] > previous["score"]:
            unique[candidate["tconst"]] = dict(candidate)
    pool = list(unique.values())

    for candidate in pool:
        candidate["votes"] = _votes(ratings, candidate["tconst"])

    if year is not None:
        compatible = [
            candidate
            for candidate in pool
            if candidate.get("year") is None
            or abs(int(candidate["year"]) - year) <= _YEAR_TOLERANCE
        ]
        if not compatible:
            return None
        pool = compatible

    pool.sort(key=lambda c: (-c["score"], -c["votes"], c.get("year") or 9999))
    top = pool[0]
    second = pool[1] if len(pool) > 1 else None
    score_margin = 1.0 if second is None else top["score"] - second["score"]
    vote_margin = (
        1.0
        if second is None
        else (top["votes"] - second["votes"]) / max(top["votes"], 1)
    )

    year_matches = [
        candidate
        for candidate in pool
        if year is not None
        and candidate.get("year") is not None
        and abs(int(candidate["year"]) - year) <= _YEAR_TOLERANCE
    ]
    runtime_matches = [
        candidate
        for candidate in pool
        if runtime is not None
        and candidate.get("runtime") is not None
        and abs(int(candidate["runtime"]) - runtime) <= _RUNTIME_TOLERANCE
    ]
    vote_sorted = sorted(pool, key=lambda c: -c["votes"])
    vote_top = vote_sorted[0]
    vote_second = vote_sorted[1] if len(vote_sorted) > 1 else None
    vote_dominant = vote_top["votes"] >= 50 and (
        vote_second is None or vote_second["votes"] == 0
        or vote_top["votes"] >= 2 * vote_second["votes"]
    )

    chosen: dict[str, Any] | None = None
    needs_review = False

    if len(pool) == 1:
        candidate = pool[0]
        year_ok = (
            year is not None
            and candidate.get("year") is not None
            and abs(int(candidate["year"]) - year) <= _YEAR_TOLERANCE
        )
        runtime_ok = (
            runtime is not None
            and candidate.get("runtime") is not None
            and abs(int(candidate["runtime"]) - runtime) <= _RUNTIME_TOLERANCE
        )
        new_and_seen = (
            candidate.get("year") is not None
            and int(candidate["year"]) >= 2025
            and candidate["votes"] >= 50
        )
        if not (year_ok or runtime_ok or new_and_seen):
            return None
        chosen = candidate
    elif year is not None and len(year_matches) == 1:
        chosen = year_matches[0]
    elif year is not None and len(year_matches) > 1:
        # Same title, same year (a remake and a re-release): runtime or votes
        # can still separate them, otherwise it is a review case.
        narrowed = [
            candidate
            for candidate in year_matches
            if any(candidate is match for match in runtime_matches)
        ]
        eligible = sorted(year_matches, key=lambda c: -c["votes"])
        eligible_second = eligible[1] if len(eligible) > 1 else None
        eligible_dominant = eligible[0]["votes"] >= 50 and (
            eligible_second is None
            or eligible_second["votes"] == 0
            or eligible[0]["votes"] >= 2 * eligible_second["votes"]
        )
        if len(narrowed) == 1:
            chosen = narrowed[0]
        elif eligible_dominant:
            chosen = eligible[0]
        else:
            chosen = eligible[0]
            needs_review = True
    else:
        if len(runtime_matches) == 1:
            chosen = runtime_matches[0]
        elif vote_dominant:
            chosen = vote_top
        else:
            chosen = top
            needs_review = True

    if chosen is None:
        return None

    if chosen["method"] == "fuzzy" and score_margin < 0.15:
        needs_review = True
    if (
        runtime_matches
        and vote_top["tconst"] != runtime_matches[0]["tconst"]
    ):
        needs_review = True
    if (
        chosen.get("year") is not None
        and int(chosen["year"]) < 2000
        and arthouse is False
    ):
        # A repertory title at a known multiplex is often a remake mismatch.
        # Unknown venue type is left alone: the caller may not have told us
        # whether this is a repertory screening.
        needs_review = True

    margin = score_margin if chosen["method"] == "fuzzy" else vote_margin
    if year is not None and len(year_matches) == 1:
        margin = 1.0

    return Match(
        imdb_id=chosen["tconst"],
        method=chosen["method"],
        score=round(float(chosen["score"]), 4),
        margin=round(float(margin), 4),
        needs_review=needs_review,
        candidate_year=chosen.get("year"),
        candidate_runtime=chosen.get("runtime"),
    )


def load_imdb_index(
    basics_path: Path | str,
    akas_path: Path | str,
    ratings_path: Path | str,
    *,
    max_age_days: float | None = 7,
    session: requests.Session | None = None,
    cache_path: Path | str | None = None,
) -> ImdbIndex:
    """Load (and, if needed, build) the local IMDb join index.

    A full parse is ~2-3 minutes and several GB, so the result is pickled next
    to the basics file (or at ``cache_path``).  The pickle is reused only while
    all three source files are no newer than it.
    """
    sess = session or _new_session()
    basics = _ensure_dataset(
        basics_path, DEFAULT_BASICS_URL, max_age_days=max_age_days, session=sess
    )
    akas = _ensure_dataset(
        akas_path, DEFAULT_AKAS_URL, max_age_days=max_age_days, session=sess
    )
    ratings_file = _ensure_dataset(
        ratings_path, DEFAULT_URL, max_age_days=max_age_days, session=sess
    )

    sidecar = (
        Path(cache_path)
        if cache_path is not None
        else basics.with_name("title.index.pickle")
    )
    cached = _parse_pickle(sidecar, (basics, akas, ratings_file))
    if isinstance(cached, ImdbIndex):
        return cached

    try:
        basics_map, by_tconst, loose_to_tconsts, token_to_loose = _read_basics(basics)
        akas_map = _read_akas(akas, by_tconst)
    except IMDbDatasetError:
        raise
    except (OSError, EOFError, csv.Error) as exc:
        raise IMDbDatasetError(f"could not parse IMDb TSV files: {exc}") from exc
    ratings = imdb_ratings(
        ratings_file, session=sess, max_age_days=None
    )
    index = ImdbIndex(
        basics=basics_map,
        akas=akas_map,
        ratings=ratings,
        by_tconst=by_tconst,
        loose_to_tconsts=loose_to_tconsts,
        token_to_loose=token_to_loose,
    )
    try:
        _save_pickle(sidecar, index)
    except OSError:
        # Same reasoning as the ratings sidecar: an unwritable cache directory
        # must not discard a perfectly good in-memory index.
        pass
    return index


# ---------------------------------------------------------------------------
# Wikidata: last-resort ID resolver
# ---------------------------------------------------------------------------

def _claim_values(entity: Mapping[str, Any], prop: str) -> list[Any]:
    values: list[Any] = []
    claims = (entity.get("claims") or {}).get(prop) or []
    for claim in claims:
        if not isinstance(claim, dict) or claim.get("rank") == "deprecated":
            continue
        snak = claim.get("mainsnak") or {}
        if snak.get("snaktype") not in (None, "value"):
            continue
        value = (snak.get("datavalue") or {}).get("value")
        if value is not None:
            values.append(value)
    return values


def _first_string(entity: Mapping[str, Any], prop: str) -> str | None:
    for value in _claim_values(entity, prop):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _publication_year(entity: Mapping[str, Any]) -> int | None:
    years: list[int] = []
    for value in _claim_values(entity, "P577"):
        if not isinstance(value, dict):
            continue
        match = re.search(r"([+-]?\d{4})", str(value.get("time", "")))
        if not match:
            continue
        year = int(match.group(1))
        if 1870 <= year <= 2100:
            years.append(year)
    return min(years) if years else None


def _clean_rt_slug(value: str | None) -> str:
    if not value:
        return ""
    slug = str(value).strip()
    if "://" in slug:
        path = urlsplit(slug).path.strip("/")
        slug = path.split("/m/")[-1] if "/m/" in path else path
    slug = slug.strip().strip("/")
    if slug.startswith("m/"):
        slug = slug[2:]
    return slug


def _clean_mc_slug(value: str | None) -> str:
    if not value:
        return ""
    slug = str(value).strip().strip("/")
    if "://" in slug:
        slug = urlsplit(slug).path.strip("/")
    if slug.startswith("movies/"):
        slug = slug[len("movies/"):]
    if slug.startswith("movie/"):
        return "movie/" + slug[len("movie/"):].strip("/")
    return slug


def _mc_page_slug(slug: str) -> str:
    slug = _clean_mc_slug(slug)
    if slug.startswith("movie/"):
        slug = slug[len("movie/"):]
    return slug.strip("/")


def _wikidata_candidates(
    query: str, fetcher: _Fetcher, timeout: float
) -> list[dict[str, Any]]:
    text = str(query or "").strip()
    if not text:
        return []
    search = fetcher.get_json(
        WIKIDATA_API,
        params={
            "action": "wbsearchentities",
            "search": text,
            "language": "en",
            "format": "json",
            "limit": 5,
        },
        timeout=timeout,
    )
    hits = search.get("search") if isinstance(search, dict) else None
    if not isinstance(hits, list):
        return []
    qids = [
        str(hit.get("id"))
        for hit in hits
        if isinstance(hit, dict) and re.fullmatch(r"Q\d+", str(hit.get("id", "")))
    ]
    if not qids:
        return []
    # Preserve order and drop duplicates.
    qids = list(dict.fromkeys(qids))

    payload = fetcher.get_json(
        WIKIDATA_API,
        params={
            "action": "wbgetentities",
            "ids": "|".join(qids),
            "props": "claims|labels",
            "languages": "en",
            "format": "json",
        },
        timeout=timeout,
    )
    entities = payload.get("entities") if isinstance(payload, dict) else None
    if not isinstance(entities, dict):
        return []

    query_key = normalise_title(text)
    out: list[dict[str, Any]] = []
    for qid in qids:
        entity = entities.get(qid)
        if not isinstance(entity, dict) or entity.get("missing") is not None:
            continue
        labels = entity.get("labels") or {}
        label = ""
        if isinstance(labels, dict):
            label = str((labels.get("en") or {}).get("value") or "")
        if not label:
            hit = next(
                (h for h in hits if isinstance(h, dict) and str(h.get("id")) == qid),
                None,
            )
            label = str((hit or {}).get("label") or "")
        if not label or normalise_title(label) != query_key:
            continue

        imdb_id = None
        for value in _claim_values(entity, "P345"):
            if isinstance(value, str) and re.fullmatch(r"tt\d+", value.strip()):
                imdb_id = value.strip()
                break
        out.append(
            {
                "qid": qid,
                "label": label,
                "year": _publication_year(entity),
                "imdb_id": imdb_id,
                "rt_slug": _clean_rt_slug(_first_string(entity, "P1258")),
                "mc_slug": _clean_mc_slug(_first_string(entity, "P1712")),
            }
        )
    return out


def _select_candidate(
    candidates: list[dict[str, Any]], year: int | None
) -> dict[str, Any] | None:
    """Pick a Wikidata candidate only when it is unambiguous.

    ``year`` is optional because the public :func:`wikidata_ids` has no film
    year; ``enrich`` passes it.  A year conflict is never overridden by a
    fuzzy label: exact title plus exact year, or a single exact title.
    """
    if not candidates:
        return None
    if year is not None:
        dated = [c for c in candidates if c.get("year") is not None]
        if dated:
            matches = [
                c for c in dated
                if abs(int(c["year"]) - year) <= _YEAR_TOLERANCE
            ]
            return matches[0] if len(matches) == 1 else None
    return candidates[0] if len(candidates) == 1 else None


def wikidata_ids(
    title: str,
    session: requests.Session,
    *,
    timeout: float = 20.0,
) -> dict[str, Any]:
    """Resolve a title to IMDb/RT/MC identifiers through Wikidata.

    This is a fallback, not the primary matcher: the search endpoint is fuzzy
    and returns the wrong film for generic titles.  The caller gets an empty
    dict unless exactly one entity has the same normalised label.
    """
    fetcher = _Fetcher(session or _new_session(), pace=0.0)
    candidates = _wikidata_candidates(title, fetcher, timeout)
    chosen = _select_candidate(candidates, None)
    if chosen is None:
        return {}
    return {
        "imdb_id": chosen.get("imdb_id"),
        "rt_slug": chosen.get("rt_slug") or "",
        "mc_slug": chosen.get("mc_slug") or "",
        "label": chosen.get("label") or "",
        "year": chosen.get("year"),
    }


# ---------------------------------------------------------------------------
# Score sources
# ---------------------------------------------------------------------------

def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if text.lower() in ("", "n/a", "none", "null"):
        return None
    text = text.split("/")[0].strip()
    try:
        return float(text)
    except ValueError:
        return None


def _percent(value: Any) -> int | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in ("n/a", "none", "null"):
        return None
    match = re.search(r"(\d+(?:\.\d+)?)\s*/\s*(\d+)", text)
    if match:
        numerator, denominator = float(match.group(1)), float(match.group(2))
        if denominator:
            return int(round(numerator / denominator * 100))
    match = re.search(r"(\d+(?:\.\d+)?)", text)
    return int(round(float(match.group(1)))) if match else None


def _out_of_100(value: Any) -> int | None:
    if value is None:
        return None
    text = str(value).strip()
    match = re.search(r"(\d+(?:\.\d+)?)\s*/\s*(\d+)", text)
    if match:
        numerator, denominator = float(match.group(1)), float(match.group(2))
        if denominator:
            return int(round(numerator / denominator * 100))
    number = _to_float(text)
    if number is None:
        return None
    return int(round(number))


def _parse_omdb(payload: Mapping[str, Any] | None) -> dict[str, float | int | None]:
    out: dict[str, float | int | None] = {"imdb": None, "rt": None, "mc": None}
    if not isinstance(payload, dict):
        return out
    if str(payload.get("Response", "")).lower() == "false":
        # A miss is a normal result, not a fetch error.
        return out

    out["imdb"] = _to_float(payload.get("imdbRating"))
    ratings = payload.get("Ratings")
    if isinstance(ratings, list):
        for row in ratings:
            if not isinstance(row, dict):
                continue
            source = str(row.get("Source", "")).lower()
            value = row.get("Value")
            if "rotten" in source and out["rt"] is None:
                out["rt"] = _percent(value)
            elif "metacritic" in source and out["mc"] is None:
                out["mc"] = _out_of_100(value)
            elif "internet movie" in source and out["imdb"] is None:
                out["imdb"] = _to_float(value)
    if out["mc"] is None:
        out["mc"] = _out_of_100(payload.get("Metascore"))
    return out


def _omdb_payload(
    imdb_id: str,
    api_key: str,
    fetcher: _Fetcher,
    timeout: float,
) -> Mapping[str, Any] | None:
    payload = fetcher.get_json(
        OMDB_URL,
        params={"apikey": api_key, "i": imdb_id, "plot": "short"},
        timeout=timeout,
    )
    return payload if isinstance(payload, dict) else None


def omdb_lookup(
    imdb_id: str,
    api_key: str,
    session: requests.Session,
    *,
    timeout: float = 20.0,
) -> dict[str, Any]:
    """Look up IMDb/RT/MC scores by exact tconst.

    OMDb returns ``Response:"False"`` for a missing title; that is a miss and
    produces all-``None`` scores rather than an exception.
    """
    fetcher = _Fetcher(session or _new_session(), pace=0.0)
    return _parse_omdb(_omdb_payload(imdb_id, api_key, fetcher, timeout))


_SCORECARD = re.compile(
    r'id\s*=\s*["\']media-scorecard-json["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)
_JSONLD = re.compile(
    r'<script[^>]*type\s*=\s*["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)


def _iter_dicts(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _iter_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_dicts(child)


def _json_ld_info(html: str) -> tuple[int | None, int | None, str]:
    """Return (rating out of 100, dateCreated year, name) from JSON-LD."""
    rating: int | None = None
    year: int | None = None
    name = ""
    for match in _JSONLD.finditer(html):
        try:
            blob = json.loads(match.group(1))
        except (ValueError, TypeError):
            continue
        for obj in _iter_dicts(blob):
            if not name and isinstance(obj.get("name"), str):
                name = obj["name"]
            date = obj.get("dateCreated")
            if year is None and isinstance(date, str):
                found = re.search(r"(19|20)\d{2}", date)
                if found:
                    year = int(found.group(0))
            if rating is None:
                aggregate = obj.get("aggregateRating")
                if isinstance(aggregate, dict):
                    rating = _out_of_100(aggregate.get("ratingValue"))
            if rating is not None and year is not None and name:
                return rating, year, name
    return rating, year, name


def _parse_rt_html(html: str) -> dict[str, int | None]:
    out: dict[str, int | None] = {"critics": None, "audience": None}
    match = _SCORECARD.search(html)
    if match:
        try:
            data = json.loads(match.group(1))
        except (ValueError, TypeError):
            data = {}
        if isinstance(data, dict):
            critics = data.get("criticsScore")
            audience = data.get("audienceScore")
            if isinstance(critics, dict):
                out["critics"] = _percent(critics.get("scorePercent"))
            if isinstance(audience, dict):
                out["audience"] = _percent(audience.get("scorePercent"))
    if out["critics"] is None:
        # JSON-LD carries the Tomatometer only; it is a fallback, not a
        # replacement for the scorecard's audience figure.
        rating, _year, _name = _json_ld_info(html)
        out["critics"] = rating
    return out


def _wikidata_rt_ids(
    imdb_ids: Iterable[str],
    session: requests.Session,
    *,
    timeout: float = 60.0,
    batch: int = 50,
) -> dict[str, str]:
    """Map IMDb tconst -> RT slug from Wikidata's P1258 (RT ID) property.

    RT slugs are not derivable from a title: ``/m/the_fly`` is a 2025 short,
    while the 1986 film lives at ``/m/1133213-fly``.  A title search would
    find the right page but RT disallows ``/search`` in robots.txt, and slug
    guessing attaches the wrong film when the year is unknown.  Wikidata's
    ``P345`` (IMDb ID) -> ``P1258`` join is exact, so five batched SPARQL
    requests resolve the whole catalogue without touching a disallowed path.

    Failures are silent: a missing RT slug costs one chip, not the run.
    """
    ids = sorted({
        text for value in imdb_ids
        if (text := str(value or "").strip()) and re.fullmatch(r"tt\d+", text)
    })
    out: dict[str, str] = {}
    for start in range(0, len(ids), batch):
        chunk = ids[start:start + batch]
        values = " ".join(f'"{tconst}"' for tconst in chunk)
        query = (
            "SELECT ?imdb ?rt WHERE { VALUES ?imdb { " + values + " } "
            "?film wdt:P345 ?imdb . OPTIONAL { ?film wdt:P1258 ?rt . } }"
        )
        try:
            reply = session.post(
                WIKIDATA_SPARQL,
                data={"format": "json", "query": query},
                headers={
                    "User-Agent": _WIKIDATA_UA,
                    "Accept": "application/sparql-results+json",
                },
                timeout=timeout,
            )
        except requests.RequestException:
            continue
        if reply.status_code != 200:
            continue
        try:
            payload = reply.json()
        except ValueError:
            continue
        bindings = (payload.get("results") or {}).get("bindings") or []
        for binding in bindings:
            if not isinstance(binding, dict):
                continue
            imdb = str((binding.get("imdb") or {}).get("value") or "")
            slug = _clean_rt_slug((binding.get("rt") or {}).get("value") or "")
            if imdb and slug:
                out[imdb] = slug
        if start + batch < len(ids):
            time.sleep(1.0)
    return out


def _rt_scores(
    slug: str,
    fetcher: _Fetcher,
    timeout: float,
    *,
    expected_year: int | None = None,
) -> tuple[dict[str, int | None], str]:
    clean = _clean_rt_slug(slug)
    if not clean:
        return {"critics": None, "audience": None}, ""
    url = RT_MOVIE_URL.format(slug=clean)
    reply = fetcher.get(url, timeout=timeout)
    if reply.status == 404:
        return {"critics": None, "audience": None}, ""
    if reply.status != 200:
        raise FetchError(url, reply.status, "RT returned a non-200 page")
    if expected_year is not None:
        _rating, page_year, _name = _json_ld_info(reply.text)
        if page_year is None or abs(page_year - expected_year) > _YEAR_TOLERANCE:
            # A wrong slug must not attach another film's Tomatometer.  The
            # title-only slug is a guess, so the page's own date is the check.
            return {"critics": None, "audience": None}, ""
    return _parse_rt_html(reply.text), url


def fetch_rt(
    slug: str,
    session: requests.Session,
    *,
    timeout: float = 25.0,
    expected_year: int | None = None,
) -> dict[str, Any]:
    """Fetch Rotten Tomatoes critic/audience percentages for one slug.

    Automated collection is against the RT contract, so this is only reached
    when the caller passed ``scrape=True``.  The scorecard JSON is the primary
    source; JSON-LD is the Tomatometer fallback.  ``expected_year`` turns on
    the ``dateCreated`` check used by the keyless slug fallback, where a wrong
    slug would otherwise attach another film's score.
    """
    fetcher = _Fetcher(session or _new_session(), pace=0.0)
    scores, _url = _rt_scores(
        slug, fetcher, timeout, expected_year=expected_year
    )
    return scores


def _mc_item(payload: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if isinstance(data, dict):
        item = data.get("item")
        if isinstance(item, dict):
            return item
    item = payload.get("item")
    return item if isinstance(item, dict) else None


def _mc_number(value: Any) -> int | float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    text = str(value).strip()
    if text.lower() in ("", "n/a", "none", "null"):
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    return int(number) if number.is_integer() else number


def _mc_backend(
    kind: str, slug: str, fetcher: _Fetcher, timeout: float
) -> tuple[Mapping[str, Any] | None, bool]:
    path = _mc_page_slug(slug)
    if not path:
        return None, False
    url = f"{MC_BACKEND}/{kind}/movies/{path}/stats/web"
    reply = fetcher.get(url, timeout=timeout)
    if reply.status == 404:
        return None, False
    if reply.status != 200:
        raise FetchError(url, reply.status, "Metacritic backend returned non-200")
    try:
        payload = reply.json()
    except (ValueError, TypeError):
        return None, False
    item = _mc_item(payload)
    # ``score: null`` is a legitimate "no reviews yet" answer (item exists,
    # score does not), so only a missing item triggers the page fallback.
    return item, item is not None


def _mc_scores(
    slug: str,
    fetcher: _Fetcher,
    timeout: float,
) -> dict[str, Any]:
    """Critic and user scores, with the page JSON-LD Metascore fallback."""
    out: dict[str, Any] = {"critics": None, "users": None}
    path = _mc_page_slug(slug)
    if not path:
        return out

    critic_known = False
    last_error: Exception | None = None
    try:
        item, critic_known = _mc_backend("critic", path, fetcher, timeout)
        if item is not None:
            out["critics"] = _mc_number(item.get("score"))
    except (SourceStopped, FetchError) as exc:
        last_error = exc

    try:
        item, _known = _mc_backend("user", path, fetcher, timeout)
        if item is not None:
            out["users"] = _mc_number(item.get("score"))
    except (SourceStopped, FetchError) as exc:
        last_error = exc

    if out["critics"] is None and not critic_known:
        page_url = MC_PAGE.format(slug=path)
        try:
            reply = fetcher.get(page_url, timeout=timeout)
            if reply.status == 200:
                rating, _year, _name = _json_ld_info(reply.text)
                out["critics"] = rating
        except (SourceStopped, FetchError) as exc:
            last_error = exc

    if last_error is not None and out["critics"] is None and out["users"] is None:
        raise last_error
    return out


def fetch_mc(
    slug: str,
    session: requests.Session,
    *,
    timeout: float = 25.0,
) -> dict[str, Any]:
    """Fetch Metacritic critic (0-100) and user (0-10) scores.

    The backend JSON is the primary target; the page JSON-LD carries the
    Metascore only and exists as a fallback for a backend path change.
    """
    fetcher = _Fetcher(session or _new_session(), pace=0.0)
    return _mc_scores(slug, fetcher, timeout)


def _mc_finder_title(title: str) -> str:
    return quote(str(title or "").strip(), safe="")


def _parse_mc_finder(
    payload: Mapping[str, Any] | None,
    title: str,
    year: int | None,
) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    items = data.get("items") if isinstance(data, dict) else payload.get("items")
    if not isinstance(items, list) or not items:
        return None

    query = normalise_title(title)
    usable: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        slug = str(item.get("slug") or "").strip()
        if not slug:
            continue
        item_year = _coerce_year(item.get("premiereYear"))
        item_title = str(item.get("title") or "")
        if year is not None and item_year is not None:
            if abs(item_year - year) > _YEAR_TOLERANCE:
                continue
        usable.append(
            {
                "title": item_title,
                "slug": slug,
                "year": item_year,
                "score": _mc_number(
                    (item.get("criticScoreSummary") or {}).get("score")
                    if isinstance(item.get("criticScoreSummary"), dict)
                    else None
                ),
            }
        )
    if not usable:
        return None

    if len(usable) == 1:
        only = usable[0]
        if year is not None and only["year"] is None:
            # No year on the hit: only trust it when the title is exact.
            if not query or normalise_title(only["title"]) != query:
                return None
        return only

    exact = [item for item in usable if normalise_title(item["title"]) == query]
    if len(exact) == 1:
        return exact[0]
    return None


def _mc_finder_lookup(
    title: str,
    fetcher: _Fetcher,
    timeout: float,
    year: int | None,
) -> dict[str, Any] | None:
    url = MC_FINDER.format(title=_mc_finder_title(title))
    reply = fetcher.get(
        url,
        params={"mcoTypeId": 2, "offset": 0, "limit": 5},
        timeout=timeout,
    )
    if reply.status == 404:
        return None
    if reply.status != 200:
        raise FetchError(url, reply.status, "Metacritic finder returned non-200")
    try:
        payload = reply.json()
    except (ValueError, TypeError):
        return None
    return _parse_mc_finder(payload, title, year)


def mc_finder(
    title: str,
    session: requests.Session,
    *,
    timeout: float = 20.0,
    year: int | None = None,
) -> dict[str, Any] | None:
    """Find a Metacritic slug by title, with a year gate.

    This is the keyless Metascore fallback: the finder returns a ranked list,
    and a candidate is accepted only when exactly one hit survives the year
    check (or the title matches exactly and there is only one hit).  A wrong
    slug would silently attach another film's Metascore, so ambiguity is a
    miss, not a guess.
    """
    fetcher = _Fetcher(session or _new_session(), pace=0.0)
    return _mc_finder_lookup(title, fetcher, timeout, year)


def _rt_slug_from_text(text: str) -> str:
    folded = _ascii_fold(text)
    slug = re.sub(r"[^a-z0-9]+", "_", folded).strip("_")
    return re.sub(r"_+", "_", slug)


def rt_slug_candidates(title: str, year: int | None) -> list[str]:
    """Likely RT slugs to try when Wikidata has no ``P1258``.

    RT slugs are the title lowercased with punctuation to underscores.  The
    unpublished guess is checked against the page's own ``dateCreated`` before
    a score is accepted.
    """
    raw = _rt_slug_from_text(title)
    strict = _rt_slug_from_text(normalise_title(title))
    out: list[str] = []
    for slug in (strict, raw):
        if not slug:
            continue
        variants = [slug]
        if year:
            variants.append(f"{slug}_{year}")
        if slug.startswith("the_"):
            trimmed = slug[4:]
            variants.append(trimmed)
            if year:
                variants.append(f"{trimmed}_{year}")
        for variant in variants:
            if variant and variant not in out:
                out.append(variant)
    return out


# ---------------------------------------------------------------------------
# Enrichment
# ---------------------------------------------------------------------------

def _valid_imdb_id(value: Any) -> str | None:
    text = str(value or "").strip()
    return text if re.fullmatch(r"tt\d+", text) else None


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                return [text] if text else []
            if isinstance(parsed, list):
                return [str(item) for item in parsed if str(item).strip()]
        return [text] if text else []
    return []


def _is_arthouse(film: Mapping[str, Any]) -> bool | None:
    """True/False when the caller marked the venue, otherwise None.

    The row shape has no venue, so "not marked arthouse" is not the same as
    "known multiplex".  Only an explicit ``False`` should trigger the
    pre-2000 review rule; unknown stays unknown.
    """
    for key in ("arthouse", "is_arthouse", "venue_arthouse", "repertory"):
        if key in film and film.get(key) is not None:
            return bool(film.get(key))
    for key in ("venue_tags", "cinema_tags", "tags"):
        tags = _as_list(film.get(key))
        if tags:
            for tag in tags:
                for part in re.split(r"[,;|]", tag):
                    if part.strip().lower() in {
                        "arthouse", "independent", "repertory", "classic"
                    }:
                        return True
            return False
    return None


def _choose_imdb_dir(cache_path: Path) -> Path:
    """Where to look for/download the IMDb bulk files.

    ``data/cache/`` is the repository's ignored cache directory, so a real
    build should put the 700 MB of TSVs there.  Fixtures and a caller's own
    layout may sit next to ``ratings.json``; prefer whichever directory already
    has the join, then whichever has any dataset, and only fall back to
    ``data/cache`` for a first download.
    """
    data_dir = cache_path.parent
    cache_dir = data_dir / "cache"
    for folder in (data_dir, cache_dir):
        if (folder / "title.basics.tsv.gz").exists() and (
            folder / "title.akas.tsv.gz"
        ).exists():
            return folder
    for folder in (data_dir, cache_dir):
        if any(
            (folder / name).exists()
            for name in (
                "title.basics.tsv.gz",
                "title.akas.tsv.gz",
                "title.ratings.tsv.gz",
            )
        ):
            return folder
    return cache_dir


def _count_scores(stats: dict[str, int], entry: Mapping[str, Any]) -> None:
    if entry.get("imdb") is not None:
        stats["imdb"] += 1
    if entry.get("rt") is not None:
        stats["rt"] += 1
    if entry.get("mc") is not None:
        stats["mc"] += 1


def _merge_sources(*groups: Iterable[str]) -> list[str]:
    out: list[str] = []
    for group in groups:
        for source in group:
            if source and source not in out:
                out.append(source)
    return out


def _seed_entry(
    old: Mapping[str, Any] | None,
    *,
    imdb_id: str | None,
) -> dict[str, Any]:
    entry = _empty_entry()
    if not isinstance(old, dict):
        return entry
    old_id = old.get("imdb_id")
    if imdb_id and old_id and old_id != imdb_id:
        # A different tconst means every score on the old row belongs to a
        # different film; carrying them over would be worse than starting over.
        return entry
    for field in (
        "imdb_id", "imdb", "imdb_votes", "rt", "rt_url", "mc", "mc_url",
        "confidence", "fetched_at",
    ):
        if old.get(field) is not None:
            entry[field] = old[field]
    entry["source"] = _as_list(old.get("source"))
    entry["match_method"] = old.get("match_method") or "none"
    entry["match_score"] = old.get("match_score")
    entry["match_margin"] = old.get("match_margin")
    entry["needs_review"] = bool(old.get("needs_review"))
    return entry


def enrich(
    films: Iterable[Mapping[str, Any]],
    *,
    cache_path: Path | str,
    omdb_key: str | None = None,
    scrape: bool = False,
    limit: int | None = None,
    ttl_days: float = 14,
    pace: float = 2.5,
    session: requests.Session | None = None,
    progress: Callable[[str, dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Enrich film rows, returning ``{"cache": ..., "stats": ...}``.

    ``films`` are SQLite-shaped rows (``key``, ``title``, ``year``, ``aka``,
    optional ``imdb_id``/``runtime_minutes``/``arthouse``).  Fresh cache
    entries are served without a request; ``limit`` counts only *new* lookups,
    so a smoke run can stop after 10 films and still report cached coverage.

    Stats:

    ``films``
        rows examined.
    ``cached``
        fresh cache entries that needed no request.
    ``resolved``
        looked-up rows that ended with an IMDb ID (including review rows).
    ``imdb``/``rt``/``mc``
        rows that end with a displayable score.
    ``skipped``
        rows skipped because of the limit or an empty title.
    ``failed``
        lookups that raised and had no usable cached fallback.
    """
    cache_path = Path(cache_path)
    cache = load_cache(cache_path)
    films = [film for film in (films or [])]
    sess = session or _new_session()
    fetcher = _Fetcher(sess, pace=pace)

    stats = {
        "films": 0,
        "cached": 0,
        "resolved": 0,
        "imdb": 0,
        "rt": 0,
        "mc": 0,
        "skipped": 0,
        "failed": 0,
    }

    imdb_dir = _choose_imdb_dir(cache_path)
    basics_path = imdb_dir / "title.basics.tsv.gz"
    akas_path = imdb_dir / "title.akas.tsv.gz"
    ratings_path = imdb_dir / "title.ratings.tsv.gz"

    index_state: dict[str, Any] = {"loaded": False, "index": None, "error": None}
    ratings_state: dict[str, Any] = {"loaded": False, "map": {}, "error": None}
    omdb_disabled = False

    # RT slugs are an exact IMDb-ID -> RT-ID join (D-018).  Only needed for the
    # opt-in scrape path, and a failure simply leaves those chips empty.
    rt_ids: dict[str, str] = {}
    if scrape:
        candidate_ids = [
            str(entry.get("imdb_id") or "")
            for entry in cache.values() if isinstance(entry, dict)
        ]
        candidate_ids.extend(str(film.get("imdb_id") or "") for film in films)
        try:
            rt_ids = _wikidata_rt_ids(candidate_ids, sess)
        except Exception:  # noqa: BLE001 - optional enrichment, never fatal
            rt_ids = {}

    def get_index() -> ImdbIndex | None:
        """Load the local join lazily, and do not download a partial set.

        A directory with only ``title.ratings`` is the legacy/offline shape:
        use it for scores but fall back to Wikidata for IDs rather than
        pulling 700 MB behind the caller's back.
        """
        if index_state["loaded"]:
            return index_state["index"]
        index_state["loaded"] = True
        present = (basics_path.exists(), akas_path.exists(), ratings_path.exists())
        if not (present[0] and present[1]):
            # Basics and akas are the join; ratings alone is the original
            # offline shape and should not cause a 700 MB download.  A real
            # build with no files at all downloads the full set below.
            if any(present) or not isinstance(sess, requests.Session):
                index_state["error"] = IMDbDatasetError(
                    "incomplete IMDb dataset set; local join skipped"
                )
                return None
        try:
            index = load_imdb_index(
                basics_path,
                akas_path,
                ratings_path,
                session=sess,
                cache_path=imdb_dir / "title.index.pickle",
            )
            index_state["index"] = index
            # Share the index's ratings dict rather than keeping a second
            # 1.7-million-row copy loaded before the join was built.
            ratings_state["map"] = index.ratings
            ratings_state["loaded"] = True
        except Exception as exc:  # noqa: BLE001 - fall back to Wikidata
            index_state["error"] = exc
        return index_state["index"]

    def get_ratings() -> dict[str, tuple[float, int]]:
        index = index_state.get("index")
        if isinstance(index, ImdbIndex):
            return index.ratings
        if ratings_state["loaded"]:
            return ratings_state["map"]
        ratings_state["loaded"] = True
        if not ratings_path.exists() and not isinstance(sess, requests.Session):
            ratings_state["error"] = IMDbDatasetError(
                "no local IMDb ratings file; download skipped for a custom session"
            )
            return ratings_state["map"]
        try:
            ratings_state["map"] = imdb_ratings(ratings_path, session=sess)
        except Exception as exc:  # noqa: BLE001 - OMDb may still work
            ratings_state["error"] = exc
        return ratings_state["map"]

    def lookup(
        film: Mapping[str, Any],
        *,
        title: str,
        year: int | None,
        aka: list[str],
        arthouse: bool | None,
    ) -> tuple[dict[str, Any], bool, bool, bool]:
        """Return ``(entry, resolved, failed, had_new_data)``."""
        nonlocal omdb_disabled
        errors: list[Exception] = []
        rt_slug = ""
        mc_slug = ""
        candidate: Match | None = None

        direct = _valid_imdb_id(film.get("imdb_id"))
        old = cache.get(cache_key(title, year))
        old = old if isinstance(old, dict) else None
        old_id = _valid_imdb_id((old or {}).get("imdb_id"))

        if direct:
            candidate = Match(
                imdb_id=direct,
                method="manual",
                score=1.0,
                margin=1.0,
                needs_review=False,
            )
        else:
            index = get_index()
            if index is not None:
                candidate = index.resolve(
                    title,
                    year=year,
                    runtime=_opt_int(
                        film.get("runtime_minutes") or film.get("runtime")
                    ),
                    arthouse=arthouse,
                )
            if candidate is None and (index is None or index_state["error"]):
                # The local join is unavailable; a previous resolution is a
                # better fallback than dropping the row.
                if old_id:
                    candidate = Match(
                        imdb_id=old_id,
                        method=str((old or {}).get("match_method") or "manual"),
                        score=float((old or {}).get("match_score") or 1.0),
                        margin=float((old or {}).get("match_margin") or 1.0),
                        needs_review=bool((old or {}).get("needs_review")),
                    )
            if candidate is None:
                title_key = parse_title(title).strict
                queries: list[str] = []
                seen_queries: set[str] = set()
                for raw in [title, title_key, *aka]:
                    text = str(raw or "").strip()
                    cleaned = parse_title(text).strict if text else ""
                    for query in (text, cleaned):
                        key = normalise_title(query)
                        if query and key not in seen_queries:
                            seen_queries.add(key)
                            queries.append(query)
                for query in queries:
                    try:
                        candidates = _wikidata_candidates(query, fetcher, 20.0)
                    except (SourceStopped, FetchError) as exc:
                        errors.append(exc)
                        break
                    chosen = _select_candidate(candidates, year)
                    if chosen is None:
                        continue
                    if not any(
                        chosen.get(field)
                        for field in ("imdb_id", "rt_slug", "mc_slug")
                    ):
                        # An exact label with no identifiers cannot score the
                        # film; keep looking at the aka list.
                        continue
                    candidate = Match(
                        imdb_id=str(chosen.get("imdb_id") or ""),
                        method="wikidata",
                        score=1.0,
                        margin=1.0,
                        needs_review=bool(
                            chosen.get("year") is not None
                            and int(chosen["year"]) < 2000
                            and arthouse is False
                        ),
                        candidate_year=chosen.get("year"),
                        rt_slug=str(chosen.get("rt_slug") or ""),
                        mc_slug=str(chosen.get("mc_slug") or ""),
                    )
                    rt_slug = candidate.rt_slug
                    mc_slug = candidate.mc_slug
                    break

        if candidate is None:
            entry = _seed_entry(old, imdb_id=None)
            entry["match_method"] = "none"
            entry["match_score"] = None
            entry["match_margin"] = None
            entry["needs_review"] = False
            entry["confidence"] = "low"
            entry["source"] = _merge_sources(entry.get("source", []))
            if old and not errors:
                entry["fetched_at"] = old.get("fetched_at")
            else:
                entry["fetched_at"] = entry.get("fetched_at") or _now_iso()
            return entry, False, bool(errors), False

        imdb_id = candidate.imdb_id or None
        if not rt_slug and imdb_id:
            rt_slug = rt_ids.get(imdb_id, "")
        if candidate.needs_review:
            entry = _seed_entry(old, imdb_id=imdb_id)
            entry["imdb_id"] = imdb_id
            entry["match_method"] = candidate.method
            entry["match_score"] = candidate.score
            entry["match_margin"] = candidate.margin
            entry["needs_review"] = True
            entry["confidence"] = "low"
            entry["rt_url"] = ""
            entry["mc_url"] = ""
            # Review rows deliberately carry no scores: a wrong score is worse
            # than an empty chip, and the exporter does not need to know.
            entry["imdb"] = None
            entry["imdb_votes"] = None
            entry["rt"] = None
            entry["mc"] = None
            entry["source"] = _merge_sources([_method_source(candidate.method)])
            entry["fetched_at"] = _now_iso()
            return entry, bool(imdb_id), False, True

        source_new: list[str] = [_method_source(candidate.method)]
        entry = _seed_entry(old, imdb_id=imdb_id)
        entry["imdb_id"] = imdb_id
        entry["confidence"] = "high"
        entry["match_method"] = candidate.method
        entry["match_score"] = candidate.score
        entry["match_margin"] = candidate.margin
        entry["needs_review"] = False

        imdb_value: float | None = None
        imdb_votes: int | None = None
        ratings = get_ratings() if imdb_id else {}
        if imdb_id and imdb_id in ratings:
            imdb_value, imdb_votes = ratings[imdb_id]
            source_new.append("imdb-bulk")
        elif ratings_state.get("error") and not ratings:
            errors.append(ratings_state["error"])

        omdb: dict[str, Any] = {"imdb": None, "rt": None, "mc": None}
        if omdb_key and imdb_id and not omdb_disabled:
            try:
                omdb = _parse_omdb(
                    _omdb_payload(imdb_id, omdb_key, fetcher, 20.0)
                )
                if any(value is not None for value in omdb.values()):
                    source_new.append("omdb")
            except (SourceStopped, FetchError) as exc:
                errors.append(exc)
                omdb_disabled = True

        rt_value = omdb.get("rt")
        mc_value = omdb.get("mc")

        if imdb_value is None and omdb.get("imdb") is not None:
            imdb_value = float(omdb["imdb"])

        if not rt_slug:
            rt_slug = candidate.rt_slug
        if not mc_slug:
            mc_slug = candidate.mc_slug

        # The IMDb match knows the film's real year even when the calendar
        # row does not; use it to disambiguate same-titled remakes on RT.
        rt_year = year or candidate.candidate_year

        if scrape and rt_value is None:
            tried: list[str] = []
            if rt_slug:
                tried.append(rt_slug)
                try:
                    scores, url = _rt_scores(rt_slug, fetcher, 25.0, expected_year=rt_year)
                except (SourceStopped, FetchError) as exc:
                    errors.append(exc)
                else:
                    if scores.get("critics") is not None:
                        rt_value = scores["critics"]
                        entry["rt_url"] = url
                        source_new.append("rt-scrape")

            if rt_value is None and rt_year is not None:
                # A guessed slug without a year is how a 2025 short gets the
                # 1986 film's Tomatometer.  The Wikidata slug above is exact
                # and needs no year; guesses do.
                slugs = [
                    slug for slug in rt_slug_candidates(title, rt_year)
                    if slug not in tried
                ]
                for slug in dict.fromkeys(slugs):
                    try:
                        scores, url = _rt_scores(
                            slug, fetcher, 25.0, expected_year=rt_year
                        )
                    except (SourceStopped, FetchError) as exc:
                        errors.append(exc)
                        break
                    if scores.get("critics") is not None:
                        rt_value = scores["critics"]
                        entry["rt_url"] = url
                        source_new.append("rt-scrape")
                        break

        if mc_value is None and mc_slug:
            # Wikidata often supplies a verified MC slug; use it before the
            # finder, which costs an extra request and is less certain.
            entry["mc_url"] = MC_PAGE.format(slug=_mc_page_slug(mc_slug))
            try:
                scores = _mc_scores(mc_slug, fetcher, 25.0)
                mc_value = scores.get("critics")
            except (SourceStopped, FetchError) as exc:
                errors.append(exc)
            if mc_value is not None:
                source_new.append("mc-scrape")

        if mc_value is None:
            # Keyless Metascore fallback.  The finder supplies a gated slug;
            # the stats endpoint supplies the score, with the finder's own
            # critic summary as a last resort.
            finder_title = parse_title(title).strict or title
            try:
                hit = _mc_finder_lookup(finder_title, fetcher, 20.0, year)
            except (SourceStopped, FetchError) as exc:
                hit = None
                errors.append(exc)
            if hit:
                mc_slug = str(hit.get("slug") or "")
                if mc_slug:
                    entry["mc_url"] = MC_PAGE.format(slug=_mc_page_slug(mc_slug))
                from_stats = False
                try:
                    scores = _mc_scores(mc_slug, fetcher, 25.0)
                    mc_value = scores.get("critics")
                    from_stats = mc_value is not None
                except (SourceStopped, FetchError) as exc:
                    errors.append(exc)
                if mc_value is None and hit.get("score") is not None:
                    # The finder response carries the same Metascore summary;
                    # using it avoids one extra request when the stats endpoint
                    # is unavailable.
                    mc_value = hit["score"]
                if mc_value is not None:
                    source_new.append("mc-finder")
                if from_stats:
                    source_new.append("mc-scrape")

        if imdb_value is not None:
            entry["imdb"] = imdb_value
            if imdb_votes is not None:
                entry["imdb_votes"] = imdb_votes
        if rt_value is not None:
            entry["rt"] = rt_value
        if rt_slug and not entry.get("rt_url"):
            entry["rt_url"] = RT_MOVIE_URL.format(slug=_clean_rt_slug(rt_slug))
        if mc_value is not None:
            entry["mc"] = mc_value
        if mc_slug:
            entry["mc_url"] = MC_PAGE.format(slug=_mc_page_slug(mc_slug))

        entry["source"] = _merge_sources(entry.get("source", []), source_new)

        score_sources = {"imdb-bulk", "omdb", "mc-finder", "mc-scrape", "rt-scrape"}
        had_new_data = (
            old is None
            or any(source in score_sources for source in source_new)
            or (imdb_id is not None and imdb_id != (old or {}).get("imdb_id"))
        )
        if had_new_data:
            entry["fetched_at"] = _now_iso()
        else:
            # The ID was already cached and no score source produced anything;
            # leave the row stale so the next run retries the failed source.
            entry["fetched_at"] = old.get("fetched_at") or _now_iso()

        if not (
            imdb_id
            or entry.get("rt_url")
            or entry.get("mc_url")
            or entry.get("rt") is not None
            or entry.get("mc") is not None
        ):
            # A matched Wikidata entity with no usable identifier is not a
            # resolution at all.  A matched film with an ID but no rating yet
            # keeps "high": the match is what confidence describes.
            entry["confidence"] = "low"
        return entry, bool(imdb_id), bool(errors) and not had_new_data, had_new_data

    def emit(film_key: str, entry: Mapping[str, Any], status: str) -> None:
        if progress is None:
            return
        payload = dict(entry)
        payload["status"] = status
        progress(film_key, payload)

    new_lookups = 0
    for film in films or []:
        stats["films"] += 1
        title = str(film.get("title") or "").strip()
        if not title:
            stats["skipped"] += 1
            emit(str(film.get("key") or ""), {}, "skipped")
            continue

        info = parse_title(title)
        year = _coerce_year(film.get("year")) or info.year_hint
        key = str(film.get("key") or "")
        ck = cache_key(title, year)
        old = cache.get(ck)

        # A fresh row without an RT score is not "done" on the opt-in path:
        # the first run often resolves the IMDb id only after the P1258 batch
        # was built, so the next run retries those rows and fills the chip.
        missing_rt = (
            scrape
            and isinstance(old, dict)
            and bool(old.get("imdb_id"))
            and old.get("rt") is None
        )
        if isinstance(old, dict) and _is_fresh(old, ttl_days) and not missing_rt:
            stats["cached"] += 1
            _count_scores(stats, old)
            emit(key or ck, old, "cached")
            continue

        if limit is not None and new_lookups >= limit:
            stats["skipped"] += 1
            emit(key or ck, {}, "skipped")
            continue

        new_lookups += 1
        try:
            entry, resolved, failed, had_new = lookup(
                film,
                title=title,
                year=year,
                aka=_as_list(film.get("aka")),
                arthouse=_is_arthouse(film),
            )
        except Exception as exc:  # noqa: BLE001 - one film must not kill a run
            stats["failed"] += 1
            emit(key or ck, {"error": str(exc)}, "failed")
            continue

        if entry and had_new:
            cache[ck] = entry
            # Save after every new row: a 40-minute build should be resumable.
            save_cache(cache_path, cache)
        elif entry and ck not in cache:
            cache[ck] = entry

        if resolved:
            stats["resolved"] += 1
        if failed:
            stats["failed"] += 1
        _count_scores(stats, entry)
        status = "resolved" if resolved else ("failed" if failed else "empty")
        emit(key or ck, entry, status)

    save_cache(cache_path, cache)
    return {"cache": cache, "stats": stats}


def _method_source(method: str) -> str:
    return {
        "exact": "imdb-basics",
        "aka": "imdb-akas",
        "fuzzy": "imdb-fuzzy",
        "wikidata": "wikidata",
        "manual": "catalogue",
    }.get(method, "")


#: Short aliases matching the design notes' names for the two keys.
key_strict = normalise_title
key_loose = loose_key
