"""Paths and constants."""

from __future__ import annotations

import os
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = PACKAGE_DIR.parent

DATA_DIR = Path(os.environ.get("CINEMELB_DATA", PROJECT_DIR / "data"))
CACHE_DIR = DATA_DIR / "cache"
DB_PATH = DATA_DIR / "cinemelb.sqlite3"
REGISTRY_PATH = DATA_DIR / "cinemas.json"

USER_AGENT = os.environ.get(
    "CINEMELB_UA",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
)

#: Metro Melbourne bounding box — used to decide whether a venue is "in
#: Melbourne" when a source returns the whole state.
MELBOURNE_BOUNDS = {
    "min_lat": -38.35,   # past Frankston / Pakenham
    "max_lat": -37.40,   # past Craigieburn / Whittlesea
    "min_lon": 144.40,   # past Melton / Werribee
    "max_lon": 145.55,   # past Belgrave / Cranbourne
}

#: Centre used when the caller supplies no location.
MELBOURNE_CBD = (-37.8136, 144.9631)

for _p in (DATA_DIR, CACHE_DIR):
    _p.mkdir(parents=True, exist_ok=True)
