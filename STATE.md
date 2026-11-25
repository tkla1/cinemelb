# STATE — cinemelb build state (handoff document)

> Written to survive a context compaction. If you are resuming, read this first,
> then `git log --oneline | head -20` and `python -m cinemelb doctor`.

## Objective

Find every cinema in Melbourne (multiplex **and** independent), build a scraper
for each, and expose a tool that answers:
- "which cinemas is film X showing at?"
- "what's on at time/date T?"
- "what's closest to location L?"

## Where things stand

| Item | State |
|---|---|
| Registry | 63 venues in `data/cinemas.json` |
| Venues with live data | 46 |
| Screenings | 16,212 |
| Coverage window | 25 Nov 2026 – 31 Jul 2027 |
| Working adapters | village, hoyts, palace, movingstory, nova, sun, acmi, generic_html |
| Missing adapters | reading (agent running), imax (Cloudflare-blocked) |
| CLI | `python -m cinemelb {now,nearest,next,at,stats,doctor,refresh,serve}` |
| Web UI | `python -m cinemelb serve --port 8765` — SSR, stdlib, read-only, all routes 200 |
| Tests | 76 API checks pass; `python -m unittest discover -s tests` |
| Auditor | 40-sample + full-week reconciliation; 4 movingstory bugs found, all fixed |

### Adapter coverage (venues / screenings)

- village 14 / 6,804 — Algolia via `/api/algolia/sessions/hits?f.c=<id>`
- hoyts 12 / 4,470 — JSON API
- palace 9 / 2,845 — `_next/data` JSON
- movingstory 3 / 999 — date-addressed `/now-showing/<date>` pages
- nova 1 / 540 — `/films-by-date-<epoch>` HTML
- sun 1 / 251 — `?wp-cinema-day=N` HTML
- acmi 1 / 34 — Nuxt `__NUXT__` devalue blob
- generic_html 14 / 269 — Veezi / wp-cinema / JSON-LD fallbacks

### Known gaps (17 venues with no data)

- **7 Reading** — `prod-api.readingcinemas.com.au` needs a Cognito Bearer JWT;
  endpoints are `/getcinemas?countryId=1` and `/films?countryId=1&cinemaId=<slug>&status=nowShowing&flag=`.
  Agent `reading-adapter` is hunting the token-minting call. **Branch: `feat/reading-adapter`.**
- **4 hard indies** — brunswick-picture-house, thornbury-picture-house,
  united-craigieburn, waverley-cinemas. Agent `indie-hard` running. **Branch: `feat/indie-hard`.**
- **IMAX** — `digital-api.imaxmelbourne.com.au/v2/experiences/get` behind Cloudflare;
  403 even from the browser. Honest-empty with a note.
- **5 likely-dormant** — rooftop-cinema, forum-theatre, capitol-theatre,
  thornbury-theatre, la-trobe-university-cinema. These are live/theatre venues
  or seasonal; they should be marked *honest empty*, not failed.

## Auditor findings — all fixed in `movingstory.py`

1. **Date shift** — weekday tabs resolved by block index; dropped "Today" tab
   shifted every block one day early. Fixed by using date-addressed pages.
2. **Missing sessions** — only home-page `/movies/` links were followed; now
   `/now-showing/<date>` pages are parsed (recovers JIFF, repertory, one-offs).
3. **HTML entities** — `Disney&#x2019;s Hexed` now decoded to `Disney's Hexed`.
4. **Title fragmentation** — entity fix removes the `disney-and-x2019-s-hexed` key split.

Verified: Classic 26 Nov now has Videodrome 19:00, Tenzing 10:40, Buddy 15:20/21:30.
Zero entity titles remain; zero mojibake.

## Git

Branches: `master`, `feat/quality`, `feat/reading-adapter`, `feat/indie-hard`.
Working tree has uncommitted changes from agents — review before merging.

## Remaining work

1. Collect `reading-adapter` and `indie-hard` results; merge their branches.
2. Mark dormant venues honest-empty with a reason note.
3. Decide IMAX: honest-empty note (recommended) vs. browser workaround.
4. Final end-to-end verification: `python -m cinemelb doctor`,
   `python scripts/verify.py`, `python -m unittest discover -s tests`,
   web UI smoke test.
5. Update README limitations to match reality.

## House rules learned the hard way

- Never `echo;` in PowerShell — use `Write-Output ""`.
- Run long things (>20 s) as background jobs.
- ASCII-only in console output (`scripts/verify.py` uses `OK`/`FAIL`, not tick marks).
- 0.0 km is falsy — never `distance or 1e9` in sort keys.
- Name agreement is required when matching venues to chain platforms; a
  rooftop 200 m from a multiplex must not be absorbed into it.
