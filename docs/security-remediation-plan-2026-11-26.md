# Remediation plan — cinemelb security review

**Status: implemented 2026-11-26.** All items below shipped in the working tree; the
verification matrix is green and the design rules now live in `docs/security-design.md`,
`SPEC.md` (D-012..D-015, AC-031..AC-038) and `scripts/check_security.py`. This file is kept
as the decision record of *what* was planned and why.

Source: `docs/security-review-2026-11-26.md` (target `57ff76e`, 7 findings).
Goal: close F-001 before the next release; close the rest in the same cycle.
Total effort: **~2.5 developer-days**, three PRs, no new runtime dependencies.

## Order and shape

| Order | Item | Why now | Effort |
|---|---|---|---|
| 1 | F-001 script injection in map export | blocking, no interaction, shipped artefact | 0.5 d |
| 2 | F-003 destructive `export --out` | irreversible data loss | 0.5 d |
| 3 | F-002 `href` scheme allowlist | same root cause as F-001; one shared helper | 0.5 d |
| 4 | F-004 `Host` allowlist | small, prevents rebinding class | 0.5 d |
| 5 | F-006 terminal escapes | small once the ingestion choke point exists | 0.5 d |
| 6 | F-005 generic 500 + F-007 SRI | trivial, fold into PR 2 / PR 1 | 0.25 d |

Three PRs, because they touch different surfaces and can be reviewed independently:

1. **`fix/security-renderers`** — F-001, F-002, F-007 + `cinemelb/sanitize.py` + static guard + unit tests.
2. **`fix/security-export-server`** — F-003, F-004, F-005 + tests.
3. **`fix/security-terminal`** — F-006 + ingestion strip + CLI tests.

Each PR: `uv run python -m unittest discover -s tests -v`, `node --check cinemelb/web/app.js`,
`uv run python scripts/check_security.py`, and the matching `scratch/review/prove_*.py` must
flip from fail to pass. PR 1 also runs the project's UI gate,
`uv run --with playwright python tests/ui/layout.py`, because it edits `app.js`. Per project
rules, PR 1 updates `SPEC.md` (decision log + AC) because it changes UI-facing renderers.

---

## Cross-cutting: `cinemelb/sanitize.py`

One module for the three contexts that were handled inconsistently. ~40 lines, no deps.

```python
"""Output-context helpers for untrusted (scraped) text."""
from __future__ import annotations
import json
import re

_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_JS = {"<": "\\u003c", ">": "\\u003e", "&": "\\u0026",
       "\u2028": "\\u2028", "\u2029": "\\u2029"}


def clean_text(value: object) -> str:
    """Strip C0/C1 control characters before display or storage."""
    return _CONTROL.sub("", str("" if value is None else value))


def safe_href(url: object) -> str:
    """Only absolute http(s) URLs may reach an href/src."""
    candidate = str("" if url is None else url).strip()
    return candidate if candidate.startswith(("http://", "https://")) else ""


def json_for_script(payload: object) -> str:
    """json.dumps that is safe inside an HTML <script> element."""
    raw = json.dumps(payload, ensure_ascii=False, default=str)
    return "".join(_JS.get(ch, ch) for ch in raw)
```

`serve.py::_safe_href` becomes a thin import of `safe_href` (keep the name so existing call
sites do not churn), which also removes the duplicate implementation.

### F-001 — map export script injection

**Change** `cinemelb/mapview.py`:

- line 64 stays `const DATA = {data_json};`
- line 198 becomes `data_json=json_for_script(payload)` (import from `.sanitize`).

Do not "fix" this by changing the client `esc()`; the injection happens before the client
runs. Do not switch `ensure_ascii=True`; it does not escape `</script>`.

**Tests** (`tests/test_security.py`):

```python
def test_map_json_cannot_close_the_script(self):
    from cinemelb.mapview import render
    hit = make_hit(title='Evil</script><script>window.__PWNED=1</script>')
    html = render([hit], title="t", out_path=self.tmp / "m.html").read_text()
    self.assertNotIn("</script><script>window.__PWNED", html)
    self.assertIn("\\u003c/script", html)
```

**Acceptance:** `scratch/review/prove_mapview.py` prints `raw </script> inside the DATA line:
False`; `node scratch/review/browser/prove.js` prints `map window.__PWNED (script tag): false`;
the map still renders markers and popups (manual check with a normal title).

### F-002 — URL scheme allowlist at every `href`

**Change** `cinemelb/web/app.js`: add next to `esc()`:

