# cinemelb

Query film session times across **every cinema in Melbourne** — the multiplexes
and the independents — from one command line.

Ask it for a film and it tells you every venue showing it. Ask it for a time
and it tells you every film. Give it a location and it ranks everything by
distance. It knows where each cinema is, so "closest showing of Dune to me"
is a single question.

```
$ cinemelb film "dune" --date tomorrow --near brunswick
$ cinemelb at 20:00 --date fri --near "melbourne cbd"
$ cinemelb now --near 3000 --hours 3
```

---

## Why this exists

Melbourne has ~63 cinema venues across 10 different booking platforms. There is
no public API that covers them. Flicks aggregates some; Google covers the
chains; none of them will answer *"what arthouse film starts between 7 and 9pm
within 3 km of me"* against the full set.

This tool scrapes each platform into one normalised SQLite database and answers
those questions.

---

## Quick start

```bash
pip install -r requirements.txt          # requests, typer, rich, tzdata

python scripts/harvest_registry.py       # build data/cinemas.json (63 venues)
python scripts/enrich_registry.py        # attach upstream chain IDs

python -m cinemelb refresh               # scrape session times
python -m cinemelb trending              # sanity check
```

Then:

```bash
python -m cinemelb --help
```

---

## Commands

### `film` — where is this film showing?

```bash
python -m cinemelb film "the fox"                    # every venue, grouped
python -m cinemelb film dune --date friday --after 18:00
python -m cinemelb film dune --near brunswick --radius 5
python -m cinemelb film dune --tag arthouse          # art-houses only
python -m cinemelb film dune --format 70mm           # presentation format
python -m cinemelb film dune --accessible            # captions / audio description
python -m cinemelb film dune --list                  # flat list, sorted by time
```

Titles are matched fuzzily, so `hunger games` finds *The Hunger Games: Sunrise
on the Reaping*, and `dune` finds *Dune: Part Three*.

### `at` — what starts around a given time?

```bash
python -m cinemelb at 20:00 --date friday            # ±30 min by default
python -m cinemelb at 8pm --window 15 --near fitzroy
```

### `now` — what can I see in the next few hours?

```bash
python -m cinemelb now --hours 3 --near "melbourne cbd"
```

### `next` — when is the next showing near me?

```bash
python -m cinemelb next dune --near brunswick
```

Answers "the soonest screening, and how far it is" in one line per cinema.

### `week` — a film's week at a glance

```bash
python -m cinemelb week dune --days 10
```

Day-by-day venue and session counts, which is how you spot that an arthouse
release only plays weekends.

### `map` — an interactive map of results

```bash
python -m cinemelb map dune --near brunswick --radius 12 --open
python -m cinemelb map --date tomorrow --near 3000 --radius 3
```

Writes a self-contained HTML page: Leaflet map with a pin per venue (sized by
how many sessions it has), your origin and search radius, and a sidebar listing
every session with booking links. Opens instantly and works offline apart from
the basemap tiles.

### `near` — everything within a radius

```bash
python -m cinemelb near brunswick --radius 2
python -m cinemelb near "-37.7981,144.9679" --radius 1.5
python -m cinemelb near 3000 --films                # one row per film
```

Locations can be a suburb, postcode, landmark (`fed-square`), street address,
or raw `lat,lon`. Suburbs and landmarks resolve offline; anything else uses a
cached Nominatim lookup.

### `whats-on` / `trending` — the shape of the week

```bash
python -m cinemelb whats-on --date tomorrow
python -m cinemelb trending
```

### `cinemas` — the venue registry

```bash
python -m cinemelb cinemas --detail
python -m cinemelb cinemas --tag arthouse
python -m cinemelb cinemas --suburb Carlton
```

### `refresh` / `stats` / `doctor` — operations

```bash
python -m cinemelb refresh                       # all venues
python -m cinemelb refresh --source hoyts        # one platform
python -m cinemelb refresh --only brunswick      # venues matching text
python -m cinemelb refresh --fresh               # bypass the HTTP cache
python -m cinemelb stats                         # what's in the database
python -m cinemelb doctor                        # adapter coverage
```

