"""cinemelb — query every cinema in Melbourne, independents included."""

import sys

__version__ = "1.0.0"


def _ensure_utf8_console() -> None:
    """Windows terminals default to cp1252 and cannot print "…" or "·".

    Reconfiguring is harmless on POSIX and avoids a UnicodeEncodeError crash
    on the first pretty-printed line of output.
    """
    for stream in (sys.stdout, sys.stderr):
        enc = (getattr(stream, "encoding", "") or "").lower()
        if enc.replace("-", "") != "utf8" and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:                                    # noqa: BLE001
                pass


_ensure_utf8_console()

from .models import Cinema, Film, Screening          # noqa: F401
from .query import Filters, Hit                     # noqa: F401
from .store import Store                            # noqa: F401

__all__ = ["Cinema", "Film", "Screening", "Filters", "Hit", "Store", "__version__"]
