"""Film page checks: hero, venue blocks, session links and grouping persistence.

Run: uv run python tests/ui/film.py
"""

from __future__ import annotations

import sys
from urllib.parse import quote

from playwright.sync_api import sync_playwright

from harness import check, load_bundle, report, serve


def main() -> int:
    bundle = load_bundle()
    films = bundle["films"]
    screenings = bundle["screenings"]

    by_film: dict[int, list[list[int]]] = {}
    for row in screenings:
        by_film.setdefault(row[1], []).append(row)
    rich = max(
        (idx for idx, rows in by_film.items()
         if films[idx][6] and films[idx][10] is not None
         and len({r[0] for r in rows}) > 1),
        key=lambda idx: len(by_film[idx]), default=None)
    check(rich is not None, "fixture: a multi-day film with a cover and a score")
    film = films[rich] if rich is not None else films[0]

    server, base = serve()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 390, "height": 844})
            page.goto(base + "#/film/" + quote(film[0]), wait_until="networkidle")
            page.wait_for_selector(".film-hero")

            hero = page.evaluate(
                "() => ({h1: document.querySelector('.film-hero h1')?.textContent || '',"
                " poster: !!document.querySelector('.film-hero .poster-lg'),"
                " fallback: document.querySelector('.film-hero .poster-fallback')"
                "   ?.textContent.trim() || '',"
                " facts: document.querySelector('.film-hero .film-meta')?.textContent || '',"
                " count: document.querySelector('.section-head .count')?.textContent || '',"
                " groups: [...document.querySelectorAll('[data-view-kind=\"film\"]')]"
                "   .map(b => b.textContent.trim())})")
            check(hero["h1"] == film[1], f"hero heading is the film title ({hero['h1']!r})")
            check(hero["poster"], "hero renders the poster box")
            check(bool(hero["facts"]), f"hero lists facts ({hero['facts']!r})")
            check("across" in hero["count"] and "day" in hero["count"],
                  f"section count names sessions and days ({hero['count']!r})")
            check(hero["groups"] == ["Days", "Cinemas", "Times"],
                  f"film grouping offers three views ({hero['groups']})")

            # Score chips open the source in a new, isolated tab.
            chips = page.evaluate(
                "() => [...document.querySelectorAll('.scores .score')].map(a => ({"
                " href: a.getAttribute('href'), target: a.getAttribute('target'),"
                " rel: a.getAttribute('rel'), label: a.getAttribute('aria-label')}))")
            check(bool(chips), "hero shows source score chips")
            check(all(c["target"] == "_blank" and "noopener" in (c["rel"] or "")
                      and c["href"].startswith("https://") and c["label"]
                      for c in chips),
                  f"score chips are safe external links ({len(chips)})")

            # Venue block links: https for the web, tel for the phone.
            links = page.evaluate(
                "() => [...document.querySelectorAll('.venue-link')].map(a => ({"
                " href: a.getAttribute('href'), target: a.getAttribute('target'),"
                " rel: a.getAttribute('rel'), text: a.textContent.trim()}))")
            check(bool(links), "expanded film lists venue links")
            external = [l for l in links if not l["href"].startswith("tel:")]
            phone = [l for l in links if l["href"].startswith("tel:")]
            check(all(l["href"].startswith("https://") and l["target"] == "_blank"
                      and "noopener" in (l["rel"] or "") for l in external),
                  f"web/directions links open safely ({len(external)})")
            check(all(l["href"].replace("tel:", "").replace("+", "").isdigit()
                      for l in phone),
                  f"phone links are dialable ({len(phone)})")

            # Grouping: cinema, then time, each persisted across a reload.
            page.click('[data-view="cinema"]')
            page.wait_for_selector(".cinema-block")
            check(not page.query_selector(".day-block"),
                  "Cinemas replaces the day blocks")
            page.reload(wait_until="networkidle")
            page.wait_for_selector(".cinema-block")
            persisted = page.evaluate(
                "() => document.querySelector('[data-view=\"cinema\"]')"
                ".getAttribute('aria-pressed')")
            check(persisted == "true", "film grouping survives a reload")

            page.click('[data-view="time"]')
            page.wait_for_selector(".time-block")
            buckets = page.evaluate(
                "() => [...document.querySelectorAll('.time-block')]"
                ".map(d => ({open: d.open, head: d.querySelector('.time-head')"
                "  ?.textContent.trim() || ''}))")
            check(bool(buckets) and all(b["open"] for b in buckets),
                  f"Times renders open buckets ({[b['head'] for b in buckets]})")

            page.reload(wait_until="networkidle")
            page.wait_for_selector(".time-block")
            persisted = page.evaluate(
                "() => document.querySelector('[data-view=\"time\"]')"
                ".getAttribute('aria-pressed')")
            check(persisted == "true", "time grouping survives a reload")

            browser.close()
    finally:
        server.shutdown()

    return report()


if __name__ == "__main__":
    sys.exit(main())