### `serve` — a read-only web page

```bash
python -m cinemelb serve                # http://127.0.0.1:8787
python -m cinemelb serve --port 8799
```

Server-rendered, no JavaScript, no CDN: the same queries as the CLI in a
browser, with every view bookmarkable and the state carried in the URL. Useful
when you want to hand someone a link rather than a terminal.

### `export` — build the static site

```bash
python -m cinemelb export                        # writes ./dist
python -m cinemelb export --out site             # elsewhere
python -m cinemelb export --days 30              # next 30 days only
python -m cinemelb export --synopsis 0           # keep full synopses
```

The output is a self-contained static site — one HTML page, two content-hashed
assets, and `data/bundle.json` — that answers the same questions as the CLI with
no server at all. It is live at **https://cinemelb.pages.dev**.

Deploying it is one command, after a refresh so the times are current:

```bash
python -m cinemelb refresh
python -m cinemelb ratings
python -m cinemelb posters
python -m cinemelb export --out dist
npx wrangler pages deploy dist --project-name cinemelb --branch main
```

`dist/` is generated, not committed: the bundle is a build artifact of whatever
is in `data/cinemelb.sqlite3` at the time. `cinemelb/web/` is the source shell
(`index.html`, `app.css`, `app.js`, `_headers`).

The bundle is index-table shaped. A screening is nine integers pointing into
shared tables (date, film, minute, cinema, format, attribute set, sold-out,
ticket URL, screen), and the whole 19k-screening dataset is about 1.9 MB raw,
or roughly 235 KB gzipped over the wire. Attribute spellings are canonicalised
(`Closed Captioning Available` → `CC`), synopses are capped at 200 characters,
and rows are sorted by date then minute so a day is a contiguous slice.

The UI reads that one file once and then works in memory: a day by day rail,
accessible-screening and chain filters, per-film pages with every remaining
session, and title search with suggestions. Routing is hash-based, so a deep
link never asks the host for a path.

**Grouping.** A day holds ~1,900 sessions, so the listing can be pivoted three
ways without ever rendering 1,900 rows:

| Group by | Shape |
|---|---|
| **By film** (default) | one card per film, with its cinemas and next times |
| **By cinema** | one expandable card per venue, listing its films and sessions |
| **By time** | the same film cards, split into morning / afternoon / evening / late night |

A film page offers the same pivot: **by day** (day → venue), **by cinema**
(venue → day), or **by time** (time of day → day → venue). The choice is a view
preference, so it is remembered in `localStorage` and survives a reload.

**Sorting.** Beside the grouping control is a **Sort** menu: *Soonest* (the
schedule), *Top rated*, *Title A–Z*, and *Nearest* once a location is set. Top
rated averages whichever scores a film has, normalised to 0–1, so a film with
only a Metascore still ranks sensibly; unrated films sort last. The choice is
remembered too.

The time-of-day sections collapse: click a heading to fold that part of the day
away, which makes "what is on after work" readable without scrolling past the
morning. And a session after midnight belongs to the **previous** listing day —
the 00:31 on the 27th is the 26th's Late night, still shown as `12:31am` — so a
Friday evening out does not lose its midnight session to Saturday's page.

**Filters.** Beyond accessible screenings and chain, two session-level filters
cut the list down:

- **After** a time of day — `After 7pm` keeps only sessions that start at or
  after 19:00, on whichever day you are looking at.
- **Within** a drive time — `Within 30 min drive` keeps only sessions at venues
  reachable in that many minutes, and only appears once a location is set.

"Leaves time to get there" becomes travel-aware when a place is set: it hides
sessions starting sooner than the drive to the venue plus 20 minutes, not a flat
20 minutes. Filters are in memory only; grouping and sort are persisted.

