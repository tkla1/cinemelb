# Rotten Tomatoes and Metacritic scores at build time

Measured on 2026-11-26. There is **no supported public API** for either service:
`developer.rottentomatoes.com` no longer resolves, and Metacritic has no
developer portal. Both pages are plain server-rendered HTML with JSON inside,
and both contracts forbid exactly what a scraper does.

## Rotten Tomatoes

**Reachable:** `GET https://www.rottentomatoes.com/m/<slug>` → 200, ~220 KB,
with any UA (Chrome, curl, python-requests). Akamai-fronted, no JS challenge at
2–3 s spacing.

**Dead ends:** `/napi/*` → 401 (needs a token the page no longer exposes);
`/api/private/v2.0/*` → 404; RT site search → robots-disallowed.

**Where the scores are:** `<script id="media-scorecard-json" type="application/json">`

```json
{"criticsScore":  {"scorePercent": "95%", "averageRating": "7.40",
                   "ratingCount": 65, "certified": true},
 "audienceScore": {"scorePercent": "84%", "averageRating": "4.1",
                   "bandedRatingCount": "5,000+ Verified Ratings"}}
```

Gotcha: the script tag has newlines between attributes, so a literal
`<script id="media-scorecard-json"` match fails — use
`id="media-scorecard-json"[^>]*>` or an HTML parser.

JSON-LD (`aggregateRating.ratingValue`) carries the **Tomatometer only**, and is
the fallback if the scorecard element disappears. Audience counts are banded
(`"100+ Ratings"`), not exact.

## Metacritic

**Reachable with a browser UA:** `GET https://www.metacritic.com/movie/<slug>/`
→ 200, ~570 KB. With a `python-requests` UA it is **403** (Cloudflare UA filter,
not a challenge), so set a Chrome/Firefox UA.

**Best target — the backend JSON the Next.js page itself calls:**

```
https://backend.metacritic.com/reviews/metacritic/critic/movies/<slug>/stats/web
→ {"data":{"item":{"max":100,"score":62,"reviewCount":16,"sentiment":"Generally favorable", ...}}}

https://backend.metacritic.com/reviews/metacritic/user/movies/<slug>/stats/web
→ {"data":{"item":{"max":10,"score":7.7,"reviewCount":10, ...}}}
```

~500 bytes, clean JSON, works even with a plain UA. `"score": null` means
"no reviews yet"; a missing title is 404. The `/finder` search needs a public
`apiKey` embedded in MC pages and is near-useless for exact titles — do not
depend on it.

Page JSON-LD also carries the **Metascore only** (`ratingValue`, 0–100).

## IMDb

Title pages return **202** with an AWS WAF JS challenge (403 for a plain UA), so
they are not usable from a script. Use the official bulk dataset:

```
https://datasets.imdbws.com/title.ratings.tsv.gz
→ 8.7 MB, ~1.7 M rows, rebuilt daily, columns: tconst  averageRating  numVotes
```

The site states these files are for **non-commercial use only**, which is a far
better fit than scraping: one download replaces hundreds of page requests and is
contractually allowed. `title.basics.tsv.gz` (title, year, type) and
`title.akas.tsv.gz` (regional titles) help with matching.

## Legal position

- **Rotten Tomatoes / Fandango ToS:** "will not … use any automated method
  (including … robots, scripts, spiders, data extractors) to collect data from,
  access or search, the Services"; content is "personal and non-commercial use
  only".
- **Metacritic / Fandom ToS:** "unauthorized spidering, 'scraping,' data mining
  or harvesting of Content".
- **IMDb:** scraping prohibited, but the bulk datasets are explicitly published.

A permissive `robots.txt` is **not** a licence — both RT and MC allow the movie
paths in robots.txt while their contracts forbid automated collection.

Practical risk for a personal, non-commercial site showing a score next to a
deep link is low, but that is a risk assessment, not permission. Cleaner
alternatives, cheapest first:

1. **OMDb** free key (~1,000 requests/day) returns IMDb + RT + Metacritic in one
   response. Content is CC BY-NC 4.0; provenance is itself scraped, one hop
   removed.
2. **MDBList** supporter API — RT + MC + IMDb under one contract.
3. **Drop RT/MC** and ship IMDb (bulk) + TMDB.

Do not put scores behind an ad or paid tier, and do not bulk-redistribute them —
that is what turns "personal use" into "commercial exploitation".

## Politeness rules for any fetch

1. Sequential only — one connection, no pools, no proxy rotation.
2. ≥2–3 s between requests to the same host.
3. Honest browser UA plus a contact (`From:`) header; do not rotate UAs.
4. Respect robots.txt (RT `/search` and MC `/search` are disallowed).
5. Cache with TTL (7–14 days): scores move daily in cinemas, catalogue titles
   barely move. Store `fetched_at`.
6. One build per day, not per commit; make it a separate job.
7. Fail closed: 404 → null; 403/429/202 → back off, then **stop that source for
   the run** and keep the cached value. Never retry a challenge page, and never
   ship a browser-automation dependency just to beat a WAF.
8. Attribute the source and link to it.

## Likely breakages

| Failure | Detector | Fallback |
|---|---|---|
| RT scorecard JSON renamed | no `criticsScore` for >30% of films | JSON-LD Tomatometer + `<rt-text slot="audience-score-type">`, else drop |
| RT movie pages challenged | 202/403 cluster | cached values, skip new titles |
| MC backend path changes | non-200 or no `data.item` | page JSON-LD + RSC, else drop |
| Wikidata lacks the slug | no P1258/P1712 | manual `slugs.json` override, else skip |
| IMDb dataset schema change | header ≠ `tconst\taverageRating\tnumVotes` | abort, keep cached TSV |
