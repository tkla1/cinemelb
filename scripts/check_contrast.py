"""WCAG contrast checks computed from tokens.json.

Text tokens must reach 4.5:1 on every surface they are used on. Border ratios are
reported as warnings: the current visual identity uses quiet 1px borders, and
phase 3 decides whether to strengthen them (SPEC D-010).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOKENS = ROOT / "cinemelb" / "web" / "tokens.json"


def luminance(value: str) -> float:
    text = value.strip().lstrip("#")
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    r, g, b = (int(text[i:i + 2], 16) / 255 for i in (0, 2, 4))

    def channel(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def ratio(fg: str, bg: str) -> float:
    a, b = luminance(fg), luminance(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def main() -> int:
    tokens = json.loads(TOKENS.read_text(encoding="utf-8"))
    color = tokens["color"]
    failures = 0
    warnings = 0

    for theme in ("dark", "light"):
        values = {name: item["$value"] for name, item in color[theme].items()}
        surfaces = ("bg", "panel", "card", "card-2")
        print(f"\n{theme}")
        for token in ("text", "text-2", "text-3"):
            worst = min((ratio(values[token], values[surface]), surface)
                        for surface in surfaces)
            status = "OK  " if worst[0] >= 4.5 else "FAIL"
            if worst[0] < 4.5:
                failures += 1
            print(f"  {status} {token:12} {worst[0]:5.2f}:1 on {worst[1]}")
        for fg, bg in (("accent-ink", "accent"), ("accent-2", "bg"),
                       ("warn", "card")):
            value = ratio(values[fg], values[bg])
            status = "OK  " if value >= 4.5 else "FAIL"
            if value < 4.5:
                failures += 1
            print(f"  {status} {fg:12} {value:5.2f}:1 on {bg}")
        for token in ("line", "line-2"):
            value = ratio(values[token], values["bg"])
            print(f"  WARN {token:12} {value:5.2f}:1 on bg (border; phase 3)")
            warnings += 1

    if failures:
        print(f"\n{failures} contrast failure(s)")
        return 1
    print(f"\nall text pairs pass (4.5:1); {warnings} border warning(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
