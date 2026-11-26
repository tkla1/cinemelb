"""Alias search: a published programme title still finds the cleaned film.

Run: uv run python tests/ui/alias.py
"""

from __future__ import annotations

import sys
from urllib.parse import quote

from playwright.sync_api import sync_playwright

from harness import check, load_bundle, report, serve


def main() -> int:
    bundle = load_bundle()
    check(bundle.get("version") == 3, f"bundle is version 3 ({bundle.get('version')})")
    aliases = bundle.get("aliases") or {}
    films = {row[0]: row for row in bundle["films"]}
    check(bool(aliases), f"bundle ships aliases for {len(aliases)} films")
    check(set(aliases) <= set(films), "every alias key is an exported film")
    check(all(isinstance(v, list) and v and all(isinstance(s, str) and s.strip()
                                                for s in v)
              for v in aliases.values()),
          "every alias list holds non-empty strings")

    titles = [row[1].lower() for row in bundle["films"]]

    def alias_token(key: str) -> str:
        for alias in aliases[key]:
            for token in alias.replace(":", " ").split():
                if len(token) >= 3 and token.lower() not in " ".join(titles):
                    return token
        return ""

    candidates = [(key, alias_token(key)) for key in aliases]
    key, token = next(((k, t) for k, t in candidates if t), ("", ""))
    check(bool(key and token),
          f"fixture: an alias token no title contains ({key!r} -> {token!r})")
    if not key:
        return report()

    film = films[key]
    server, base = serve()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 390, "height": 844})
            page.goto(base, wait_until="networkidle")
            page.wait_for_selector(".film, .tile")

            # The dropdown explains why the row matched.
            page.fill("#q", token)
            page.wait_for_selector("#suggest:not([hidden]) .sug")
            options = page.evaluate(
                "() => [...document.querySelectorAll('#suggest .sug')].map(el => ({"
                " key: el.getAttribute('data-key'),"
                " text: el.textContent.trim(),"
                " meta: el.querySelector('.sug-meta')?.textContent.trim() || ''}))")
            hit = next((o for o in options if o["key"] == key), None)
            check(hit is not None,
                  f"{token!r} suggests the film listed under it ({film[1]!r})")
            check(hit is not None and "listed as" in hit["meta"],
                  f"the suggestion names the published title ({hit and hit['meta']!r})")

            # The full grid contains the film too.
            page.goto(base + "#/search/" + quote(token), wait_until="networkidle")
            page.wait_for_selector(".poster-grid .tile")
            hrefs = page.evaluate(
                "() => [...document.querySelectorAll('.poster-grid .tile')]"
                ".map(a => a.getAttribute('href'))")
            check("#/film/" + quote(key) in hrefs,
                  f"the search grid includes {film[1]!r}")
            check(all(h.startswith("#/film/") for h in hrefs),
                  "alias hits are ordinary film links")

            # A real title match outranks the alias match for the same film.
            page.goto(base + "#/search/" + quote(film[1]), wait_until="networkidle")
            page.wait_for_selector(".poster-grid .tile")
            first = page.get_attribute(".poster-grid .tile", "href")
            check(first == "#/film/" + quote(key),
                  f"the title match ranks first ({first})")

            browser.close()
    finally:
        server.shutdown()

    return report()


if __name__ == "__main__":
    sys.exit(main())
