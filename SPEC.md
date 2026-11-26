# Spec: cinemelb — static showtimes UI and design system

Status: active | Owner: project maintainer (agent-authored) | Updated: 2026-11-26
Spec version: 1 | Supersedes: -

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

ARCHITECTURE: vanilla JS IIFE — cinemelb/web/app.js (~82,000 B), one stylesheet app.css
  (~30,000 B), one shell index.html (~4,824 B). Zero-build. Entry cinemelb/web/index.html.
  Hash routing: #/ (today), #/day/<iso>, #/film/<key>, #/search/<q>.
  Build: cinemelb/export.py::write_site -> dist/{index.html, assets/app.<hash>.{css,js},
  _headers, robots.txt, data/bundle.json}. Render functions are private to the IIFE, with a
  frozen window.CINEMELB.render namespace for the gallery.

CONSTRAINTS: Cloudflare Pages static, no server, no CSP header, hashed assets immutable
  (1 year), bundle cached 5 min + revalidate, nosniff + strict-origin-referrer.
  No offline/SW. Runtime network: one Nominatim geocode on explicit typed address, plus
  remote poster images (IMDb CDN / venue hosts). Browser matrix: iOS Safari 16+,
  Android Chrome, desktop Chrome/Edge, Firefox ESR. Budgets in §8.

SPEC: this file (SPEC.md), version 1. It is the only UI spec.

UI INVENTORY: 4 routes + shared chrome (header/search, day rail, filter bar, near bar,
  footer). Components exist as functions in app.js: renderRail, renderFilters, renderNear,
  viewBar, sortSelect, layoutToggle, filmCard, posterTile, dayByFilmGrid, dayByCinema,
  cinemaCard, dayByTime, venueBlocks, sessions, scoreChips, renderFilm, renderSearch,
  renderSuggest. One CSS file; no per-component CSS; tokens now generated from
  tokens.json; components.json + gallery.html added with this spec.

DESIGN SYSTEM: cinemelb/web/tokens.json -> generated block in app.css; components.json;
  cinemelb/web/gallery.html. See §7.

VERIFY COMMANDS: `uv run python -m unittest discover -s tests` -> 129 tests, OK
  (skipped=1), verified 2026-11-26; `node --check cinemelb/web/app.js` -> exit 0, verified;
  `uv run python -m cinemelb export --out dist` -> bundle build + _validate();
  `uv run python scripts/check_tokens.py --coverage --literals --motion`,
  `check_contrast.py`, `check_manifest.py`, `check_bundle.py --titles --against-db`,
  `check_budget.py dist` -> all green (phase 1). `python scripts/verify.py` -> live
  adapters, not UI. Playwright aria/visual suite is planned.

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
| Deploy | Cloudflare Pages, static, root base path `./`, hash routing, SPA fallback to index.html |
| CSP | None today. Any new inline handler is a liability; prefer delegated listeners |
| Offline | No. Bundle is fetched once; no service worker |
| Licensing | IMDb datasets non-commercial; OMDb CC BY-NC; Metacritic keyless finder undocumented; RT automation against ToS (opt-in only). App code has **no LICENSE file** (open question) |
| Bundle budget | §8 caps; exceeding one requires a decision entry |
| Browser matrix | iOS Safari 16+, Android Chrome (last 2), Chrome/Edge (last 2), Firefox ESR |
| Data/privacy | Typed places persisted in localStorage; GPS coordinates in memory only; no keys or PII in the bundle; Nominatim is the only outbound call, on user action |
| House rules | 44px primary tap targets; 16px inputs on iOS; ASCII in console output; `uv` only |

## 5. Data contracts

Upstream is SQLite (`data/cinemelb.sqlite3`, gitignored) -> `build_bundle()` -> one JSON
file. The bundle is versioned rather than back-compatible: a field change bumps
`version` and the exporter validates it.

