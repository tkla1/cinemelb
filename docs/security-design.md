# Security design — cinemelb

Companion to `SPEC.md` and `docs/security-review-2026-11-26.md`. This is the durable
design: the invariants that make the app safe to extend, where they are enforced, and the
check that fails when someone removes them. Read this before adding a renderer, an adapter,
or a new output format.

## 1. What is trusted

| Input | Trust | Why |
|---|---|---|
| Scraped cinema/film/session text and URLs | **untrusted** | any third-party site, any listing, can say anything |
| Poster and rating APIs (OMDb/TMDB/IMDb datasets) | semi-trusted | values still validated by type/range before export |
| Operator arguments (`--out`, `--db`, `--host`, `--near`) | trusted for intent, untrusted for safety | a typo must not delete a directory or expose a port |
| `.env` keys | secret | never logged, never exported, never sent to the browser |
| The app's own generated HTML/JSON | trusted structure | but it embeds the untrusted values above |

Deployment: single-user CLI; a loopback-only read-only HTTP UI; a static SPA and a
single-file map that may be **hosted or shared**. A defect that only exposes public
showtime data on loopback is low; a defect that executes script in a shared artefact, or
deletes the operator's files, is not.

## 2. The four output contexts

Every security defect in the 2026 review was one of these four going wrong. There are no
other sinks.

| Context | Control | Enforced by | Test |
|---|---|---|---|
| Terminal / table | strip C0+C1, no rich markup from data | `sanitize.clean_text`, `Text(...)`, `markup_escape` | `tests/test_security.py::TestCliOutput` |
| HTML text and attributes | `html.escape(..., quote=True)` | `serve._esc`, `mapview` `esc()` | existing server XSS test |
| `href` / `src` | scheme allowlist `http(s)` only | `sanitize.safe_href` at **export** and in `mapview`/`app.js` | `TestSanitize`, `TestMapExport` |
| Inline `<script>` JSON | escape `<`, `>`, `&`, U+2028/9 | `sanitize.json_for_script` | `TestMapExport` |

Rule: a value may enter a context only through that context's helper. `json.dumps` is not
an HTML escaper; `esc()` is not a URL validator; stripping HTML tags is not sanitisation.

## 3. Invariants

| id | Invariant | Where | Check |
|---|---|---|---|
| INV-1 | Scraped text cannot carry control characters into any output | `models.__post_init__` (ingest) + `cli._cells`/`_safe` (render) | `test_scraped_control_characters_never_enter_the_model`, `test_scraped_escapes_do_not_reach_the_terminal` |
| INV-2 | No scraped string closes the map's inline script | `mapview.render` via `json_for_script` | `test_render_escapes_the_inline_payload`, `check_security.py` |
| INV-3 | Only `http(s)` URLs enter the bundle or an `href`/`src` | `export.build_bundle`, `mapview._venue_payload`, `app.js safeHref` | `test_safe_href_scheme_allowlist`, `test_render_drops_javascript_ticket_urls`, `prove_bundle.py` |
| INV-4 | `export` never deletes a directory it did not create | `export._guard_out` + marker + temp rename | `TestExportGuard` (4 cases) |
| INV-5 | The loopback server answers only loopback `Host` names | `serve._Server.allowed_hosts` | `TestServer` (421/200) |
| INV-6 | No exception text, path or SQL reaches a browser | `serve._Handler.do_GET` (ref + stderr) | `test_500_does_not_echo_the_exception` |
| INV-7 | Every external asset is version-pinned with SRI | `mapview` template | `test_cdn_assets_have_integrity`, `sri.js` browser check |
| INV-8 | Secrets never enter the repo, bundle, logs or URLs | `.gitignore`, `scripts/audit_secrets.py`, gitleaks | CI / review step |
| INV-9 | The server is read-only and GET-only | `serve._App.handle` routes | `TestServer` (POST 501, no write route) |
| INV-10 | All SQL values are parameters | `store`/`query` | code review; `scripts/check_security.py` does not cover it |

## 4. Trust boundaries and the rules that cross them

**Scrape → store.** Adapters hand `Cinema`/`Film`/`Screening` objects. Their
`__post_init__` strips control characters; nothing downstream sees raw ESC/BEL/CR. Length
and type validation stays in the adapters and in `export._validate`.

**Store → HTML.** Two servers of HTML: `serve.py` (escaped, scheme-checked, read-only) and
`mapview.py` (script JSON). They deliberately share `sanitize`, so a fix lands once.

**Store → bundle → browser.** `build_bundle` is the boundary: URLs are filtered there, so a
missed renderer cannot reintroduce a `javascript:` link. The SPA still filters again
(`safeHref`), because bundles can be edited by hand.

**Process → filesystem.** Reads: registry, SQLite, caches. Writes: scrape cache, ratings,
posters, `export` output, `map` output. The only destructive operation is `export`'s
replacement of `--out`, now guarded by a marker, a root/ancestor refusal, a temp build and
`--force`.

**Process → network.** Literal hosts only (cinema sites, OMDb/TMDB, Nominatim, unpkg for the
map). No user-supplied URL is fetched. `verify=False` is banned.

**Browser → localhost.** Loopback bind by default, `Host` allowlist on loopback, GET-only,
no credentials, no state change. A hostile page that rebinds gets 421.

## 5. Adding a new renderer or output format

1. Name the context: terminal, HTML text/attribute, URL, or inline script.
2. Use the corresponding helper; do not hand-roll an escape.
3. If it is a new context (CSV, PDF, header, log), add a helper to `sanitize.py` **and** a
   test in `tests/test_security.py` first.
4. If it emits a new file, decide whether that file is shared. If yes, it gets the same
   URL/script treatment as the map and is added to `CHECKS` in `scripts/check_security.py`.
5. Run `uv run python scripts/check_security.py`; it fails on the old patterns.

## 6. Adding a new adapter

- Return model objects; do not write SQL or HTML.
- Validate types, ranges and lengths before returning (the store trusts the model layer).
- Never fetch a URL taken from the page being parsed without an allowlist; prefer literal
  hosts and path templates.
- Cap response size and time (`Http` does timeout/retry; add a content-length cap when the
  platform allows).
- Add a fixture with hostile title/URL text to `tests/test_adapters.py` where the parser is
  non-trivial.

## 7. Verification

```text
uv run python scripts/check_security.py          # static invariants
uv run python -m unittest tests.test_security -v # behavioural invariants
node --check cinemelb/web/app.js
uv run python tests/ui/layout.py
uv run python scripts/check_bundle.py --against-db
```

The review's executable witnesses remain in `scratch/review/prove_*.py` and flip from fail
to pass; they are scratch, not a CI dependency.

## 8. Residual risks (accepted or open)

- **No CSP** on the generated map or the SPA. The map is a single inline-script document;
  adding a hash/nonce is a build change with its own decision. The SPA's Cloudflare
  `_headers` sets nosniff and referrer policy only.
- **Poster images** are third-party; a hostile image is a browser-parser risk, not a script
  risk in an `<img>`, and `referrerpolicy="no-referrer"` is set in the SPA.
- **Localhost UI has no authentication.** Accepted: it serves public data and changes
  nothing. Do not bind it to a LAN without understanding that.
- **`scratch/` is gitignored** and may contain live proof data; never publish it.
- **First-party scraping** is a legal/ToS question tracked in `docs/api-reference/`, not a
  security control.
