"""Shared harness for the SPA DOM checks: static server, assertions, fixtures.

The checks read the built site in ``dist`` and measure the DOM. They never
capture screenshots: structure is geometry, computed style and the ARIA tree.
"""

from __future__ import annotations

import functools
import json
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DIST = ROOT / "dist"
FAILS: list[str] = []


def check(condition: bool, message: str) -> None:
    if condition:
        print(f"OK   {message}")
    else:
        print(f"FAIL {message}")
        FAILS.append(message)


def info(message: str) -> None:
    print(f"INFO {message}")


def serve(directory: Path = DIST) -> tuple[ThreadingHTTPServer, str]:
    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(directory))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/"


def load_bundle() -> dict:
    return json.loads((DIST / "data" / "bundle.json").read_text(encoding="utf-8"))


def report() -> int:
    if FAILS:
        print(f"\n{len(FAILS)} check(s) failed")
        return 1
    print("\nall checks pass")
    return 0


def day_rows(bundle: dict, date_index: int) -> list[list[int]]:
    return [row for row in bundle["screenings"] if row[0] == date_index]
