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
| Screenings | 19,091 |
| Coverage window | 25 Nov 2026 – 31 Jul 2027 |
| Working adapters | hoyts, village, palace, reading, movingstory, nova, sun, acmi, imax, indy, lumos, waverley, generic_html |
| Missing adapters | none |
| CLI | `python -m cinemelb {film,at,now,near,next,week,whats-on,trending,cinemas,search,stats,doctor,refresh,map,serve,export}` |
| Web UI | `python -m cinemelb serve --port 8765` — SSR, stdlib, read-only |
| Static site | **https://cinemelb.pages.dev** — `python -m cinemelb export` → Cloudflare Pages |
| Tests | 45 pass (1 skipped); `python -m unittest discover -s tests` |
| Verification | `python scripts/verify.py` — 51/51 checks pass |

### Static site

`cinemelb export --out dist` writes a self-contained SPA: `index.html`, two
content-hashed assets, `_headers`, `robots.txt`, and `data/bundle.json`
(19,057 sessions · 393 films · 58 cinemas · through 31 Jul 2027; 1.8 MB raw,
~170 KB gzipped). Deployed to the Cloudflare Pages project `cinemelb`:

```bash
python -m cinemelb refresh
python -m cinemelb export --out dist
npx wrangler pages deploy dist --project-name cinemelb --branch main
```

`dist/` is gitignored — it is a build artifact of `data/cinemelb.sqlite3`.
`cinemelb/web/` is the source shell and is committed (`.gitignore` had a blanket
`*.html` rule that silently excluded the shell's `index.html`; there is now an
exception for `cinemelb/web/*.html`).

Bugs found and fixed while finishing the SPA (all verified in a browser against
the built bundle and again against the live deploy):

1. `boot()` called `tickClock()` before `loadBundle()`, and `tickClock` read
   `S.bundle.dates` — the page threw on first paint and never loaded the data.
2. The chain filter read `.chain` on a cinema **row array** (always `undefined`),
   so choosing any chain emptied the page.
3. `isPast()` only considered today's rows, so yesterday's sessions still
   appeared in a film's "all sessions" list. It now compares the date index.
4. `render()` read `S.route` after rendering the rail, leaving the highlighted
   day one click behind.
5. Replaced the stylesheet, which had been written for an older `app.js` and no
   longer matched the markup (the rewrite died mid-step; see Git below).
6. Smaller: duplicate start times in the card time row, duplicated session
   notes (`IMAX 3D … 3D`), "nothing left today" shown when a filter hid the
   sessions, missing `#foot-meta` counts, no-op suggestion list, no `/` focus
   shortcut, low-contrast active day in light mode.
7. Poster URLs were not always URLs. Palace publishes `posterOverride` as a
   bare CMS filename (`BFF26 Parky 345x500.gif`) and Cinema Nova has
   site-relative teaser images (`/media/Webtiles/…`), so 20 films asked the
   static host for `/BFF26 Parky 345x500.gif` and 404ed. The Palace adapter now
   resolves those through its `/poster/` endpoint (verified 200 for all 12) and
   Nova resolves against its own site.

Grouping and time filters were added after the location work. A day holds ~1,900
sessions, so the listing pivots three ways — by film (default), by cinema (one
expandable card per venue) and by time (film cards split into morning /
afternoon / evening / late night). A film page offers the same pivot: by day,
by cinema or by time. Two session-level filters sit alongside the existing chips:
**After** a time of day, and **Within** a drive time once a location is set.
"Leaves time to get there" is now travel-aware — it uses the drive estimate plus
20 minutes rather than a flat 20. The grouping choice is a view preference and
is persisted in `localStorage`; filters are in memory only. Clearing the location
also clears the drive-time filter, so a hidden control cannot keep hiding
sessions.

Three follow-ups after using it on a phone: the time-of-day sections are now
`<details open>` and collapse on a tap; sessions starting before 06:00 are
shifted onto the previous listing day (`minute + 1440`, `date - 1` at index time,
rows on the first day dropped), so a 00:31 on the 27th is the 26th's Late night
and `isPast`/`isTooSoon` compare absolute minutes; and the grouping control moved
into the day/section heading row, with tighter padding and a shortened mobile
count, cutting the chrome above the first card from ~371 px to ~337 px on an
iPhone 13 viewport. Verified in Chrome at both sizes with no console errors.

Bundle v2 adds venue and rating fields: cinema rows are
`[name, chain, suburb, address, website, tags, lat, lon, phone, booking_url]` and
film rows append `[imdb_id, imdb, rt, mc, rt_url, mc_url]`. Every film card now
carries an arrow into its own page, every venue line shows its address plus a
Google Maps "Directions" link (and site/booking/phone when known), and ratings
render only when present. Scores are build-time data: `python -m cinemelb ratings`
fills `data/ratings.json` (local IMDb bulk join, Wikidata fallback, OMDb for
RT/MC when keyed, opt-in direct RT/MC parsing), and `export` bakes whatever is
cached into the bundle. See `docs/api-reference/` for sources and terms.

First full ratings pass (26 Nov, no OMDb key): 248 of 393 films resolved to an
IMDb id, **218 IMDb scores**, **158 Metascores** via the keyless Metacritic
finder, 0 Rotten Tomatoes, 0 failures. RT needs either `OMDB_API_KEY` or
`--scrape-rt-mc`; `AKIRA` resolved to `tt0094625` (1988), which is the case the
confidence gate exists for. The datasets live in `data/cache/` (755 MB, cached
and pickled); `data/ratings.json` is gitignored and rebuilt by the command.

