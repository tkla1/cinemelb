# Security review — cinemelb

**Date:** 2026-11-26
**Target:** `C:\Users\alyosha\claude\projects\cinemas` at `57ff76e95c07f512987ef847e670c9c8cdcaaaff`
**Tree:** dirty at review time (tracked modifications to `cinemelb/web/app.{css,js}`; untracked
`AGENTS.md`, `SPEC.md`, `components*.json`, `opencode.json`, `cinemelb/web/{gallery.html,tokens.json}`,
`scripts/{audit_secrets,build_tokens,check_*}.py`). `README.md`/`STATE.md` changed by another
process mid-review (documentation only). Findings are against the tree as read; re-check line
numbers after the pending work is committed.
**Method:** `secure-code-review` skill — SCOPE → SCAN → TRIAGE → HUNT → PROVE → REPORT.
Automated candidates: 37 (semgrep 1.187.0, bandit 1.9.4, ruff 0.17.6, gitleaks 8.30.1,
osv-scanner 2.7.0). Survivors: 7. Every survivor reproduced with a fixture, an HTTP request
or a browser run; each was re-run by a second reviewer before publication. Raw evidence is in
`scratch/review/` (`findings.json`, `report.md`, `prove_*.py`, `browser/`).

## Deployment model (severity is rated against this)

- Single-user CLI on the user's machine.
- A read-only stdlib `ThreadingHTTPServer` UI + JSON API, default bind `127.0.0.1`, no auth,
  no state-changing routes.
- `cinemelb export` produces a static SPA bundle the user may host or share.
- `cinemelb map` writes a self-contained HTML file (inline JSON + Leaflet from a CDN) that is
  opened locally or hosted.
- ~60 third-party cinema sites are **untrusted input** flowing into SQLite, HTML, JS, terminal
  output and filesystem paths.
- The only secret is an optional `OMDB_API_KEY` in `.env`, used at build time.

## Result

**1 high, 1 medium, 4 low, 1 informational.** One release-blocking issue: the generated map
executes scraped text as script with no user interaction. The rest is hardening; no
authentication, injection-in-SQL, secret-exposure or dependency issue was found.

| id | sev | finding |
|---|---|---|
| F-001 | **high** | Scraped title/venue text breaks out of the inline JSON `<script>` in `cinemelb map` output and executes on open |
| F-002 | low | Scraped URLs reach `href` with no scheme allowlist in the SPA/map (inert in Chrome today) |
| F-003 | medium | `cinemelb export --out` deletes any existing non-ancestor directory without a marker check |
| F-004 | low | Local server answers any `Host` header (DNS-rebinding control absent; data is public) |
| F-005 | info | 500 page echoes the exception text and database path |
| F-006 | low | Scraped names reach the terminal with control/escape sequences intact |
| F-007 | low | Map loads Leaflet from unpkg without subresource integrity |

---

## F-001 — Script injection through the map's inline JSON

- **Severity:** high · **Confidence:** confirmed · **CWE-79** (stored XSS)
- **Location:** `cinemelb/mapview.py:64`, `cinemelb/mapview.py:195-199`
- **Source:** film titles, venue names and tags scraped from third-party sites. `hoyts.py:119`
  stores the upstream movie name verbatim; `generic_html.py:360/530` unescape scraped anchors.
- **Path:** adapter → SQLite → `query.Hit.film` → `_venue_payload()` → `json.dumps()` →
  `<script>const DATA = {data_json};</script>`.
- **Missing control:** JSON is not escaped for the HTML script context. `json.dumps` leaves
  `<`, `</script>` and `<img onerror=…>` intact; `html.escape` is applied to `title`/`subtitle`
  only, and the client `esc()` runs after the JSON has already been parsed.
- **Evidence:**

  ```
  $ uv run python scratch/review/prove_mapview.py
  raw </script> inside the DATA line : True
  bytes of attacker markup now outside the script element:
    '</script><script>window.__PWNED=2</script>", "suburb": "Brunswick", ...
  $ node scratch/review/browser/prove.js
  map window.__PWNED (script tag) : true
  ```

