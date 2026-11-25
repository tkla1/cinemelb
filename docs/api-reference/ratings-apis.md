# Ratings APIs: OMDb, IMDb datasets, TMDB, Wikidata

What a **build-time** Python script can use to attach scores to a film. The
browser never calls these.

## OMDb (the "DVDb for films")

One free key returns IMDb, Rotten Tomatoes and Metacritic for a title.

```
GET https://www.omdbapi.com/?apikey=<KEY>&i=tt15239678
GET https://www.omdbapi.com/?apikey=<KEY>&t=Dune%20Part%20Two&y=2024&type=movie
```

```json
{"Title":"Dune: Part Two","Year":"2024","imdbRating":"8.4","imdbVotes":"809,060",
 "Metascore":"79",
 "Ratings":[{"Source":"Internet Movie Database","Value":"8.4/10"},
            {"Source":"Rotten Tomatoes","Value":"92%"},
            {"Source":"Metacritic","Value":"79/100"}],
 "Response":"True"}
```

- Lookup by `i` (IMDb id) is exact and preferred; `t`+`y` is fuzzy and can pick
  the wrong remake.
- Free tier: ~1,000 requests/day. A key is one minute's work:
  <https://www.omdbapi.com/apikey.aspx>.
- `Response:"False"` with `Error:"Movie not found!"` is the miss shape.
- `Ratings[]` is no longer in the published Swagger but still returned; parse
  defensively and fall back to `imdbRating`/`Metascore`.
- Content is CC BY-NC 4.0; provenance is scraped upstream, so the legal position
  is "one vendor removed", not "licensed from RT".

## IMDb bulk datasets (keyless, sanctioned)

```
https://datasets.imdbws.com/title.ratings.tsv.gz   ~8.7 MB, daily
    tconst  averageRating  numVotes
https://datasets.imdbws.com/title.basics.tsv.gz    ~90 MB
    tconst  titleType  primaryTitle  originalTitle  isAdult  startYear  ...
```

- Non-commercial use, explicitly published — the supported alternative to
  scraping title pages (which return a WAF challenge).
- Join `title.ratings` to the `tconst` resolved from Wikidata.
- `title.basics` is only needed if you must match titles without IDs; cache a
  filtered slice (2024–2027, `titleType == movie`) instead of re-parsing 10M rows
  every build.

## TMDB (matching aid, not a score source)

```
GET https://api.themoviedb.org/3/search/movie?api_key=<KEY>&query=…&year=…
GET https://api.themoviedb.org/3/movie/<id>/external_ids?api_key=<KEY>
→ {"imdb_id":"tt15239678", ...}
```

`vote_average` is TMDB's own score, not RT/MC. Useful only when Wikidata has no
IMDb id (common for festival/indie titles). Free key, attribution required.

## Wikidata (keyless ID resolution)

IDs are the hard part: RT and MC slugs cannot be discovered by searching those
sites (RT `/search` is robots-disallowed, `/napi` is 401, MC's finder returns
nothing for exact titles). Wikidata solves it for free.

```
GET https://www.wikidata.org/w/api.php?action=wbsearchentities
      &search=Dune%20Part%20Two&language=en&format=json&limit=5
GET https://www.wikidata.org/w/api.php?action=wbgetentities&ids=Q109228991
      &props=claims|labels&languages=en&format=json
      (or https://www.wikidata.org/wiki/Special:EntityData/Q109228991.json)
```

| Property | Meaning | Example |
|---|---|---|
| P345 | IMDb id | `tt15239678` |
| P1258 | Rotten Tomatoes id | `m/dune_part_two` |
| P1712 | Metacritic id | `movie/dune-part-two` |
| P444 + P447 | review score + reviewer | occasionally present |
| P577 | publication date | for a year check |

Coverage is good for wide releases, patchy for festival/indie titles. Keep a
manual `data/ratings-overrides.json` for the gaps.

## MDBList (paid, cleanest contract)

Supporter-tier API that returns IMDb, TMDb, Letterboxd, **RT and Metacritic**
under one agreement. The option to take if the site ever stops being personal.

## Matching strategy

The dataset is noisy: `DUNE: PART THREE - IMAX 70MM`, `Dune: Part Three`,
`Disney's Hexed`, `JFF26 Look Back`, `Seven (1995)`, `History of Concrete, The`,
`Aakasamlo Oka Tara (Telugu, Eng Sub)`, and only **43 of 393** films carry a
year. Local matching against the IMDb bulk files was measured at **207/393
auto-matches, 182 of them rated (53%)**; Wikidata search alone matched far fewer
and produced confident errors (`Angel's Egg` → a 2006 film, `Tender Loving Care`
→ a 1998 video game). So the bulk join is the primary matcher, Wikidata only a
fallback for IDs.

1. **Normalise** — NFC and mojibake repair; unify curly quotes and dashes;
   `&` → `and`; extract any `(19|20)\d{2}` as `year_hint`; strip a leading
   festival/venue code (`BFF26`, `JFF26`, `POLFF26`, `JIFF:`, `Astor:`,
   `Cult Classix:`, `Reading Rewind`, `Met Opera`); strip a trailing
   presentation/event clause (`IMAX`, `70MM`, `3D`, `VISTAVISION`, `Live`,
   `Q&A`, `Encore`, `- 50th Anniversary`, `Subbed`, `Tickets on Sale`); rotate
   `", The"`; spell numerals 1–10; delete apostrophes. Keep the raw title.
   Produce `key_strict` and a stop-worded `key_loose`.
2. **Resolve a tconst** — exact `key_strict` in `title.basics`
   (`titleType in {movie, tvMovie}`) → `title.akas` (AU, then GB/US/NZ) → top-5
   `difflib` on `key_loose` at ≥ 0.88 → Wikidata → unmatched.
3. **Gate on confidence** (runtime and vote count beat recency; a repertory
   calendar makes recency actively wrong — `AKIRA` is the 1988 film, not the
   2016 remake):
   - **A**: one candidate and (year ±1 **or** runtime ±2 **or** 2025+ with ≥50
     votes) → display.
   - **B**: several candidates, exactly one within ±1 year → display.
   - **C**: margin < 0.15, or runtime/votes disagree → store `needs_review`,
     don't display.
   - **D**: none → hide the badge.
4. **Fetch scores by tconst** — IMDb from the bulk TSV; RT/MC from OMDb when
   keyed; MC keyless via the finder endpoint below; RT keyless only under
   `scrape=True`.

### Metacritic finder (keyless, ~92% coverage)

```
GET https://backend.metacritic.com/finder/metacritic/search/<title>/desktop
      ?mcoTypeId=2&offset=0&limit=5
→ data.items[0]: {title, slug, premiereYear, criticScoreSummary:{score, url}}
```

`mcoTypeId=2` is movies. Accept only when `premiereYear` agrees within ±1 (or
there is a single exact title hit). Critic and user scores then come from
`/reviews/metacritic/{critic,user}/movies/<slug>/stats/web`.


## Cache shape

`data/ratings.json` (gitignored), keyed by normalised title + year:

```json
{"dune part three": {
  "imdb_id": "tt31378509",
  "imdb": 8.1, "imdb_votes": 12345,
  "rt": 92, "rt_url": "https://www.rottentomatoes.com/m/dune_part_three",
  "mc": 79, "mc_url": "https://www.metacritic.com/movie/dune-part-three/",
  "confidence": "high",
  "source": ["wikidata", "imdb-bulk", "omdb"],
  "fetched_at": "2026-11-26T04:00:00Z"
}}
```

TTL 7–14 days; a re-export must never re-fetch.
