"""Display-level title clean-up.

Melbourne listings inject the *programme* into the film title: Palace publishes
``BFF26 Parky`` (British Film Festival 2026), the Astor publishes
``Astor: The Last Waltz``, and Cinema Nova publishes ``Cult Classix: Suspiria``.
Those prefixes are useful context on the venue's own page but wrong in a
catalogue: they fragment one film into several rows, break the rating and cover
lookups, and make search results read like a festival schedule.

This module removes only the programme/venue prefix. It is deliberately
conservative: a bare ``Astor`` or ``Palace`` is left alone, and a title that is
*only* a prefix is left alone. The original string is kept by the caller in
``aka`` so no information is lost.
"""

from __future__ import annotations

import re
import unicodedata

#: Festival codes and season names. These may be followed by a colon, a dash or
#: just a space ("BFF26 Parky" is how Palace writes it).
_FESTIVAL = re.compile(
    r"""^(?:
        (?:bff|jiff|miff|jff|sff)\s?\d{0,2}
      | jodofest
      | cult\s+classix
    )
    \s*(?:[:\-–—]\s*|\s+)
    """,
    re.I | re.X,
)

#: An all-caps festival acronym ending in FF, with an optional letter and year
#: ("CASFFA: ..."). Kept case-sensitive on purpose: with re.I, "Staff Only"
#: would look like the code "STAFF" and lose its first word.
_CODE = re.compile(r"^[A-Z]{2,8}FF[A-Z]?\d{0,2}\s*[:\-–—]\s+")

#: Venue prefixes are only stripped when the venue is clearly separated from
#: the title ("Palace Cinemas: ..."), never on a bare word.
_VENUE = re.compile(
    r"""^(?:
        astor(?:\s+theatre)?
      | classic\s+elsternwick
      | cinema\s+nova
      | sun\s+theatre
      | the\s+kino
      | pentridge
      | palace(?:\s+cinemas)?
      | lido
      | cameo
    )
    \s*[:\-–—]\s*
    """,
    re.I | re.X,
)

#: A venue prefix without punctuation is only safe for names that are not
#: plausible film-title words ("Astor Something", "Cult Classix Something").
_BARE_VENUE = re.compile(r"^(?:astor|cult\s+classix)\s+", re.I)


def strip_programme_prefix(title: str) -> str:
    """Remove a leading programme/venue prefix, if there is one."""
    text = unicodedata.normalize("NFC", str(title or ""))
    text = " ".join(text.split())
    original = text
    # A listing occasionally stacks two ("JIFF: Astor: ...").
    for _ in range(3):
        before = text
        for pattern in (_FESTIVAL, _CODE, _VENUE, _BARE_VENUE):
            match = pattern.match(text)
            if match:
                candidate = text[match.end():].strip()
                if candidate:
                    text = candidate
                    break
        if text == before:
            break
    return text or original


def display_title(title: str) -> str:
    """The title a person should see in the catalogue."""
    return strip_programme_prefix(title)


def programme_of(title: str) -> str:
    """Return the prefix that :func:`display_title` removed, or ""."""
    text = unicodedata.normalize("NFC", str(title or ""))
    text = " ".join(text.split())
    for pattern in (_FESTIVAL, _CODE, _VENUE, _BARE_VENUE):
        match = pattern.match(text)
        if match:
            prefix = text[: match.end()].strip(" :-–—")
            if prefix and text[match.end():].strip():
                return prefix
    return ""