**"Near me"** takes either a typed suburb or address, or the browser's own
location (`navigator.geolocation`, which needs HTTPS and your permission), then
sorts the day by distance and shows `1.6 km · ~4 min drive` on each film and
next to every venue. Suburbs and landmarks resolve from a gazetteer shipped in
the bundle; anything else is looked up through OpenStreetMap's Nominatim, which
is the only request the page makes to a third party. Distances are
great-circle and drive times divide by 22 km/h — the same planning figure
`cinemelb near` uses, so the page and the CLI agree. Typed places are
remembered in `localStorage`; coordinates from the browser's location are kept
in memory only and never written to disk.

**Film pages and venues.** A film card expands to times and venues in place, and
carries a small arrow to its own page (the page used to be reachable only by
searching). Every venue line shows its street address plus a Google Maps
"Directions" link, and its website, booking page and phone where the registry
has them. Session times link straight to the venue's own ticket page; when a
session has no URL, the venue link is the fallback rather than a dead button.

**Ratings.** IMDb, Rotten Tomatoes and Metacritic chips come from
`data/ratings.json`, baked in at export time by `python -m cinemelb ratings`;
the browser never calls a ratings API and holds no key. Scores are printed on
the collapsed film card (`IMDb 7.5 · MC 55`) as well as in its expanded body
and on the film page, so the Top rated sort has something visible to order by.
A missing score is not rendered — never a dash or a zero — and each chip links
to its source. See `docs/api-reference/` for the matching rules and the terms
behind each source.

**Covers.** Posters are resolved at build time by `python -m cinemelb posters`
and cached in `data/posters.json`. Venue feeds were the original source, but
Palace answers every `/poster/<name>` request with the same Palace Cinemas logo,
so a third of the catalogue was wearing the house advert as a cover. The
resolver asks IMDb's public search-suggestion endpoint and accepts only an
exact normalised-title match, a near-exact match with a year within one, or a
candidate whose id is the one the ratings pass already resolved. A film with no
confident match keeps its venue poster when that poster is real, and otherwise
gets a typographic tile in the UI. The image is requested at 300×444, so a
cover costs ~16 KB and the full-size original is never downloaded.

`_headers` gives the hashed assets a one-year immutable cache and the bundle
five minutes with revalidation. A `404.html` is deliberately absent: Cloudflare
Pages answers unknown paths with `index.html`, and the app checks the data
file's `Content-Type` before parsing it so a misconfigured deploy fails loudly
instead of "parsing" an HTML error page as JSON.

### `ratings` — attach IMDb / RT / Metacritic scores

```bash
python -m cinemelb ratings                  # fill data/ratings.json
python -m cinemelb ratings --limit 20       # smoke run (new lookups only)
OMDB_API_KEY=... python -m cinemelb ratings # also Rotten Tomatoes + Metacritic
python -m cinemelb ratings --refresh        # ignore the cache
```

Build-time only: the browser never sees a key or calls a ratings service. Titles
are normalised (`- IMAX 70MM`, `JFF26 …`, `(2003)`, `, The`, `Se7en`) and matched
against the official IMDb bulk files, with Wikidata as a fallback for IDs. The
confidence gate stores ambiguous matches as `needs_review` and never displays
them; runtime and vote count outrank recency, so a repertory screening of
*AKIRA* resolves to the 1988 film rather than the remake.

IMDb scores come from `title.ratings.tsv.gz`. RT/MC come from OMDb when a free
`OMDB_API_KEY` is set (1,000 requests/day); without one, Metacritic has a
keyless fallback and Rotten Tomatoes needs `--scrape-rt-mc`, which is against
both sites' terms — see `docs/api-reference/rt-metacritic.md`. Everything lands
in `data/ratings.json` (gitignored); `export` bakes it in, and `--no-ratings`
leaves the fields null.

### `posters` — resolve a real cover for every film

```bash
python -m cinemelb posters                  # fill data/posters.json
python -m cinemelb posters --limit 20       # smoke run (new lookups only)
python -m cinemelb posters --refresh        # ignore the cache
```

