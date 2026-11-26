"""Film page checks: hero, venue blocks, tickets, time scope and cinema sort.

Run: uv run python tests/ui/film.py
"""

from __future__ import annotations

import re
import sys
from datetime import date
from urllib.parse import quote

from playwright.sync_api import sync_playwright

from harness import check, load_bundle, report, serve


def to_minute(text: str) -> int:
    """Parse "7:30pm" / "12:30am"; early hours belong to the previous listing day."""
    match = re.match(r"(\d{1,2})(?::(\d{2}))?(am|pm)$", text.strip())
    if not match:
        return -1
    hour = int(match.group(1)) % 12
    if match.group(3) == "pm":
        hour += 12
    minute = hour * 60 + int(match.group(2) or 0)
    return minute + 1440 if minute < 6 * 60 else minute


def to_km(text: str) -> float:
    match = re.search(r"([\d.]+)\s*km", text)
    if match:
        return float(match.group(1))
    match = re.search(r"(\d+)\s*m\b", text)
    return float(match.group(1)) / 1000 if match else float("inf")


def main() -> int:
    bundle = load_bundle()
    films = bundle["films"]
    screenings = bundle["screenings"]
    dates = bundle["dates"]
    today_iso = date.today().isoformat()
    future = [row for row in screenings if dates[row[0]] > today_iso]

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

    # A late-night session whose film also has a daytime/evening session, so
    # "Show all times" has something to restore.
    late = next(
        (row for row in future
         if row[2] >= 21 * 60
         and any(other[1] == row[1] and 6 * 60 <= other[2] < 21 * 60
                 for other in future)),
        None)
    check(late is not None, "fixture: a late-night session with a daytime sibling")
    late_date = dates[late[0]] if late else dates[1]

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
                "   .map(b => b.textContent.trim()),"
                " sort: document.querySelectorAll('[data-cinema-sort]').length})")
            check(hero["h1"] == film[1], f"hero heading is the film title ({hero['h1']!r})")
            check(hero["poster"], "hero renders the poster box")
            check(bool(hero["facts"]), f"hero lists facts ({hero['facts']!r})")
            check("across" in hero["count"] and "day" in hero["count"],
                  f"section count names sessions and days ({hero['count']!r})")
            check(hero["groups"] == ["Days", "Cinemas", "Times"],
                  f"film grouping offers three views ({hero['groups']})")
            check(hero["sort"] == 0,
                  "cinema sort is absent while no place is set")

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

            # Venue actions: named icon buttons, https for the web, tel for the phone.
            links = page.evaluate(
                "() => [...document.querySelectorAll('.venue-link')].map(a => ({"
                " href: a.getAttribute('href'), target: a.getAttribute('target'),"
                " rel: a.getAttribute('rel'), label: a.getAttribute('aria-label') || '',"
                " icons: a.querySelectorAll('svg').length}))")
            check(bool(links), "expanded film lists venue actions")
            check(all(l["icons"] == 1 and l["label"] for l in links),
                  f"venue actions are named icon buttons ({len(links)})")
            external = [l for l in links if not l["href"].startswith("tel:")]
            phone = [l for l in links if l["href"].startswith("tel:")]
            check(all(l["href"].startswith("https://") and l["target"] == "_blank"
                      and "noopener" in (l["rel"] or "") for l in external),
                  f"web/directions links open safely ({len(external)})")
            check(all(l["href"].replace("tel:", "").replace("+", "").isdigit()
                      for l in phone),
                  f"phone links are dialable ({len(phone)})")
            addresses = page.evaluate(
                "() => document.querySelectorAll('.day-block .venue-addr').length")
            check(addresses == 0, "day venue blocks do not repeat street addresses")

            # AC-024 on the new table: at 320px the page does not scroll sideways
            # and the ticket cell stays a 24px target.
            narrow = browser.new_page(viewport={"width": 320, "height": 800})
            narrow.goto(base + "#/film/" + quote(film[0]), wait_until="networkidle")
            narrow.wait_for_selector(".session-table")
            metrics = narrow.evaluate(
                "() => { const d = document.documentElement;"
                " const t = document.querySelector('.session-table');"
                " const s = document.querySelector('.session');"
                " const r = s.getBoundingClientRect();"
                " return {scroll: d.scrollWidth, client: d.clientWidth,"
                "  table: Math.round(t.getBoundingClientRect().width),"
                "  box: Math.round(t.parentElement.getBoundingClientRect().width),"
                "  overflow: t.scrollWidth - t.clientWidth,"
                "  target: [Math.round(r.width), Math.round(r.height)]}; }")
            check(metrics["scroll"] <= metrics["client"] + 1,
                  f"320px: the film page does not scroll sideways "
                  f"({metrics['scroll']} <= {metrics['client']})")
            check(metrics["table"] <= metrics["box"] + 1 and metrics["overflow"] <= 1,
                  f"320px: the session table fits its column ({metrics['table']} "
                  f"<= {metrics['box']})")
            check(min(metrics["target"]) >= 24,
                  f"320px: the time/ticket target is at least 24px ({metrics['target']})")
            narrow.close()

            placed = browser.new_page(viewport={"width": 320, "height": 800})
            placed.add_init_script(
                "try { localStorage.setItem('cinemelb-place', JSON.stringify("
                "{name: 'Melbourne', lat: -37.8136, lon: 144.9631,"
                " source: 'gazetteer'})); } catch (e) {}")
            placed.goto(base + "#/film/" + quote(film[0]), wait_until="networkidle")
            placed.wait_for_selector(".session-table")
            placed_metrics = placed.evaluate(
                "() => ({scroll: document.documentElement.scrollWidth,"
                " client: document.documentElement.clientWidth,"
                " sorts: document.querySelectorAll('[data-cinema-sort]').length,"
                " dists: document.querySelectorAll('.venue-dist').length,"
                " row: document.querySelectorAll('.venue-link').length})")
            check(placed_metrics["scroll"] <= placed_metrics["client"] + 1,
                  f"320px with a place: the film page still does not scroll "
                  f"({placed_metrics['scroll']} <= {placed_metrics['client']})")
            check(placed_metrics["sorts"] == 2 and placed_metrics["dists"] > 0,
                  f"320px with a place: sort and distances render "
                  f"({placed_metrics['sorts']} sorts, {placed_metrics['dists']} distances)")
            placed.close()

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

            # AC-042: a film opened from a time section keeps that section's range.
            if late:
                scope_page = browser.new_page(viewport={"width": 390, "height": 844})
                scope_page.goto(base + "#/day/" + late_date, wait_until="networkidle")
                scope_page.wait_for_selector(".films .film")
                scope_page.click('[data-view="time"]')
                scope_page.wait_for_selector(".time-block")
                bucket = scope_page.locator(".time-block").filter(has_text="Late night")
                card = bucket.locator(f'.film[data-film="{late[1]}"]')
                check(card.count() > 0,
                      f"fixture: {films[late[1]][1]!r} is in the Late night section")
                if card.count():
                    href = card.locator(".film-open").first.get_attribute("href") or ""
                    check("?time=late-night" in href,
                          f"section film link carries the time scope ({href})")
                    card.locator(".film-open").first.click()
                    scope_page.wait_for_selector(".film-hero")
                    scope_page.wait_for_function(
                        "location.hash.indexOf('?time=late-night') >= 0")
                    heading = scope_page.evaluate(
                        "() => document.querySelector('.section-head h2')"
                        ".textContent.trim()")
                    check(heading == "Late night sessions",
                          f"scoped film page names the section ({heading!r})")
                    check(scope_page.evaluate(
                              "() => document.querySelectorAll('.scope-clear').length") == 1,
                          "scoped film page offers Show all times")
                    scoped = scope_page.evaluate(
                        "() => [...document.querySelectorAll("
                        "'.session-table .session .time')]"
                        ".map(e => e.textContent.trim())")
                    check(bool(scoped) and all(
                        to_minute(t) >= 21 * 60 or to_minute(t) < 6 * 60 for t in scoped),
                        f"scoped film page lists only late-night times ({scoped[:6]})")
                    scope_page.click(".scope-clear")
                    scope_page.wait_for_function(
                        "location.hash.indexOf('?time=') < 0")
                    scope_page.wait_for_function(
                        "document.querySelector('.section-head h2') && "
                        "document.querySelector('.section-head h2')"
                        ".textContent.trim() === 'All sessions'")
                    restored = scope_page.evaluate(
                        "() => [...document.querySelectorAll("
                        "'.session-table .session .time')]"
                        ".map(e => e.textContent.trim())")
                    restored_heading = scope_page.evaluate(
                        "() => document.querySelector('.section-head h2')"
                        ".textContent.trim()")
                    check(restored_heading == "All sessions",
                          "Show all times restores the full film page")
                    check(len(restored) > len(scoped),
                          f"Show all times reveals more sessions "
                          f"({len(scoped)} -> {len(restored)})")
                scope_page.close()

            # AC-042 (existing filter): a film opened from a time-filtered day
            # keeps the filter bar's After value.
            filter_page = browser.new_page(viewport={"width": 390, "height": 844})
            filter_page.goto(base + "#/day/" + late_date, wait_until="networkidle")
            filter_page.wait_for_selector(".films .film")
            filter_page.select_option("#after", "1260")
            filter_page.wait_for_selector(".film .film-open")
            filter_page.locator(".film .film-open").first.click()
            filter_page.wait_for_selector(".film-hero")
            after_value = filter_page.eval_on_selector("#after", "el => el.value")
            check(after_value == "1260",
                  f"film page keeps the day's After filter ({after_value})")
            filter_page.close()

            # AC-043: venues sort by next session by default; a placed location
            # unlocks Distance, which reorders and survives a reload.
            sort_page = browser.new_page(viewport={"width": 1280, "height": 800})
            sort_page.add_init_script(
                "try { localStorage.setItem('cinemelb-place', JSON.stringify("
                "{name: 'Melbourne', lat: -37.8136, lon: 144.9631,"
                " source: 'gazetteer'})); } catch (e) {}")
            sort_page.goto(base + "#/film/" + quote(film[0]), wait_until="networkidle")
            sort_page.wait_for_selector(".film-hero")
            sort_buttons = sort_page.evaluate(
                "() => [...document.querySelectorAll('[data-cinema-sort]')]"
                ".map(b => ({value: b.getAttribute('data-cinema-sort'),"
                "  pressed: b.getAttribute('aria-pressed')}))")
            check([b["value"] for b in sort_buttons] == ["time", "distance"],
                  f"film page offers Time and Distance ({sort_buttons})")
            check(sort_buttons and sort_buttons[0]["pressed"] == "true",
                  "cinema sort defaults to time")

            def venues() -> list[dict[str, str]]:
                return sort_page.evaluate(
                    "() => { const blocks = [...document.querySelectorAll('.day-block')];"
                    " if (!blocks.length) return [];"
                    " let best = blocks[0], n = 0;"
                    " for (const b of blocks) {"
                    "  const c = b.querySelectorAll('.venue').length;"
                    "  if (c > n) { n = c; best = b; } }"
                    " return [...best.querySelectorAll('.venue')].map(v => ({"
                    "  name: v.querySelector('.venue-name')?.textContent.trim() || '',"
                    "  first: v.querySelector('.session .time')?.textContent.trim() || '',"
                    "  dist: v.querySelector('.venue-dist')?.textContent.trim() || ''})); }")

            before = venues()
            check(len(before) >= 2,
                  f"fixture: a film day with several venues ({len(before)})")
            times = [to_minute(v["first"]) for v in before]
            check(times == sorted(times),
                  f"venues default to next-session order ({[v['first'] for v in before]})")

            sort_page.click('[data-cinema-sort="distance"]')
            sort_page.wait_for_function(
                "document.querySelector('[data-cinema-sort=\"distance\"]')"
                ".getAttribute('aria-pressed') === 'true'")
            after = venues()
            distances = [to_km(v["dist"]) for v in after]
            check(distances == sorted(distances),
                  f"Distance reorders venues ({[v['dist'] for v in after]})")

            sort_page.reload(wait_until="networkidle")
            sort_page.wait_for_selector(".film-hero")
            persisted = sort_page.evaluate(
                "() => ({pressed: document.querySelector("
                "'[data-cinema-sort=\"distance\"]')?.getAttribute('aria-pressed') || '',"
                " view: localStorage.getItem('cinemelb-view') || ''})")
            check(persisted["pressed"] == "true",
                  "cinema sort survives a reload")
            check('"cinemaSort":"distance"' in persisted["view"],
                  "cinema sort is stored in cinemelb-view")
            sort_page.close()

            browser.close()
    finally:
        server.shutdown()

    return report()


if __name__ == "__main__":
    sys.exit(main())