```js
function safeHref(u) {
  u = String(u == null ? '' : u).trim();
  return /^https?:\/\//i.test(u) ? u : '';
}
```

Apply `esc(safeHref(x))` at the six sinks (line numbers from the review commit; re-locate by
pattern): the RT and MC score links, the venue `booking`/`website` links, the session `url`
(`app.js:1568`), and the trailer link. Leave non-URL `href`s (`#/…` routes) alone.

**Change** `cinemelb/mapview.py::_venue_payload`: set `"ticket_url": safe_href(s.get("ticket_url"))`
(import from `.sanitize`). The template's `esc()` stays for text.

**Tests:**

- Python: build a payload with `javascript:`/`data:`/`//evil` and assert `safe_href` returns `""`
  and that rendered map HTML contains no `javascript:`.
- JS: `scripts/check_security.py` asserts every `href="' + esc(` in `app.js` is
  `href="' + esc(safeHref(` (regex over the six sites, fail on a new one).
- Browser spot check: a `javascript:` ticket URL renders an inert/missing link, not
  `about:blank`.

### F-007 — Leaflet integrity

**Change** `cinemelb/mapview.py` lines 28 and 62 (hashes computed from the pinned unpkg bytes
on 2026-11-26 and matching the published Leaflet 1.9.4 values):

```html
<link rel="stylesheet"
      href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
      integrity="sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY="
      crossorigin="anonymous">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
        integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo="
        crossorigin="anonymous"></script>
```

**Alternative** (choose if offline use matters more than file size): vendor both files into
`cinemelb/web/vendor/` and inline them at render time. +162 KB per generated map, but no CDN,
no SRI drift, no privacy leak. Recommendation: SRI now; revisit vendoring if maps are used
offline.

**Acceptance:** generated map loads Leaflet with no console integrity error; the static guard
fails any `<script src="http…` in `mapview.py` without `integrity=`.

### F-003 — safe `export --out`

**Change** `cinemelb/export.py`:

```python
_MARKER = ".cinemelb-site"

def _guard_out(out: Path, *, force: bool) -> None:
    if out.parent == out:                      # C:\ or /
        raise SystemExit(f"refusing a filesystem root: {out}")
    if out.exists() and not force and not (out / _MARKER).exists():
        raise SystemExit(
            f"refusing to replace {out}: it is not a cinemelb site "
            f"(no {_MARKER}); pass --force if you are sure"
        )
```

Build into `out.with_name(out.name + ".building")`, then:

1. `_guard_out(out, force=force)`
2. write the complete site into the temp dir
3. `shutil.rmtree(out)` only now (if it exists)
4. `tmp.rename(out)`
5. write `out / _MARKER` with the version

`write_site(bundle, out_dir, *, force: bool = False)`; `cli export` gains `--force`.
This makes deletion happen only after a successful build, and only for a marked or forced
directory.

**Tests:**

- existing dir with a user file, no marker → `SystemExit`, file intact, no temp left behind
- marked dir → replaced
- `--force` → replaced
- `Path("C:/")` (guarded by `os.name == "nt"`) → `SystemExit`

**Acceptance:** `prove_rmtree.py` now exits before deleting; a normal `cinemelb export -o dist`
still works twice in a row (second run sees the marker).

### F-004 — `Host` allowlist

**Change** `cinemelb/serve.py`:

- `_Server` gains `allowed_hosts: set[str]`.
- `make_server` computes it from the bind host:
  - loopback bind (`127.0.0.1`, `localhost`, `::1`, empty) → `{"127.0.0.1", "localhost", "[::1]", "::1"}`
  - explicit non-loopback bind → loopback set **plus** the bind host (so `--host 0.0.0.0`
    still serves its LAN name only if requested by that literal; document this)
- In `do_GET`, before dispatch: parse `Host` (strip the port, handle `[::1]:8787`), and return
  `421` with a one-line body if it is not in the set. A missing `Host` is also `421` (HTTP/1.1
  requires it).
- `serve()` prints a warning when `--host` is not loopback.

**Tests:** start `make_server(port=0)` on a temp DB; `http.client`/`urllib` requests with
`Host: attacker.example` → 421; `Host: 127.0.0.1:<port>` and `Host: localhost:<port>` → 200.

**Acceptance:** `prove_serve.py` shows `Host: attacker.example -> 421`, normal requests
unchanged, `curl http://127.0.0.1:8787/api/venues` works.

### F-005 — generic 500

**Change** `cinemelb/serve.py::do_GET`:

