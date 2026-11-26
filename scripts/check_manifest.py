"""Validate components.json against the app, the gallery and the token set."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import build_tokens

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "components.json"
APP = ROOT / "cinemelb" / "web" / "app.js"
GALLERY = ROOT / "cinemelb" / "web" / "gallery.html"
REQUIRED = {"id", "name", "status", "source", "render", "gallery", "states", "tokens"}


def main() -> int:
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    app = APP.read_text(encoding="utf-8")
    gallery = GALLERY.read_text(encoding="utf-8") if GALLERY.exists() else ""
    tokens = json.loads(build_tokens.TOKENS.read_text(encoding="utf-8"))
    rendered = build_tokens.render(tokens)
    defined = set(re.findall(r"^\s*--([\w-]+)\s*:", rendered, re.M))
    manifest_tokens = set()
    for component in data.get("components", []):
        manifest_tokens.update(name.lstrip("-") for name in component.get("tokens", []))

    problems = []
    ids = set()
    for index, component in enumerate(data.get("components", [])):
        label = component.get("id") or f"#{index}"
        missing = REQUIRED - set(component)
        if missing:
            problems.append(f"{label}: missing {', '.join(sorted(missing))}")
        if component.get("id") in ids:
            problems.append(f"{label}: duplicate id")
        ids.add(component.get("id"))
        if component.get("status") not in {"stable", "beta", "deprecated", "planned"}:
            problems.append(f"{label}: bad status")
        render = str(component.get("render") or "")
        if render.startswith("window.CINEMELB.render."):
            name = render.rsplit(".", 1)[-1]
            if not re.search(rf"\b{re.escape(name)}\s*:", app):
                problems.append(f"{label}: render function {name} is not exposed")
        elif render not in {"planned", "static"}:
            problems.append(f"{label}: unknown render {render!r}")
        gallery_path = str(component.get("gallery") or "")
        if f"#{label}" not in gallery_path:
            problems.append(f"{label}: gallery path does not point at its own anchor")
        if gallery and f'id="{label}"' not in gallery:
            problems.append(f"{label}: no section in gallery.html")
        if not component.get("states"):
            problems.append(f"{label}: no states")
        for token in component.get("tokens", []):
            if token.lstrip("-") not in defined:
                problems.append(f"{label}: unknown token {token}")

    unknown = sorted(manifest_tokens - defined)
    if unknown:
        problems.append("unknown tokens in manifest: " + ", ".join("--" + t for t in unknown))
    unused = sorted(defined - manifest_tokens)
    if unused:
        problems.append("tokens not used by any component: " +
                        ", ".join("--" + t for t in unused))

    if problems:
        print("FAIL component manifest")
        for problem in problems:
            print("  -", problem)
        return 1
    print(f"OK   {len(ids)} components; {len(defined)} tokens; render + gallery + manifest agree")
    return 0


if __name__ == "__main__":
    sys.exit(main())
