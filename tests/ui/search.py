"""Combobox and search-surface checks. ARIA/DOM only.

Run: uv run python tests/ui/search.py
"""

from __future__ import annotations

import sys

from playwright.sync_api import sync_playwright

from harness import check, load_bundle, report, serve

OPTIONS_JS = r"""
() => {
  const input = document.getElementById('q');
  const box = document.getElementById('suggest');
  return {
    role: input.getAttribute('role'),
    expanded: input.getAttribute('aria-expanded'),
    controls: input.getAttribute('aria-controls'),
    autocomplete: input.getAttribute('aria-autocomplete'),
    listRole: box.getAttribute('role'),
    hidden: box.hidden,
    options: [...box.querySelectorAll('.sug')].map(el => ({
      tag: el.tagName, role: el.getAttribute('role'), id: el.id,
      selected: el.getAttribute('aria-selected'),
      key: el.getAttribute('data-key'),
      all: el.getAttribute('data-search-all'),
      text: el.textContent.trim()
    })),
    empty: box.querySelector('.sug-empty')?.textContent.trim() || ''
  };
}
"""


def open_suggest(page) -> None:
    page.fill("#q", "dune")
    page.wait_for_selector("#suggest:not([hidden]) .sug")


def main() -> int:
    bundle = load_bundle()
    server, base = serve()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 390, "height": 844})
            page.goto(base, wait_until="networkidle")
            page.wait_for_selector(".film, .tile")

            # Combobox wiring and option semantics.
            open_suggest(page)
            state = page.evaluate(OPTIONS_JS)
            check(state["role"] == "combobox" and state["listRole"] == "listbox",
                  "input is a combobox and the popup is a listbox")
            check(state["expanded"] == "true" and state["controls"] == "suggest",
                  "expanded state and aria-controls are set")
            check(state["autocomplete"] == "list",
                  "aria-autocomplete is list")
            check(len(state["options"]) == 7,
                  f"six quick hits and a See-all row ({len(state['options'])})")
            check(all(o["tag"] == "DIV" and o["role"] == "option"
                      for o in state["options"]),
                  "every suggestion is a non-interactive role=option row")
            check("See all" in state["options"][-1]["text"] and
                  bool(state["options"][-1]["all"]),
                  "last option opens the full result grid")

            # Arrow keys move aria-activedescendant without moving focus.
            page.press("#q", "ArrowDown")
            page.press("#q", "ArrowDown")
            moved = page.evaluate(
                "() => ({active: document.getElementById('q')"
                ".getAttribute('aria-activedescendant'),"
                " focus: document.activeElement.id,"
                " selected: [...document.querySelectorAll('#suggest .sug')]"
                ".map(el => el.getAttribute('aria-selected'))})")
            check(moved["active"] == state["options"][1]["id"] and moved["focus"] == "q",
                  f"Arrow Down moves the active option ({moved['active']})")
            check(moved["selected"].count("true") == 1 and moved["selected"][1] == "true",
                  f"exactly one option is selected ({moved['selected']})")

            # Escape closes, keeps the query and keeps focus in the input.
            page.press("#q", "Escape")
            closed = page.evaluate(OPTIONS_JS)
            kept = page.evaluate(
                "() => ({value: document.getElementById('q').value,"
                " focus: document.activeElement.id,"
                " active: document.getElementById('q').hasAttribute('aria-activedescendant')})")
            check(closed["hidden"] and closed["expanded"] == "false",
                  "Escape closes the popup")
            check(kept["value"] == "dune" and kept["focus"] == "q" and not kept["active"],
                  f"Escape keeps the query and focus ({kept})")

            # Enter on a highlighted film opens that film.
            open_suggest(page)
            page.press("#q", "ArrowDown")
            chosen = page.evaluate(
                "() => document.querySelector('#suggest .sug')"
                ".getAttribute('data-key')")
            page.press("#q", "Enter")
            page.wait_for_function("location.hash.startsWith('#/film/')")
            check(page.evaluate("() => location.hash") ==
                  "#/film/" + chosen,
                  f"Enter opens the highlighted film ({chosen})")

            # Enter on the See-all row opens the full grid.
            page.goto(base, wait_until="networkidle")
            page.wait_for_selector(".film, .tile")
            open_suggest(page)
            for _ in range(7):
                page.press("#q", "ArrowDown")
            page.press("#q", "Enter")
            page.wait_for_function("location.hash === '#/search/dune'")
            page.wait_for_function("document.title.startsWith('Search:')")
            check(page.evaluate("() => location.hash") == "#/search/dune",
                  "Enter on See all opens #/search/<q>")

            # No match: the popup says so and names the query.
            page.fill("#q", "zzzz")
            page.wait_for_function(
                "() => document.querySelector('#suggest .sug-empty')"
                "?.textContent.includes('zzzz')")
            none = page.evaluate(OPTIONS_JS)
            check(none["empty"].startswith("No films match"),
                  f"no-match popup names the query ({none['empty']!r})")
            check(not none["options"], "no-match popup has no options")

            # Search page: zero results names the query and offers browsing.
            page.goto(base + "#/search/zzzz", wait_until="networkidle")
            page.wait_for_selector(".empty")
            zero = page.evaluate(
                "() => ({h1: document.querySelector('h1')?.textContent || '',"
                " h2: document.querySelector('.empty h2')?.textContent || '',"
                " browse: document.querySelector('.empty a[href=\"#/\"]')?.textContent || ''})")
            check("zzzz" in zero["h1"] and "zzzz" in zero["h2"],
                  f"zero-result page names the query ({zero['h2']!r})")
            check(bool(zero["browse"]), "zero-result page offers a browse link")

            page.goto(base + "#/search/", wait_until="networkidle")
            page.wait_for_selector(".empty")
            empty = page.evaluate(
                "() => ({h2: document.querySelector('.empty h2')?.textContent || '',"
                " browse: document.querySelector('.empty a[href=\"#/\"]')?.textContent || ''})")
            check(empty["h2"] == "Search films" and bool(empty["browse"]),
                  f"empty search invites browsing ({empty['h2']!r})")

            browser.close()
    finally:
        server.shutdown()

    return report()


if __name__ == "__main__":
    sys.exit(main())