```python
except Exception as exc:
    ref = uuid.uuid4().hex[:8]
    print(f"[{ref}] {type(exc).__name__}: {exc}", file=sys.stderr)
    status, content_type, body = 500, "text/html; charset=utf-8",
        _error_page(f"The query failed (reference {ref}). See the terminal for details.")
```

**Test:** monkeypatch `_build_payload` to raise; response is 500, contains `reference`, does
not contain `sqlite3`, `.sqlite`, or a path separator.

### F-006 — terminal control characters

**Change, two layers:**

1. **Ingestion** (`cinemelb/store.py` row builders, or `models.py` factories): apply
   `clean_text()` to `cinema.name/suburb/chain/address`, `film.title/aka/genres`,
   `screening.film_title/format/screen`. This fixes future refreshes at the source.
2. **Render** (`cinemelb/cli.py`): in `_show_hits`, wrap every cell in
   `rich.text.Text(clean_text(cell))` (markup off). Audit the other `console.print` sites that
   interpolate DB text (`film`, `at`, `now`, `next`, `week`, group headers) and route them
   through the same helper. Existing DB rows are covered by this layer until the next refresh.

Do not strip Unicode formatting characters (ZWJ, combining marks); only C0/C1.

**Tests:** CliRunner against a DB whose title/venue contain `\x1b[2J` and a full OSC 52
sequence; assert `"\x1b" not in stdout` for `whats-on`, `film`, `at`, `now`; assert the
legitimate title text is still present.

**Acceptance:** `prove_cli_ansi.py` prints `ESC byte in stdout: False`; `--json` unchanged.

---

## Regression guard: `scripts/check_security.py`

A ~60-line stdlib script, wired into the existing verify habit (and CI if present). It fails
on the three mistakes that caused the high findings, so a future refactor cannot reintroduce
them silently:

1. `json.dumps(` interpolated into `<script>` without `json_for_script(` — scan `mapview.py`.
2. `href="' + esc(` in `cinemelb/web/app.js` — must be `esc(safeHref(`.
3. `<script src="http` / `<link rel="stylesheet" href="http` in `mapview.py` without
   `integrity=` on the same tag.
4. `shutil.rmtree(` in `export.py` must be preceded by the marker guard (presence check of
   `_MARKER` / `--force` in the same function).

Exit non-zero with `file:line` for each violation. Add it to the PR checklist.

## Verification matrix (run after all three PRs)

| Proof | Before | Expected after |
|---|---|---|
| `prove_mapview.py` | `raw </script>` True | False |
| `browser/prove.js` | map `__PWNED` true | false |
| `prove_bundle.py` | `javascript:` href emitted | empty href |
| `prove_rmtree.py` | accepts and deletes | `SystemExit`, file intact |
| `prove_serve.py` | foreign Host 200; path leak | 421; generic 500 |
| `prove_cli_ansi.py` | ESC True | ESC False |
| unit suite | baseline green | green + ~12 new tests |
| `node --check cinemelb/web/app.js` | ok | ok |

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| SRI breaks if the CDN serves different bytes | version + hash pinned; load test; vendoring fallback documented |
| `Host` check breaks LAN use | allowlist derived from the bind host; warning printed; documented in README/`--help` |
| `\u003c` changes the JSON payload | it is still valid JS/JSON; only the source representation changes; round-trip test |
| control stripping alters legitimate titles | strip only C0/C1; keep ZWJ/combining marks; test an emoji + accented title |
| temp-dir build leaves debris on crash | create under `out.parent`, remove in `finally`; name is deterministic and ignored by `.gitignore` |
| dirty tree / concurrent UI work | rebase PR 1 on the token/SPEC work; resolve line numbers then |

## Explicit non-goals

- No CSP for the generated map (it is a single inline-script file; that needs a nonce/hash
  build step — separate decision).
- No auth on the localhost UI (it serves public data by design).
- No rewrite of adapter parsing or `ratings.py` (out of scope in the review).
- No dependency changes: the runtime lock was clean.

## Definition of done

- [ ] All six proofs flip as tabulated, re-run from a clean checkout of the merged commit.
- [ ] `uv run python -m unittest discover -s tests -v` green, including the new
      `tests/test_security.py`; `uv run --with playwright python tests/ui/layout.py` green.
- [ ] `scripts/check_security.py` passes and is referenced in the PR checklist.
- [ ] `SPEC.md` decision log updated for the renderer changes (project rule).
- [ ] `docs/security-review-2026-11-26.md` gets a short "remediated in <commit>" footer with
      the re-test date, or a follow-up review notes which items remain.