Build-time only, like ratings. IMDb's public search-suggestion endpoint returns
a poster URL with each candidate; the gate accepts an exact normalised title,
a near-exact title with a release year within one, or the IMDb id the ratings
pass resolved. Nothing looser — a wrong cover is worse than no cover. Films with
no confident match keep their venue's poster if it is real (Palace's
`/poster/` endpoint is always its logo, so it is treated as a placeholder), and
otherwise render a typographic tile. Cached URLs are requested at 300×444 from
the IMDb image CDN, roughly 16 KB per cover.

`--json` is a global flag and goes **before** the command:

```bash
python -m cinemelb --json film dune | jq '.screenings[].cinema'
python -m cinemelb --json trending | jq -r '.[] | "\(.cinema_count)\t\(.title)"'
```

---

## Coverage

63 venues in greater Melbourne, including every independent and art-house
cinema. 58 are active and **all 58 return live session data** (~19,000 sessions
across 400 films); the 5 remaining registry rows are documented as dormant or
non-cinema in the Limitations below.

Verified against the venues' own pages by an independent auditor
(`_recon/audit.py`): 40 randomly sampled sessions across Hoyts, Village, Palace
and Moving Story all matched the venue's real listing for film, venue and
advertised local date/time, with no timezone or day-of-week drift. Moving Story's
coverage was reconciled against its date-keyed pages at 743/744 sessions present.

| Platform | Venues | How |
|---|---|---|
| Hoyts | 12 | JSON API — `/api/sessions` returns the whole chain in one ~9 MB request |
| Village | 14 | same-origin `/api/algolia/sessions/hits?f.c=<cinemaId>` proxy (one call per venue) |
| Palace (incl. The Astor, The Kino, Pentridge) | 9 | per-venue Next.js data payload |
| Reading | 7 | SPA API at `prod-api.readingcinemas.com.au` — anonymous token from `/settings/1`, then Bearer auth |
| Moving Story (Lido, Classic Elsternwick, Cameo Belgrave) | 3 | server-rendered HTML, date-addressed `/now-showing/<date>` pages |
| Cinema Nova | 1 | `/films-by-date-<epoch>` server-rendered pages |
| Sun Theatre Yarraville | 1 | WordPress pages with `?wp-cinema-day=N` |
| ACMI | 1 | Nuxt `window.__NUXT__` payload |
| IMAX Melbourne | 1 | Lumos SPA → Vista Connect OCAPI (`/film-screening-dates`, `/showtimes/by-business-date`) |
| Indy-systems (Brunswick, Thornbury Picture House) | 2 | GraphQL `POST /graphql` |
| Lumos/Vista (United Craigieburn) | 1 | `gasapi` business-date endpoint |
| Waverley Cinemas | 1 | `/api/movie/playing-now` JSON |
| Small independents | 9 | `generic_html` strategies (Veezi, JSON APIs, WordPress, HTML) |

The independents in the registry include Cinema Nova, The Astor, Lido, Classic
Elsternwick, Sun Theatre, Cameo Belgrave, Thornbury Picture House, Brunswick
Picture House, Eclipse Cinema Collingwood, FoMo East Brunswick, Chinatown
Cinema, Metro Boronia, Waverley Cinemas, United Craigieburn, ACMI, IMAX, and
Moonlight Cinema (seasonal, outdoor).

---

## How it works

```
scripts/harvest_registry.py   → data/cinemas.json     (where venues are)
scripts/enrich_registry.py    → data/cinemas.json     (what platforms call them)
cinemelb refresh              → data/cinemelb.sqlite3 (what's on)
cinemelb <query>              → answers
```

**`data/cinemas.json`** is the master venue registry: name, address, suburb,
postcode, lat/lon, phone, website, tags, and which adapter owns the venue plus
its upstream IDs. It is generated from Flicks' per-venue pages (the only source
covering independents) and cross-checked against each chain's own location feed
by coordinates.

**`cinemelb/adapters/`** holds one module per booking *platform*, not per
venue. Adding a new cinema that runs on an existing platform is usually a
registry entry, not code.

**`cinemelb/models.py`** normalises everything into `Cinema`, `Film`,
`Screening`. Times are stored twice: the advertised **Melbourne wall-clock**
time and an aware UTC instant. Cinemas publish wall-clock times, and several
Australian APIs append a `Z` to local timestamps — reading those as UTC shifts
every session by 11 hours, so the wall-clock value is what adapters supply.

