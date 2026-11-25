"""Adapter for ACMI (Australian Centre for the Moving Image), Fed Square.

ACMI's site is a Nuxt 2 app whose film program is server-rendered, and — a
little unusually — it makes **no XHR at all** for the program listing.  The
entire "what's on" dataset is embedded in the page as ``window.__NUXT__``.

That blob is not JSON.  Nuxt's devalue serialiser emits a self-calling
function whose repeated strings and numbers are lifted into parameters::

    window.__NUXT__=(function(a,b,c){return {…a…}}("x",5,null));

so the payload can only be read by resolving the identifiers against the
argument list.  :func:`_nuxt_payload` does exactly that: it parses the
return-expression as a JavaScript literal (objects, arrays, strings, numbers,
``true``/``false``/``null`` and bare identifiers) and substitutes the args.
That keeps the adapter to one request of ~1 MB instead of one request per
film page (~550 KB each, and the listing shows ~100 events).

The data we want lives at ``fetch["EventIndexPage:0"].rawOccurrences``: an
array of ``{date, occurrences: [...]}`` where each occurrence carries
``start_datetime`` (Melbourne local, wearing a ``+11:00`` offset),
``venue`` (ACMI's screen, e.g. "Cinema 1, Level 2"), ``purchase_url`` and the
owning ``event`` (title, synopsis, classification, card image).  Filtering on
``event.event_type.name == "Film"`` drops exhibitions, talks and workshops
that share the same listing.

Times are Melbourne wall-clock; the offset on ``start_datetime`` is stripped
rather than converted, per the project's wall-clock rule.
"""

from __future__ import annotations

import json
import re
from datetime import datetime

from ..http import Http
from ..models import Film, film_key, make_screening
from .base import Adapter, AdapterOutput

BASE = "https://www.acmi.net.au"
#: ``?what=film`` is the film-program filter of the "what's on" index.
PROGRAMME_URL = f"{BASE}/whats-on/?what=film"

_PAYLOAD_START = "window.__NUXT__="
_IIFE_HEAD = re.compile(r"\(function\(([^)]*)\)\{return ")
_WS = re.compile(r"[ \t\r\n]*")
_FORMATS = {"70mm": "70mm", "35mm": "35mm", "4k": "4K", "imax": "IMAX"}
#: Venues that are galleries / labs, not a cinema auditorium.
_SCREEN_SUFFIX = re.compile(r",\s*Level\s*\d+.*$", re.I)


# --------------------------------------------------------------------------- #
# Nuxt / devalue payload
# --------------------------------------------------------------------------- #

class _Ref:
    """Placeholder for a devalue parameter reference (``a``, ``b``, ``aB``)."""

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name


class _JsReader:
    """Minimal reader for the JavaScript-literal subset devalue emits."""

    def __init__(self, text: str) -> None:
        self.s = text
        self.i = 0

    def _ws(self) -> None:
        self.i = _WS.match(self.s, self.i).end()

    def value(self):
        self._ws()
        char = self.s[self.i]
        if char in "\"'":
            return self._string()
        if char == "[":
            return self._array()
        if char == "{":
            return self._object()
        if char.isdigit() or char == "-":
            return self._number()
        return self._identifier()

    def _string(self) -> str:
        quote = self.s[self.i]
        end = self.i + 1
        while True:
            char = self.s[end]
            if char == "\\":
                end += 2
                continue
            if char == quote:
                break
            end += 1
        raw = self.s[self.i:end + 1]
        self.i = end + 1
        return json.loads(raw) if quote == '"' else raw[1:-1]

    def _number(self):
        end = self.i
        while end < len(self.s) and self.s[end] in "+-.eE0123456789":
            end += 1
        token = self.s[self.i:end]
        self.i = end
        return float(token) if any(c in token for c in ".eE") else int(token)

    def _name(self) -> str:
        end = self.i
        while end < len(self.s) and (self.s[end].isalnum() or self.s[end] in "_$"):
            end += 1
        name = self.s[self.i:end]
        self.i = end
        return name

    def _identifier(self):
        name = self._name()
        if name == "true":
            return True
        if name == "false":
            return False
        if name == "null":
            return None
        return _Ref(name)

    def _array(self) -> list:
        self.i += 1
        out: list = []
        while True:
            self._ws()
            if self.s[self.i] == "]":
                self.i += 1
                return out
            out.append(self.value())
            self._ws()
            if self.s[self.i] == ",":
                self.i += 1

    def _object(self) -> dict:
        self.i += 1
        out: dict = {}
        while True:
            self._ws()
            if self.s[self.i] == "}":
                self.i += 1
                return out
            key = self._string() if self.s[self.i] in "\"'" else self._name()
            self._ws()
            if self.s[self.i] != ":":
                raise ValueError(f"expected ':' at offset {self.i}")
            self.i += 1
            out[key] = self.value()
            self._ws()
            if self.s[self.i] == ",":
                self.i += 1


