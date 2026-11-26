"""Output-context helpers for untrusted (scraped) text.

Everything a scraper collects is data, and the same string can be safe in one
context and dangerous in another.  Rather than re-implementing a guard at each
sink (which is how ``serve.py`` ended up safer than ``mapview.py``), the three
contexts the app actually renders into live here:

* ``clean_text``    - into a terminal, a table cell, or storage;
* ``safe_href``     - into an ``href``/``src`` (scheme allowlist);
* ``json_for_script`` - into an inline ``<script>`` element.

``json.dumps`` is not a script-context escaper: it leaves ``</script>`` intact,
so a scraped title can close the tag and become markup.  The replacement uses
JSON's own ``\\uXXXX`` escapes, which parse back to the same string.
"""

from __future__ import annotations

import json
import re

#: C0 and C1 control characters.  ESC, BEL, CR, LF and tab all qualify: these
#: helpers are used for single-line display fields and URL slots, never for
#: prose that should keep its newlines.
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")

#: Escapes that are inert inside a JSON string and stop the HTML parser from
#: seeing a tag boundary.  ``&`` and the line separators are belt-and-braces.
_JS_ESCAPES = {
    "<": "\\u003c",
    ">": "\\u003e",
    "&": "\\u0026",
    "\u2028": "\\u2028",
    "\u2029": "\\u2029",
}

_HREF_SCHEMES = ("http://", "https://")


def clean_text(value: object) -> str:
    """Strip control characters from a value destined for display."""
    return _CONTROL.sub("", str("" if value is None else value))


def safe_href(url: object) -> str:
    """Return ``url`` only when it is an absolute http(s) URL, else ``""``.

    HTML-escaping a URL does not make it safe: ``javascript:alert(1)`` has no
    characters to escape.  The scheme is the control.
    """
    candidate = str("" if url is None else url).strip()
    return candidate if candidate.startswith(_HREF_SCHEMES) else ""


def json_for_script(payload: object) -> str:
    """``json.dumps`` output that is safe inside an HTML ``<script>`` element."""
    raw = json.dumps(payload, ensure_ascii=False, default=str)
    return "".join(_JS_ESCAPES.get(ch, ch) for ch in raw)
