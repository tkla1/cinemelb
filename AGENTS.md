# cinemelb — project instructions

- **UI work is spec-driven.** `SPEC.md` is authoritative; read it before any UI change and
  update it (decision log + AC statuses) in the same change. Use the `spec-driven-design`
  skill.
- **Design decisions live in two files.** UI/design records go in `SPEC.md` (§10 decision
  log); security records go in `docs/security-design.md` (invariants, contexts, rules).
  Record decisions there, not in chat or code comments.
- **Zero-build.** No framework, bundler, or npm step for the app. Edit
  `cinemelb/web/{index.html,app.css,app.js}`; `cinemelb export` builds `dist/`.
- **Tokens are the only homes for visual values.** Source: `cinemelb/web/tokens.json`.
  Regenerate the block with `uv run python scripts/build_tokens.py`; never hand-edit inside
  the `tokens:begin`/`tokens:end` markers.
- **Components.** `components.json` is the manifest; `cinemelb/web/gallery.html` renders
  components through `window.CINEMELB.render` (real render functions, no copied markup).
- **Verify cheapest-first.** `node --check cinemelb/web/app.js`, then
  `uv run python scripts/check_tokens.py --coverage --literals`,
  `check_contrast.py`, `check_manifest.py`, `check_bundle.py --against-db`,
  `check_budget.py dist`, `uv run python scripts/security_gate.py`,
  `uv run python tests/ui/layout.py`, and
  `uv run python -m unittest discover -s tests`.
  Do not screenshot to discover structure; the layout script measures geometry, focus and
  aria snapshots at 390px and 1280px. A screenshot is only for an appearance question
  (contrast, rhythm, clipping) that those measurements and tokens cannot answer.
- **Titles.** Programme prefixes (`BFF26`, `JIFF:`, `Cult Classix:`, `Astor:`, …) are
  stripped at ingestion; originals live in `aka`. See `cinemelb/titles.py` and SPEC D-004.
- **Data.** Scraping, ratings, covers, and bundle contracts are documented in `README.md`,
  `STATE.md`, and SPEC §5. Do not change them as a side effect of a UI task.