A Cloudflare Pages *probe* project (`cinemelb-spa-probe`) still exists from the
hosting experiment; it is unreferenced and can be deleted from the dashboard.

**Location.** The SPA has a "Near me" bar: a typed suburb/address or the
browser's geolocation, resolved against a `places` gazetteer in the bundle
(83 landmarks + every venue suburb) with a Nominatim fallback for street
addresses. With a place set, the day sorts by distance, each film leads with
`1.6 km · ~4 min`, and every venue on a film page shows `km · ~min drive`.
Distances are great-circle and time divides by 22 km/h (`DRIVE_KMH`), matching
`cinemelb.geo.SPEED_KMH["drive"]` so the page and CLI agree.

iOS was handled explicitly after it "did nothing" in testing: the top-left brand
mark was a circle with crosshair ticks, which read as a second locate button and
swallowed taps (it is a film-strip now, and the locate control is a map pin);
`#q`/`#near-q` are exactly 16px so Safari does not zoom on focus; the request
checks `isSecureContext`, names the iOS permission path on `code 1`, and puts a
15-second watchdog on a prompt that never answers (in-app browsers). Verified in
Chrome at iPhone 13 size: grant, denial message, silent-prompt watchdog, and a
single locate control.

### Adapter coverage (venues / screenings)

- hoyts 12 / 4,422 — JSON API, whole chain in one request
- village 14 / 6,711 — Algolia proxy `/api/algolia/sessions/hits?f.c=<id>`
- palace 9 / 2,800 — `_next/data` JSON keyed by slug
- reading 7 / 2,193 — Cognito Bearer from `/settings`, then `/films?cinemaId=`
- movingstory 3 / 990 — date-addressed `/now-showing/<date>` pages
- nova 1 / 530 — `/films-by-date-<epoch>` HTML
- sun 1 / 245 — `?wp-cinema-day=N` HTML
- **imax 1 / 175** — Vista OCAPI (`/sites`, `/film-screening-dates`, `/showtimes/by-business-date/<day>`)
- indy 2 / 95 — Indy Systems GraphQL
- lumos 1 / 429 — United Craigieburn gasapi
- waverley 1 / 200 — Flicks white-label JSON
- acmi 1 / 34 — Nuxt `__NUXT__` devalue blob
- generic_html 10 / 267 — Veezi / wp-cinema / JSON-LD fallbacks

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

## Defects found while exercising the CLI against the real database (all fixed)

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
5. **`nearest` let one megaplex fill every slot** — it truncated a distance-sorted
   list with `hits[:limit]`, so asking for 8 sessions near Melbourne Central
   returned 8 sessions at Chinatown Cinema and nothing else within 2 km. It now
   uses the same breadth-first truncation as `find_film`; the per-venue cap
   scales with the limit (`limit=200` returns 200, not the 64 a fixed cap of 8
   allowed).
6. **Display mixed title casing** — Hoyts publishes `THE HUNGER GAMES: SUNRISE
   ON THE REAPING` while the catalogue holds the cased form, so result tables
   and the web page shouted for some rows and not others. `Hit.film` now returns
   the catalogue spelling; the raw row title is unchanged in the database.

## Auditor findings — all fixed in `movingstory.py`

1. **Date shift** — weekday tabs resolved by block index; dropped "Today" tab
   shifted every block one day early. Fixed by using date-addressed pages.
2. **Missing sessions** — only home-page `/movies/` links were followed; now
   `/now-showing/<date>` pages are parsed (recovers JIFF, repertory, one-offs).
3. **HTML entities** — `Disney&#x2019;s Hexed` now decoded to `Disney's Hexed`.
4. **Title fragmentation** — entity fix removes the `disney-and-x2019-s-hexed` key split.
5. **Sold-out sessions were dropped** — a sold-out performance renders as a bare
   `<a class="Link sessions-link" data-id="..." ...>` with **no href**, and the
   session regex required one. Every sold-out Moving Story session was silently
   missing. The href is now optional and `sold_out` is set from the
   `Attribute SoldOut` span. Confirmed by the Classic Live Comedy 20:00 session
   on 2026-11-27, which is now stored with `sold_out=1`.

Verified by the independent auditor (`_recon/audit.py`, 40/40 samples pass):
movingstory coverage is now **1/744 absent** (was 94/751) and 0 date-shifted.
All four platforms audited (hoyts, village, palace, movingstory) are rated
"Trustworthy".

## Git

`master` at `9ed0c07` (static site: exporter, shell, deploy). The three agent
branches (`feat/quality`, `feat/reading-adapter`, `feat/indie-hard`) are all at
the baseline commit and hold no work — master is strictly ahead of them. The
public repo is `https://github.com/tkla1/cinemelb`.

## Remaining work

The objective is met: every active venue is scraped, every query shape the user
asked for works against live data, and the results have been reconciled against
the venues' own pages. Nothing is known-broken.

Optional, not requested:
1. A "watch" / notification feature (e-mail or desktop when a session appears).
2. Retire the `_recon/` scripts into a proper `scripts/audit.py` if the audit is
   to be re-run routinely.

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
- A sold-out session may have **no booking link at all** — if the parser
  requires an `href` on the session anchor, it silently drops every full house.
- An auditor that re-uses a cached snapshot re-audits yesterday's database.
  `_recon/audit.py` skipped regeneration whenever `audit_snapshot.json` existed,
  so it kept reporting a 94/751 gap long after the adapter was fixed.
