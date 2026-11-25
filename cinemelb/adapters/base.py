"""Adapter contract.

One adapter per *platform*, not per venue.  Palace runs ~10 Melbourne cinemas
and Hoyts runs 12, but each platform needs one implementation; the registry
tells the orchestrator which adapter owns which venue and hands over the
upstream identifier.

To add a platform, subclass :class:`Adapter`, implement :meth:`fetch`, and
register it in ``cinemelb/adapters/__init__.py``.

Implementation rules that keep the rest of the tool honest:

* Return wall-clock times as naive ``datetime`` and let
  :func:`cinemelb.models.make_screening` attach the Melbourne timezone.  Most
  Australian cinema APIs emit a local timestamp with a bogus ``Z`` suffix;
  reading it as UTC would shift every session by 11 hours.
* Never invent screenings.  An empty list with an explanation is better than
  a plausible-looking guess, because the orchestrator records the failure and
  the venue simply drops out of results for that refresh.
* Include the canonical ``film_title`` for every screening; the query engine
  matches on the normalised title, not on per-chain film ids, which are not
  shared.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from ..http import Http
from ..models import Film, Screening


@dataclass(slots=True)
class AdapterOutput:
    screenings: list[Screening] = field(default_factory=list)
    films: list[Film] = field(default_factory=list)
    #: non-fatal note surfaced in `cinemelb stats` / refresh report
    note: str = ""


class Adapter(ABC):
    """Base class for platform adapters."""

    #: identifier used in the registry's ``source`` field
    name: str = ""

    @abstractmethod
    def fetch(self, cinema: dict, http: Http) -> AdapterOutput:
        """Fetch all upcoming screenings for one venue.

        ``cinema`` is a row from the registry (a plain dict, so adapters stay
        independent of the storage layer).  It carries ``source_ids`` with
        whatever upstream identifier the platform needs.
        """

    def fetch_many(self, cinemas: list[dict], http: Http) -> dict[str, AdapterOutput]:
        """Fetch several venues that share one platform.

        Override when the platform serves *all* venues in a single request
        (Hoyts returns the whole country's sessions in one 9 MB response — a
        per-venue loop would make twelve identical downloads).  The default is
        the obvious loop, with per-venue failures isolated so one dead venue
        cannot abort a refresh.
        """
        from concurrent.futures import ThreadPoolExecutor

        results: dict[str, AdapterOutput] = {}

        def one(cinema: dict) -> tuple[str, AdapterOutput]:
            try:
                return self.venue_id(cinema), self.fetch(cinema, http)
            except Exception as exc:                            # noqa: BLE001
                return self.venue_id(cinema), AdapterOutput(note=f"error: {exc}")

        # Venues on one platform are independent, and `Http` throttles per
        # host and keeps thread-local sessions, so a small pool is both safe
        # and a large win on the HTML platforms (~20 pages per venue).
        workers = min(6, max(1, len(cinemas)))
        if workers == 1:
            for cinema in cinemas:
                key, value = one(cinema)
                results[key] = value
            return results
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for key, value in pool.map(one, cinemas):
                results[key] = value
        return results

    # -- helpers -----------------------------------------------------------
    def source_id(self, cinema: dict, key: str = None) -> str:
        ids = cinema.get("source_ids") or {}
        return str(ids.get(key or self.name, "") or "")

    def venue_id(self, cinema: dict) -> str:
        return str(cinema.get("id", ""))

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{type(self).__name__} {self.name}>"
