"""State checks for cards, tiles and sessions. DOM/computed style only.

Run: uv run python tests/ui/states.py
"""

from __future__ import annotations

import sys
from datetime import date
from urllib.parse import quote

from playwright.sync_api import sync_playwright

from harness import check, load_bundle, report, serve


def goto(page, base: str, route: str, selector: str) -> None:
    page.goto(base + route, wait_until="networkidle")
    page.wait_for_selector(selector)


def main() -> int:
    bundle = load_bundle()
    films = bundle["films"]
    screenings = bundle["screenings"]
    dates = bundle["dates"]

    no_score = next(f for f in films
                    if f[10] is None and f[11] is None and f[12] is None)
    no_poster = next(f for f in films if not f[6])
    with_poster = next(f for f in films if f[6])
    long_title = max(films, key=lambda f: len(f[1]))
    future_rows = [row for row in screenings if row[0] >= 1] or screenings

    def film_for(rows):
        return films[rows[0][1]] if rows else None

    sold_film = film_for([r for r in future_rows if r[6] == 1])
    nolink_film = film_for([r for r in future_rows if r[7] == -1 and r[6] == 0])
    linked_film = film_for([r for r in future_rows if r[7] >= 0 and r[6] == 0])
    check(sold_film is not None, "fixture: a future sold-out session exists")
    check(nolink_film is not None, "fixture: a future session without a ticket URL exists")
    check(linked_film is not None, "fixture: a future session with a ticket URL exists")

    # A film/day with more than five distinct start times, for the +N pill.
    by_film_day: dict[tuple[int, int], set[int]] = {}
    for row in future_rows:
        by_film_day.setdefault((row[1], row[0]), set()).add(row[2])
    busy = max(by_film_day.items(), key=lambda item: len(item[1]))
    busy_film, busy_date = films[busy[0][0]], dates[busy[0][1]]
    indie_names = {c[0] for c in bundle["cinemas"] if "independent" in c[5]}
    today_iso = date.today().isoformat()
    indie_date = next(
        dates[s[0]] for s in screenings
        if 0 <= s[0] < len(dates) and dates[s[0]] > today_iso
        and bundle["cinemas"][s[3]][0] in indie_names
    )
    check(len(busy[1]) > 5,
          f"fixture: {busy_film[1]!r} has {len(busy[1])} distinct times on {busy_date}")

    # A day/chain pair with no sessions, for the filtered-empty state. The
    # chain must still run on some other day, so the empty state is caused by
    # the filter rather than by an absent chain.
    empty_date_idx, empty_chain = 0, ""
    for candidate_idx in sorted({row[0] for row in screenings if row[0] >= 1}):
        on_day = {bundle["cinemas"][row[3]][1]
                  for row in screenings if row[0] == candidate_idx}
        elsewhere = {bundle["cinemas"][row[3]][1]
                     for row in screenings if row[0] != candidate_idx}
        choices = sorted((elsewhere - on_day) - {""})
        if choices:
            empty_date_idx, empty_chain = candidate_idx, choices[0]
            break

    server, base = serve()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 390, "height": 844})

            # AC-012: no zero or dash placeholders anywhere in the data.
            zeros = [f[1] for f in films if f[10] == 0 or f[12] == 0]
            check(not zeros, f"no film row ships a zero score ({len(zeros)} found)")

            # No scores -> no chips, no dash, no "0".
            goto(page, base, "#/film/" + quote(no_score[0]), ".film-hero")
            scores = page.evaluate(
                "() => ({chips: document.querySelectorAll('.scores').length,"
                " bits: document.querySelectorAll('.mscore').length,"
                " meta: document.querySelector('.film-meta')?.textContent || ''})")
            check(scores["chips"] == 0 and scores["bits"] == 0,
                  f"film with no scores renders no score chip ({scores})")
            check(not any(tag in scores["meta"] for tag in ("IMDb", "RT ", "MC ")),
                  "no-score meta line names no source")

            goto(page, base, "#/search/" + quote(no_score[1]), ".poster-grid .tile")
            tile_scores = page.evaluate(
                "(title) => [...document.querySelectorAll('.tile')]"
                ".filter(t => t.querySelector('.tile-title')?.textContent.trim() === title)"
                ".map(t => t.querySelectorAll('.tile-scores').length)", no_score[1])
            check(tile_scores and max(tile_scores) == 0,
                  "search tile for a no-score film renders no score line")

            # AC-013: a missing cover reserves the same box as a real one.
            goto(page, base, "#/film/" + quote(no_poster[0]), ".poster-lg")
            fallback = page.evaluate(
                "() => { const p = document.querySelector('.poster-lg');"
                " const f = p.querySelector('.poster-fallback');"
                " const r = p.getBoundingClientRect();"
                " return {w: Math.round(r.width), h: Math.round(r.height),"
                "  img: p.querySelectorAll('img').length,"
                "  fallbackText: f ? f.textContent.trim() : '',"
                "  position: f ? getComputedStyle(f).position : ''}; }")
            check(fallback["img"] == 0 and bool(fallback["fallbackText"]),
                  f"film with no cover renders its initials ({fallback['fallbackText']!r})")
            check(fallback["position"] == "absolute",
                  "fallback is absolutely positioned inside the poster box")
            goto(page, base, "#/film/" + quote(with_poster[0]), ".poster-lg")
            covered = page.evaluate(
                "() => { const r = document.querySelector('.poster-lg')"
                ".getBoundingClientRect();"
                " return {w: Math.round(r.width), h: Math.round(r.height)}; }")
            check((fallback["w"], fallback["h"]) == (covered["w"], covered["h"]),
                  f"cover and fallback reserve one box ({fallback['w']}x{fallback['h']})")

            # Long title: two-line clamp inside the tile, no overflow.
            goto(page, base, "#/search/" + quote(long_title[1]), ".poster-grid .tile")
            clamped = page.evaluate(
                "(title) => { const t = [...document.querySelectorAll('.tile-title')]"
                ".find(e => e.textContent.trim() === title);"
                " if (!t) return null;"
                " const r = t.getBoundingClientRect();"
                " const next = t.parentNode.children[1];"
                " const n = next ? next.getBoundingClientRect() : null;"
                " return {clamp: getComputedStyle(t).webkitLineClamp,"
                "  lines: Math.round(r.height / parseFloat(getComputedStyle(t).lineHeight)),"
                "  overlap: n ? Math.round(r.bottom - n.top) : 0,"
                "  overflowX: t.scrollWidth - t.clientWidth}; }", long_title[1])
            check(clamped is not None, "long-title film has a search tile")
            check(clamped and clamped["clamp"] == "2",
                  f"long title clamps to two lines ({clamped['clamp']})")
            check(clamped and clamped["lines"] <= 2,
                  f"long title occupies at most two lines ({clamped['lines']})")
            check(clamped and clamped["overlap"] <= 1 and clamped["overflowX"] <= 1,
                  f"long title does not overlap the next line (overlap={clamped['overlap']})")

            # AC-014: sold out is dashed, struck through, labelled and unlinked.
            if sold_film:
                goto(page, base, "#/film/" + quote(sold_film[0]), ".sessions")
                sold = page.evaluate(
                    "() => { const s = document.querySelector('.session.is-sold');"
                    " if (!s) return null;"
                    " const t = s.querySelector('.time');"
                    " return {tag: s.tagName, href: s.getAttribute('href'),"
                    "  border: getComputedStyle(s).borderStyle,"
                    "  strike: getComputedStyle(t).textDecorationLine,"
                    "  tagText: s.querySelector('.sold-tag')?.textContent.trim() || ''}; }")
                check(sold is not None, "film page shows a sold-out session")
                check(sold and sold["tag"] == "SPAN" and sold["href"] is None,
                      "sold-out session is not a link")
                check(sold and sold["border"] == "dashed" and "line-through" in sold["strike"],
                      f"sold-out is dashed and struck through ({sold})")
                check(sold and sold["tagText"] == "Sold out",
                      "sold-out session is labelled Sold out")

            if nolink_film:
                goto(page, base, "#/film/" + quote(nolink_film[0]), ".sessions")
                plain = page.evaluate(
                    "() => { const s = [...document.querySelectorAll('span.session')]"
                    ".find(x => !x.classList.contains('is-sold'));"
                    " return s ? {href: s.getAttribute('href'),"
                    "  time: s.querySelector('.time')?.textContent || ''} : null; }")
                check(plain is not None and plain["href"] is None and bool(plain["time"]),
                      f"session without a ticket URL renders as a time pill ({plain})")

            # The time display is a table; one link per row, and that link says
            # "Tickets" so the affordance is visible before hovering.
            if linked_film:
                goto(page, base, "#/film/" + quote(linked_film[0]), ".session-table")
                table = page.evaluate(
                    "() => { const t = document.querySelector('.sessions .session-table');"
                    " if (!t) return null;"
                    " const rows = [...t.querySelectorAll('tbody tr')];"
                    " return {headers: [...t.querySelectorAll('thead th')]"
                    "   .map(h => h.textContent.trim()),"
                    "  cap: t.querySelector('caption')?.textContent.trim() || '',"
                    "  rows: rows.length,"
                    "  links: rows.map(tr => tr.querySelectorAll('a').length),"
                    "  cues: t.querySelectorAll('a.session .st-buy').length,"
                    "  labels: [...t.querySelectorAll('a.session')]"
                    "   .map(a => a.getAttribute('aria-label') || ''),"
                    "  addrs: document.querySelectorAll('.day-block .venue-addr').length,"
                    "  actions: [...document.querySelectorAll('.day-block .venue-link')]"
                    "   .map(a => a.getAttribute('aria-label') || '')}; }")
                check(table is not None, "film page renders a session table")
                check(table and table["headers"] == ["Time", "Format & screen"],
                      f"session table names its two columns ({table and table['headers']})")
                check(table and table["rows"] > 0 and max(table["links"]) <= 1,
                      "each session row has at most one link")
                check(table and table["cues"] == table["links"].count(1),
                      f"linked rows show the Tickets cue ({table and table['cues']})")
                check(table and all(l.startswith("Tickets for ") for l in table["labels"]),
                      "ticket links name the time, venue and day")
                check(table and table["addrs"] == 0,
                      "day venue blocks omit the repeated street address")
                check(table and table["actions"] and all(table["actions"]),
                      f"venue actions are named icon buttons ({table and len(table['actions'])})")

            # +N: five time pills then an overflow count.
            goto(page, base, f"#/day/{busy_date}", ".films .film")
            idx = page.evaluate(
                "(key) => window.CINEMELB.state.films.findIndex(f => f[0] === key)",
                busy_film[0])
            card = page.evaluate(
                "(i) => { const c = document.querySelector('.film[data-film=\"' + i + '\"]');"
                " if (!c) return null;"
                " return {times: c.querySelectorAll('.film-times .time').length,"
                "  more: c.querySelector('.more')?.textContent.trim() || ''}; }", idx)
            check(card is not None, "busy film has a card on its day")
            check(card and card["times"] == 5,
                  f"card shows five time pills ({card and card['times']})")
            check(card and card["more"].startswith("+") and card["more"][1:].isdigit(),
                  f"overflow pill counts the rest ({card and card['more']!r})")

            # Cinema-type filter: the independent half is tagged in the bundle,
            # and the chain list narrows with it.
            goto(page, base, f"#/day/{indie_date}", ".films .film")
            page.select_option("#cinema-type", "indie")
            page.wait_for_selector(".films .film")
            page.evaluate("() => { document.querySelector('.films .film').open = true; }")
            kind = page.evaluate(
                "() => { const st = window.CINEMELB.state;"
                " const indie = new Set(st.cinemas"
                "   .filter(c => c[5].indexOf('independent') >= 0).map(c => c[0]));"
                " return {shown: [...document.querySelectorAll("
                "   '.films .film .venue-name')].map(e => e.textContent.trim()),"
                "  indie: [...indie],"
                "  chains: [...document.querySelectorAll('#chain option')]"
                "   .map(o => o.value).filter(Boolean)}; }")
            check(kind["shown"] and all(v in kind["indie"] for v in kind["shown"]),
                  f"independent filter shows only indie venues ({kind['shown']})")
            check("Hoyts" not in kind["chains"],
                  f"chain list narrows with the cinema type ({kind['chains']})")

            # AC-015: filters that hide a whole day name the hidden count and clear.
            goto(page, base, f"#/day/{dates[empty_date_idx]}", ".films .film")
            check(bool(empty_chain), f"fixture: chain {empty_chain!r} has no sessions that day")
            if empty_chain:
                page.select_option("#chain", empty_chain)
                page.wait_for_selector(".empty h2")
                empty = page.evaluate(
                    "() => ({heading: document.querySelector('.empty h2')?.textContent || '',"
                    " body: document.querySelector('.empty p')?.textContent || '',"
                    " clear: !!document.querySelector('.empty [data-clear]')})")
                check("No sessions match those filters" in empty["heading"],
                      f"filtered day names the filter empty ({empty['heading']!r})")
                check("hidden by the current filters" in empty["body"],
                      "filtered day says how many sessions the filters hid")
                check(empty["clear"], "filtered day offers a Clear control")
                page.click(".empty [data-clear]")
                page.wait_for_selector(".films .film")
                check(page.evaluate("() => document.querySelectorAll('.films .film').length") > 0,
                      "clearing the filter restores the day")

            browser.close()
    finally:
        server.shutdown()

    return report()


if __name__ == "__main__":
    sys.exit(main())
