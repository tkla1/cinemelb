# Spec: cinemelb — static showtimes UI and design system

Status: active | Owner: project maintainer (agent-authored) | Updated: 2026-11-26
Spec version: 7 | Supersedes: 6

This is the project's single authoritative spec for the web UI. `docs/plan-2026-11-26.md`
is a historical feature plan (ratings/posters), not a rival spec. The skill default path
`docs/spec.md` was overridden by the requesting session; the authority lives at repo root.

## 0. Discovery block (Phase A)

```
INSTRUCTION FILES: README.md (product, deploy, bundle shape), STATE.md (handoff, bundle v2
  fields, defect history), docs/plan-2026-11-26.md (ratings/posters decisions),
  docs/api-reference/*.md (source terms), AGENTS.md (project rules added with this spec),
  C:\Users\alyosha\AGENTS.md + global AGENTS.md.
  No prior docs/spec.md. Rules that constrain UI work: (1) zero-build — no bundler,
  no npm build, no framework; (2) Python only through `uv run`, never bare python;
  (3) screenshots answer appearance questions only; structure is verified by aria/DOM
  checks; (4) the spec is authoritative for UI work.

ARCHITECTURE: vanilla JS IIFE — cinemelb/web/app.js (~103,000 B), one stylesheet app.css
  (~35,000 B), one shell index.html (~4,848 B). Zero-build. Entry cinemelb/web/index.html.
  Hash routing: #/ (today), #/day/<iso>, #/film/<key>[?time=<bucket>], #/search/<q>.
  Build: cinemelb/export.py::write_site -> dist/{index.html, assets/app.<hash>.{css,js},
  _headers, robots.txt, data/bundle.json}. Render functions are private to the IIFE, with a
  frozen window.CINEMELB.render namespace for the gallery.

CONSTRAINTS: Cloudflare Pages static (D-019: unmetered bandwidth, 200-status SPA fallback,
  `_headers` honoured), no server, no CSP header, hashed assets immutable
  (1 year), bundle cached 5 min + revalidate, nosniff + strict-origin-referrer.
  No offline/SW. Runtime network: one Nominatim geocode on explicit typed address, plus
  remote poster images (IMDb CDN / venue hosts). Browser matrix: iOS Safari 16+,
  Android Chrome, desktop Chrome/Edge, Firefox ESR. Budgets in §8.

SPEC: this file (SPEC.md), version 7. It is the only UI spec.

UI INVENTORY: 4 routes + shared chrome (header/search, day rail, filter bar, near bar,
  footer). Components exist as functions in app.js: renderRail, renderFilters, renderNear,
  viewBar, sortSelect, layoutToggle, cinemaSortBar, startingSoon, filmCard, posterTile,
  dayByFilmGrid, dayByCinema, cinemaCard, dayByTime, venueBlocks, venueActions, sessions
  (session table), scoreChips, renderFilm, renderSearch, renderSuggest. One CSS file; no
  per-component CSS; tokens now generated from tokens.json; components.json + gallery.html
  added with this spec.

DESIGN SYSTEM: cinemelb/web/tokens.json -> generated block in app.css; components.json;
  cinemelb/web/gallery.html. See §7.

VERIFY COMMANDS: `uv run python -m unittest discover -s tests` -> 165 tests, OK
  (skipped=1), verified 2026-11-26; `node --check cinemelb/web/app.js` -> exit 0, verified;
  `uv run python -m cinemelb export --out dist` -> bundle build + _validate();
  `uv run python scripts/release.py --dry-run` -> full release gate without push/deploy,
  verified 2026-11-26;
  `uv run python scripts/check_tokens.py --coverage --literals --motion`,
  `check_contrast.py`, `check_manifest.py`, `check_bundle.py --titles --against-db`,
  `check_budget.py dist`, `security_gate.py` (gitleaks + ruff + JS sinks + invariants)
  -> all green (security phase). `python
  scripts/verify.py` -> live adapters, not UI. Playwright layout/states/search are green.

BROWNFIELD RISK: one global stylesheet and one global reset (`*{box-sizing}`); `:root`
  custom properties are the de-facto tokens and every selector reads them;
  `html[data-theme="light"]` redefines tokens; the score colours are the one component-level
  exception and are migrated to tokens in phase 1. The theme is set by an inline head script
  before CSS (FOUC guard) and meta[theme-color] is a literal #0b0b14; app.js is one IIFE with
  one inline onclick (`.film-open`); load order is theme script -> CSS -> body -> app.js at
  end. State-gap census: route changes do not move focus (main has tabindex="-1" but is never
  focused); no aria-live for filtered result counts; combobox options are
  <button role="option">; loading exists only as first paint; some controls are 32-38px on
  mobile vs the 44px house rule.
```

## 1. Problem

The data layer is finished (58 cinemas, 19,057 sessions, 390 films, 128 dates) and the app
answers every query, but its visual layer is one-off: colours, radii and spacing are
literals inside a single stylesheet, states are inconsistent between surfaces, and an agent
changing UI must read ~80 KB of JS and ~28 KB of CSS to guess what exists. The cost is paid
three ways: inconsistent states (no route focus, silent filter changes), unbounded
regressions (nothing characterizes current behaviour), and token burn (screenshots used to
discover structure that an aria snapshot would state for free).

## 2. Users and jobs

