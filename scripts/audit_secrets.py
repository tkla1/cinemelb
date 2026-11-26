# /// script
# requires-python = ">=3.11"
# ///
"""Sensitive-data audit for this repository.

This is the only sanctioned way to inspect .env. It reads the file
programmatically and reports key names, value lengths and locations — never
values, and never the surrounding text of a match.

Checks three surfaces:
  1. committed working tree (git ls-files)
  2. every blob in the git object database (reachable and unreachable)
  3. untracked/ignored local files, reported separately (never public)

Run: uv run python scripts/audit_secrets.py
Exit code 1 only when a secret is found in committed content.

For the fast deterministic gate (gitleaks + ruff + project invariants), run
`uv run python scripts/security_gate.py`. This tool remains the deeper audit:
it also reports unreachable objects and local ignored files.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "scratch" / "secret_audit.json"
IGNORE_DIRS = {".git", ".venv", "node_modules", "__pycache__", ".zvec-grep", ".pytest_cache"}
PLACEHOLDER = re.compile(r"^(?:x+|X+|\*+|\.+|change[-_ ]?me|your[-_ ].*|example|<.*>)$", re.I)

SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("aws_access_key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("github_token", re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}")),
    ("github_pat_v2", re.compile(r"github_pat_[A-Za-z0-9_]{50,}")),
    ("slack_token", re.compile(r"xox[abposr]-[A-Za-z0-9-]{10,}")),
    ("google_api_key", re.compile(r"AIza[0-9A-Za-z_-]{35}")),
    ("stripe_live", re.compile(r"(?:sk|rk)_live_[0-9A-Za-z]{10,}")),
    ("openai_key", re.compile(r"sk-(?:proj-)?[A-Za-z0-9_-]{32,}")),
    ("anthropic_key", re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}")),
    ("private_key_block", re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----")),
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    ("http_basic_auth", re.compile(r"https?://[^\s/:@]+:[^\s/@]{6,}@[^\s/]+")),
    (
        "assigned_secret",
        re.compile(
            r"(?i)\b(?:api[_-]?key|apikey|secret[_-]?key|access[_-]?token|auth[_-]?token|"
            r"client[_-]?secret|password|passwd)\b\s*[:=]\s*['\"]([^'\"\s]{8,})['\"]"
        ),
    ),
]


def git(*args: str) -> tuple[int, str]:
    proc = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, check=False)
    return proc.returncode, proc.stdout.decode("utf-8", "replace")


def read_env() -> list[tuple[str, str]]:
    """Parse .env into (key, value) pairs. Values stay inside this process."""
    path = ROOT / ".env"
    if not path.exists():
        return []
    pairs: list[tuple[str, str]] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = line.removeprefix("export ").strip()
        key, sep, value = line.partition("=")
        if not sep or not key.strip():
            continue
        pairs.append((key.strip(), value.strip().strip('"').strip("'")))
    return pairs


def classify(value: str) -> str:
    if not value:
        return "empty"
    if PLACEHOLDER.match(value) or "xxxx" in value.lower():
        return "placeholder"
    if value.startswith("${"):
        return "indirection"
    return "set"


def scan_text(text: str, patterns=SECRET_PATTERNS) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    seen: set[tuple[str, int]] = set()
    for name, rx in patterns:
        for match in rx.finditer(text):
            key = (name, match.start())
            if key in seen:
                continue
            seen.add(key)
            found.append((name, match.group(0)[:6]))
    return found


def scan_committed(env_values: list[tuple[str, str]]) -> dict[str, list[dict[str, object]]]:
    _, listing = git("ls-files", "-z")
    hits: dict[str, list[dict[str, object]]] = {"value": [], "pattern": []}
    for rel in listing.split("\0"):
        if not rel:
            continue
        path = ROOT / rel
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if b"\x00" in data[:4096]:
            continue
        text = data.decode("utf-8", "replace")
        for key, value in env_values:
            if value in text:
                hits["value"].append({"key": key, "path": rel, "line": text[: text.index(value)].count("\n") + 1})
        for name, start in scan_text(text):
            hits["pattern"].append({"pattern": name, "path": rel, "masked": start + "…"})
    return hits


def iter_all_blobs():
    """Stream every object in the object database, including unreachable ones."""
    proc = subprocess.Popen(
        ["git", "cat-file", "--batch-all-objects", "--batch"],
        cwd=ROOT,
        stdout=subprocess.PIPE,
    )
    assert proc.stdout is not None
    stream = proc.stdout
    while True:
        header = stream.readline()
        if not header:
            break
        sha, kind, size = header.split()
        size = int(size)
        body = stream.read(size)
        stream.read(1)  # trailing newline
        if kind == b"blob":
            yield sha.decode(), body
    proc.wait()


def scan_history(env_values: list[tuple[str, str]]) -> dict[str, list[dict[str, object]]]:
    _, objects = git("rev-list", "--all", "--objects")
    paths: dict[str, list[str]] = defaultdict(list)
    for line in objects.splitlines():
        sha, _, path = line.partition(" ")
        if path:
            paths[sha].append(path)

    hits: dict[str, list[dict[str, object]]] = {"value": [], "pattern": []}
    for sha, body in iter_all_blobs():
        if not body or b"\x00" in body[:4096]:
            continue
        text = body.decode("utf-8", "replace")
        label = paths.get(sha, ["<unreachable blob>"])[0]
        for key, value in env_values:
            if value in text:
                hits["value"].append({"key": key, "path": label, "blob": sha[:10]})
        for name, start in scan_text(text):
            hits["pattern"].append({"pattern": name, "path": label, "blob": sha[:10], "masked": start + "…"})
    return hits


def scan_local() -> dict[str, object]:
    """Untracked/ignored files: local only, but report what is lying around."""
    tracked = set(git("ls-files", "-z")[1].split("\0"))
    by_pattern: Counter[str] = Counter()
    by_dir: Counter[str] = Counter()
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(ROOT).as_posix()
        if rel in tracked or any(part in IGNORE_DIRS for part in path.parts):
            continue
        if path.name == ".env":
            continue
        try:
            if path.stat().st_size > 2_000_000:
                continue
            data = path.read_bytes()
        except OSError:
            continue
        if b"\x00" in data[:4096]:
            continue
        matches = scan_text(data.decode("utf-8", "replace"))
        for name, _ in matches:
            by_pattern[name] += 1
            by_dir[rel.split("/")[0] + ("/" + rel.split("/")[1] if "/" in rel else "")] += 1
    return {"by_pattern": dict(by_pattern), "by_dir": dict(by_dir)}


def main() -> int:
    pairs = read_env()
    env_entries = [
        {"key": key, "status": classify(value), "length": len(value)} for key, value in pairs
    ]
    real_values = [(key, value) for key, value in pairs if classify(value) == "set" and len(value) >= 8]

    env_path = ROOT / ".env"
    ignored = subprocess.run(["git", "check-ignore", "-q", ".env"], cwd=ROOT).returncode == 0
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", ".env"], cwd=ROOT, capture_output=True
    ).returncode == 0

    committed = scan_committed(real_values)
    history = scan_history(real_values)
    local = scan_local()

    _, remote = git("remote", "-v")
    report = {
        "env": {
            "exists": env_path.exists(),
            "tracked_by_git": tracked,
            "gitignored": ignored,
            "entries": env_entries,
        },
        "git": {
            "commits": len(git("rev-list", "--all", "--count")[1].split()),
            "remote_credentials": bool(re.search(r"https?://[^/\s]+@", remote)),
            "remotes": remote.splitlines(),
        },
        "committed": committed,
        "history": history,
        "local_only": local,
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(".env: " + ("exists" if env_path.exists() else "absent") + f", tracked={tracked}, gitignored={ignored}")
    if env_entries:
        for entry in env_entries:
            print(f"  - {entry['key']}: {entry['status']} (len {entry['length']})")
    else:
        print("  - no KEY=VALUE entries")

    print(f"\ncommitted files: value leaks={len(committed['value'])}, pattern hits={len(committed['pattern'])}")
    for hit in committed["value"] + committed["pattern"]:
        print(f"  - {hit['path']}: {hit.get('pattern') or hit.get('key')} {hit.get('masked', '')}")
    print(f"git history blobs: value leaks={len(history['value'])}, pattern hits={len(history['pattern'])}")
    for hit in history["value"] + history["pattern"]:
        print(f"  - {hit['path']} @ {hit.get('blob')}: {hit.get('pattern') or hit.get('key')} {hit.get('masked', '')}")
    print(f"local-only (ignored/untracked): {sum(local['by_pattern'].values())} matches in {len(local['by_dir'])} dirs")
    for name, count in sorted(local["by_pattern"].items()):
        print(f"  - {name}: {count}")
    print(f"\nreport: {REPORT.relative_to(ROOT)}")

    leaked = bool(committed["value"] or committed["pattern"] or history["value"] or history["pattern"])
    if leaked:
        print("\nRESULT: committed secrets found — rotate and purge before pushing.")
    else:
        print("\nRESULT: no secrets in committed content or git history.")
    return 1 if leaked else 0


if __name__ == "__main__":
    sys.exit(main())
