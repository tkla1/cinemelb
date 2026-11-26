"""Layout and DOM checks for the built SPA. Measurements only, no screenshots.

Run: uv run --with playwright python tests/ui/layout.py
"""

from __future__ import annotations

import sys

from playwright.sync_api import sync_playwright

from harness import check, load_bundle, report, serve


CONTROLS_JS = r"""
() => {
  const nodes = [...document.querySelectorAll(
    'button, select, input, summary, .rail-day, .tile')]
    .filter(el => el.getClientRects().length);
  return nodes.map(el => {
    const r = el.getBoundingClientRect();
    return {
      tag: el.tagName, cls: el.className || '',
      label: el.getAttribute('aria-label') || '',
      w: Math.round(r.width), h: Math.round(r.height)
    };
  });
}
"""

FOCUS_JS = r"""
() => {
  const el = document.activeElement;
  if (!el) return null;
  const s = getComputedStyle(el);
  return {tag: el.tagName, cls: el.className || '',
          color: s.outlineColor, style: s.outlineStyle, width: s.outlineWidth};
}
"""


def main() -> int:
    bundle = load_bundle()
    film = next((row for row in bundle["films"] if row[6] and row[10] is not None),
                bundle["films"][0])
    server, base = serve()
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

            # AC-025 / AC-026: control sizes and the token focus ring, at both
            # ends of the layout range. A control is a button, select, input or
            # summary; inline links in a sentence are exempt from 2.5.8.
            rings = {"dark": "rgb(243, 200, 118)", "light": "rgb(125, 86, 13)"}
            for width, height, label in ((320, 800, "320px"), (1280, 800, "desktop")):
                page = browser.new_page(viewport={"width": width, "height": height})
                page.goto(base, wait_until="networkidle")
                page.wait_for_selector(".film, .tile")
                controls = page.evaluate(CONTROLS_JS)
                small = [c for c in controls if c["w"] < 24 or c["h"] < 24]
                check(not small,
                      f"{label}: every control is at least 24x24 "
                      f"({len(controls)} measured, {len(small)} small)")
                named = page.evaluate(
                    "() => [...document.querySelectorAll('input, select')]"
                    ".filter(el => el.getClientRects().length)"
                    ".filter(el => !(el.getAttribute('aria-label') || "
                    "  [...(el.labels || [])].map(l => l.textContent.trim()).join(' ')))"
                    ".map(el => el.id || el.tagName)")
                check(not named, f"{label}: every input/select has an accessible name ({named})")

                page.keyboard.press("Tab")
                focus = page.evaluate(FOCUS_JS)
                theme = page.evaluate("() => document.documentElement.dataset.theme")
                check(bool(focus) and focus["style"] == "solid" and focus["width"] == "2px",
                      f"{label}: keyboard focus shows a 2px ring ({focus and focus['tag']})")
                check(bool(focus) and focus["color"] == rings.get(theme),
                      f"{label}: {theme} ring uses --focus-ring ({focus and focus['color']})")

                page.click("#theme")
                page.keyboard.press("Tab")
                focus = page.evaluate(FOCUS_JS)
                theme = page.evaluate("() => document.documentElement.dataset.theme")
                check(bool(focus) and focus["color"] == rings.get(theme),
                      f"{label}: {theme} ring uses its own token ({focus and focus['color']})")
                page.close()

            # AC-017: grouping and sort persist across a reload, and "Nearest"
            # is absent while no place is set.
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            page.goto(base, wait_until="networkidle")
            page.wait_for_selector(".film, .tile")
            page.click('[data-view="cinema"]')
            page.select_option("#sort", "title")
            page.reload(wait_until="networkidle")
            page.wait_for_selector(".film")
            persisted = page.evaluate(
                "() => ({group: document.querySelector('[data-view=\"cinema\"]')"
                ".getAttribute('aria-pressed'),"
                " sort: document.getElementById('sort').value,"
                " near: !!document.querySelector('#sort option[value=\"near\"]'),"
                " view: localStorage.getItem('cinemelb-view'),"
                " savedSort: localStorage.getItem('cinemelb-sort')})")
            check(persisted["group"] == "true",
                  "grouping survives a reload (aria-pressed on Cinemas)")
            check(persisted["sort"] == "title",
                  f"sort survives a reload ({persisted['sort']})")
            check(not persisted["near"],
                  "Nearest is absent while no place is set")
            check(persisted["savedSort"] == "title" and '"cinema"' in persisted["view"],
                  "preferences are stored under cinemelb-view/cinemelb-sort")
            page.close()

            # AC-023: changing a filter rewrites a polite live region.
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            page.goto(base + "#/day/" + bundle["dates"][-1], wait_until="networkidle")
            page.wait_for_selector(".film, .tile")
            before = page.inner_text(".day-head .count")
            polite = page.get_attribute(".day-head .count", "aria-live")
            page.select_option("#after", "1260")
            page.wait_for_function(
                "(before) => document.querySelector('.day-head .count')"
                ".textContent !== before", arg=before)
            after = page.inner_text(".day-head .count")
            check(polite == "polite" and after != before,
                  f"filter change rewrites the polite count ({before!r} -> {after!r})")
            page.close()

            # AC-027: reduced motion is honoured and the spinner is slowed.
            page = browser.new_page(viewport={"width": 1280, "height": 800},
                                    reduced_motion="reduce")
            page.goto(base, wait_until="networkidle")
            page.wait_for_selector(".film, .tile")
            motion = page.evaluate(
                "() => { const probe = document.createElement('div');"
                " probe.className = 'spinner'; document.body.appendChild(probe);"
                " const s = getComputedStyle(probe);"
                " const others = [...document.querySelectorAll('*')].filter(el =>"
                "  getComputedStyle(el).animationIterationCount === 'infinite');"
                " return {media: matchMedia('(prefers-reduced-motion: reduce)').matches,"
                "  duration: s.animationDuration, probes: others.length}; }")
            check(motion["media"], "the browser reports reduced motion")
            check(motion["duration"] == "2.4s",
                  f"spinner slows under reduced motion ({motion['duration']})")
            check(motion["probes"] <= 1,
                  f"no other infinite animation runs ({motion['probes']} found)")
            page.close()
            browser.close()
    finally:
        server.shutdown()

    return report()


if __name__ == "__main__":
    sys.exit(main())