| User | Job to be done | Success signal |
|---|---|---|
| Melburnian planning tonight | "What is on near me after 7pm?" | Day list reachable in one tap; filters visible; distance and next time on every card |
| Repertory/arthouse regular | "When is this film on, at which venue?" | Film page lists every remaining session by day/venue free of festival-prefix noise |
| Cinema-goer on a phone, one hand | "Is it worth opening?" | First card within one screen; 3-column poster grid; no horizontal page scroll |
| Screen-reader/keyboard user | "What changed, where am I?" | Focus follows route, counts announced, labels on every control, visible focus |
| Agent (this repo's actual maintainer) | "Change one component without breaking the rest" | Tokens + manifest + gallery + aria snapshot name the component and its states |
| Deployer | "Ship today's scrape" | `refresh -> ratings -> posters -> export -> wrangler` unchanged; bundle validated |

## 3. Non-goals

- Rewriting the app shell, routing, or data flow. The IIFE and hash routes stay.
- A framework, bundler, SSR, or hydration. The app must run from three static files.
- User accounts, watchlists, notifications, seat maps, ticket purchase in-app.
- Regional Victoria coverage, Event Cinemas, or new adapters (data work, not UI).
- Runtime ratings/poster APIs, analytics, cookies, or any new third-party runtime call.
- Replacing native `<details>`/`<select>` with custom widgets where they pass the checks.
- Dark/light auto-switching beyond the existing preference + `prefers-color-scheme` default.

## 4. Constraints

| Area | Constraint |
|---|---|
| Tech | Vanilla ES5-compatible IIFE, no modules, no polyfills; native HTML elements first |
| Build | Zero-build. `cinemelb export` copies/hashes `cinemelb/web/*` only; no npm, no Tailwind CLI |
| Deploy | Cloudflare Pages (D-019), static, root base path `./`, hash routing, SPA fallback to index.html; Direct Upload, so `dist/` stays a gitignored artifact |
| CSP | None today. Any new inline handler is a liability; prefer delegated listeners. Security design in `docs/security-design.md`; external assets require SRI (D-012) |
| Offline | No. Bundle is fetched once; no service worker |
| Security | Untrusted by default: scraped text and URLs. Four output contexts (terminal, HTML text/attr, URL, inline script) each have one helper in `cinemelb/sanitize.py`; `export` filters URLs at the trust boundary; the loopback server checks `Host`; `export --out` refuses unmarked directories. Invariants INV-1..INV-10 in `docs/security-design.md` |
| Licensing | IMDb datasets non-commercial; OMDb CC BY-NC (IMDb/Metacritic only — it no longer returns RT); Metacritic keyless finder undocumented; RT page parsing against ToS (opt-in only, no `/search`). App code has **no LICENSE file** (open question) |
| Bundle budget | §8 caps; exceeding one requires a decision entry |
| Browser matrix | iOS Safari 16+, Android Chrome (last 2), Chrome/Edge (last 2), Firefox ESR |
| Data/privacy | Typed places persisted in localStorage; GPS coordinates in memory only and requested on load only when permission is already granted, until Clear opts out; no keys or PII in the bundle; Nominatim is the only outbound call, on user action |
| House rules | 44px primary tap targets; 16px inputs on iOS; ASCII in console output; `uv` only |

## 5. Data contracts

Upstream is SQLite (`data/cinemelb.sqlite3`, gitignored) -> `build_bundle()` -> one JSON
file. The bundle is versioned rather than back-compatible: a field change bumps
`version` and the exporter validates it.

| Entity | Shape (fields + types) | Source | Validation | Failure mode |
|---|---|---|---|---|
| bundle | `{version:3, generated:ISO, counts:{cinemas,films,screenings,dates,urls,rated,posters}, cinemas[], films[], aliases{}, screenings[], dates[], formats[], attrs[], screens[], urls[], places[]}` | `export.build_bundle` | `_validate()` raises `SystemExit` on version/arity/index/range/alias errors | Export aborts — never a half-valid deploy |
| film row | 15 fields: `[key:str, title:str, year:int\|null, rating:str, runtime:int\|null, genres:str[], poster:str, synopsis:str(<=200), trailer:str, imdb_id:str, imdb:float\|null, rt:int\|null, mc:int\|null, rt_url:str, mc_url:str]` | `store.films()` + ratings + posters | arity 15; imdb 0..10; rt/mc 0..100; imdb 0 and mc 0 normalised to null | Missing score -> null -> chip omitted; never `0` or `—` |
| cinema row | 10 fields: `[name, chain, suburb, address, website, tags:str[], lat:float\|null, lon:float\|null, phone, booking_url]`; `tags` always carries the derived `independent` tag for non-big-brand venues and never for `CHAIN_BRANDS` (Hoyts, Village, Reading, Palace, IMAX) | `store.cinemas(active_only=True)` + `export._cinema_tags` | arity 10; lat/lon rounded 5dp; `independent` unique and authoritative for the type filter | No coords -> Maps link falls back to name+address; no address and no links -> venue meta omitted; absent tag would read as chain |
| screening row | 9 ints: `[date_i, film_i, minute, cinema_i, format_i, attr_i, sold:0\|1, url_i(-1=none), screen_i]` | `store.screenings()` | each index in range; minute 0..1439; sold in {0,1}; url -1..len-1 | Dangling index aborts export |
| dates / formats / attrs / screens / urls | sorted string tables; attrs is `string[][]` of canonical badges (`CC, AD, Open Captions, HI, Subtitled, NFT`) in fixed order | export | attr spellings canonicalised before indexing | UI renders one vocabulary; unknown badge kept verbatim |
| places | `[[name, lat, lon], ...]` = landmarks + every venue suburb | `geo.known_places()` + registry | lat -90..90, lon -180..180, non-empty name | No places -> export aborts (SPA cannot geocode) |
| ratings.json | map `film key -> {imdb_id, imdb, rt, mc, rt_url, mc_url, source[], match_method, confidence, needs_review, fetched_at}` (gitignored) | `cinemelb ratings` | confidence gate; ambiguous -> `needs_review`, not displayed | Missing file -> all nulls; `--no-ratings` skips |
| posters.json | map `film key -> {url, source, imdb_id, matched_title, matched_year, confidence, match_method, status, fetched_at}` (gitignored) | `cinemelb posters` | exact/near-exact/year±1/ratings-id only; Palace `/poster/` treated as placeholder | No entry -> venue poster if real, else initials tile |
| title contract | `titles.display_title()` strips programme/venue prefixes (`BFF26`, `JIFF:`, `MIFF`, `JFF`, `SFF`, `CASFFA:`, `Cult Classix:`, `Astor:`, `Palace Cinemas:`, ...) at ingestion; `film_key()` keys on the cleaned title; `Store.normalise_films()` rewrites and merges collisions, original strings kept in SQLite `aka` and shipped in the bundle `aliases` map | `cinemelb/titles.py`, `models.film_key`, `store.normalise_films`, `export.build_bundle` | `tests/test_titles.py`, `tests/test_export.py`; conservative guards: bare `Astor`/`Palace` alone untouched, bare-word strip only for `astor`/`cult classix`, `_CODE` case-sensitive so "Staff Only" survives | Regression = duplicate film rows, broken rating/cover joins, festival noise in search |
| aliases | map `film key -> [published title, ...]` for films whose `aka` differs from the cleaned title; 79 films today | `store.films()[].aka`, normalised in `build_bundle` | `_validate`: keys are exported films, lists non-empty strings <=160 chars; title-equal aliases dropped | Missing alias -> that spelling does not match; an empty map is valid |
| derived fields | `minute + 1440` and `date - 1` for starts before 06:00 at index time; first exported day's early rows dropped; `independent` tag on each cinema row | `app.js::index`, `export._cinema_tags` | `isPast`/`isTooSoon` compare absolute minutes; `cinemaKind` reads the tag | A 00:31 session belongs to the previous listing day, shown as `12:31am`; a missing tag falls back to "chain" rather than mislabelling a multiplex |

Deploy freshness: `counts` + `generated` + last date are printed in the footer. The app
checks `Content-Type` before parsing so an SPA-fallback HTML page cannot be "parsed" as
JSON. Current `dist` reports 390 films, 58 cinemas, 19,057 screenings, 128 dates, 320
covers, 217 IMDb-rated, 184 with a Tomatometer (D-018), 238 KB gzipped.

Security at the data boundary: `Cinema`/`Film`/`Screening.__post_init__` strip C0/C1 control
characters, so no adapter can put ESC/BEL into the store. `build_bundle` admits only
`http(s)` values into `urls` and `films[].poster`, so a renderer that forgets a check cannot
reintroduce a `javascript:` link. The bundle contract is v3: v2 plus the `aliases` map (D-016).

## 6. UI inventory and required states

| Surface | Route | Component ids | States required | Empty / loading / error |
|---|---|---|---|---|
| App shell | all | `app-header`, `brand`, `theme-toggle`, `clock` | default, scrolled/sticky, light, dark, 320px | clock hidden <640px; no loading state |
| Search box + suggestions | all | `search-box`, `search-suggest`, `suggest-option`, `suggest-all` | closed, open, no-match, 6 results, keyboard-selected, query preserved | `sug-empty` "No films match"; suggestions closed on route change |
| Day rail | all | `day-rail`, `day-rail-item` | default, hover, focus-visible, current (`aria-current="date"`), 14-day window, scrolled | not rendered before bundle; no error state |
| Filter bar | day, film | `filter-bar`, `filter-chip`, `filter-select`, `clear-filters` | off, on (`aria-pressed`), disabled n/a, horizontal scroll <640px, cinema type (all / chains / independents), chain list narrowed by type, chain absent when 1 chain, "within" absent until place | hidden on search route |
| Near bar | day, film | `near-bar`, `near-me`, `near-input`, `near-status`, `near-clear` | unset, requesting (button disabled + status), set (distance line), denied, insecure context, 15s timeout, geolocation vs typed | error text names the platform cause; input remains usable; hidden on search route |
| Day — starting soon | `#/`, `#/day/<today>` | `starting-soon`, `soon-row`, `soon-time` | 1..5 rows, ticket-link time, no-URL row, soon accent, place-set distance, filtered, empty hidden | omitted when no catchable session starts in the next 120 min |
| Day list — by film | `#/`, `#/day/<iso>` | `film-list`, `film-card`, `film-times`, `film-open` | collapsed, expanded, soon pill, +N overflow, 1..3 times shown, relevant venue, sold-out-free (card level), no place, place set, 1 vs many cinemas | three distinct empties: no sessions that day / day over / filters hid N sessions with Clear |
| Day grid — poster tiles | same | `poster-grid`, `poster-tile`, `poster`, `poster-fallback` | 3 cols phone, 8 max desktop, no cover, broken image, no scores, next session not today, long title (2-line clamp) | same empties as list |
| Day — by cinema | same | `cinema-card`, `day-sub` | collapsed, expanded, distance, from-time, 1 film vs many, address shown once | same empties |
| Day — by time | same | `time-section` (`<details open>`) | open, collapsed, empty bucket omitted, late-night >24:00 label, film links carry `?time=<bucket>` | same empties |
| Venue block + session table | day, film | `venue-block`, `venue-actions`, `session-table` | icon actions, linked / no-url / sold-out / soon rows, time-scoped list, no repeated address | table omitted when no rows |
| Film page | `#/film/<key>[?time=<bucket>]` | `film-hero`, `film-poster`, `score-chip`, `film-facts`, `trailer`, `session-section`, `scope-clear`, `cinema-sort` | by day / by cinema / by time (persisted), time-scoped from a day bucket, cinema sort time/distance, no poster, no scores, no synopsis, many days, not found | "Film not found"; "No sessions left"; "No <bucket> sessions" + Show all; "N hidden by filters" + Clear |
| Search page | `#/search/<q>` | `search-head`, `search-result-grid`, `search-sort` | results, 1 result, 0 results, empty query, long query, sort by relevance/rating/title | 0 results names the query; empty query invites browsing |
| View bar | day, film, search | `view-bar`, `sort-select`, `layout-toggle`, `cinema-sort` | pressed/unpressed, Nearest absent without place, Distance absent without place, grid active, mobile wrap | n/a |
| Empty / loading / error | all | `empty-state`, `loading`, `error-panel` | first paint, 404 film, wrong Content-Type, HTTP error, offline fetch | already implemented; needs aria-live + retry affordance |
| Footer | all | `footer-meta`, `footer-notes` | ratings hidden when 0, posters hidden when 0, long date range | n/a |

Breakpoints: 640px (mobile chrome/rail/filters, 3-column grid), 980px max content width.
Themes: `dark` (default) and `light`, both first-class; no auto beyond initial default.

## 7. Design system

- **Token source:** `cinemelb/web/tokens.json` (DTCG 2025.10-style; `$type`/`$value`/`$description`).
- **Generator:** `uv run python scripts/build_tokens.py` writes a marker block
  (`/* tokens:begin */ … /* tokens:end */`) at the top of `cinemelb/web/app.css`.
  No second stylesheet: one request, no `export.py` change, hashing covers the result.
- **Semantic layer:** phase 1 emits the existing names (`--bg`, `--panel`, `--card`,
  `--line`, `--text`, `--text-2`, `--text-3`, `--accent`, `--radius`, ...) plus
  `--score-imdb`, `--score-rt`, `--score-mc`, `--focus-ring`; later phases add
  `--surface-0/1/2`, `--text-strong/muted/faint`, `--border`, `--space-*`, `--radius-1..3`
  as aliases while components migrate. No component selector is renamed.
- **Manifest:** `components.json` (schema v1, `components.schema.json`), gallery at
  `cinemelb/web/gallery.html` (dev-only, never exported). Render access:
  `window.CINEMELB.render.<fn>` added at the end of the IIFE (~200 B shipped).
- **Gallery contract:** imports `./app.css` and `./app.js`, fetches
  `/dist/data/bundle.json`, builds fixtures from real rows, and renders each component by
  calling the same function the app calls. `?theme=light` switches the theme; `#<id>`
  deep-links a component. No copied markup, ever.
- **A11y:** WCAG 2.2 AA. Native semantics first (`button`, `a`, `summary`, `select`,
  `label`, landmarks). Keyboard map: `/` focus search, Up/Down move suggestions, Enter
  activates, Escape closes, Tab visits controls in DOM order. Visible `:focus-visible`
  ring on every interactive element (`--focus-ring`). `prefers-reduced-motion` respected.
- **Responsive:** must work at 320px and at 200% zoom on 1280px (≈640 CSS px). Content
  max-width 980px. Filter bar may scroll horizontally by design; the page must not.
- **Themes:** token switch only. `[data-theme]` may redefine semantic values; it must not
  style components. Contrast targets: 4.5:1 body text, 3:1 large text and UI borders.

Component set (all real, all in `app.js` today):

| id | status | source fn | notes |
|---|---|---|---|
| `app-header` | stable | inline HTML + `onTheme` | brand, search slot, clock, theme toggle |
| `search-suggest` | beta | `renderSuggest`, `moveSuggest` | combobox/listbox; options are buttons today |
| `day-rail` | stable | `renderRail` | 14 days, `aria-current="date"` |
| `filter-bar` | stable | `renderFilters`, `onChip`, `onSelect` | chips + native selects |
| `near-bar` | beta | `renderNear`, `requestLocation`, `setPlace` | 5 error paths incl. iOS in-app browser |
| `view-bar` | stable | `viewBar`, `sortSelect`, `layoutToggle`, `cinemaSortBar` | grouping + sort + grid toggle + Time/Distance |
| `starting-soon` | stable | `startingSoon` | today only; up to five catchable films in the next 120 min, ticket-link time, venue and distance |
| `film-card` | stable | `filmCard` | `<details>` with a 36x54 cover, a relevant venue, up to three time pills on every width, arrow; time-section links carry `?time=` |
| `poster-tile` | stable | `filmTile`, `posterTile` | one link per film; initials fallback |
| `poster-grid` | stable | `dayByFilmGrid`, `renderSearch` | 3..8 columns |
| `score-chip` | stable | `scoreBits`, `scoreChips` | inline in meta vs linked chips |
| `venue-block` | stable | `venueBlocks`, `venueActions` | name, suburb, distance, icon actions, session table; no repeated address |
| `session-table` | stable | `sessions` | Time, Format table; the time is the ticket link; linked / no-URL / sold-out / soon rows |
| `cinema-card` | stable | `cinemaCard` | day grouped by venue |
| `time-section` | stable | `dayByTime` | `<details open>` buckets |
| `film-hero` | stable | `renderFilm` | 132x198 cover, facts, trailer |
| `empty-state` | stable | `renderDay`, `renderFilm`, `renderSearch` | three distinct empties |
| `error-panel` | stable | `fail` | Content-Type / HTTP / JSON errors |
| `loading` | stable | shell markup | first paint only |

## 8. Performance budget

Measured 2026-11-26 from `dist/` and `cinemelb/web/` (gzip level 9; Cloudflare serves
brotli, not measured — no brotli module installed and none to be added):

| Asset | Raw | Gzip | Cap | Headroom |
|---|---|---|---|---|
| `data/bundle.json` (390-film v3 build) | 1,912,611 | 238.4 KB | 260 KB | 8% |
| `app.js` | 103,291 | 27.0 KB | 28 KB | 4% |
| `app.css` (with generated tokens) | 34,851 | 7.5 KB | 12 KB | 38% |
| `index.html` | 4,848 | 1.8 KB | 6 KB | 70% |
| Font files | 0 | 0 | 0 | system stack only |

- JS <= 28 KB gz, CSS <= 12 KB gz, HTML <= 6 KB gz, bundle <= 260 KB gz.
- Poster images are lazy, 300x444, `referrerpolicy="no-referrer"`, ~16 KB each, never
  counted in first load. Adding a poster to a card must not add a request per tile beyond
  the image itself.
- LCP <= 2.5s on a mid-range Android over 4G, CLS <= 0.1 (poster `aspect-ratio: 2/3`
  reserves space). **Unmeasured** — labelled `manual:` until an LCP harness exists.
- No network request added without a decision entry.

## 9. Acceptance criteria

```
AC-001 [must] The generated token block in app.css is byte-identical to a fresh
regeneration from tokens.json.
  given: cinemelb/web/tokens.json
  check: tokens: uv run python scripts/check_tokens.py
  status: passing (2026-11-26)

AC-002 [must] Every var(--x) referenced in app.css is defined by the generated block, and
every generated token is referenced by at least one rule or manifest entry.
  check: tokens: uv run python scripts/check_tokens.py --coverage
  status: passing (25 tokens)

AC-003 [must] No colour literal (hex, rgb(), hsl()) appears in app.css outside the
generated block; the one inline SVG data-URI stroke is allowlisted by name.
  check: tokens: uv run python scripts/check_tokens.py --literals
  status: passing

AC-004 [must] Body text meets 4.5:1 and large text/UI borders 3:1 in both themes, computed
from tokens.json.
  check: tokens: uv run python scripts/check_contrast.py
  status: text pairs passing (4.5:1); border contrast deferred to D-010

AC-005 [must] No new runtime dependency or third-party request; the only outbound call is
Nominatim, on an explicit typed address.
  given: #/ with no place set
  check: dom: uv run python tests/ui/network.py::no-third-party
  status: planned(phase-1)

AC-006 [must] Every components.json id resolves to an existing window.CINEMELB.render
function and a live gallery example; missing ids fail the check.
  check: manifest: uv run python scripts/check_manifest.py
  status: passing (19 components)

AC-007 [must] Every token named in components.json exists in the generated block.
  check: manifest: uv run python scripts/check_manifest.py --tokens
  status: passing (25 tokens)

AC-008 [must] The gallery renders components by calling the app's render functions, and
each manifest state has an aria snapshot.
  given: cinemelb/web/gallery.html#film-card
  check: aria: uv run python tests/ui/aria.py --gallery film-card
  status: partial — gallery renders 13 components through the real renderers; aria
          snapshots are the next harness step (open question 3)

AC-009 [must] Every shipped bundle has film rows of 15, cinema rows of 10, screening rows
of 9; all indices in range; counts equal array lengths; version == 2.
  given: dist/data/bundle.json
  check: bundle: uv run python scripts/check_bundle.py dist/data/bundle.json
  status: passing

AC-010 [must] No exported film title begins with a programme or venue prefix.
  given: a fresh export
  check: bundle: uv run python scripts/check_bundle.py --titles
  status: passing

AC-011 [must] Titles that differ only by a programme prefix collapse to one film row; the
published strings stay in SQLite `aka` and never appear as bundle titles.
  check: unittest: uv run python -m unittest tests.test_titles -v
  status: passing (12 cases, 2026-11-26)

AC-012 [must] A missing score renders no chip; Metascore 0 is dropped at export; no card
or page shows a dash or a zero for a score.
  check: dom: uv run python tests/ui/states.py  (no-scores case)
  status: passing (no chip, no source name; 0 zero-scored film rows)

AC-013 [must] A missing or broken cover renders the initials tile without layout shift.
  check: dom: uv run python tests/ui/states.py  (poster-fallback case)
  status: passing (initials tile, absolute fallback, cover/fallback boxes equal)

AC-014 [must] Session times render as a two-column table (Time, Format & screen). A
session with no ticket URL is a non-link time cell with its time and "At venue"; a sold-out
session is dashed, struck through, labelled "Sold out", and unlinked.
  check: dom: uv run python tests/ui/states.py  (session-variants + session-table cases)
  status: passing (table headers, one link per row, span/no-href for no-URL and sold out)

AC-015 [must] While a filter hides every session on a day, the results region says how
many sessions were hidden and offers a Clear control.
  check: dom: uv run python tests/ui/states.py  (filtered-empty case)
  status: passing (hidden count named, Clear restores the day)

AC-016 [must] The day rail marks exactly one day with aria-current="date" and shows the
14-day window from today.
  check: dom: uv run python tests/ui/layout.py  (rail case, 320/390/1280)
  status: passing (1 aria-current, 14 days at every width)

AC-017 [must] Grouping and sort controls expose aria-pressed/selected state and persist
across reload via cinemelb-view/cinemelb-sort; "Nearest" is absent without a place.
  check: dom: uv run python tests/ui/layout.py  (persistence case)
  status: passing (Cinemas + Title survive reload; no near option without a place)

AC-018 [must] In grid layout each film is exactly one link whose accessible name carries
the title, and that link opens #/film/<key>.
  check: dom: uv run python tests/ui/layout.py  (poster-tile case)
  status: passing (tile is one <a>, href #/film/, text includes the title)

AC-019 [must] When Enter is pressed with text in the header search, the app routes to
#/search/<q>; an empty query does not navigate; the suggestion list ends with "See all N
results".
  check: dom: uv run python tests/ui/layout.py  (search cases)
  status: passing ("See all 13 results", empty Enter keeps the hash, Enter routes)

AC-020 [must] The search page hides the session filter bar and the near bar.
  check: dom: uv run python tests/ui/layout.py  (search-chrome case)
  status: passing (both display: none on #/search/<q>)

AC-021 [must] Arrow Down/Up move aria-activedescendant to a real option id, Enter opens
the highlighted film (or the results page), Escape closes the list and keeps the query.
  check: aria: uv run python tests/ui/search.py  (combobox cases)
  status: passing — options are non-interactive role=option rows; focus stays in the
          input; Escape closes, keeps the query and prevents the UA search-clear

AC-022 [must] Route changes move focus to the results heading and update document.title,
so a screen reader starts at the new content.
  check: dom: uv run python tests/ui/layout.py  (route-focus case)
  status: passing (activeElement is H1; title is "Search: … — cinemelb")

AC-023 [must] When filters change the result set, the new film/session count is announced
politely.
  check: dom: uv run python tests/ui/layout.py  (announce-count case)
  status: passing (aria-live="polite"; a filter rewrites "1 film · 1 session" to "0 films · 0 sessions")

AC-024 [must] At 320px wide, document.scrollWidth <= clientWidth on every route,
including a film page with the session table.
  check: dom: uv run python tests/ui/layout.py (320px route sweep) and tests/ui/film.py
  status: passing (day, search, film and film-table routes at 320px; also 390/1280;
          the 296px table fits its 296px column)

AC-025 [must] Every interactive target is >= 24x24 CSS px (WCAG 2.2 2.5.8); primary
mobile controls are >= 44px high as a project rule.
  check: dom: uv run python tests/ui/layout.py (target-size case) and tests/ui/film.py
  status: passing — 109 controls measured at 320/1280, none below 24x24; the session
          time/ticket link is at least 28px high at 320px; primary mobile controls are
          44px. Inline links in a sentence use the 2.5.8 exception.

AC-026 [must] In both themes every interactive element shows the focus ring; ring colour
comes from --focus-ring, not a literal.
  check: dom: uv run python tests/ui/layout.py  (focus-ring case)
  status: passing — keyboard focus paints a 2px ring whose computed colour equals the
          active theme's --focus-ring (#f3c876 dark, #7d560d light)

AC-027 [must] prefers-reduced-motion removes non-essential animation and shortens the
spinner.
  check: dom: uv run python tests/ui/layout.py  (reduced-motion case)
  status: passing (spinner 2.4s; no other infinite animation; media query active)

AC-028 [must] Budgets hold: bundle <= 260 KB gz, JS <= 28 KB gz, CSS <= 12 KB gz,
HTML <= 6 KB gz.
  check: budget: uv run python scripts/check_budget.py dist
  status: passing (bundle 238.4 KB, js 27.0 KB, css 7.5 KB, html 1.8 KB gz)

AC-029 [must] A fresh export from the current database reports 390 films, 58 cinemas,
19,057 screenings and 128 dates, matching Store counts.
  check: bundle: uv run python scripts/check_bundle.py --against-db
  status: passing

AC-030 [should] The film page groups sessions by day, cinema or time, and remembers the
choice with the day view's preference.
  check: dom: uv run python tests/ui/film.py  (grouping cases)
  status: passing — Days/Cinemas/Times; the choice survives a reload; hero, score
          chips, icon venue actions and the session table verified in the same run

AC-031 [must] Scraped text cannot close the map's inline script or inject an element from
it; a title containing `</script><script>…` renders as inert JSON.
  check: unittest: uv run python -m unittest tests.test_security.TestMapExport -v
  status: passing (2026-11-26; browser witness: scratch/review/browser/prove.js)

AC-032 [must] Only absolute http(s) URLs reach an href/src or the bundle. A `javascript:`,
`data:` or `file:` ticket URL is dropped at export and again at render.
  check: unittest: uv run python -m unittest tests.test_security.TestSanitize tests.test_security.TestMapExport -v
  status: passing (2026-11-26; prove_bundle.py shows `bundle.urls == []`)

AC-033 [must] `cinemelb export --out` refuses to replace a directory that lacks the
`.cinemelb-site` marker (unless `--force`), refuses filesystem roots and cwd/ancestors, and
builds aside before renaming.
  check: unittest: uv run python -m unittest tests.test_security.TestExportGuard -v
  status: passing (2026-11-26)

AC-034 [must] On a loopback bind the server answers only loopback `Host` names and returns
421 for anything else, so a DNS-rebinding page cannot read the API; a non-loopback bind
prints a warning and disables the check.
  check: unittest: uv run python -m unittest tests.test_security.TestServer -v
  status: passing (2026-11-26)

AC-035 [must] A 500 response contains a reference id and no exception text, path or SQL;
the detail is written to stderr.
  check: unittest: uv run python -m unittest tests.test_security.TestServer -v
  status: passing (2026-11-26)

AC-036 [must] Scraped control characters never reach the terminal: values are stripped at
ingestion and rendered as plain text, so ESC/OSC sequences cannot reach stdout even for rows
written before the strip existed.
  check: unittest: uv run python -m unittest tests.test_security.TestCliOutput tests.test_security.TestModelIngestion -v
  status: passing (2026-11-26; prove_cli_ansi.py shows no ESC)

AC-037 [must] Every external asset in a generated artefact carries a pinned SRI hash and
`crossorigin`; Leaflet 1.9.4 loads with integrity in a real browser.
  check: unittest: uv run python -m unittest tests.test_security.TestMapExport -v
  status: passing (2026-11-26; browser check: scratch/review/browser/sri.js)

AC-038 [must] The security invariants are checked statically: `check_security.py` fails on
raw `json.dumps` in the map, a bare `esc()` href/src, an external tag without `integrity`,
an unguarded `rmtree`, exception text in the 500 page, or a missing `Host` allowlist.
  check: static: uv run python scripts/check_security.py
  status: passing (2026-11-26)

AC-039 [must] While a query matches a film's published alias, search returns that film, and a
real title match ranks above an alias match for the same film.
  given: #/search/BFF26 and #/search/500 Miles
  check: dom: uv run python tests/ui/alias.py
  status: passing (2026-11-26; 79 aliased films; "BFF26" finds 500 Miles and the
          suggestion says "listed as BFF26 500 Miles"; the title match ranks first)

AC-040 [must] One offline, deterministic command scans the publishable worktree and every
git commit for secrets, runs the Python S-rules and JS sinks, checks the static invariants,
the lock contract and the exported bundle, and exits 2 rather than pass if a scanner cannot
run.
  check: static: uv run python scripts/security_gate.py
  status: passing (2026-11-26; ~1.1s; gate caught a staged AWS-shaped key and an
          unreachable history blob during its own test, then both were purged)

AC-041 [must] The cinema-type filter offers All / Cinema chains / Independent cinemas; choosing
one restricts every day and film view to venues whose bundle row carries (or omits) the derived
`independent` tag, and the chain dropdown only offers brands in that half. Clearing the filter
restores the unfiltered list.
  given: #/day/<date> with the first indie session, then cinema-type=indie
  check: dom: uv run python tests/ui/states.py  (independent-filter case)
  status: passing (2026-11-26; 41 chain / 17 independent venues in the bundle)

AC-042 [must] A film opened from a day-view time section keeps that section's range: the card
link carries `?time=<bucket>`, the film page names the bucket, lists only sessions inside it,
and "Show all times" restores the full list. A film opened from the existing After filter
keeps that filter selected.
  given: #/day/<date>, group Times, Late night, then the film arrow
  check: dom: uv run python tests/ui/film.py  (time-scope and after-filter cases)
  status: passing (2026-11-26; Late night 9 -> 273 sessions after Show all times)

AC-043 [should] On a film page with a place set, venues sort by their next session by
default; a Time/Distance segmented control switches the order and persists in
`cinemelb-view`; Distance is absent while no place is set.
  check: dom: uv run python tests/ui/film.py  (cinema-sort case)
  status: passing (2026-11-26; 48 venues, time then distance order, survives reload)

AC-044 [must] Session times render as a table whose time cell is the single ticket link,
its time is underlined, and the only visible ticket cue is a glyph (no "Tickets" text).
Sold-out and no-URL rows are static. Venue actions are icon buttons with accessible names,
and the repeated film-page venue blocks omit the street address.
  check: dom: uv run python tests/ui/states.py and tests/ui/film.py
  status: passing (2026-11-26; 1 link per row, glyph-only cue on linked rows, 746 named
          actions, 0 addresses in day venue blocks). Appearance witness:
          scratch/visual/phase10-film-390.png — the underlined time and ticket glyph read
          cleanly; verdict: keep.

AC-045 [must] On today's day view, a "Starting soon" module lists at most five unique films
with a catchable session in the next 120 minutes, before the day's grouping. Each row names
the start time, film, venue and suburb; with a place set it also shows distance and drive
time. Past, sold-out, filtered and leaves-too-little-time sessions are excluded, and the
module is absent when no row qualifies.
  given: 2026-11-27 19:00 Melbourne, #/
  check: dom: uv run python tests/ui/layout.py  (starting-soon case)
  status: passing (2026-11-26; 5 rows, ascending start, 120-min window, safe ticket links)

AC-046 [must] Every collapsed day film card shows up to three distinct start times at every
width, including 390px; a film with three or fewer shows all of them, and a busier film ends
with +N. The meta line names a relevant venue and, with a place set, the distance and drive
time for the nearest one.
  check: dom: uv run python tests/ui/states.py  (card-times case) and layout.py
  status: passing (2026-11-26; 3 pills at 390px, +N for a busy film, nearest/next venue in meta)
  evidence: scratch/visual/phase13-day-df9eb69-390.png and -1280.png — the Starting
          soon module and denser cards read cleanly; verdict: keep (appearance only).

AC-047 [must] Geolocation is requested on load only when permission is already granted; a
first-time visitor is not prompted. A granted location sets the place, shows distances
immediately, and is never written to localStorage. Clear stops the silent request until the
user asks for a place again.
  given: context geolocation (-37.8136, 144.9631) + permission granted, #/
  check: dom: uv run python tests/ui/layout.py  (auto-locate case)
  status: passing (2026-11-26; state.place.source=geolocation, km in cards, no cinemelb-place,
          Clear survives a reload without a stored coordinate)
```

## 10. Decision log

### D-001 Design library: tokens-first CSS + native HTML (2026-11-26)

Context: zero-build static SPA, three hand-written web files, 26 KB gz JS cap, no bundler,
no server, no framework, and one maintainer (often an agent). Hard filters applied first:
must work as static files with no build step; no npm install for the app; must not add a
runtime request; must not require a rewrite of the IIFE.

| Criterion (weight) | Current (vars only) | **A: tokens+native** | B: utility CSS (Tailwind/Pico) | C: component lib (DaisyUI/Bootstrap) | D: web components (Web Awesome) |
|---|---|---|---|---|---|
| Fits build/hosting (3) | 2 | **2** | 0 | 1 | 1 |
| Bundle cost (2) | 2 | **2** | 0 | 0 | 0 |
| A11y of required primitives (3) | 1 | **1** | 0 | 1 | 2 |
| Maintenance + licence (2) | 2 | **2** | 1 | 1 | 1 |
| Token-friendly styling (2) | 1 | **2** | 0 | 0 | 1 |
| Agent legibility (1) | 1 | **2** | 1 | 1 | 1 |
| Migration cost (3) | 2 | **2** | 0 | 0 | 0 |
| **Weighted total /32** | 26 | **29** | 3 | 9 | 14 |

Decision: DTCG `tokens.json` -> generated block in `app.css`; native HTML elements; a
machine-readable manifest and a gallery that calls the app's own render functions. Open
Props is used as naming vocabulary for scales (`--size-*`, `--font-size-*`), never as a
runtime dependency or a file to copy.
Rejected:
- Tailwind (CLI or CDN): needs a build step for the real thing; the CDN build is dev-only
  and 100+ KB (https://tailwindcss.com/docs/installation/play-cdn).
- Pico CSS: unmaintained upstream and supplies no primitive this app lacks
  (https://picocss.com/docs/maintenance).
- DaisyUI/Bootstrap: opinionated component classes, 100+ KB, would fight the existing
  selector vocabulary and the amber theme.
- React + Radix: full rewrite of the shell; violates zero-build and the JS budget.
- Web Awesome now: MIT core, zero-build, and a real candidate — rejected only because no
  named primitive is missing yet (https://www.webawesome.com/). Escape hatch if a dialog
  or searchable multi-select becomes required and native elements fail the aria checks.
- Storybook: requires a bundler; the gallery route replaces it.
Revisit when: a modal/combobox/menu is required and native `<dialog>`/`<select>`/`<details>`
fail an aria AC, or when a second maintainer joins and needs a component API.
Consequences: we own focus management and state styling; every new value must be a token.

### D-002 Token delivery: generated block inside `app.css` (2026-11-26)

Context: `export.py::write_site` hashes exactly two assets; a second stylesheet would need
an exporter change and a second blocking request.
Decision: `scripts/build_tokens.py` rewrites a marker block at the top of `app.css`;
`check_tokens.py` fails on drift.
Rejected: `tokens.css` linked separately (extra request, exporter change); CSS `@import`
(serial request, not hashed); runtime CSS variables from JS (FOUC, no static diff).
Revisit if: the token block exceeds ~120 lines or the gallery needs tokens without the
app stylesheet.
Consequences: `app.css` is generated in part; hand edits inside the markers are overwritten
and caught by the drift check.

### D-003 Gallery is a dev route driven by a render namespace (2026-11-26)

Context: the gallery must not drift from the app, but `app.js` is an IIFE with private
functions.
Decision: append `window.CINEMELB = Object.freeze({render: {...}, state: S, hydrate})`
inside the IIFE; `cinemelb/web/gallery.html` (never exported) loads `app.css`, `app.js` and
`/dist/data/bundle.json`, then calls those functions with real rows and state fixtures.
Rejected: copied markup samples (drift by construction); Storybook (bundler); exporting a
module build (adds an export step and duplicates the file).
Revisit if: the namespace grows past ~12 functions or the gallery needs its own data
fixtures beyond real rows.

### D-004 Title normalisation at ingestion (shipped 2026-11-26)

Context: listings inject the programme into the title (`BFF26 Parky`, `JIFF: ...`,
`Cult Classix: ...`, `Astor: ...`). Prefixes fragmented one film into several rows and
broke rating/cover joins.
Decision: strip at ingestion — `models.film_key()` calls `titles.display_title()`;
`Store.normalise_films()` rewrites existing rows and merges key collisions; the published
string is kept in SQLite `aka`; the bundle ships only the cleaned title. Catalogue went
393 -> 390 (3 merges).
Rejected: display-only stripping (leaves duplicate keys, ratings and covers still split);
export-only stripping (keeps the bad keys in the DB and in CLI output); keeping prefixes
(search reads like a festival schedule, CLI and web disagree).
Revisit if: a venue uses a prefix that is genuinely part of the title (add a fixture and a
guard, not a global loosening). Known consequence: search does not match `aka` yet (§12).
Verification: `uv run python -m unittest tests.test_titles` (12 cases).

### D-005 Poster grid plus a full search page (shipped 2026-11-26)

Context: a day holds ~1,900 sessions; list-only reading is slow for browsing, and the
six-item dropdown was being mistaken for search.
Decision: keep the list as the default; add a persisted List/Grid toggle where the grid is
one poster tile per film linking to the film page; Enter in the header search routes to
`#/search/<q>` where every match is a tile with Best match / Top rated / Title A–Z, and the
dropdown keeps a "See all N results" row. Session filters and the near bar are hidden on
the search route because they describe sessions, not films.
Rejected: grid-only (loses showtimes per venue); list-only (unbrowsable at 1,900 rows);
a separate search HTML page (breaks the one-file shell and hash routing).
Revisit if: result sets routinely exceed ~200 tiles on a phone, or grid tiles need session
times (then reuse `session-table` inside the tile).

### D-006 Ratings and covers are build-time data (shipped 2026-11-26)

Context: scores and posters are wanted on every card, but keys, CORS and rate limits make
runtime calls hostile, and the page must make no third-party API call.
Decision: `python -m cinemelb ratings` and `posters` populate gitignored caches; `export`
bakes them into bundle v2; the browser never holds a key or calls a ratings service.
Missing data is null and the UI omits it.
Rejected: a runtime proxy (needs a server); client-side OMDb (leaks a key, adds a request);
dash/zero placeholders (misread as a real score).
Revisit if: a licensed score feed with redistribution rights appears, or covers move to a
self-hosted CDN.

### D-007 Verification ladder, screenshots exceptional (2026-11-26)

Context: agents burn tokens screenshotting to discover page structure; the repo has no
browser test harness at all.
Decision: T0 parse/JSON, T1 tokens+manifest+contrast, T2 aria snapshots and DOM
assertions, T3 Python unit tests, T4 visual diffs for appearance only, T5 manual.
Playwright is a dev dependency (`uv add --dev playwright`), so it runs with plain
`uv run python` and no npm enters the repo; baselines live in `tests/ui/snapshots/` and
`scratch/visual/` (gitignored) for one-offs.
Rejected: Playwright via npm (`npx playwright test`) — pulls Node packages into a repo that
has none; manual browser-only checks (the current cost); screenshot-first review (the
anti-pattern this spec exists to stop).
Revisit if: `uv run playwright install chromium` cannot provision browser binaries here,
in which case fall back to `manual:` with recorded evidence and log the gap in §4.

### D-008 Tokens adopted without renaming existing custom properties (2026-11-26)

Context: every selector in `app.css` reads `--bg`, `--panel`, `--card`, `--line`,
`--text*`, `--accent*`. Renaming them in one pass would touch every rule and make the
diff unreviewable.
Decision: `tokens.json` is the source; the generator emits the existing names as the
semantic layer, plus new aliases (`--score-*`, `--focus-ring`) used by phase 1. Components
migrate to further aliases one at a time; old names survive until no selector reads them,
then get deleted with a decision note.
Rejected: a rename-first migration (big-bang diff, no rollback boundary); parallel token
sets with no deletion rule (orphan tokens forever).
Revisit when: the last consumer of a legacy name is migrated.

### D-009 Phase 1 substrate (complete 2026-11-26)

Context: the spec requires token-first UI work and a gallery/manifest harness before
component changes.
Decision: implemented `tokens.json` + generated block, `components.json` + gallery, and the
T0/T1 check scripts in one phase. Existing component markup and CSS selectors stay
unchanged except for moving score colours and the focus ring onto tokens.
Implementation: `scripts/build_tokens.py` (generate/check/strip), `check_tokens.py`
(coverage/literals/motion), `check_contrast.py`, `check_manifest.py`, `check_bundle.py`,
`check_budget.py`; `window.CINEMELB.render`; `gallery.html` with 14 live sections and 5
planned. `--text-3` was raised to 4.5:1 in both themes; score colours and focus ring moved
to tokens. All phase-1 checks are green.
Rejected: component work first (would hardcode values again); a second stylesheet
(violates D-002); Storybook (D-001).
Revisit when: the gallery needs interactive state fixtures beyond the real render output.

### D-010 Border contrast deferred (2026-11-26)

Context: `--line`/`--line-2` are 1.3–1.6:1 against the page background. Raising them to
3:1 would change the quiet 1px card language across every surface, and controls remain
identifiable through their fill, text and focus ring.
Decision: gate text and accent contrast at 4.5:1 now; report border ratios as warnings in
`check_contrast.py` and revisit in phase 3 with a measured, component-scoped change.
Rejected: a global border lightening in phase 1 (visual rewrite with no component
isolation); dropping the check entirely (loses the measurement).
Revisit when: phase 3 touches `view-bar`/`filter-bar`/`near-bar` controls.

### D-011 Measurement-first verification; screenshots are appearance-only (2026-11-26)

Context: the prior working loop captured screenshots to discover structure and to check
whether a layout change worked, which burnt tokens and produced verdicts like "looks off"
with no citable state. The design system now exposes tokens, a manifest and real render
functions, and the app is deterministic enough to measure.
Decision: `tests/ui/layout.py` is the structural oracle for the SPA. It boots the built
`dist`, then at 320px, 390px and 1280px asserts: no horizontal scroll, rail `aria-current`
and 14-day window, live result count, poster tile is one link naming its title, empty-query
Enter does not navigate, suggestions end with "See all N results", search chrome is hidden,
route focus lands on the h1, film heading matches the row, and primary mobile controls are
44px. Aria snapshots of `main` are taken in the same run.
Consequences: a UI change is reviewed by running the script and reading the numbers
(`view-row` control tops, control heights, scroll width), not by re-capturing the page.
Screenshots remain allowed only for appearance questions that measurements and token
values cannot answer (contrast, rhythm, clipping), recorded once in `scratch/visual/` with
the verdict next to the AC. The 390px view-row wrap that motivated this entry was found by
the measured control tops (`[301, 302, 302]`), not by a screenshot.
Revisit when: a new surface has states the script cannot express (e.g. hover-only or
animation timing); add a case before falling back to images.

### D-012 Security by design: four output contexts, one helper each (2026-11-26)

Context: the 2026-11-26 review (`docs/security-review-2026-11-26.md`) found the same root
cause in three artefacts - server HTML was escaped and scheme-checked, while the map and SPA
were neither. `json.dumps` in `<script>`, `esc()` without a scheme allowlist, and raw
control characters in rich tables are each one missing context rule, not three unrelated
bugs.
Decision: `cinemelb/sanitize.py` is the only home for output-context guards:
`clean_text` (terminal/storage), `safe_href` (href/src), `json_for_script` (inline script).
`serve`, `mapview`, `export`, `cli` and `models` import it; `build_bundle` filters URLs at
the trust boundary so a missed renderer is not a single point of failure. Invariants and
the "adding a renderer" checklist live in `docs/security-design.md`. `scripts/security_gate.py`
is the one deterministic gate (gitleaks, ruff S-rules, JS sinks, and `check_security.py`), and
`tests/test_security.py` is the behavioural suite.
Rejected:
- Fix each sink inline (three copies of the rule; the review already showed the drift).
- HTML-escape JSON (`src`/`href` semantics unchanged; `</script>` still parses as markup).
- Trust `esc()` for URLs (it escapes characters, not schemes).
Revisit when: a new output context appears (CSV, PDF, response headers, log format) - add a
helper and a test before the first use, not after the first incident.
Consequences: one extra module; renderers must import it; the guard makes the old patterns
fail loudly.

### D-013 `export --out` requires a site marker (2026-11-26)

Context: `write_site` deleted any existing `--out` directory that was not cwd or an ancestor;
a sibling folder or another drive root passed the guard.
Decision: a `.cinemelb-site` marker authorises replacement; otherwise refuse unless
`--force`. Filesystem roots are refused explicitly. The site is built in a sibling
`.building` directory and renamed into place only after a successful build.
Rejected: prompt on stdin (breaks scripts); compare directory contents (heuristics rot);
delete only files the tool knows (leaves stale hashed assets).
Revisit when: export gains an incremental mode, which would change the replacement model.

### D-014 Loopback server: `Host` allowlist and generic errors (2026-11-26)

Context: the read-only server answered any `Host` header (DNS rebinding) and rendered the
exception text on a 500. The data is public and the server cannot write, so the impact was
low - but the controls are cheap and the class recurs.
Decision: a loopback bind accepts only `127.0.0.1`, `localhost`, `[::1]`, `::1` and answers
421 otherwise; a deliberate non-loopback bind prints a warning and disables the check
(it is already reachable). 500s log the exception with a short reference and render only the
reference. Request lines are stripped of control characters before logging.
Rejected: authentication (no state to protect, adds a credential to a local tool); Origin-only
checks (Host is the rebinding signal and needs no CORS preflight).
Revisit when: the server gains a write route or serves private data; then add origin/CSRF
checks and reconsider auth.

### D-015 External assets: pinned SRI, no new request without a decision (2026-11-26)

Context: the generated map pulled Leaflet 1.9.4 from unpkg without integrity, in an artefact
users are expected to open and host. The SPA itself has no external assets.
Decision: external `<script>`/`<link>` carry `integrity` (sha256 of the pinned bytes) and
`crossorigin="anonymous"`; the static guard fails an external tag without integrity. Vendoring
remains the fallback if offline map use matters more than file size.
Rejected: vendoring now (+162 KB per generated map, new files to ship); version pin alone
(pins the URL, not the bytes).
Revisit when: maps are used offline, or unpkg availability/CORS becomes unreliable.

### D-016 Alias search through a bundle-level map (2026-11-26)

Context: D-004 strips programme prefixes at ingestion and keeps the published strings in
SQLite `aka`. Search read only the cleaned title, so "BFF26" found nothing and the title fix
read as a regression for anyone who knew the festival code.
Decision: bundle v3 adds `aliases: {film key: [published title, ...]}` for films whose `aka`
differs from the cleaned title (79 films today). `searchMatches` scores an alias 25 points
below the same title match, so a real title match always ranks first; the suggestion row says
"listed as …" so the result is explicable.
Rejected: aliases inside each 15-field film row (arity churn for every consumer); shipping
`aka` on unaliased rows (size for nothing); client-side prefix guessing (finds "BFF26 500
Miles" but not arbitrary venue spellings); search ignoring `aka` (the regression).
Consequences: `_validate` rejects dangling or empty aliases; the bundle grows ~4 KB raw;
`check_bundle.py` reports the aliased count.
Revisit if: alias volume passes ~200 films, or non-Latin aliases need folding — then add a
normalised search key rather than loosening substring matching.

### D-017 Cinema type is a derived bundle tag (2026-11-26)

Context: the registry's `chain` field is empty for most independents but also non-empty for
the suburban Metro and United brands, so the UI cannot split "big chain" from "independent"
from that field alone. The request was a filter between the big chains and indie cinemas.
Decision: `export._cinema_tags` appends one `independent` tag to every cinema row whose chain
is not in `CHAIN_BRANDS` = {Hoyts, Village, Reading, Palace, IMAX}, and strips any hand-written
duplicate. The SPA reads the tag (`cinemaKind`) and narrows the chain dropdown to the selected
half; changing type resets a chain that no longer applies. The bundle stays v3: tags already
exist, so no row arity or consumer changes.
Rejected: duplicating the brand list in `app.js` (two sources of truth); a new cinema-row
field or top-level map (schema/validation churn for one boolean); using `chain != ""` as the
test (would make Metro and United chains).
Consequences: Palace counts as a chain despite its arthouse catalogue; the split changes in
one Python constant. A future per-venue override needs a registry field, not a regex.
Revisit if: a venue changes brand, or users want a third bucket (festival/ACMI).

### D-018 Rotten Tomatoes via Wikidata P1258, not OMDb (2026-11-26)

Context: the site shipped zero RT chips. The pipeline assumed OMDb returned a Tomatometer in
its `Ratings` array; as of 2026-11-26 a fresh key returns only IMDb and Metacritic for every
tested title (classics and current releases). RT slugs are not derivable from a title
(`/m/the_fly` is a 2025 short; the 1986 film is `/m/1133213-fly`), RT's private JSON search is
404, and its site search is disallowed in robots.txt.
Decision: when `--scrape-rt-mc` is passed, `_wikidata_rt_ids` joins the catalogue's IMDb IDs
(`P345`) to RT IDs (`P1258`) with batched SPARQL requests (50 per query, five for today's 239
IDs), then `_rt_scores` reads the film page's scorecard with its year gate. Generated slug
guesses remain as a fallback for films Wikidata does not map; no request touches `/search`.
Rejected: flipping the scrape default (RT/MC terms forbid automated collection; stays
explicit); trusting OMDb's `tomatoes=true` legacy fields (return `N/A`); RT site search
(robots-disallowed, and the project's politeness rule forbids it); fuzzy title matching (puts
a remake's score on the original); the RT sitemap index (76 MB for a join Wikidata already
provides); a paid RT API (no public tier).
Consequences: one page request per resolved film, cached for the TTL; 177/239 IMDb IDs resolve
today; `data/ratings.json` carries `rt`/`rt_url`; the footer names RT as a source. OMDb stays
optional for MC coverage.
Revisit if: Wikidata coverage drops below ~70%, or OMDb restores RT data.

### D-019 Host: Cloudflare Pages, because bandwidth is unmetered and SPA fallback is free (2026-11-25)

Context: the request was "research online and find a suitable free service to eventually
deploy this app as an SPA webapp. Maybe github pages or vercel". The app is a Python + SQLite
SSR server, so an SPA is a *different shape*: a static export (JSON bundle + vanilla JS) with
no server at all. Four free hosts were compared on the same five axes — bandwidth, SPA
deep-link behaviour, custom cache headers, commercial-use terms, and how a *daily data
refresh* interacts with the free tier — and the winner was verified with a throwaway deploy
before the real one.
Decision: **Cloudflare Pages**, Direct Upload (`npx wrangler pages deploy dist --project-name
cinemelb --branch main`). The deciding properties, in order:
1. **Bandwidth is unmetered.** The bundle is ~235 KB gz loaded once per visit; 100 GB/month on
   the rivals is a ceiling this app would have to keep thinking about, and Cloudflare's is not.
2. **SPA deep links need no configuration.** Cloudflare answers an unknown path with
   `index.html` and a 200, so hash routing plus any hand-typed URL works with no `_redirects`
   entry and no `404.html` copy. GitHub Pages serves its 404 fallback **with a 404 status** and
   needs the copy trick; Vercel needs a `vercel.json` rewrite.
3. **`_headers` is honoured**, which is what makes the cache contract in §8 possible: hashed
   assets `max-age=31536000, immutable`, the bundle `max-age=300, must-revalidate`, plus
   nosniff and referrer policy. GitHub Pages has **no custom headers** (assets get the fixed
   600 s cache), so hashed-forever assets would be revalidated every ten minutes.
4. **No commercial-use clause and no public-repo requirement.** Vercel Hobby is
   non-commercial-only; GitHub Pages requires a public repo on the free plan, which would
   publish the scraper and the data.
5. **A daily refresh fits the free tier.** Netlify's post-2025 credit model charges 15 credits
   per production deploy against 300/month, so 30 daily deploys exhaust the allowance before
   any bandwidth is counted. Cloudflare's 500 builds/month is defined as *git-push-triggered*
   builds, so Direct Upload deploys do not consume it.

Rejected:
- **GitHub Pages** — the requested default, and the closest runner-up: static-only, no custom
  headers, 404-status deep links, 1 GB site cap, public repo on the free plan.
- **Vercel Hobby** — the other requested default: 100 GB/month, `vercel.json` rewrites, and an
  explicit non-commercial-only restriction.
- **Netlify Free** — credit-metered deploys make a refreshing app the wrong shape for it.
- **Cloudflare Workers + D1** — the SQLite-in-the-edge path: needs a Python→JS port or WASM
  for no gain, since the whole dataset is 235 KB gz and the query engine runs fine in memory.
- **PythonAnywhere / Render / Fly.io / Hugging Face Spaces** — keep the Python server, which
  contradicts the SPA decision; Render spins down, Fly.io no longer has a real free tier, and
  PythonAnywhere is whitelist-only outbound with no custom domain on free.
Consequences (all load-bearing for later work):
- The deploy is a **Direct Upload**, not a git build, so `dist/` stays a gitignored build
  artifact and the release command is a manual T5 step (§9 verification ladder).
- `_headers` and the SPA fallback are Cloudflare semantics. **Moving hosts reopens this
  decision**: `_headers` has no equivalent on GitHub Pages, and the fallback's 200-not-404
  behaviour is what `app.js` tolerates.
- The 200-with-HTML behaviour for a *missing* file is a real gotcha — a renamed or absent
  `data/bundle.json` returns the shell, not a 404 — so the loader checks `Content-Type`
  before `JSON.parse` (`app.js:585`), and `_headers` pins the bundle's type explicitly.
- Refresh is still local (`cinemelb refresh` → `export` → `wrangler`); the free tier imposes no
  schedule, so the cron/automation question is deliberately still open.
Revisit if: bandwidth stops being unmetered, the app grows a server need (then Workers/D1 or a
Python host re-enters), GitHub Pages gains header control, or a git-build CI deploy is wanted
(then the 500-builds/month accounting becomes relevant).
Verification: the choice was smoke-tested before adoption with a 4-file throwaway deploy
(`cinemelb-spa-probe.pages.dev`): `/film/dune` and `/near/brunswick` both returned
`200 text/html`, `/assets/app.js` returned the immutable cache header exactly as written, and
`/assets/missing.js` returned `200 text/html` — the gotcha above, found for real rather than
assumed. The probe project still exists and is unreferenced (`STATE.md`).

### D-020 Film-page continuity: time scope, session table, icon actions, cinema sort (2026-11-26)

Context: the day page's "Late night" section showed a film's late sessions, but opening the
film page dropped that range and listed every session. The session display was a pile of
pills with no visible ticket affordance; every venue block repeated the street address and
text links; and the film page flipped its venue order to distance whenever a place was set.

Decision:
- A film link inside a time section carries `?time=<bucket>`; `renderFilm` applies that
  bucket range **on top of** the ordinary filter bar, names the bucket in the section
  heading, and offers "Show all times" as a route link. The scope is route state, not a
  global filter, so Back returns to the section and the filter bar keeps working within it.
- Session times render as a two-column table (Time | Format & screen). The time cell is the
  one link per row, with the time underlined and a ticket glyph as the visible cue; sold-out
  and no-URL rows are static. The table keeps the `.sessions` wrapper and `.session` time-cell
  vocabulary so the gallery and state checks keep working.
- Venue actions (directions, tickets, website, phone) are icon buttons with accessible
  names and tooltips. The street address is dropped from the repeated film-page venue
  blocks and kept only in the day-by-cinema card, where each venue appears once.
- Film-page venues sort by next session by default; a Time/Distance segmented control
  (Distance only when a place is set) reorders them and persists in `cinemelb-view`.

Rejected: reusing only the `after` filter (a section has a lower and upper bound; "After 9pm"
would leak later buckets into Evening); a separate scoped film page (breaks the film page's
own filters and Back); a flat day-wide table (loses the per-venue action row and the
distance sort); making a whole table row a link (invalid table markup and duplicate tab
stops); keeping text action links (the requested icon affordance).

Revisit if: the film page needs a date-range picker, the session table gains price/seat
data, or the time buckets become user-defined rather than code constants.

### D-021 Release gate: one deterministic script after the commit (2026-11-26)

Context: releases were a hand-run ladder (`refresh -> ratings -> posters -> export ->
wrangler`) with the same syntax, token, bundle, security and UI checks repeated from memory.
A skipped check or a dirty tree could reach production, and the deploy could not prove the
live alias was serving the build that was just pushed.

Decision: `scripts/release.py` is the single release gate. It refuses a dirty working tree,
runs `node --check`, the token/contrast/manifest checks, builds `dist`, then runs the bundle,
budget, security, unit and five Playwright checks. Only after all pass does it push the
current git branch, verify `origin/<branch>` equals `HEAD`, deploy `dist` with a pinned
`wrangler@4.166.0` to the Pages production branch `main`, and poll
`https://cinemelb.pages.dev` until the live hashed asset names match `dist/index.html`.
`--dry-run` runs every check and stops before push/deploy. The data refresh remains a manual
pre-step because it needs network and the ratings/posters rights; the gate builds from the
current database and caches.

Rejected: a shell one-liner (quoting and exit-code drift across PowerShell and POSIX); a
Makefile or npm script (two toolchains, Windows/POSIX split); deploying before pushing (the
live build would not correspond to an upstream commit); unpinned wrangler (a tool update
silently changes the release); letting the script refresh data (network/rights side effects
inside a gate).

Revisit if: the Pages project or production branch changes, or the release needs a data
refresh step (add an explicit flag with network consent, not a default).

### D-022 Ticket cue is the glyph, not the word (2026-11-26)

Context: after the session table shipped, every linked time repeated the word "Tickets".
On a venue with six sessions that is six identical words, and on a phone it crowded the
time pill without adding information.

Decision: linked rows show only the ticket glyph after the time; the time is underlined
(`--text-3`, accent on hover) so it reads as the link. The link's `aria-label` and `title`
still say "Tickets for <time> at <venue>, <day>", so assistive technology and hover keep
the action explicit. Sold-out and no-URL rows keep their text ("Sold out" / "At venue").
Each row still has exactly one link.

Rejected: a glyph-only cue without underlining the time (loses the click affordance);
keeping the word (noise); making the whole row a link (invalid table markup and duplicate
tab stops); a separate ticket column (two tab stops per session).

Revisit if: the glyph tests poorly with real users, or the session table gains a separate
price/ticket column.

### D-023 Day view starts with what is catchable now (2026-11-26)

Context: a day holds ~1,900 sessions and ~100 film cards. On a phone the time pills were
hidden below 640px, so the first screen showed titles and scores but not when or where the
next session was. Distance appeared only after an explicit location action, and the default
"Soonest" order had no "can I actually get there" signal.

Decision:
- Today's day view opens with a **Starting soon** module before the grouping: up to five
  unique films with a catchable session starting in the next 120 minutes. Rows order by
  start time without a place and by `minutes until start - drive minutes` once a place is
  set, so a nearby 9:30 session can outrank a distant 9:10 one. Each row names the start,
  film, venue and suburb, plus distance/drive when known; the start is the ticket link when
  the session has a URL and the title opens the film page. Past, sold-out, filtered and
  leaves-too-little-time rows are excluded; the module disappears when nothing qualifies.
- The collapsed day card is denser: 14px title, 11.5px meta, 36x54 cover, 58px summary. It
  names a relevant venue (`Cinema Nova +2`): the nearest one with a place, so the distance
  matches, otherwise the next session's venue. It shows distance/drive when a place is set,
  and shows up to three distinct start times at every width (all when <=3, `+N` otherwise).
  The phone-only `display:none` on time pills is removed.
- On load the app requests geolocation only when
  `navigator.permissions.query({name:'geolocation'})` already reports `granted`; a
  first-time visitor gets no surprise prompt and can use the near bar. GPS coordinates stay
  in memory (never localStorage); typed places still persist. The explicit Near me action
  keeps its 15-second watchdog. **Clear** writes a `cinemelb-nolocate` preference so a
  cleared location is not silently reacquired; Near me or typing a place removes it.

Rejected:
- A separate "now" route or tab: splits the day and hides the rest of the schedule.
- Auto-prompting geolocation on first paint: a surprise prompt is easy to deny, and a denial
  sticks; permission state is the honest signal for a silent request.
- Keeping five time pills: three is the most that fits a 320px card without wrapping, and
  fewer, larger targets read better.
- Removing list posters: recognition is worth the 36x54 box, which is about a sixth of the
  row.
- Showing every session in the module: five is a decision screen; the full list below keeps
  the complete schedule.

Revisit if: the app learns session end times (then "already started but catchable" enters
the module), or walk/transit modes replace the single drive estimate.

Consequences: the module respects filters and sold-out data and duplicates at most five
films from the list by design. Auto-location adds no network request; it only reads a
permission the user already granted.

### D-024 JS budget raised to 28 KB gz for the day-view decision layer (2026-11-26)

Context: D-023 added the Starting soon module, the venue/time-card logic and the
granted-permission locator. A fresh export measured `app.js` at 103,291 raw / 27.0 KB gz,
1.0 KB over the 26 KB cap; the release before this change was 25.1 KB gz (0.9 KB headroom),
so the old cap could not absorb a decision surface that renders session rows separately from
the film list.

Decision: raise the `app.js` cap to **28 KB gz** and keep the bundle (260), CSS (12) and HTML
(6) caps unchanged. The cap remains a real ceiling: the measured build is 27.0 KB with ~4%
headroom. This is a decision entry, not a silent budget change.

Rejected:
- Hand-minifying the IIFE: no build step, and an unreadable diff defeats the agent-legible
  source the project depends on.
- Dropping the module: it is the requested behaviour (what can I catch now, near me).
- Shipping it behind a flag: the bytes still ship, and the default path is the one that
  matters.
- Shaving comments to fit 26 KB: it would buy a few hundred bytes at the cost of the
  rationale future agents need.

Revisit when: `app.js` passes 28 KB gz, or a real minifier becomes acceptable; then reopen
the zero-build budget rather than shaving meaning out of the source.

## 11. Phase plan

| Phase | Scope (<=1 component or surface) | Acceptance check | Rollback boundary |
|---|---|---|---|
| 1 (complete 2026-11-26) | Substrate: `tokens.json`, `build_tokens.py`, `check_tokens.py`, `check_contrast.py`, `check_manifest.py`, `check_bundle.py`, `check_budget.py`, `components.json`, `gallery.html`, `window.CINEMELB`, `tests/ui/layout.py`, route focus + live counts, 44px mobile controls, rebuild `dist` | AC-001,002,003,006,007,009,010,016,018,019,020,022,024,028,029 green; gallery renders 13 live sections; 49 layout/DOM checks | Revert the commit. Generated block stripped leaves `app.css` byte-identical; no consumer changed |
| 2 (complete 2026-11-26) | `film-card` + `poster-tile` states (long title, no score, no cover, sold out, +N), score tokens, contrast fix for `--text-3` | AC-004,012,013,014,015 green via `tests/ui/states.py`; contrast >= 4.5:1 both themes | Revert `filmCard`/`posterTile` + manifest entries + token delta; no route change |
| 3 (complete 2026-11-26) | `view-bar`, `filter-bar`, `near-bar` controls (24/44px targets, `--focus-ring`, label association, pressed states) | AC-017,018,025,026 green via `tests/ui/layout.py`; 119 controls measured | Revert the three render functions and their CSS block; persisted localStorage keys unchanged |
| 4 (complete 2026-11-26) | Route/Chrome a11y remainder: announcement assertion for live counts, reduced-motion audit (focus, rail and 320px scroll already covered in phase 1) | AC-023,027 green via `tests/ui/layout.py` | Remove the focus/live-region calls; render output byte-identical otherwise |
| 5 (complete 2026-11-26) | `search-suggest` + search page states: option semantics, combobox keys, empty states, See-all row | AC-015,019,020,021 green via `tests/ui/states.py` + `tests/ui/search.py` | Revert `renderSuggest`/`renderSearch`; dropdown falls back to the current button options |
| 6 (complete 2026-11-26) | `film-hero` + `venue-block`/`session-pill` polish and film-page grouping persistence | AC-030 green via `tests/ui/film.py` (hero, safe score/venue links, three groupings, reload) | Revert `renderFilm`/`venueBlocks`; data path untouched |
| 7 (complete 2026-11-26) | Search refinement: match `aka`/programme prefix and loose title (D-016; bundle v3 adds `aliases`) | AC-039 green via `tests/ui/alias.py`; `check_bundle.py` validates aliases; 79 aliased films | Keep the v2 bundle and old `searchMatches`; `S.aliases` defaults to `{}` so the app tolerates an older bundle |
| 8 (complete 2026-11-26) | Security hardening: `sanitize.py`, map script/href/SRI, SPA `safeHref`, export marker/temp, `Host` allowlist, generic 500, ingestion + terminal strip, `tests/test_security.py`, `scripts/check_security.py`, `scripts/security_gate.py` | AC-031..AC-038, AC-040 green; gate ~1.1s; all review proofs flipped | Revert the per-file commits; UI render output is unchanged for benign data. Bundle v3 (phase 7) is orthogonal and validated separately |
| 9 (complete 2026-11-26) | Cinema-type filter (D-017) + RT resolver (D-018): derived `independent` tag, chain list narrowing, Wikidata P1258 join | AC-041 green via `tests/ui/states.py`; unit tests for tag derivation and P1258 batching; RT chips populated | Revert `_cinema_tags`/`cinemaKind` and the filter bar block; revert `_wikidata_rt_ids`; the bundle stays v3 and the cache keeps its old scores |
| 10 (complete 2026-11-26) | Film-page continuity (D-020): time-bucket scope, session table with a ticket cue, icon venue actions, Time/Distance cinema sort | AC-042,043,044 green via `tests/ui/film.py` + `states.py`; 320px table geometry; budgets hold | Revert `renderFilm`/`dayByTime`/`filmCard`/`venueBlocks`/`sessions`/`viewBar` and the session/venue CSS blocks; no data or bundle change |
| 11 (complete 2026-11-26) | Release gate (D-021): `scripts/release.py` with clean-tree refusal, full check ladder, pinned wrangler, push verification and live asset-hash poll; `--dry-run` | `--dry-run` green; full run pushes, deploys and verifies the production alias | Revert the script and its README/STATE/SPEC rows; no app or data change |
| 12 (complete 2026-11-26) | Ticket cue refinement (D-022): glyph-only cue, underlined time, accessible label retained | AC-044 green via `tests/ui/states.py`; 320px/film tests and live hash check | Revert `sessions()` and the `.st-buy`/`a.session .time` CSS; no data change |
| 13 (complete 2026-11-26) | Day-view efficiency (D-023): Starting soon module, dense cards with visible times/venue, granted-permission auto-locate | AC-045,046,047 green via `tests/ui/layout.py` + `states.py`; 320/390/1280; budgets hold | Revert `startingSoon`, `filmCard`, `autoLocate` and the day/soon CSS; persisted keys unchanged |

Rules: no phase starts before its decision exists; phase 1 is the substrate; each phase
names its AC ids; budgets (`check_budget.py`) run in every phase, not at the end.

### `components.json` v1 example

```json
{
  "$schema": "./components.schema.json",
  "version": 1,
  "updated": "2026-11-26",
  "components": [
    {
      "id": "film-card",
      "name": "Film card",
      "status": "stable",
      "source": "cinemelb/web/app.js",
      "render": "window.CINEMELB.render.filmCard",
      "gallery": "cinemelb/web/gallery.html#film-card",
      "props": [
        {"name": "filmIdx", "type": "int", "required": true},
        {"name": "rows", "type": "int[]", "required": true, "description": "screening row indices"}
      ],
      "states": ["collapsed", "expanded", "soon", "overflow", "no-score", "long-title"],
      "tokens": ["--card", "--line", "--radius", "--text", "--text-2", "--text-3",
                 "--accent", "--accent-dim", "--score-imdb", "--score-rt", "--score-mc",
                 "--focus-ring"],
      "a11y": {"element": "details/summary", "heading": "h2", "labelledBy": "summary text",
               "focusVisible": true, "keyboard": "native disclosure"},
      "examples": ["gallery/film-card--collapsed", "gallery/film-card--expanded"],
      "since": "2026-11-26"
    },
    {
      "id": "poster-tile",
      "name": "Poster tile",
      "status": "stable",
      "source": "cinemelb/web/app.js",
      "render": "window.CINEMELB.render.posterTile",
      "gallery": "cinemelb/web/gallery.html#poster-tile",
      "props": [
        {"name": "film", "type": "filmRow", "required": true},
        {"name": "extra", "type": "string", "required": false, "default": ""}
      ],
      "states": ["cover", "fallback-initials", "broken-image"],
      "tokens": ["--bg-2", "--line", "--text-3", "--radius-sm"],
      "a11y": {"role": "img (decorative)", "alt": "", "fallbackAriaHidden": true},
      "examples": ["gallery/poster-tile--cover", "gallery/poster-tile--fallback"],
      "since": "2026-11-26"
    },
    {
      "id": "score-chip",
      "name": "Score chip",
      "status": "stable",
      "source": "cinemelb/web/app.js",
      "render": "window.CINEMELB.render.scoreChips",
      "gallery": "cinemelb/web/gallery.html#score-chip",
      "props": [{"name": "film", "type": "filmRow", "required": true}],
      "states": ["imdb-only", "metacritic-only", "all-sources", "none"],
      "tokens": ["--bg-2", "--line", "--text", "--score-imdb", "--score-rt", "--score-mc"],
      "a11y": {"element": "a", "labelPattern": "<source> <score> (opens <source>)",
               "rel": "noopener noreferrer", "target": "_blank"},
      "examples": ["gallery/score-chip--all", "gallery/score-chip--none"],
      "since": "2026-11-26"
    },
    {
      "id": "session-table",
      "name": "Session table",
      "status": "stable",
      "source": "cinemelb/web/app.js",
      "render": "window.CINEMELB.render.sessions",
      "gallery": "cinemelb/web/gallery.html#session-table",
      "props": [{"name": "rows", "type": "int[]", "required": true}],
      "states": ["linked", "glyph-cue", "no-url", "sold-out", "soon"],
      "tokens": ["--card", "--card-2", "--line", "--line-2", "--text", "--text-2",
                 "--text-3", "--accent", "--accent-2", "--accent-dim", "--warn",
                 "--focus-ring"],
      "a11y": {"element": "table", "caption": "Sessions at <venue> on <day>",
               "headers": "Time, Format & screen",
               "labelPattern": "Tickets for <time> at <venue>, <day>",
               "visibleCue": "underlined time + ticket glyph (no text label)",
               "soldOutLabel": "Sold out"},
      "examples": ["gallery/session-table--linked", "gallery/session-table--sold-out"],
      "since": "2026-11-26"
    },
    {
      "id": "day-rail",
      "name": "Day rail",
      "status": "stable",
      "source": "cinemelb/web/app.js",
      "render": "window.CINEMELB.render.rail",
      "gallery": "cinemelb/web/gallery.html#day-rail",
      "props": [{"name": "current", "type": "isoDate|null", "required": false, "default": null}],
      "states": ["idle", "hover", "focus-visible", "current"],
      "tokens": ["--panel", "--line", "--text-2", "--accent", "--accent-ink", "--radius-sm", "--tap"],
      "a11y": {"nav": "Choose a day", "current": "aria-current=date", "element": "a"},
      "examples": ["gallery/day-rail--today"],
      "since": "2026-11-26"
    },
    {
      "id": "search-suggest",
      "name": "Search suggestions",
      "status": "beta",
      "source": "cinemelb/web/app.js",
      "render": "window.CINEMELB.render.suggest",
      "gallery": "cinemelb/web/gallery.html#search-suggest",
      "props": [{"name": "query", "type": "string", "required": true}],
      "states": ["closed", "open", "no-match", "keyboard-selected"],
      "tokens": ["--card", "--card-2", "--line", "--text", "--text-3", "--accent-2",
                 "--shadow", "--radius"],
      "a11y": {"combobox": true, "listbox": "suggest", "activeDescendant": true,
               "keys": "ArrowDown ArrowUp Enter Escape"},
      "examples": ["gallery/search-suggest--open", "gallery/search-suggest--empty"],
      "since": "2026-11-26"
    }
  ]
}
```

Remaining ids in §7 are added to the same file in phase 1 as `status: planned` with their
current `render` paths, so the manifest never lags the code.

## 12. Open questions

| # | Question | Owner | Blocks |
|---|---|---|---|
| 1 | Is `dist/data/bundle.json` (390 films) the build every baseline uses, and must `check_bundle --against-db` run before any UI evidence? — **resolved:** yes, dist is rebuilt and the check is green | maintainer | AC-029, all visual baselines |
| 2 | Should search match `aka`/programme prefixes ("BFF26", "Parky")? — **resolved:** bundle v3 `aliases` map (D-016), AC-039; "BFF26" finds the film and title matches still rank first | maintainer | Phase 7, AC-039 |
| 3 | Do the Playwright Python browser binaries install in this environment (`uv run playwright install chromium`)? - **resolved:** Chromium installs via the dev dependency and `tests/ui/layout.py` is green, so the T2 tier needs no npm | maintainer | AC-016..025 |
| 4 | Is `--text-3` allowed to stay below 4.5:1 for decorative 11px meta text, or does it become a AA token change? — **resolved:** text tokens now meet 4.5:1; border contrast deferred (D-010) | maintainer | AC-004, phase 2 |
| 5 | Does hotlinking IMDb/venue poster images carry a licensing or reliability risk worth self-hosting 320 covers? | maintainer | §4 licensing, `posterTile` |
| 6 | Should the app gain a CSP? The one inline `onclick` on `.film-open` and the inline theme script both shape that answer. External assets now carry SRI (D-015); the map's inline JSON is escaped (D-012) | maintainer | D-007 consequences, phase 2 |
| 7 | Is the 15s geolocation watchdog + iOS copy still correct on iOS 18/26, or does `navigator.permissions.query` allow a cleaner path? — **resolved for auto-locate:** the permissions state decides whether to locate silently on load; the explicit Near me action keeps the 15s watchdog | maintainer | near-bar AC (not yet written) |
| 8 | Does the repo get a LICENSE (code) now that posters and scores carry third-party terms? | maintainer | §4 |
| 9 | Should the deploy be automated? Hosting is a manual Direct Upload (D-019), so `refresh → export → wrangler` runs by hand; a cron needs a scheduler decision (GitHub Actions committing regenerated data vs a Cloudflare build hook, which would move the deploy to a git build) | maintainer | D-019 consequences, T5 |

## 13. Verification commands

| Tier | Command | Covers | Status |
|---|---|---|---|
| T0 | `node --check cinemelb/web/app.js` | app parses | green (verified exit 0) |
| T0 | `uv run python -c "import json;json.load(open('dist/data/bundle.json'))"` | bundle parses | green |
| T1 | `uv run python scripts/check_tokens.py` (`--coverage`, `--literals`, `--motion`) | token drift, orphan tokens, raw literals | green |
| T1 | `uv run python scripts/check_contrast.py` | WCAG ratios from tokens.json | green (text 4.5:1; borders warning D-010) |
| T1 | `uv run python scripts/check_manifest.py` | manifest <-> render fn <-> gallery <-> tokens | green (19 components / 25 tokens) |
| T1 | `uv run python scripts/check_bundle.py dist/data/bundle.json` (`--titles`, `--against-db`) | arity, indices, counts, prefixes, DB parity | green |
| T1 | `uv run python scripts/check_budget.py dist` | §8 gzip caps | green (238.4 / 27.0 / 7.5 / 1.8 KB gz) |
| T1 | `uv run python scripts/security_gate.py` | secrets in worktree + every commit, ruff S-rules, JS sinks, invariants, lock, bundle | green (~1.2s, 2026-11-26) |
| T1 | `uv run python scripts/check_security.py` | output-context invariants, SRI, export guard, Host allowlist, 500 text (also run by the gate) | green (2026-11-26) |
| T2 | `uv run python tests/ui/layout.py` | aria snapshots, geometry, focus, target size, Starting soon window, granted-permission auto-locate and Clear opt-out at 320/390/1280 | green |
| T2 | `uv run python tests/ui/states.py` | card/tile states, card time pills, session table, poster fallback, filtered empty | green |
| T2 | `uv run python tests/ui/search.py` | combobox semantics, keys, See-all, zero/empty search | green |
| T2 | `uv run python tests/ui/film.py` | hero, icon actions, time-bucket scope, After carry, cinema sort, 320px table geometry | green |
| T2 | `uv run python tests/ui/alias.py` | bundle aliases, alias search, title ranking, "listed as" | green |
| T2 | `uv run python tests/ui/aria.py --route day` | per-route aria baselines | planned (phase 4) |
| T3 | `uv run python -m unittest discover -s tests` | data, titles, ratings, posters, adapters, security, gate adapters | green (165 tests, 1 skipped) |
| T3 | `uv run python tests/ui/search.py` | search submit, combobox, empty states | green |
| T4 | `uv run python tests/ui/visual.py --only focus,poster-fallback` | focus ring, fallback tile | planned (phase 2/3) |
| T5 | `uv run python -m http.server 8801` then `/dist/` and `/cinemelb/web/gallery.html` | 320px/200% zoom, screen reader, both themes | manual, every phase |
| T5 | `uv run python scripts/release.py --dry-run` | clean tree + syntax/token/contrast/manifest/export/bundle/budget/security/unit/UI checks; no push/deploy | green (2026-11-26) |
| T5 | `uv run python scripts/release.py` | the gate + push + Pages deploy + live asset-hash poll | green (2026-11-26) |
| T5 | `uv run python -m cinemelb refresh && ...` then `uv run python scripts/release.py` | data refresh + full release | manual refresh; gate automated |

Dev preview note: `cinemelb/web/gallery.html` loads `./app.css`/`./app.js` directly and
fetches `/dist/data/bundle.json`, so it requires a prior `export` (which also proves the
bundle contract).

Screenshot rule (all five must hold): appearance question; T1/T2 already green; a stated
hypothesis and region; fixed capture (1280x800 + 360x800, DPR 1, one theme, reduced
motion); one artifact in `scratch/visual/<slug>-<hash>.png` with the verdict written next
to the AC. Never screenshot to discover structure.