**`cinemelb/query.py`** is the question layer: fuzzy film resolution, time
windows, distance ranking with travel-time estimates, tag/format/accessibility
filters.

A refresh is **authoritative per venue**: whatever a venue's source says is
live replaces that venue's future screenings (so cancelled shows disappear),
but a platform that fails leaves the previous rows intact rather than blanking
them.

---

## Adding a venue or platform

1. **Venue on a known platform** — append its Flicks slug to `REGIONS` in
   `scripts/harvest_registry.py`, re-run the two harvest scripts. `enrich_registry.py`
   will match it to its chain ID by coordinates.
2. **New platform** — subclass `Adapter` in `cinemelb/adapters/base.py`,
   implement `fetch()` (and `fetch_many()` if the platform serves all venues in
   one request), register it in `cinemelb/adapters/__init__.py`, and point the
   venue's `source` at it.
3. **One-off venue needing special handling** — add a strategy to
   `GenericHtmlAdapter`.

Verify with:

```bash
python -m unittest discover -s tests -v    # offline: parsing, geo, queries
python scripts/verify.py                   # live: adapters return real data
```

---

## Notes

- **Politeness.** Requests are cached to `data/cache/` for 15 minutes and
  throttled per host (350 ms default, `CINEMELB_DELAY` to override). One Hoyts
  refresh downloads the chain once, not twelve times.
- **Time zones.** Everything is anchored to `Australia/Melbourne`, with AEST/AEDT
  handled, including a fallback that works on Windows without the `tzdata` wheel.
- **No secrets, no accounts, no headless browser.** Only public pages and public
  JSON endpoints.
- **Data freshness.** Session times reflect the last `refresh`. Run it before
  trusting "what's on tonight".

## Limitations

Stated plainly, because a showtimes tool that hides its gaps sends you to a
cinema for a session that does not exist.

- **Coverage is all 58 active venues with live session data.** The last gap
  closed when IMAX Melbourne turned out to be reachable: its marketing site
  blocks non-browser clients, but the Vista Connect OCAPI behind it
  (`digital-api.imaxmelbourne.com.au`) answers plain HTTP with the bearer token
  in the page's own hydration blob. The deprecated `/v2/experiences` route is
  the one that stays Cloudflare-403, which is why the venue looked unscrapable.
  IMAX does rate-limit aggressively (Cloudflare 1015 after ~40 rapid requests),
  so the adapter crawls its business days through the project's throttled
  `Http` client and reports a 429 as a failed day rather than "no sessions".
- **Five registry rows are marked inactive** because they are not active
  cinemas: `rooftop-cinema` (stale site, no programme), `forum-theatre`
  (live-music venue), `capitol-theatre` (the registered URL is the Sydney
  Capitol; the Melbourne one is a retail arcade), `thornbury-theatre`
  (live-music venue), and `la-trobe-university-cinema` (no public programme).
  Each carries a `notes` field with the evidence. They are excluded from
  refresh and from query results.
- **Seasonal venues** (Moonlight, outdoor screens) legitimately return
  nothing out of season.
- **The registry covers greater Melbourne.** Regional Victorian cinemas
  (Ballarat, Bendigo, Geelong, Gippsland) are outside scope. Event Cinemas has
  no Victorian venues beyond the outdoor Moonlight site, so its absence as a
  chain is correct.
- **Title spelling varies by chain.** The same film can be listed as "Hexed"
  at Village and "Disney's Hexed" at Palace/Moving Story. Queries resolve one
  title string per film; pass `--fuzzy` to widen a search when a title looks
  fragmented. Curly-vs-straight apostrophes and en-dashes are normalised to one
  key, so "Disney’s Hexed" and "Disney's Hexed" already collapse.
- **Upstream sites change.** These are scrapers against undocumented endpoints;
  a redesign breaks an adapter. `cinemelb doctor` and `python scripts/verify.py`
  exist to make that failure loud rather than silent.
