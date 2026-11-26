"""Layout and DOM checks for the built SPA. Measurements only, no screenshots.

Run: uv run --with playwright python tests/ui/layout.py
"""

from __future__ import annotations

import functools
import json
import sys
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
DIST = ROOT / "dist"
FAILS: list[str] = []


def check(condition: bool, message: str) -> None:
    if condition:
        print(f"OK   {message}")
    else:
        print(f"FAIL {message}")
        FAILS.append(message)


def serve(directory: Path) -> tuple[ThreadingHTTPServer, str]:
    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(directory))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/"


def main() -> int:
    bundle = json.loads((DIST / "data" / "bundle.json").read_text(encoding="utf-8"))
    film = next((row for row in bundle["films"] if row[6] and row[10] is not None),
                bundle["films"][0])
    server, base = serve(DIST)
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            for width, height, label in ((320, 800, "320px"), (390, 844, "phone"),
                                         (1280, 800, "desktop")):
                page = browser.new_page(viewport={"width": width, "height": height})
                page.goto(base, wait_until="networkidle")
                page.wait_for_selector(".film, .tile")

                scroll = page.evaluate(
                    "() => ({doc: document.documentElement.scrollWidth, "
                    "client: document.documentElement.clientWidth})")
                check(scroll["doc"] <= scroll["client"] + 1,
                      f"{label}: no horizontal page scroll ({scroll['doc']} <= {scroll['client']})")

                if width == 390:
                    line = page.evaluate(
                        "() => [...document.querySelector('.view-row').children]"
                        ".map(el => Math.round(el.getBoundingClientRect().top))")
                    check(max(line) - min(line) <= 1,
                          f"{label}: view-row controls share one line ({line})")

                if width <= 640:
                    heights = page.evaluate(
                        "() => ['.chip', '.select', '.seg-btn', '.layout-btn', "
                        "'#near-q', '.near-me', '#sort'].map(sel => "
                        "Math.max(...[...document.querySelectorAll(sel)]"
                        ".map(el => el.getBoundingClientRect().height)))")
                    check(min(heights) >= 44,
                          f"{label}: primary controls >= 44px ({[round(h) for h in heights]})")

                day_snapshot = page.locator("main").aria_snapshot()
                check("heading" in day_snapshot and film[1] in day_snapshot,
                      f"{label}: day aria snapshot has the heading and a film")
                rail = page.evaluate(
                    "() => ({current: document.querySelectorAll('#rail [aria-current=\"date\"]').length,"
                    " days: document.querySelectorAll('#rail a').length})")
                check(rail["current"] == 1,
                      f"{label}: exactly one current day in the rail")
                check(rail["days"] == 14,
                      f"{label}: rail shows the 14-day window ({rail['days']})")
                live_count = page.evaluate(
                    "() => !!document.querySelector('.day-head .count[aria-live=\"polite\"]')")
                check(live_count, f"{label}: result count is a live region")

                page.click("[data-layout-toggle]")
                page.wait_for_selector(".poster-grid .tile")
                tile = page.evaluate(
                    "() => { const t = document.querySelector('.poster-grid .tile');"
                    " const title = t.querySelector('.tile-title');"
                    " return {tag: t.tagName, links: t.querySelectorAll('a').length,"
                    " href: t.getAttribute('href') || '',"
                    " title: title ? title.textContent.trim() : '',"
                    " text: t.textContent.trim()}; }")
                check(tile["tag"] == "A" and tile["links"] <= 1,
                      f"{label}: poster tile is one link ({tile['tag']}, {tile['links']} inner)")
                check(tile["href"].startswith("#/film/"),
                      f"{label}: grid tile links to a film ({tile['href'][:24]}…)")
                check(bool(tile["title"]) and tile["title"] in tile["text"],
                      f"{label}: tile link names its title ({tile['title']!r})")

                before = page.evaluate("() => location.hash")
                page.focus("#q")
                page.press("#q", "Enter")
                page.wait_for_timeout(150)
                check(page.evaluate("() => location.hash") == before,
                      f"{label}: empty search query does not navigate")

                page.fill("#q", "dune")
                page.wait_for_selector("#suggest .sug")
                last_sug = page.evaluate(
                    "() => [...document.querySelectorAll('#suggest .sug-title')]"
                    ".pop()?.textContent.trim() || ''")
                check(last_sug.startswith("See all") and "result" in last_sug,
                      f"{label}: suggestions end with {last_sug!r}")
                page.press("#q", "Enter")
                page.wait_for_function("location.hash === '#/search/dune'")
                page.wait_for_function("document.title.startsWith('Search:')")
                search = page.evaluate(
                    "() => ({tiles: document.querySelectorAll('.tile').length, "
                    "filters: getComputedStyle(document.getElementById('filters')).display, "
                    "near: getComputedStyle(document.getElementById('nearbar')).display, "
                    "active: document.activeElement.tagName, "
                    "heading: document.querySelector('h1')?.textContent})")
                check(search["tiles"] > 0, f"{label}: search has result tiles")
                check(search["filters"] == "none" and search["near"] == "none",
                      f"{label}: search hides filters + near bar")
                check(search["active"] == "H1",
                      f"{label}: route change focuses the heading ({search['active']})")
                if search["heading"]:
                    check("Results for" in search["heading"],
                          f"{label}: search heading names the query")

                page.evaluate(
                    f"location.hash = '#/film/{film[0]}'")
                page.wait_for_function("document.title.includes('— cinemelb')")
                film_title = page.locator("h1").inner_text()
                check(film_title == film[1],
                      f"{label}: film page heading is {film[1]!r}")
                page.close()
            browser.close()
    finally:
        server.shutdown()

    if FAILS:
        print(f"\n{len(FAILS)} layout/DOM failure(s)")
        return 1
    print("\nall layout/DOM checks pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
