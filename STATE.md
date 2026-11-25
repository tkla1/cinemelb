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
| Registry | 63 rows in `data/cinemas.json` (58 active) |
| Venues with live data | **58 of 58 active** |
| Screenings | 19,159 |
| Coverage window | 25 Nov 2026 – 31 Jul 2027 |
| Working adapters | hoyts, village, palace, reading, movingstory, nova, sun, acmi, imax, indy, lumos, waverley, generic_html |
| Missing adapters | none |
| CLI | `python -m cinemelb {film,at,now,near,next,week,whats-on,trending,cinemas,search,stats,doctor,refresh,map,serve}` |
| Web UI | `python -m cinemelb serve --port 8765` — SSR, stdlib, read-only |
| Tests | 41 pass (1 skipped); `python -m unittest discover -s tests` |
| Verification | `python scripts/verify.py` — 38/38 checks pass |

### Adapter coverage (venues / screenings)

- hoyts 12 / 4,422 — JSON API, whole chain in one request
- village 14 / 6,734 — Algolia proxy `/api/algolia/sessions/hits?f.c=<id>`
- palace 9 / 2,823 — `_next/data` JSON keyed by slug
- reading 7 / 2,203 — Cognito Bearer from `/settings`, then `/films?cinemaId=`
- movingstory 3 / 991 — date-addressed `/now-showing/<date>` pages
- nova 1 / 535 — `/films-by-date-<epoch>` HTML
- sun 1 / 247 — `?wp-cinema-day=N` HTML
- **imax 1 / 175** — Vista OCAPI (`/sites`, `/film-screening-dates`, `/showtimes/by-business-date/<day>`)
- indy 2 / 97 — Indy Systems GraphQL
- lumos 1 / 430 — United Craigieburn gasapi
- waverley 1 / 200 — Flicks white-label JSON
- acmi 1 / 34 — Nuxt `__NUXT__` devalue blob
- generic_html 10 / 268 — Veezi / wp-cinema / JSON-LD fallbacks

## IMAX — how the "blocked" venue was actually reachable

The earlier conclusion was wrong. `web.imaxmelbourne.com.au` is a Lumos SPA and
the *legacy* GAS route `/v2/experiences/get` really does return Cloudflare 403.
But the OCAPI routes the same page calls are open to plain `requests` with the
bearer token from the page's own `__NEXT_DATA__`:

- `GET /ocapi/sites` — tenant list (`id` is `IMAX`)
- `GET /ocapi/film-screening-dates?siteIds=IMAX` — business dates
- `GET /ocapi/showtimes/by-business-date/<date>?siteIds=IMAX` — one day's sessions

Lesson: a 403 on one endpoint is not a 403 on the host. Probe the routes the
page itself calls before declaring a venue unscrapable.

IMAX rate-limits hard (Cloudflare 1015 after ~40 rapid requests), so the adapter
runs through the throttled `Http` client and treats a 429 as a failed day rather
than an empty one.

## Defects found while doing the IMAX work (all fixed)

1. **`find_film` truncated a time-sorted list** — a venue whose only session is
   weeks out vanished behind venues with many sessions. IMAX's 70mm run was
   invisible in "where is Dune: Part Three showing?". Every venue's earliest
   session is now reserved before the total cap applies.
2. **ALL-CAPS titles leaked into the catalogue** — IMAX publishes
   `DUNE: PART THREE - IMAX 70MM`; the suffix split the film into its own key and
   the caps overwrote the cased title. Suffixes are stripped to the base title
   (kept in `aka`); `upsert_films` no longer lets an all-caps title overwrite a
   properly cased one.
3. **`refresh()` filtered inactive venues out before `upsert_cinemas`** — the
   registry's `active` flag never reached the database, so 5 dormant venues kept
   appearing in queries. Now 63 rows stored, 58 active.
4. **A bare "dune" query showed 10 screenings** (the 1984/2021 film) with no hint
   that "Dune: Part Three" was playing at 40+ venues. The CLI now names the
   bigger alternative.

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

`master` at `981adaa`. Working tree clean. The three agent branches
(`feat/quality`, `feat/reading-adapter`, `feat/indie-hard`) are all at the
baseline commit and hold no work — master is strictly ahead of them.

## Remaining work

1. Optional: browser-side "watch" / notification feature (not requested).
2. Optional: verify the web UI in a browser after the IMAX changes.

## House rules learned the hard way

- Never `echo;` in PowerShell — use `Write-Output ""`.
- Run long things (>20 s) as background jobs.
- ASCII-only in console output (`scripts/verify.py` uses `OK`/`FAIL`, not tick marks).
- 0.0 km is falsy — never `distance or 1e9` in sort keys.
- Name agreement is required when matching venues to chain platforms; a
  rooftop 200 m from a multiplex must not be absorbed into it.
- A 403 on one endpoint is not a 403 on the host — probe the routes the page
  itself calls.
- A "recount" that disagrees with the adapter may be the broken instrument:
  a 429 read as an empty day made IMAX look like 175 vs 0.
