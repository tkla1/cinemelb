"""Adapter registry.

Maps the ``source`` field in ``data/cinemas.json`` to an implementation.
Imports are lazy and defensive: a platform module that is missing (or whose
third-party assumptions have rotted) must not stop the other platforms from
refreshing, because a partial refresh is far more useful than none.
"""

from __future__ import annotations

import importlib
from typing import Callable

from .base import Adapter, AdapterOutput

#: source name -> (module path, class name)
_REGISTRY: dict[str, tuple[str, str]] = {
    "hoyts": ("cinemelb.adapters.hoyts", "HoytsAdapter"),
    "palace": ("cinemelb.adapters.palace", "PalaceAdapter"),
    "movingstory": ("cinemelb.adapters.movingstory", "MovingStoryAdapter"),
    "village": ("cinemelb.adapters.village", "VillageAdapter"),
    "reading": ("cinemelb.adapters.reading", "ReadingAdapter"),
    "event": ("cinemelb.adapters.event", "EventAdapter"),
    "acmi": ("cinemelb.adapters.acmi", "AcmiAdapter"),
    "nova": ("cinemelb.adapters.nova", "NovaAdapter"),
    "imax": ("cinemelb.adapters.imax", "ImaxAdapter"),
    "sun": ("cinemelb.adapters.sun", "SunAdapter"),
    #: Indy Systems SPA (Brunswick + Thornbury Picture House)
    "indy": ("cinemelb.adapters.indy", "IndyAdapter"),
    #: MovieXchange "Lumos" website + Vista Connect (United Cinemas)
    "lumos": ("cinemelb.adapters.lumos", "LumosAdapter"),
    #: Flicks-built white-label site API (Waverley Cinemas)
    "waverley": ("cinemelb.adapters.waverley", "WaverleyAdapter"),
    "generic_html": ("cinemelb.adapters.generic_html", "GenericHtmlAdapter"),
}

_cache: dict[str, Adapter | None] = {}


def get_adapter(name: str) -> Adapter | None:
    """Return the adapter for ``name``, or ``None`` if it cannot be loaded."""
    if name in _cache:
        return _cache[name]
    entry = _REGISTRY.get(name)
    if not entry:
        _cache[name] = None
        return None
    module_path, class_name = entry
    try:
        mod = importlib.import_module(module_path)
        cls: Callable[[], Adapter] = getattr(mod, class_name)
        inst = cls()
    except Exception:                                            # noqa: BLE001
        _cache[name] = None
        return None
    _cache[name] = inst
    return inst


def available() -> list[str]:
    """Names of adapters that currently import cleanly."""
    return [name for name in _REGISTRY if get_adapter(name) is not None]


def known() -> list[str]:
    return sorted(_REGISTRY)


__all__ = ["Adapter", "AdapterOutput", "get_adapter", "available", "known"]
