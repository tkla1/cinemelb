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
`Disney's Hexed`, `JFF26 Look Back`, `Aakasamlo Oka Tara (Telugu, Eng Sub)`, and
only 43 of 393 films carry a year.

1. **Normalise** — casefold, collapse whitespace, unify apostrophes/dashes, drop
   a leading `Disney's`, strip parentheses and known suffixes
   (`- IMAX 70MM`, `IMAX`, `4K`, `Live`, `Q&A`, `Encore`, `- Nth Anniversary`,
   `(2003)`, `(Telugu, Eng Sub)`), and strip festival prefixes (`JFF26`, `BFF26`,
   `MIFF26`). Keep the suffix in `aka` so the UI can still show it.
2. **Resolve an ID**, in order: cache → manual override → Wikidata search on the
   normalised title (and each `aka`) → TMDB search.
3. **Accept a candidate** only if the normalised label matches exactly *and*
   either the year agrees (when both are known) or there is a single candidate.
   Otherwise record `confidence: "low"` and leave the score empty rather than
   guessing.
4. **Fetch scores** by id: IMDb from the bulk TSV; RT/MC via OMDb when a key is
   set. Direct RT/MC page parsing is opt-in only (see `rt-metacritic.md`).

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
