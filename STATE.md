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
| Screenings | 19,057 |
| Coverage window | 25 Nov 2026 – 31 Jul 2027 |
| Working adapters | hoyts, village, palace, reading, movingstory, nova, sun, acmi, imax, indy, lumos, waverley, generic_html |
| Missing adapters | none |
| CLI | `python -m cinemelb {film,at,now,near,next,week,whats-on,trending,cinemas,search,stats,doctor,refresh,normalise,map,ratings,posters,serve,export}` |
| Web UI | `python -m cinemelb serve --port 8765` — SSR, stdlib, read-only |
| Static site | **https://cinemelb.pages.dev** — `python -m cinemelb export` → Cloudflare Pages |
| Tests | 165 pass (1 skipped); `uv run python -m unittest discover -s tests` |
| Verification | `uv run python scripts/security_gate.py` + the SPEC §9 ladder |

### Static site

`cinemelb export --out dist` writes a self-contained SPA: `index.html`, two
content-hashed assets, `_headers`, `robots.txt`, and `data/bundle.json`
(19,057 sessions · 390 films · 58 cinemas · through 31 Jul 2027; 1.9 MB raw,
~238 KB gzipped). Deployed to the Cloudflare Pages project `cinemelb`:

```bash
python -m cinemelb refresh
python -m cinemelb ratings --scrape-rt-mc
python -m cinemelb posters
python -m cinemelb export --out dist
npx wrangler pages deploy dist --project-name cinemelb --branch main
```

For a committed release, `uv run python scripts/release.py` is the deterministic
gate: it refuses a dirty tree, runs the full T0-T3 ladder (including all five
Playwright suites), rebuilds `dist`, pushes the current branch, deploys with a
pinned wrangler, and polls the production alias until its hashed assets match.
`--dry-run` runs every check and stops before push/deploy.

`dist/` is gitignored — it is a build artifact of `data/cinemelb.sqlite3`.
`cinemelb/web/` is the source shell and is committed (`.gitignore` had a blanket
`*.html` rule that silently excluded the shell's `index.html`; there is now an
exception for `cinemelb/web/*.html`).

### Cinema type and Rotten Tomatoes (2026-11-26, phase 9)

- The filter bar now has **All cinemas / Cinema chains / Independent cinemas**.
  The exporter derives an `independent` tag on each bundle cinema row from
  `CHAIN_BRANDS = {Hoyts, Village, Reading, Palace, IMAX}` (`export._cinema_tags`);
  `_validate` rejects a missing/duplicated tag, so the filter cannot silently
  turn a multiplex into an indie. The chain dropdown only lists brands in the
  chosen half.
- OMDb no longer returns a Tomatometer (`Ratings` carries IMDb + Metacritic
  only; `tomatoes=true` is `N/A`). `ratings --scrape-rt-mc` now joins IMDb IDs
  to Wikidata's RT IDs (`P345` -> `P1258`) in five batched SPARQL requests
  (`_wikidata_rt_ids`, 177/239 mapped), then reads the film page's scorecard
  with the existing year gate. RT's site search is robots-disallowed and is not
  used; generated slug guesses remain a fallback. Still an explicit opt-in
  because RT/MC terms forbid automated collection.
- 2026-11-26 build: **184 of 390 films carry a Tomatometer** (217 IMDb, 154
  Metacritic) after the P1258 join plus slug-guess fallback. The OMDb key is
  optional for MC coverage; the key is a secret, keep it in `.env`
  (`OMDB_API_KEY`), never in the bundle.

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

Bundle v3 adds the `aliases` map (`film key -> published titles`) on top of v2:
search matches the listing strings kept in `aka`, so programme codes still find
the cleaned film (D-016). Bundle v2 adds venue and rating fields: cinema rows are
`[name, chain, suburb, address, website, tags, lat, lon, phone, booking_url]` and
film rows append `[imdb_id, imdb, rt, mc, rt_url, mc_url]`. Every film card now
carries an arrow into its own page, every venue line shows its address plus a
Google Maps "Directions" link (and site/booking/phone when known), and ratings
render only when present. Scores are build-time data: `python -m cinemelb ratings`
fills `data/ratings.json` (local IMDb bulk join, Wikidata fallback, OMDb for
IMDb/MC when keyed, opt-in direct RT/MC parsing), and `export` bakes whatever
is cached into the bundle. See `docs/api-reference/` for sources and terms.

First full ratings pass (26 Nov, no OMDb key): 248 of 393 films resolved to an
IMDb id, **218 IMDb scores**, **158 Metascores** via the keyless Metacritic
finder, 0 Rotten Tomatoes, 0 failures. RT needs `--scrape-rt-mc` (OMDb no
longer returns it); `AKIRA` resolved to `tt0094625` (1988), which is the case
the confidence gate exists for. The datasets live in `data/cache/` (755 MB, cached
and pickled); `data/ratings.json` is gitignored and rebuilt by the command.

