"""Generate the token block in cinemelb/web/app.css from tokens.json.

Tokens are the only source of visual values. The generated block lives at the top
of the single stylesheet, between markers, so the app keeps one CSS request and
`export.py` stays unchanged. `--check` fails on drift; `--strip` removes the
block for the rollback test.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOKENS = ROOT / "cinemelb" / "web" / "tokens.json"
CSS = ROOT / "cinemelb" / "web" / "app.css"

BEGIN = "/* tokens:begin — generated from cinemelb/web/tokens.json by scripts/build_tokens.py. Do not edit. */"
END = "/* tokens:end */"
BLOCK = re.compile(r"/\* tokens:begin.*?/\* tokens:end \*/", re.S)


def _comment(value: dict) -> str:
    text = str(value.get("$description") or "").strip()
    return f"  /* {text} */" if text else ""


def _declarations(colors: dict, extra: str = "") -> list[str]:
    lines = []
    for name, value in colors.items():
        if not isinstance(value, dict) or "$value" not in value:
            continue
        lines.append(f"  --{name}: {value['$value']};{_comment(value)}")
    if extra:
        lines.append(extra)
    return lines


def render(tokens: dict) -> str:
    color = tokens.get("color") or {}
    dark = color.get("dark") or {}
    light = color.get("light") or {}
    shadow = tokens.get("shadow") or {}
    size = tokens.get("size") or {}
    radius = tokens.get("radius") or {}

    dark_lines = ["  color-scheme: dark;"]
    dark_lines += _declarations(dark)
    dark_lines.append(f"  --shadow: {shadow['dark']['$value']};")
    for name, value in size.items():
        dark_lines.append(f"  --{name}: {value['$value']};{_comment(value)}")
    for name, value in radius.items():
        dark_lines.append(f"  --{name}: {value['$value']};{_comment(value)}")

    light_lines = ["  color-scheme: light;"]
    light_lines += _declarations({name: light.get(name, dark.get(name, {}))
                                  for name in dark})
    light_lines.append(f"  --shadow: {shadow['light']['$value']};")

    return "\n".join([
        BEGIN,
        ":root {",
        *dark_lines,
        "}",
        'html[data-theme="light"] {',
        *light_lines,
        "}",
        END,
    ])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if app.css differs")
    parser.add_argument("--strip", action="store_true", help="remove the generated block")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    tokens = json.loads(TOKENS.read_text(encoding="utf-8"))
    block = render(tokens)
    css = CSS.read_text(encoding="utf-8")

    if args.strip:
        stripped = BLOCK.sub("", css, count=1)
        stripped = re.sub(r"\n{3,}", "\n\n", stripped).lstrip("\n")
        CSS.write_text(stripped, encoding="utf-8")
        if not args.quiet:
            print(f"removed token block from {CSS.relative_to(ROOT)}")
        return 0

    if BLOCK.search(css):
        updated = BLOCK.sub(lambda _: block, css, count=1)
    else:
        updated = block + "\n\n" + css.lstrip("\n")

    if args.check:
        if css != updated:
            print("token block is stale; run: uv run python scripts/build_tokens.py")
            return 1
        if not args.quiet:
            print("token block is current")
        return 0

    CSS.write_text(updated, encoding="utf-8")
    if not args.quiet:
        print(f"wrote token block to {CSS.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