| Entity | Shape (fields + types) | Source | Validation | Failure mode |
|---|---|---|---|---|
| bundle | `{version:2, generated:ISO, counts:{cinemas,films,screenings,dates,urls,rated,posters}, cinemas[], films[], screenings[], dates[], formats[], attrs[], screens[], urls[], places[]}` | `export.build_bundle` | `_validate()` raises `SystemExit` on version/arity/index/range errors | Export aborts — never a half-valid deploy |
| film row | 15 fields: `[key:str, title:str, year:int\|null, rating:str, runtime:int\|null, genres:str[], poster:str, synopsis:str(<=200), trailer:str, imdb_id:str, imdb:float\|null, rt:int\|null, mc:int\|null, rt_url:str, mc_url:str]` | `store.films()` + ratings + posters | arity 15; imdb 0..10; rt/mc 0..100; imdb 0 and mc 0 normalised to null | Missing score -> null -> chip omitted; never `0` or `—` |
| cinema row | 10 fields: `[name, chain, suburb, address, website, tags:str[], lat:float\|null, lon:float\|null, phone, booking_url]` | `store.cinemas(active_only=True)` | arity 10; lat/lon rounded 5dp | No coords -> Maps link falls back to name+address; no address and no links -> venue meta omitted |
| screening row | 9 ints: `[date_i, film_i, minute, cinema_i, format_i, attr_i, sold:0\|1, url_i(-1=none), screen_i]` | `store.screenings()` | each index in range; minute 0..1439; sold in {0,1}; url -1..len-1 | Dangling index aborts export |
| dates / formats / attrs / screens / urls | sorted string tables; attrs is `string[][]` of canonical badges (`CC, AD, Open Captions, HI, Subtitled, NFT`) in fixed order | export | attr spellings canonicalised before indexing | UI renders one vocabulary; unknown badge kept verbatim |
| places | `[[name, lat, lon], ...]` = landmarks + every venue suburb | `geo.known_places()` + registry | lat -90..90, lon -180..180, non-empty name | No places -> export aborts (SPA cannot geocode) |
| ratings.json | map `film key -> {imdb_id, imdb, rt, mc, rt_url, mc_url, source[], match_method, confidence, needs_review, fetched_at}` (gitignored) | `cinemelb ratings` | confidence gate; ambiguous -> `needs_review`, not displayed | Missing file -> all nulls; `--no-ratings` skips |
| posters.json | map `film key -> {url, source, imdb_id, matched_title, matched_year, confidence, match_method, status, fetched_at}` (gitignored) | `cinemelb posters` | exact/near-exact/year±1/ratings-id only; Palace `/poster/` treated as placeholder | No entry -> venue poster if real, else initials tile |
| title contract | `titles.display_title()` strips programme/venue prefixes (`BFF26`, `JIFF:`, `MIFF`, `JFF`, `SFF`, `CASFFA:`, `Cult Classix:`, `Astor:`, `Palace Cinemas:`, ...) at ingestion; `film_key()` keys on the cleaned title; `Store.normalise_films()` rewrites and merges collisions, original strings kept in SQLite `aka` | `cinemelb/titles.py`, `models.film_key`, `store.normalise_films` | `tests/test_titles.py`; conservative guards: bare `Astor`/`Palace` alone untouched, bare-word strip only for `astor`/`cult classix`, `_CODE` case-sensitive so "Staff Only" survives | Regression = duplicate film rows, broken rating/cover joins, festival noise in search |
| derived fields | `minute + 1440` and `date - 1` for starts before 06:00 at index time; first exported day's early rows dropped | `app.js::index` | `isPast`/`isTooSoon` compare absolute minutes | A 00:31 session belongs to the previous listing day, shown as `12:31am` |

Deploy freshness: `counts` + `generated` + last date are printed in the footer. The app
checks `Content-Type` before parsing so an SPA-fallback HTML page cannot be "parsed" as
JSON. Current `dist` (rebuilt after the title fix) reports 390 films, 58 cinemas,
19,057 screenings, 128 dates, 320 covers, 216 rated.

## 6. UI inventory and required states