**Covers and sorting (26 Nov follow-up).** Palace's `/poster/<name>` endpoint
answers every filename with the same Palace Cinemas logo, so films at its nine
venues were showing the house advert as a cover, and only 192 of 393 films had
any cover at all. `python -m cinemelb posters` now resolves covers from IMDb's
public search-suggestion endpoint into `data/posters.json` (gitignored),
accepting an exact normalised title, a near-exact title with a year within one,
or the ratings pass's IMDb id; Palace `/poster/` URLs are treated as
placeholders and dropped. Result: **287 IMDb covers + 31 real venue covers =
318 of 393 films**, and 16 of today's 17 films have a real cover; the remaining
75 fall back to an initials tile. The card shows a 44×66 cover and puts
`IMDb 7.5 · MC 55` in its meta line, the film page shows a 132×198 (96×144 on
mobile) cover, and a broken/missing image degrades to the initials tile. A
**Sort** select beside the grouping control offers Soonest / Top rated /
Title A–Z / Nearest (the last only with a place set); Top rated averages the
available scores normalised to 0–1, unrated films last. Both the sort and the
grouping are persisted in `localStorage`. Metascore `0` (Metacritic's "no
reviews yet" sentinel) is dropped at export, so no card shows a bogus `MC 0`.

**Poster grid + full search (26 Nov, second follow-up).** The day listing has a
layout toggle beside Sort: the list, or a responsive poster grid (3 columns on
a phone, up to 8 on desktop). Tiles carry the cover, scores and next session;
the whole tile links to the film page. Grid/list is stored with the grouping in
`cinemelb-view`. Pressing Enter in the header search now routes to
`#/search/<query>` — a full results page with every match as poster tiles,
Best match / Top rated / Title A–Z sorting, and the day filters and location
bar hidden (they describe sessions, not films). The header dropdown stays a
six-item shortcut and adds a **See all N results** row. Search-result tiles
show the date when the next session is not today; on phones the cinema count is
dropped so the next time fits.

**Title hygiene (26 Nov).** Listings inject the programme into the film title:
`BFF26 Parky`, `JIFF: Paper Tiger`, `Cult Classix: Suspiria (4k)`,
`Astor: The Last Waltz`, `CASFFA: …`, `Jodofest: …`. Those prefixes split one
film from its screenings elsewhere and broke scores/covers, which are keyed by
title. `cinemelb/titles.py` strips the programme/venue prefix before the key is
built, keeps the published string in `aka`, and `Store.normalise_films()` (run
by every `refresh`, or manually via `cinemelb normalise`) renames and merges
existing rows and moves their screenings. Result: 45 prefixed titles cleaned,
390 films after 3 merges, 0 orphans, 0 duplicates, and the ratings cache still
resolves because its keys were already prefix-free.

**Spec-driven UI harness (26 Nov).** `SPEC.md` is now the authoritative UI spec
(problem, constraints, data contracts, required states, 39 acceptance criteria,
decision log, phase plan). It was produced with the reusable
`spec-driven-design` skill and the prior design-tooling research. Phase 1 is
complete: `cinemelb/web/tokens.json` -> generated block in `app.css` (25 tokens,
`scripts/build_tokens.py`), `components.json` (18 components), `gallery.html`
driven by the real `window.CINEMELB.render` functions, and the T0/T1 checks
(`check_tokens`, `check_contrast`, `check_manifest`, `check_bundle`,
`check_budget`). Score colours and focus rings moved from literals to tokens;
`--text-3` was raised to meet AA.

Phases 2-10 are also complete: card/tile/session states, control sizes and the
token focus ring, live-count announcements and reduced motion, combobox option
semantics, the film-page groupings, alias search (bundle v3), and film-page
continuity (phase 10: time-bucket scope links, the session table, icon venue
actions, Time/Distance cinema sort). The structural gate is four Playwright
scripts (`layout`, `states`, `search`, `film`) plus `alias`, all under
`tests/ui/` and run with plain `uv run python` (Playwright is a dev dependency).
They measure geometry, computed style, focus and ARIA at 320/390/1280 and never
screenshot. Current build: bundle 238.4 KB gz, JS 25.1 KB gz, CSS 7.1 KB gz,
HTML 1.8 KB gz; 165 unit tests; text contrast 4.5:1 in both themes. Border
contrast is a recorded phase-3 warning (D-010). `components.json` is the
agent's index; screenshots are reserved for appearance questions.

**Security gate (26 Nov).** `uv run python scripts/security_gate.py` is the one
deterministic, offline check: gitleaks scans the publishable snapshot and every
commit, ruff's flake8-bandit rules cover Python sinks, the three reviewed JS
sinks are checked directly, and the project invariants + lock + bundle follow.
It runs in about 1.1s and exits 2 rather than pass if a scanner cannot run.
Dependency CVEs are deliberately an online companion (`uvx pip-audit`), not part
of the fast gate. Design and rejected alternatives: `docs/security-design.md` §9.

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
