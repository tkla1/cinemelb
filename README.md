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

`--json` is a global flag and goes **before** the command:

```bash
python -m cinemelb --json film dune | jq '.screenings[].cinema'
python -m cinemelb --json trending | jq -r '.[] | "\(.cinema_count)\t\(.title)"'
```

---

## Coverage

63 venues in greater Melbourne, including every independent and art-house
cinema. Each venue is scraped through the adapter appropriate to its platform.

| Platform | Venues | How |
|---|---|---|
| Hoyts | 12 | JSON API — `/api/sessions` returns the whole chain in one ~9 MB request |
| Village | 14 | same-origin `/api/algolia/sessions/hits?f.c=<cinemaId>` proxy (one call per venue) |
| Palace (incl. The Astor, The Kino, Pentridge) | 9 | per-venue Next.js data payload |
| Reading | 7 | SPA API at `prod-api.readingcinemas.com.au` — *blocked, see Limitations* |
| Moving Story (Lido, Classic Elsternwick, Cameo Belgrave) | 3 | server-rendered HTML, one block per day |
| Cinema Nova | 1 | `/films-by-date-<epoch>` server-rendered pages |
| Sun Theatre Yarraville | 1 | WordPress pages with `?wp-cinema-day=N` |
| ACMI | 1 | Nuxt `window.__NUXT__` payload |
| IMAX Melbourne | 1 | own site |
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

- **Coverage is 46 of 58 venues with live session data.** The remaining 12 are
  a mix of genuinely hard and genuinely seasonal:
  - **Reading (7 venues)** — the SPA's API (`prod-api.readingcinemas.com.au`)
    rejects non-browser clients with 403. Needs a browser session or a
    different endpoint.
  - **IMAX Melbourne** — its session API
    (`digital-api.imaxmelbourne.com.au/v2/experiences/get`) is behind
    Cloudflare and 403s non-browser clients.
  - **United Craigieburn, Waverley Cinemas** — JS-only booking widgets with no
    discoverable HTTP endpoint.
  - **Brunswick Picture House, Thornbury Picture House** — both run the
    "indy-systems" booking platform, whose own GraphQL endpoint is currently
    returning schema errors upstream.
  - **Moonlight Cinema** — outdoor and seasonal; it opens 2 December, so an
    empty result out of season is correct, not a failure.

  Every one of these is listed in the venue registry with full address and
  coordinates, so they still appear in `cinemelb cinemas` and in map exports —
  they simply carry no session times yet. `cinemelb stats` shows per-adapter
  health so the gaps are always visible.
- **Seasonal venues** (Moonlight, rooftop/outdoor screens) legitimately return
  nothing out of season.
- **The registry covers greater Melbourne.** Regional Victorian cinemas
  (Ballarat, Bendigo, Geelong, Gippsland) are outside scope. Event Cinemas has
  no Victorian venues beyond the outdoor Moonlight site, so its absence as a
  chain is correct.
- **Upstream sites change.** These are scrapers against undocumented endpoints;
  a redesign breaks an adapter. `cinemelb doctor` and `python scripts/verify.py`
  exist to make that failure loud rather than silent.