| Surface | Route | Component ids | States required | Empty / loading / error |
|---|---|---|---|---|
| App shell | all | `app-header`, `brand`, `theme-toggle`, `clock` | default, scrolled/sticky, light, dark, 320px | clock hidden <640px; no loading state |
| Search box + suggestions | all | `search-box`, `search-suggest`, `suggest-option`, `suggest-all` | closed, open, no-match, 6 results, keyboard-selected, query preserved | `sug-empty` "No films match"; suggestions closed on route change |
| Day rail | all | `day-rail`, `day-rail-item` | default, hover, focus-visible, current (`aria-current="date"`), 14-day window, scrolled | not rendered before bundle; no error state |
| Filter bar | day, film | `filter-bar`, `filter-chip`, `filter-select`, `clear-filters` | off, on (`aria-pressed`), disabled n/a, horizontal scroll <640px, chain absent when 1 chain, "within" absent until place | hidden on search route |
| Near bar | day, film | `near-bar`, `near-me`, `near-input`, `near-status`, `near-clear` | unset, requesting (button disabled + status), set (distance line), denied, insecure context, 15s timeout, geolocation vs typed | error text names the platform cause; input remains usable; hidden on search route |
| Day list — by film | `#/`, `#/day/<iso>` | `film-list`, `film-card`, `film-times`, `film-open` | collapsed, expanded, soon pill, +N overflow, sold-out-free (card level), no place, place set, 1 vs many cinemas | three distinct empties: no sessions that day / day over / filters hid N sessions with Clear |
| Day grid — poster tiles | same | `poster-grid`, `poster-tile`, `poster`, `poster-fallback` | 3 cols phone, 8 max desktop, no cover, broken image, no scores, next session not today, long title (2-line clamp) | same empties as list |
| Day — by cinema | same | `cinema-card`, `day-sub` | collapsed, expanded, distance, from-time, 1 film vs many | same empties |
| Day — by time | same | `time-section` (`<details open>`) | open, collapsed, empty bucket omitted, late-night >24:00 label | same empties |
| Film page | `#/film/<key>` | `film-hero`, `film-poster`, `score-chip`, `film-facts`, `trailer`, `session-section` | by day / by cinema / by time (persisted), no poster, no scores, no synopsis, many days, not found | "Film not found"; "No sessions left"; "N hidden by filters" + Clear |
| Search page | `#/search/<q>` | `search-head`, `search-result-grid`, `search-sort` | results, 1 result, 0 results, empty query, long query, sort by relevance/rating/title | 0 results names the query; empty query invites browsing |
| View bar | day, film, search | `view-bar`, `sort-select`, `layout-toggle` | pressed/unpressed, Nearest absent without place, grid active, mobile wrap | n/a |
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
| `view-bar` | stable | `viewBar`, `sortSelect`, `layoutToggle` | grouping + sort + grid toggle |
| `film-card` | stable | `filmCard` | `<details>` with poster, meta, time pills, arrow |
| `poster-tile` | stable | `filmTile`, `posterTile` | one link per film; initials fallback |
| `poster-grid` | stable | `dayByFilmGrid`, `renderSearch` | 3..8 columns |
| `score-chip` | stable | `scoreBits`, `scoreChips` | inline in meta vs linked chips |
| `venue-block` | stable | `venueBlocks`, `venueMeta`, `sessions` | address + Directions/Tickets/Website/phone |
| `session-pill` | stable | `sessions` | link / static / sold-out variants |
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
| `data/bundle.json` (390-film build) | 1,900,000 | 240,000 | 260 KB | 8% |
| `app.js` | 82,000 | 21.3 KB | 26 KB | 18% |
| `app.css` (with generated tokens) | 30,000 | 6.7 KB | 12 KB | 44% |
| `index.html` | 4,824 | 1.8 KB | 6 KB | 70% |
| Font files | 0 | 0 | 0 | system stack only |

- JS <= 26 KB gz, CSS <= 12 KB gz, HTML <= 6 KB gz, bundle <= 260 KB gz.
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
  check: dom: uv run --with playwright python tests/ui/network.py::no-third-party
  status: planned(phase-1)

AC-006 [must] Every components.json id resolves to an existing window.CINEMELB.render
function and a live gallery example; missing ids fail the check.
  check: manifest: uv run python scripts/check_manifest.py
  status: passing (18 components)

AC-007 [must] Every token named in components.json exists in the generated block.
  check: manifest: uv run python scripts/check_manifest.py --tokens
  status: passing (25 tokens)

AC-008 [must] The gallery renders components by calling the app's render functions, and
each manifest state has an aria snapshot.
  given: cinemelb/web/gallery.html#film-card
  check: aria: uv run --with playwright python tests/ui/aria.py --gallery film-card
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
  check: dom: uv run --with playwright python tests/ui/states.py  (no-scores case)
  status: passing (no chip, no source name; 0 zero-scored film rows)

AC-013 [must] A missing or broken cover renders the initials tile without layout shift.
  check: dom: uv run --with playwright python tests/ui/states.py  (poster-fallback case)
  status: passing (initials tile, absolute fallback, cover/fallback boxes equal)

