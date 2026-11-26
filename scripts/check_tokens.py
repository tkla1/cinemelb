"""Check the token block for drift, undefined/unused tokens, and raw colours."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import build_tokens

DECL = re.compile(r"^\s*--([\w-]+)\s*:", re.M)
USE = re.compile(r"var\(--([\w-]+)")
LITERAL = re.compile(r"#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--coverage", action="store_true", help="check defined vs used")
    parser.add_argument("--literals", action="store_true", help="reject raw colours outside tokens")
    parser.add_argument("--motion", action="store_true", help="check reduced-motion handling")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()
    if not (args.coverage or args.literals or args.motion):
        args.coverage = args.literals = True

    tokens = json.loads(build_tokens.TOKENS.read_text(encoding="utf-8"))
    expected = build_tokens.render(tokens)
    css = build_tokens.CSS.read_text(encoding="utf-8")
    block_match = build_tokens.BLOCK.search(css)
    ok = True

    if block_match is None:
        print("FAIL token block missing from app.css")
        return 1
    if block_match.group(0) != expected:
        print("FAIL token block is stale; run: uv run python scripts/build_tokens.py")
        ok = False
    elif not args.quiet:
        print("OK   token block matches tokens.json")

    if args.coverage:
        defined = set(DECL.findall(expected))
        used = set(USE.findall(css))
        manifest = Path("components.json")
        if manifest.exists():
            data = json.loads(manifest.read_text(encoding="utf-8"))
            for component in data.get("components", []):
                for name in component.get("tokens", []):
                    used.add(name.lstrip("-"))
        missing = sorted(used - defined)
        unused = sorted(defined - used)
        if missing:
            print("FAIL undefined tokens:", ", ".join("--" + name for name in missing))
            ok = False
        if unused:
            print("FAIL unused tokens:", ", ".join("--" + name for name in unused))
            ok = False
        if not missing and not unused and not args.quiet:
            print(f"OK   {len(defined)} tokens defined and referenced")

    if args.literals:
        body = build_tokens.BLOCK.sub("", css)
        hits = []
        for number, line in enumerate(body.splitlines(), 1):
            if LITERAL.search(line):
                hits.append(f"{number}: {line.strip()}")
        if hits:
            print("FAIL raw colour outside tokens:")
            for hit in hits[:20]:
                print("  ", hit)
            ok = False
        elif not args.quiet:
            print("OK   no raw colour literals outside the token block")

    if args.motion:
        if "prefers-reduced-motion" not in css:
            print("FAIL no prefers-reduced-motion rule")
            ok = False
        elif not args.quiet:
            print("OK   reduced-motion rule present")

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
