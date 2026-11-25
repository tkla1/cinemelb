# API reference

One page per external source, written for a **build-time** Python script: the
browser never calls these and never holds a key.

| Source | What we take | Key needed | Status |
|---|---|---|---|
| [Maps & tickets](maps-and-tickets.md) | venue maps, chain ticket URLs | no | verified |
| [Ratings APIs](ratings-apis.md) | IMDb, RT, Metacritic, matching | OMDb optional | verified |
| [RT / Metacritic access](rt-metacritic.md) | score pages, ToS, reliability | no | verified |

Shared rules for every source:

- **Pace requests.** One per second, a real User-Agent, retry with backoff on
  429/5xx, and stop rather than hammer.
- **Cache everything** in `data/ratings.json`; a re-export must not re-fetch.
- **Never ship a key** in the bundle. OMDb/TMDB keys live in `.env` only.
- **Degrade quietly.** A missing score is a missing chip, not an error.