AC-014 [must] A session with no ticket URL renders as a non-link pill with its time; a
sold-out session is dashed, struck through, labelled "Sold out", and unlinked.
  check: dom: uv run --with playwright python tests/ui/states.py  (session-variants case)
  status: passing (span, no href, dashed, line-through, "Sold out")

AC-015 [must] While a filter hides every session on a day, the results region says how
many sessions were hidden and offers a Clear control.
  check: dom: uv run --with playwright python tests/ui/states.py  (filtered-empty case)
  status: passing (hidden count named, Clear restores the day)

AC-016 [must] The day rail marks exactly one day with aria-current="date" and shows the
14-day window from today.
  check: dom: uv run --with playwright python tests/ui/layout.py  (rail case, 320/390/1280)
  status: passing (1 aria-current, 14 days at every width)

AC-017 [must] Grouping and sort controls expose aria-pressed/selected state and persist
across reload via cinemelb-view/cinemelb-sort; "Nearest" is absent without a place.
  check: dom: uv run --with playwright python tests/ui/layout.py  (persistence case)
  status: passing (Cinemas + Title survive reload; no near option without a place)

AC-018 [must] In grid layout each film is exactly one link whose accessible name carries
the title, and that link opens #/film/<key>.
  check: dom: uv run --with playwright python tests/ui/layout.py  (poster-tile case)
  status: passing (tile is one <a>, href #/film/, text includes the title)

AC-019 [must] When Enter is pressed with text in the header search, the app routes to
#/search/<q>; an empty query does not navigate; the suggestion list ends with "See all N
results".
  check: dom: uv run --with playwright python tests/ui/layout.py  (search cases)
  status: passing ("See all 13 results", empty Enter keeps the hash, Enter routes)

AC-020 [must] The search page hides the session filter bar and the near bar.
  check: dom: uv run --with playwright python tests/ui/layout.py  (search-chrome case)
  status: passing (both display: none on #/search/<q>)

AC-021 [must] Arrow Down/Up move aria-activedescendant to a real option id, Enter opens
the highlighted film (or the results page), Escape closes the list and keeps the query.
  check: aria: uv run --with playwright python tests/ui/search.py  (combobox cases)
  status: passing — options are non-interactive role=option rows; focus stays in the
          input; Escape closes, keeps the query and prevents the UA search-clear

AC-022 [must] Route changes move focus to the results heading and update document.title,
so a screen reader starts at the new content.
  check: dom: uv run --with playwright python tests/ui/layout.py  (route-focus case)
  status: passing (activeElement is H1; title is "Search: … — cinemelb")

AC-023 [must] When filters change the result set, the new film/session count is announced
politely.
  check: dom: uv run --with playwright python tests/ui/layout.py  (announce-count case)
  status: passing (aria-live="polite"; a filter rewrites "1 film · 1 session" to "0 films · 0 sessions")

AC-024 [must] At 320px wide, document.scrollWidth <= clientWidth on every route.
  check: dom: uv run --with playwright python tests/ui/layout.py  (320px route sweep)
  status: passing (day, search and film routes at 320px; also 390/1280)

AC-025 [must] Every interactive target is >= 24x24 CSS px (WCAG 2.2 2.5.8); primary
mobile controls are >= 44px high as a project rule.
  check: dom: uv run --with playwright python tests/ui/layout.py  (target-size case)
  status: passing — 119 controls measured at 320/1280, none below 24x24; primary mobile
          controls are 44px. Inline links in a sentence use the 2.5.8 exception.

AC-026 [must] In both themes every interactive element shows the focus ring; ring colour
comes from --focus-ring, not a literal.
  check: dom: uv run --with playwright python tests/ui/layout.py  (focus-ring case)
  status: passing — keyboard focus paints a 2px ring whose computed colour equals the
          active theme's --focus-ring (#f3c876 dark, #7d560d light)

AC-027 [must] prefers-reduced-motion removes non-essential animation and shortens the
spinner.
  check: dom: uv run --with playwright python tests/ui/layout.py  (reduced-motion case)
  status: passing (spinner 2.4s; no other infinite animation; media query active)

AC-028 [must] Budgets hold: bundle <= 260 KB gz, JS <= 26 KB gz, CSS <= 12 KB gz,
HTML <= 6 KB gz.
  check: budget: uv run python scripts/check_budget.py dist
  status: passing (bundle 240 KB, js 21.3 KB, css 6.7 KB, html 1.8 KB gz)

AC-029 [must] A fresh export from the current database reports 390 films, 58 cinemas,
19,057 screenings and 128 dates, matching Store counts.
  check: bundle: uv run python scripts/check_bundle.py --against-db
  status: passing

AC-030 [should] The film page groups sessions by day, cinema or time, and remembers the
choice with the day view's preference.
  check: dom: uv run --with playwright python tests/ui/film.py::grouping
  status: planned(phase-6)
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
pills (then reuse `session-pill` inside the tile).

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
Playwright is used from Python via `uv run --with playwright` so no npm enters the repo;
baselines live in `tests/ui/snapshots/` and `scratch/visual/` (gitignored) for one-offs.
Rejected: Playwright via npm (`npx playwright test`) — pulls Node packages into a repo that
has none; manual browser-only checks (the current cost); screenshot-first review (the
anti-pattern this spec exists to stop).
Revisit if: `uv run --with playwright` cannot provision browser binaries here, in which
case fall back to `manual:` with recorded evidence and log the gap in §4.

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
`check_budget.py`; `window.CINEMELB.render`; `gallery.html` with 13 live sections and 5
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


## 11. Phase plan

| Phase | Scope (<=1 component or surface) | Acceptance check | Rollback boundary |
|---|---|---|---|
| 1 (complete 2026-11-26) | Substrate: `tokens.json`, `build_tokens.py`, `check_tokens.py`, `check_contrast.py`, `check_manifest.py`, `check_bundle.py`, `check_budget.py`, `components.json`, `gallery.html`, `window.CINEMELB`, `tests/ui/layout.py`, route focus + live counts, 44px mobile controls, rebuild `dist` | AC-001,002,003,006,007,009,010,016,018,019,020,022,024,028,029 green; gallery renders 13 live sections; 49 layout/DOM checks | Revert the commit. Generated block stripped leaves `app.css` byte-identical; no consumer changed |
| 2 (complete 2026-11-26) | `film-card` + `poster-tile` states (long title, no score, no cover, sold out, +N), score tokens, contrast fix for `--text-3` | AC-004,012,013,014,015 green via `tests/ui/states.py`; contrast >= 4.5:1 both themes | Revert `filmCard`/`posterTile` + manifest entries + token delta; no route change |
| 3 (complete 2026-11-26) | `view-bar`, `filter-bar`, `near-bar` controls (24/44px targets, `--focus-ring`, label association, pressed states) | AC-017,018,025,026 green via `tests/ui/layout.py`; 119 controls measured | Revert the three render functions and their CSS block; persisted localStorage keys unchanged |
| 4 (complete 2026-11-26) | Route/Chrome a11y remainder: announcement assertion for live counts, reduced-motion audit (focus, rail and 320px scroll already covered in phase 1) | AC-023,027 green via `tests/ui/layout.py` | Remove the focus/live-region calls; render output byte-identical otherwise |
| 5 (complete 2026-11-26) | `search-suggest` + search page states: option semantics, combobox keys, empty states, See-all row | AC-015,019,020,021 green via `tests/ui/states.py` + `tests/ui/search.py` | Revert `renderSuggest`/`renderSearch`; dropdown falls back to the current button options |
| 6 | `film-hero` + `venue-block`/`session-pill` polish and film-page grouping persistence | AC-030 | Revert `renderFilm`/`venueBlocks`; data path untouched |
| 7 | Search refinement: match `aka`/programme prefix and loose title (needs a new D-entry; likely bundle v3 or a `aliases` table) | New AC: query "BFF26" and "Parky" both return the film; `bundle` arity check updated | Keep v2 bundle and old `searchMatches`; ship behind `#/search/<q>?aliases=0` until the AC is green |

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
      "id": "session-pill",
      "name": "Session pill",
      "status": "stable",
      "source": "cinemelb/web/app.js",
      "render": "window.CINEMELB.render.sessions",
      "gallery": "cinemelb/web/gallery.html#session-pill",
      "props": [{"name": "rows", "type": "int[]", "required": true}],
      "states": ["linked", "no-url", "sold-out", "soon"],
      "tokens": ["--card", "--line", "--text", "--text-3", "--accent-2", "--warn"],
      "a11y": {"element": "a | span", "labelPattern": "<time>[ · <format> · <badges>]",
               "soldOutLabel": "Sold out"},
      "examples": ["gallery/session-pill--linked", "gallery/session-pill--sold-out"],
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
| 2 | Should search match `aka`/programme prefixes ("BFF26", "Parky")? Needs a bundle v3 field or an aliases table, and a decision entry | maintainer | Phase 7, AC for alias search |
| 3 | Do the Playwright Python browser binaries install in this environment (`uv run --with playwright playwright install chromium`)? — **resolved:** Chromium installs and `tests/ui/layout.py` is green, so the T2 tier needs no npm | maintainer | AC-016..025 |
| 4 | Is `--text-3` allowed to stay below 4.5:1 for decorative 11px meta text, or does it become a AA token change? — **resolved:** text tokens now meet 4.5:1; border contrast deferred (D-010) | maintainer | AC-004, phase 2 |
| 5 | Does hotlinking IMDb/venue poster images carry a licensing or reliability risk worth self-hosting 320 covers? | maintainer | §4 licensing, `posterTile` |
| 6 | Should the app gain a CSP? The one inline `onclick` on `.film-open` and the inline theme script both shape that answer | maintainer | D-007 consequences, phase 2 |
| 7 | Is the 15s geolocation watchdog + iOS copy still correct on iOS 18/26, or does `navigator.permissions.query` allow a cleaner path? | maintainer | near-bar AC (not yet written) |
| 8 | Does the repo get a LICENSE (code) now that posters and scores carry third-party terms? | maintainer | §4 |

## 13. Verification commands

| Tier | Command | Covers | Status |
|---|---|---|---|
| T0 | `node --check cinemelb/web/app.js` | app parses | green (verified exit 0) |
| T0 | `uv run python -c "import json;json.load(open('dist/data/bundle.json'))"` | bundle parses | green |
| T1 | `uv run python scripts/check_tokens.py` (`--coverage`, `--literals`, `--motion`) | token drift, orphan tokens, raw literals | green |
| T1 | `uv run python scripts/check_contrast.py` | WCAG ratios from tokens.json | green (text 4.5:1; borders warning D-010) |
| T1 | `uv run python scripts/check_manifest.py` | manifest <-> render fn <-> gallery <-> tokens | green (18 components / 25 tokens) |
| T1 | `uv run python scripts/check_bundle.py dist/data/bundle.json` (`--titles`, `--against-db`) | arity, indices, counts, prefixes, DB parity | green |
| T1 | `uv run python scripts/check_budget.py dist` | §8 gzip caps | green (240 / 21.3 / 6.7 / 1.8 KB gz) |
| T2 | `uv run --with playwright python tests/ui/layout.py` | aria snapshots, geometry, focus, target size at 320/390/1280 | green (49 checks) |
| T2 | `uv run --with playwright python tests/ui/states.py` | card/tile/session states, poster fallback, filtered empty | green |
| T2 | `uv run --with playwright python tests/ui/search.py` | combobox semantics, keys, See-all, zero/empty search | green |
| T2 | `uv run --with playwright python tests/ui/aria.py --route day` | per-route aria baselines | planned (phase 4) |
| T3 | `uv run python -m unittest discover -s tests` | data, titles, ratings, posters, adapters | green (129 tests, 1 skipped) |
| T3 | `uv run --with playwright python tests/ui/search.py` | search submit, combobox, empty states | planned (phase 5) |
| T4 | `uv run --with playwright python tests/ui/visual.py --only focus,poster-fallback` | focus ring, fallback tile | planned (phase 2/3) |
| T5 | `uv run python -m http.server 8801` then `/dist/` and `/cinemelb/web/gallery.html` | 320px/200% zoom, screen reader, both themes | manual, every phase |
| T5 | `uv run python -m cinemelb refresh && ... && npx wrangler pages deploy dist --project-name cinemelb --branch main` | deploy | manual, release gate |

Dev preview note: `cinemelb/web/gallery.html` loads `./app.css`/`./app.js` directly and
fetches `/dist/data/bundle.json`, so it requires a prior `export` (which also proves the
bundle contract).

Screenshot rule (all five must hold): appearance question; T1/T2 already green; a stated
hypothesis and region; fixed capture (1280x800 + 360x800, DPR 1, one theme, reduced
motion); one artifact in `scratch/visual/<slug>-<hash>.png` with the verdict written next
to the AC. Never screenshot to discover structure.
