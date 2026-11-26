"""Normalised data model shared by every scraped source.

Everything the tool knows about the real world passes through these three
records.  Adapters are only allowed to produce these; the store, the query
engine and the CLI only ever consume these.

Design notes
------------
* Times are stored twice: ``starts_at`` is an aware UTC datetime, and
  ``local_date`` / ``local_time`` are the *Melbourne wall-clock* values the
  cinema actually advertises.  Cinemas publish wall-clock times; a screening
  at "7:00pm" is 19:00 in Australia/Melbourne even across a DST boundary.
  Keeping both means queries ("films after 7pm on Friday") are exact, and the
  user still sees the time on the ticket.
* ``film_key`` / ``cinema_key`` are stable slugs so the same film at six
  different cinemas collapses to one film record.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field, asdict
from datetime import datetime, date, time, timedelta, timezone, tzinfo
from typing import Any

from .sanitize import clean_text

try:  # Linux/macOS ship the IANA db; Windows needs the `tzdata` package.
    from zoneinfo import ZoneInfo

    MELBOURNE_TZ: tzinfo = ZoneInfo("Australia/Melbourne")
except Exception:  # pragma: no cover - exercised only on bare Windows
    class _MelbourneTZ(tzinfo):
        """AEST/AEDT with the real Australian DST rules.

        DST runs from the first Sunday in October (02:00 AEST) to the first
        Sunday in April (03:00 AEDT).  This fallback exists so the tool works
        on a Windows box without the `tzdata` wheel installed; when `tzdata`
        is present the IANA rules are used instead.
        """

        @staticmethod
        def _first_sunday(year: int, month: int) -> date:
            d = date(year, month, 1)
            # weekday(): Monday=0 … Sunday=6
            return d + timedelta(days=(6 - d.weekday()) % 7)

        def utcoffset(self, dt):
            if dt is None:
                return timedelta(hours=10)
            y = dt.year
            dst_start = datetime.combine(self._first_sunday(y, 10), time(2))
            dst_end = datetime.combine(self._first_sunday(y, 4), time(3))
            naive = dt.replace(tzinfo=None)
            in_dst = naive >= dst_start or naive < dst_end
            return timedelta(hours=11 if in_dst else 10)

        def dst(self, dt):
            return self.utcoffset(dt) - timedelta(hours=10)

        def tzname(self, dt):
            return "AEDT" if self.utcoffset(dt) == timedelta(hours=11) else "AEST"

    MELBOURNE_TZ = _MelbourneTZ()


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

_WS = re.compile(r"\s+")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_LEADING_ARTICLE = re.compile(r"^(?:the|a|an)\s+", re.IGNORECASE)


def slugify(text: str) -> str:
    """ASCII slug, good enough for stable keys."""
    if not text:
        return ""
    norm = unicodedata.normalize("NFKD", text)
    norm = norm.encode("ascii", "ignore").decode("ascii").lower()
    norm = _NON_ALNUM.sub("-", norm).strip("-")
    return norm


def normalise_title(title: str) -> str:
    """Canonical comparison form for a film title.

    Cinema chains disagree about capitalisation, articles and punctuation
    ("The Hunger Games: Sunrise on the Reaping" vs "Hunger Games - Sunrise on
    the Reaping").  Matching elsewhere in the tool compares this string, so
    it must be stable.
    """
    if not title:
        return ""
    t = unicodedata.normalize("NFKD", title)
    # Fold curly quotes to a straight apostrophe *before* the ASCII encode.
    # Doing it after was a real bug: the encode drops U+2019 outright, so
    # "Disney’s Hexed" became "disneys hexed" while "Disney's Hexed" became
    # "disney s hexed" — the same film split into two catalogue keys.
    t = re.sub(r"[\u2018\u2019\u201c\u201d]", "'", t)
    t = t.encode("ascii", "ignore").decode("ascii")
    t = t.lower()
    t = t.replace("&", " and ")
    t = _NON_ALNUM.sub(" ", t)
    t = _WS.sub(" ", t).strip()

    # Cinema Nova sorts titles alphabetically and prints "Fox, The"; every
    # other source writes "The Fox". Without this the same film fragments into
    # two catalogue entries and a search finds only half its screenings.
    words = t.split()
    if len(words) >= 2 and words[-1] in ("the", "a", "an"):
        words = [words[-1]] + words[:-1]
        t = " ".join(words)

    t = _LEADING_ARTICLE.sub("", t)
    return t


def film_key(title: str, year: int | None = None) -> str:
    # Programme prefixes ("BFF26 Parky", "Astor: ...") are listing furniture,
    # not part of the film's identity; fold them out before the key is built.
    from .titles import display_title

    key = slugify(normalise_title(display_title(title)))
    if year:
        key = f"{key}-{year}"
    return key or "unknown"


def make_id(*parts: Any) -> str:
    """Deterministic short id for de-duplicating scraped rows."""
    raw = "|".join("" if p is None else str(p) for p in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


@dataclass(slots=True)
class Cinema:
    """A physical venue.

    ``id`` is our own slug and never changes; ``source_ids`` maps adapter name
    -> that source's own venue identifier so scrapers can find it again.
    """

    id: str
    name: str
    chain: str = ""
    address: str = ""
    suburb: str = ""
    postcode: str = ""
    state: str = "VIC"
    latitude: float | None = None
    longitude: float | None = None
    phone: str = ""
    website: str = ""
    booking_url: str = ""
    screens: int | None = None
    #: adapter name (see cinemelb.adapters) used to fetch this venue
    source: str = ""
    #: identifiers inside the upstream platform, e.g. {"hoyts": "MCECIN"}
    source_ids: dict[str, str] = field(default_factory=dict)
    #: free-form venue tags: "arthouse", "imax", "drive-in", "rooftop", ...
    tags: list[str] = field(default_factory=list)
    #: human notes (accessibility, getting there, "closed for renovation")
    notes: str = ""
    active: bool = True
    #: False for venues outside greater Melbourne, so default queries stay
    #: local while the registry can still hold regional cinemas
    metro: bool = True

    def __post_init__(self) -> None:
        self.name = clean_text(self.name)
        self.chain = clean_text(self.chain)
        self.address = clean_text(self.address)
        self.suburb = clean_text(self.suburb)
        self.postcode = clean_text(self.postcode)
        self.phone = clean_text(self.phone)
        self.website = clean_text(self.website)
        self.booking_url = clean_text(self.booking_url)
        self.notes = clean_text(self.notes)
        self.tags = [clean_text(t) for t in self.tags]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def display_location(self) -> str:
        bits = [b for b in (self.suburb, self.state) if b]
        return ", ".join(bits)


@dataclass(slots=True)
class Film:
    """A title, independent of where/when it screens."""

    key: str
    title: str
    year: int | None = None
    runtime_minutes: int | None = None
    rating: str = ""            # M, MA15+, PG, …
    genres: list[str] = field(default_factory=list)
    synopsis: str = ""
    poster_url: str = ""
    trailer_url: str = ""
    directors: list[str] = field(default_factory=list)
    #: other titles this film is known by (festival name, original title)
    aka: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.title = clean_text(self.title)
        self.rating = clean_text(self.rating)
        self.synopsis = clean_text(self.synopsis)
        self.poster_url = clean_text(self.poster_url)
        self.trailer_url = clean_text(self.trailer_url)
        self.genres = [clean_text(g) for g in self.genres]
        self.directors = [clean_text(d) for d in self.directors]
        self.aka = [clean_text(a) for a in self.aka]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Screening:
    """One performance of one film at one cinema."""

    id: str
    cinema_id: str
    film_key: str
    film_title: str
    starts_at: datetime          # aware, UTC
    local_date: date             # Melbourne wall-clock date
    local_time: time             # Melbourne wall-clock start time
    screen: str = ""
    format: str = ""             # e.g. "35mm", "70mm", "IMAX", "Vmax"
    attributes: list[str] = field(default_factory=list)   # CC, AD, deluxe…
    ticket_url: str = ""
    sold_out: bool = False
    source: str = ""

    def __post_init__(self) -> None:
        self.film_title = clean_text(self.film_title)
        self.screen = clean_text(self.screen)
        self.format = clean_text(self.format)
        self.ticket_url = clean_text(self.ticket_url)
        self.attributes = [clean_text(a) for a in self.attributes]

    @property
    def local_dt(self) -> datetime:
        return datetime.combine(self.local_date, self.local_time)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["starts_at"] = self.starts_at.isoformat()
        d["local_date"] = self.local_date.isoformat()
        d["local_time"] = self.local_time.strftime("%H:%M")
        return d


# --------------------------------------------------------------------------- #
# time helpers used by adapters
# --------------------------------------------------------------------------- #

def to_utc(local_naive: datetime) -> datetime:
    """Attach Melbourne timezone to a naive wall-clock datetime."""
    if local_naive.tzinfo is not None:
        return local_naive.astimezone(timezone.utc)
    return local_naive.replace(tzinfo=MELBOURNE_TZ).astimezone(timezone.utc)


def make_screening(
    *,
    cinema_id: str,
    film_title: str,
    local_naive: datetime,
    source: str,
    screen: str = "",
    fmt: str = "",
    attributes: list[str] | None = None,
    ticket_url: str = "",
    film_key_override: str | None = None,
    sold_out: bool = False,
) -> Screening:
    """Build a Screening from a cinema's advertised wall-clock time."""
    local = local_naive.replace(tzinfo=None)
    return Screening(
        id=make_id(source, cinema_id, film_title, local.isoformat(), screen),
        cinema_id=cinema_id,
        film_key=film_key_override or film_key(film_title),
        film_title=film_title,
        starts_at=to_utc(local),
        local_date=local.date(),
        local_time=local.time(),
        screen=screen,
        format=fmt,
        attributes=attributes or [],
        ticket_url=ticket_url,
        sold_out=sold_out,
        source=source,
    )