def _resolve(node, table: dict):
    if isinstance(node, _Ref):
        return table.get(node.name)
    if isinstance(node, list):
        return [_resolve(x, table) for x in node]
    if isinstance(node, dict):
        return {k: _resolve(v, table) for k, v in node.items()}
    return node


def _nuxt_payload(html: str) -> dict:
    """Return ``window.__NUXT__`` as plain Python data."""
    start = html.find(_PAYLOAD_START)
    if start < 0:
        raise ValueError("no window.__NUXT__ payload")
    start += len(_PAYLOAD_START)
    end = html.find("</script>", start)
    blob = (html[start:end] if end > 0 else html[start:]).strip().rstrip(";")
    if blob.startswith("{"):
        # Some Nuxt builds emit plain JSON; only the devalue form needs parsing.
        return json.loads(blob)

    head = _IIFE_HEAD.match(blob)
    if not head:
        raise ValueError("unrecognised __NUXT__ serialisation")
    params = [p.strip() for p in head.group(1).split(",") if p.strip()]
    reader = _JsReader(blob)
    reader.i = head.end()
    tree = reader.value()
    reader._ws()
    if blob[reader.i] != "}":
        raise ValueError("payload tail not found")
    reader.i += 1
    reader._ws()
    if blob[reader.i] != "(":
        raise ValueError("payload arguments not found")
    reader.i += 1
    args: list = []
    while True:
        reader._ws()
        if blob[reader.i] == ")":
            break
        args.append(reader.value())
        reader._ws()
        if blob[reader.i] == ",":
            reader.i += 1
    table: dict = dict(zip(params, args))
    # A parameter may itself be an alias of another (devalue allows it).
    for name, value in list(table.items()):
        if isinstance(value, _Ref):
            table[name] = table.get(value.name)
    return _resolve(tree, table)


def _event_index(payload: dict) -> dict:
    """Find the film-index fetch node; its key is namespaced (``X:0``)."""
    for value in (payload.get("fetch") or {}).values():
        if isinstance(value, dict) and isinstance(value.get("rawOccurrences"), list):
            return value
    return {}


# --------------------------------------------------------------------------- #
# adapter
# --------------------------------------------------------------------------- #

class AcmiAdapter(Adapter):
    name = "acmi"

    def fetch(self, cinema: dict, http: Http) -> AdapterOutput:
        cinema_id = self.venue_id(cinema)
        try:
            page = http.get(PROGRAMME_URL)
            index = _event_index(_nuxt_payload(page.text))
        except Exception as exc:                                 # noqa: BLE001
            return AdapterOutput(note=f"ACMI programme payload unreadable: {exc}")

        occurrences = index.get("rawOccurrences") or []
        if not occurrences:
            return AdapterOutput(note="no rawOccurrences in ACMI payload")

        out = AdapterOutput()
        films: dict[str, Film] = {}
        for group in occurrences:
            for occ in group.get("occurrences") or []:
                event = occ.get("event") or {}
                if ((event.get("event_type") or {}).get("name") or "") != "Film":
                    continue
                title = (event.get("title") or "").strip()
                start = self._local_start(occ.get("start_datetime") or "")
                if not title or not start:
                    continue

                key = film_key(title)
                if key not in films:
                    image = event.get("card_image") or {}
                    films[key] = Film(
                        key=key,
                        title=title,
                        rating=(event.get("classification") or "").strip(),
                        synopsis=(event.get("intro") or "").strip(),
                        poster_url=(image.get("full_url") or image.get("url") or ""),
                    )

                attributes: list[str] = []
                card_label = (event.get("card_label") or "").strip()
                if card_label:
                    attributes.append(card_label)
                status = (occ.get("status") or "").strip()
                if status:
                    attributes.append(status)
                out.screenings.append(
                    make_screening(
                        cinema_id=cinema_id,
                        film_title=title,
                        local_naive=start,
                        source=self.name,
                        screen=_screen_name(occ.get("venue") or ""),
                        fmt=self._format_from(f"{title} {card_label}"),
                        attributes=attributes,
                        ticket_url=(occ.get("purchase_url") or "").strip(),
                        sold_out=bool(event.get("sold_out")),
                        film_key_override=key,
                    )
                )

        out.films = list(films.values())
        if not out.screenings:
            out.note = "ACMI payload held no Film occurrences"
        return out

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _local_start(value: str) -> datetime | None:
        """``2026-11-25T19:00:00+11:00`` -> naive 19:00 (wall clock, not UTC)."""
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed.replace(tzinfo=None)

    @staticmethod
    def _format_from(text: str) -> str:
        low = text.lower()
        for token, label in _FORMATS.items():
            if token in low:
                return label
        return ""


def _screen_name(venue: str) -> str:
    """``"Cinema 1, Level 2"`` -> ``"Cinema 1"``."""
    return _SCREEN_SUFFIX.sub("", venue).strip()
