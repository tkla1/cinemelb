"""Static guard for the security invariants the review established.

These are the mistakes that produced the high findings, expressed as patterns a
linter can catch. It is deliberately dumb and fast: no imports, no AST, no
network. Any hit is a regression of a control that now has a unit test too.

    uv run python scripts/check_security.py

Exit 0 = clean, 1 = violation (printed as file:line).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAPVIEW = ROOT / "cinemelb" / "mapview.py"
APP_JS = ROOT / "cinemelb" / "web" / "app.js"
EXPORT = ROOT / "cinemelb" / "export.py"
SERVE = ROOT / "cinemelb" / "serve.py"
SANITIZE = ROOT / "cinemelb" / "sanitize.py"

failures: list[str] = []


def fail(path: Path, line: int, message: str) -> None:
    try:
        rel = path.relative_to(ROOT).as_posix()
    except ValueError:
        rel = str(path)
    failures.append(f"{rel}:{line}: {message}")


def lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def check_mapview() -> None:
    text = MAPVIEW.read_text(encoding="utf-8")
    for i, line in enumerate(lines(MAPVIEW), 1):
        if "json.dumps(" in line:
            fail(MAPVIEW, i, "use json_for_script(): json.dumps is not script-safe")
    # Tags span lines, so match whole tags rather than single lines.
    for match in re.finditer(r"<(?:script|link)\b[^>]*>", text, re.S):
        tag = match.group(0)
        if "http" in tag and "integrity=" not in tag:
            line = text[: match.start()].count("\n") + 1
            fail(MAPVIEW, line, "external asset without integrity=")
    if "json_for_script(" not in text:
        fail(MAPVIEW, 1, "map payload must go through json_for_script()")
    if "safe_href(" not in text:
        fail(MAPVIEW, 1, "map ticket URLs must go through safe_href()")


def check_app_js() -> None:
    for i, line in enumerate(lines(APP_JS), 1):
        # An href built straight from esc() has no scheme allowlist.
        if re.search(r'href="\'\s*\+\s*esc\((?!safeHref\()', line):
            fail(APP_JS, i, "href uses esc() without safeHref()")
        if re.search(r'src="\'\s*\+\s*esc\((?!safeHref\()', line):
            fail(APP_JS, i, "src uses esc() without safeHref()")
        if "safeHref(" not in line and re.search(r"href=\"\$\{esc\(", line):
            fail(APP_JS, i, "template href uses esc() without safeHref()")


def check_export() -> None:
    text = EXPORT.read_text(encoding="utf-8")
    if "SITE_MARKER" not in text or "_guard_out(" not in text:
        fail(EXPORT, 1, "write_site must guard on SITE_MARKER before replacing")
    if "shutil.rmtree(out)" in text and "if out.exists()" not in text:
        fail(EXPORT, 1, "rmtree(out) must be conditional")
    if "build.rename(out)" not in text:
        fail(EXPORT, 1, "site must be built aside and renamed into place")
    if "url = safe_href(" not in text:
        fail(EXPORT, 1, "bundle URLs must be filtered with safe_href()")


def check_serve() -> None:
    text = SERVE.read_text(encoding="utf-8")
    if "The query failed: {exc}" in text:
        fail(SERVE, 1, "500 page must not echo the exception text")
    if "allowed_hosts" not in text:
        fail(SERVE, 1, "loopback Host allowlist missing")


def check_sanitize() -> None:
    if not SANITIZE.exists():
        failures.append("cinemelb/sanitize.py: missing")
        return
    text = SANITIZE.read_text(encoding="utf-8")
    for needle in ("def clean_text", "def safe_href", "def json_for_script"):
        if needle not in text:
            failures.append(f"cinemelb/sanitize.py: {needle} missing")


def main() -> int:
    check_sanitize()
    check_mapview()
    check_app_js()
    check_export()
    check_serve()
    if failures:
        print("security invariants violated:")
        for item in failures:
            print("  " + item)
        return 1
    print("security invariants: ok "
          "(sanitize, map script context, href schemes, SRI, export guard, Host, 500)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
