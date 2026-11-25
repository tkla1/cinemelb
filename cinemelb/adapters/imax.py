"""Adapter for IMAX Melbourne — currently blocked upstream.

IMAX moved to Movio's "Lumos" web platform.  What that means for a scraper:

* ``https://imaxmelbourne.com.au`` 302s to ``https://web.imaxmelbourne.com.au``,
  a Next.js SPA that server-renders nothing but its own configuration.  The
  legacy marketing site's ``/now_showing`` and ``/coming_soon`` pages redirect
  into that same SPA, so there is no server-rendered session markup anywhere.
* Session data is fetched client-side from the Vista CXM "GAS" API at
  ``https://digital-api.imaxmelbourne.com.au`` (``/v2/experiences/get``),
  authenticated with the short-lived ``gasToken`` embedded in the page's
  ``__NEXT_DATA__``.
* That API answers **403 "Attention Required"** from Cloudflare for every
  non-browser client, with or without the token, with or without a full set of
  browser headers — and a real browser on this machine was refused as well
  (Cloudflare access block, not a solvable JS challenge).

So there is no honest HTTP-only source for IMAX screenings: the alternative is
a headless browser that can pass Cloudflare, which the project does not use.
This adapter therefore reports the situation instead of inventing screenings;
it returns no rows and a note the refresh report will surface.  If the venue
ever exposes the GAS response through a plain endpoint, that payload has the
shape ``{context, experiences: [...]}`` with each experience carrying the film
title, ``site``/``screen`` and a list of ``{startUtc, ...}`` sessions.
"""

from __future__ import annotations

from ..http import Http
from .base import Adapter, AdapterOutput

BLOCKED_NOTE = (
    "IMAX Melbourne is a client-rendered Lumos SPA; its session API "
    "(digital-api.imaxmelbourne.com.au/v2/experiences/get) is Cloudflare-403 "
    "for non-browser clients. No HTTP-only source for sessions."
)


class ImaxAdapter(Adapter):
    name = "imax"

    def fetch(self, cinema: dict, http: Http) -> AdapterOutput:
        return AdapterOutput(note=BLOCKED_NOTE)