- **Trigger:** a listing titled `Evil</script><script>window.__PWNED=2</script>`, then
  `cinemelb map`, then open the generated file. No click required.
- **Impact:** script runs with the file's origin when the map is opened, and with the site's
  origin if the map is hosted. It can read the page (including the user's search location),
  make requests, and phish. If the map is published for others, this is stored XSS for every
  visitor.
- **Fix:** escape `<` when serialising into the script:

  ```python
  data_json = json.dumps(payload, ensure_ascii=False, default=str).replace("<", "\\u003c")
  ```

  Alternatively put the payload in a `data-` attribute or a separate `.json` file and
  `JSON.parse` it. Add a fixture to the test suite: a title containing `</script>` must not
  produce a `</script>` in the DATA line.
- **Verify:** re-run `prove_mapview.py` and assert `raw </script> inside the DATA line: False`;
  load the page and assert `window.__PWNED` is undefined.
- **Refutation attempted:** `html.escape` covers the page title and subtitle; the client
  `esc()` covers DOM text insertion; neither covers the script context. Chrome executed it.

## F-002 — No URL-scheme allowlist at `href` sinks

- **Severity:** low · **Confidence:** confirmed · **CWE-79**
- **Location:** `cinemelb/web/app.js:1568` (also 1427, 1436, 1465, 1469, 1620);
  `cinemelb/mapview.py:108`
- **Source:** scraped `ticket_url`, `website`, `booking_url`, `trailer_url`, and ratings search
  URLs.
- **Path:** adapter → DB → `bundle.urls` → `'href="' + esc(url) + '"'`. `esc()` escapes HTML
  metacharacters, not the scheme.
- **Missing control:** no allowlist, although `serve.py:543` (`_safe_href`) already implements
  the correct check for server-rendered HTML.
- **Evidence:** `prove_bundle.py` emits `href="javascript:window.__PWNED=1"`. Browser test:
  with `target="_blank"` (every current call site) Chrome opens `about:blank`, runs nothing,
  and has no opener — with and without `rel="noopener"`.
- **Trigger:** a venue whose booking anchor is `javascript:…`; click a session chip.
- **Impact:** none measured in Chrome 143 today. Reported because the control the codebase
  itself uses elsewhere is missing here, and a same-tab link or another engine changes the
  outcome.
- **Fix:** at every `href` sink:

  ```js
  function safeHref(u) {
    return /^https?:\/\//i.test(String(u || '').trim()) ? u : '';
  }
  ```

  Use `esc(safeHref(url))`; do the equivalent in `mapview.py` before templating.
- **Verify:** `prove_bundle.py` must emit no `javascript:` href; click each chip in the browser
  and assert the tab does not navigate to `about:blank` from user data.

## F-003 — `export --out` deletes an existing directory without a marker check

- **Severity:** medium · **Confidence:** confirmed · **CWE-73**
- **Location:** `cinemelb/export.py:375-385`
- **Source:** the operator's `--out` argument.
- **Path:** `cli export --out DIR` → `write_site` → guard rejects only `cwd` and its ancestors
  → `shutil.rmtree(out)`.
- **Missing control:** no marker file, emptiness test, or `--force` confirmation. A path on
  another drive (e.g. `D:\`) is not an ancestor of the cwd and passes the guard.
- **Evidence:**

  ```
  $ uv run python scratch/review/prove_rmtree.py
  before: exists = True
  write_site accepted the directory
  after : exists = False
  ```

- **Trigger:** `cinemelb export --out ../site` over a hand-maintained folder.
- **Impact:** irreversible deletion of arbitrary non-ancestor directories, up to a whole
  drive.
- **Fix:** build into a temporary sibling directory and `os.replace`/rename into place; refuse
  to touch an existing directory unless it contains a `.cinemelb-site` marker or `--force` is
  passed; refuse drive roots explicitly.
- **Verify:** run `write_site` against a directory containing a user file; expect `SystemExit`
  and the file intact.
- **Refutation attempted:** `/`, home, repo root, `.` and symlinked ancestors are refused; the
  gap is sibling and other-drive paths.

## F-004 — Local HTTP server accepts any `Host` header

- **Severity:** low · **Confidence:** confirmed · **CWE-350** (DNS rebinding)
- **Location:** `cinemelb/serve.py:1054-1083`
- **Source:** the HTTP `Host` header.
- **Path:** hostile page → DNS rebinding to `127.0.0.1` → `GET /api/search` with a foreign
  `Host` → `200`.
- **Missing control:** no `Host` allowlist, no `Origin`/`Sec-Fetch-Site` check.
- **Evidence:**

  ```
  $ uv run python scratch/review/prove_serve.py
  GET / with Host: attacker.example -> 200 | page rendered: True
  GET /api/search with foreign Host -> 200 | json: {...}
  ```

- **Trigger:** a page on an attacker domain that rebinds to `127.0.0.1:8787`.
- **Impact:** reads public showtime data only — no auth, no secrets, no state change. Low by
  deployment, not by CWE. It also lets an attacker page trigger Nominatim lookups through the
  victim.
- **Fix:** reject non-loopback `Host` values with `421 Misdirected Request`; print a warning
  when `--host` is not loopback.
- **Verify:** `curl -H 'Host: evil.example' http://127.0.0.1:8787/api/search` → 421;
  `Host: localhost:8787` and `127.0.0.1:8787` → 200.

## F-005 — 500 page leaks the exception text and database path

- **Severity:** info · **Confidence:** confirmed · **CWE-209**
- **Location:** `cinemelb/serve.py:1063-1069`
- **Source:** any internal exception while building the payload.
- **Path:** `except Exception as exc → _error_page(f"The query failed: {exc}")`.
- **Missing control:** exception text rendered instead of logged.
- **Evidence:** `prove_serve.py` prints `leaks the db path: True`. The message is
  HTML-escaped, so there is no injection.
- **Trigger:** corrupt SQLite file or a query-engine bug.
- **Impact:** local disclosure of paths and statement text; amplified if combined with F-004.
- **Fix:** log the full exception to stderr with a short id; render a generic message plus the
  id.
- **Verify:** force a 500; response contains no path or exception text.

## F-006 — Terminal control sequences from scraped names

- **Severity:** low · **Confidence:** confirmed · **CWE-117**
- **Location:** `cinemelb/cli.py:215-230`; `cinemelb/titles.py:73-75`
- **Source:** scraped film titles and venue names.
- **Path:** adapter → SQLite → `_show_hits` → `rich` table → `console.print`.
- **Missing control:** no C0/C1 stripping before rendering.
- **Evidence:** `prove_cli_ansi.py` shows an `ESC` byte, a clear-screen/cursor-home sequence,
  and an OSC 52 attempt in stdout:
  `' Proof Film \x1b[2J\x1b[H ... Proof Venue \x1b]52;c;...'`.
- **Correction after re-review:** a *complete* OSC 52 clipboard write did not survive the
  render path (the BEL terminator is stripped, so terminals abort the sequence —
  `prove_cli_ansi_refine.py`). What is proven is escape-sequence injection: screen clearing,
  cursor movement, and the ability to corrupt the table display. Clipboard write is not
  claimed.
- **Trigger:** a venue page whose title contains `\x1b[2J`.
- **Impact:** the user's terminal is cleared/repositioned and output can be spoofed; no code
  execution.
- **Fix:** strip `[\x00-\x08\x0b-\x1f\x7f-\x9f]` at ingestion (adapter/store boundary) and
  render cells as `rich.text.Text(value)` so scraped `[bold]` is not parsed as markup.
- **Verify:** `prove_cli_ansi.py` must show no `ESC` byte in stdout; `--json` is already safe.

## F-007 — Leaflet loaded from a CDN without SRI

- **Severity:** low · **Confidence:** confirmed · **CWE-829**
- **Location:** `cinemelb/mapview.py:28` (CSS), `cinemelb/mapview.py:62` (JS)
- **Source:** `https://unpkg.com/leaflet@1.9.4/dist/leaflet.{css,js}`.
- **Missing control:** no `integrity`/`crossorigin`; the version is pinned, the bytes are not.
- **Trigger:** a compromised or MITM'd CDN response for that path.
- **Impact:** script execution in a document users are expected to open and host.
- **Fix:** add the published SRI hashes and `crossorigin="anonymous"`, or vendor both files
  into the generated output.
- **Verify:** generated map contains `integrity="sha256-…"` on both tags, or no external
  references.
- **Note:** this is not the excluded "missing SRI on a localhost page" case — the artefact is
  designed for sharing. The SPA bundle itself has no external assets.

---

## Verified clean (and the test used)

| Area | Test | Result |
|---|---|---|
| Server-rendered HTML | hostile title/venue/suburb/ticket URL through the real renderer (`prove_serve_xss.py`) | every interpolation escaped; `javascript:` ticket URL dropped by `_safe_href` |
| SPA text contexts | hostile title/name/chain/tags/genres/screen/format in Chrome | rendered as text, no execution |
| SQL | review of all `execute*` call sites; `film=x' OR 1=1 --` | all values parameterised; `store.py:407` concatenates a constant `ORDER BY` only |
| Deserialisation | `ratings.py` pickle sidecar | locally written, mtime-gated, JSON alternatives exist; no untrusted source |
| Outbound URLs | enumeration of `requests` call sites | hosts are literals; no user-controlled URL; no `verify=False` |
| Secrets | gitleaks worktree + 29-commit history; bundle/untracked files; `opencode.json` | no live secrets; `.env` excluded and never read; keys absent from build output |
| Dependencies | osv-scanner on `uv.lock`; osv batch on 8 locked runtime deps | 8/8 clean (requests 2.34.2, typer 0.29.2, rich 15.0.0, tzdata) |
| Request bounds | `?film=` at 100 / 1k / 8k / 60k chars | 0.4–12 ms, 200; no amplification |
| Methods | `POST /` | 501; no state-changing route exists |
| Localhost default | `serve.py:1100` | binds `127.0.0.1` |

## Not reviewed / limits

- Adapter parsing bodies beyond title/URL provenance; most of `ratings.py`; `data/` contents;
  harvest scripts; tests; the CSS/token pipeline.
- No fuzzing; no Firefox/Safari testing (F-002 is Chrome 143 only).
- F-001 was not exploited beyond `window.__PWNED`; exfiltration was not attempted.
- The tree was dirty and changed mid-review. Re-run `prove_*.py` after committing.

## Notes for the next review

- The highest-value regression tests to add: a `</script>` title through `mapview.render`;
  a `javascript:` ticket URL through `build_bundle` + the SPA; a user file in an `--out`
  directory; a foreign `Host` request.
- `serve.py` is the best-hardened module (escaping, URL allowlist, read-only, bounded). The
  two renderers that never got the same treatment are `mapview.py` and `web/app.js` — that
  asymmetry is the root cause of F-001, F-002 and F-007.

---

## Remediation status (2026-11-26)

All seven findings were fixed in the same working tree and re-tested; the design rules that
prevent recurrence are recorded in `docs/security-design.md`, `SPEC.md` D-012..D-015 and
AC-031..AC-038, and enforced by `scripts/check_security.py`.

| id | fix | witness after fix |
|---|---|---|
| F-001 | `mapview` uses `sanitize.json_for_script` | `prove_mapview.py`: breakout False; `browser/prove.js`: map `__PWNED` false |
| F-002 | URLs filtered in `build_bundle`, `mapview`, `app.js safeHref` | `prove_bundle.py`: `bundle.urls == []`; no bare `esc()` href |
| F-003 | marker + `--force` + temp rename + root refusal | `prove_rmtree.py`: refused, directory intact |
| F-004 | loopback `Host` allowlist, 421 otherwise | `prove_serve.py`: foreign Host 421 |
| F-005 | reference id on 500, detail to stderr | `prove_serve.py`: path leak False |
| F-006 | C0/C1 strip at ingestion + plain-text cells | `prove_cli_ansi.py`: ESC in stdout False (impact corrected in the report) |
| F-007 | Leaflet 1.9.4 SRI + crossorigin | `browser/sri.js`: Leaflet loads, no console errors |

Gates: 144 unit tests, `check_security.py`, `check_tokens/contrast/manifest/bundle/budget`,
`tests/ui/layout.py` and `node --check` all green. Re-review the commit diff, not this table.
