"""Deterministic security gate: established scanners plus project invariants.

One command, offline, no model judgement. Generic checks come from tools that
already own the rules; only this project's invariants and its real ``.env``
values are hand-written.

  1. secrets, worktree   gitleaks dir over the files that would be published
                         (tracked + untracked, ignored files excluded)
  2. secrets, history    gitleaks git over every reachable commit, plus a
                         verbatim check for values from the local ``.env``
  3. Python SAST         ruff with its flake8-bandit rules (S301, S307, S501,
                         S506, S602, S605, S608)
  4. JS sinks            the three reviewed JS sinks (eval, new Function,
                         document.write). Semgrep is not used: its Windows
                         build fails on the system certificate store.
  5. invariants          check_security.py (map script, hrefs, SRI, export,
                         Host, 500 - no off-the-shelf rule knows these)
  6. supply chain        uv.lock parses and names every direct dependency
  7. bundle              exported JSON has no secret shapes or script URLs

Exit 0 = clean, 1 = finding, 2 = a scanner could not run (never a silent pass).

    uv run python scripts/security_gate.py
    uv run python scripts/security_gate.py --no-history
    uv run python scripts/security_gate.py --json scratch/security-gate.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

import audit_secrets as audit  # noqa: E402
import check_security as static  # noqa: E402

MAX_FILE_BYTES = 5_000_000
MAX_HISTORY_SECONDS = 20.0
TOOL_TIMEOUT = 180
SCRIPT_URL = re.compile(r"\"(?:javascript|data:text/html)\s*:", re.I)


class GateError(RuntimeError):
    """The gate could not complete; exit 2 rather than pass."""


@dataclass(frozen=True)
class Finding:
    section: str
    severity: str          # "error" | "warn"
    where: str
    message: str           # masked; never a secret value


@dataclass
class Section:
    name: str
    status: str            # "pass" | "fail" | "warn"
    detail: str
    seconds: float = 0.0


# --------------------------------------------------------------------- shell

def git(root: Path, *args: str) -> tuple[int, str]:
    proc = subprocess.run(["git", *args], cwd=root, capture_output=True, check=False)
    return proc.returncode, proc.stdout.decode("utf-8", "replace")


def tool_path(name: str) -> str:
    """Prefer the persistent tool installs over the project venv.

    The venv may carry a broken copy (semgrep's cert store on this machine);
    the tools are installed once with `uv tool install` or a native installer,
    which is also where their rules and binaries stay current.
    """
    suffix = ".exe" if os.name == "nt" else ""
    hints = [Path.home() / ".local" / "bin" / (name + suffix),
             Path(sys.executable).with_name(name + suffix)]
    for candidate in hints:
        if candidate.exists():
            return str(candidate)
    found = shutil.which(name)
    if not found:
        raise GateError(
            f"{name} not found; install it as a persistent tool "
            f"(uv tool install {name}; gitleaks via winget install "
            f"gitleaks.gitleaks)")
    return found


def run_tool(cmd: list[str], timeout: int = TOOL_TIMEOUT) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                              timeout=timeout, check=False)
    except FileNotFoundError as exc:
        raise GateError(f"{cmd[0]} not found: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise GateError(f"{Path(cmd[0]).name} exceeded {timeout}s") from exc


def read_json(path: Path, tool: str):
    if not path.exists():
        raise GateError(f"{tool} wrote no report at {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8") or "null")
    except json.JSONDecodeError as exc:
        raise GateError(f"{tool} report is not JSON: {exc}") from exc


# ------------------------------------------------------------------ secrets

def publishable_files(root: Path) -> list[str]:
    """Tracked files plus untracked files that are not ignored."""
    code, listing = git(root, "ls-files", "-z", "--cached", "--others",
                        "--exclude-standard")
    if code != 0:
        raise GateError("git ls-files failed; is this a git repository?")
    files = []
    for rel in listing.split("\0"):
        if not rel:
            continue
        path = root / rel
        try:
            if path.is_file() and path.stat().st_size <= MAX_FILE_BYTES:
                files.append(rel)
        except OSError:
            continue
    return sorted(files)


def snapshot(root: Path, files: list[str]) -> tuple[Path, tempfile.TemporaryDirectory]:
    handle = tempfile.TemporaryDirectory(prefix="gate-", dir=root / "scratch")
    base = Path(handle.name)
    for rel in files:
        target = base / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copyfile(root / rel, target)
        except OSError as exc:
            raise GateError(f"cannot stage {rel} for scanning: {exc}") from exc
    return base, handle


def snapshot_relative(file: str, work: Path) -> str:
    try:
        return os.path.relpath(Path(file), work).replace("\\", "/")
    except (ValueError, OSError):
        return file


def gitleaks_findings(section: str, data: list[dict],
                      work: Path | None = None) -> list[Finding]:
    findings = []
    for item in data:
        label = (snapshot_relative(str(item.get("File", "?")), work)
                 if work is not None else item.get("File", "?"))
        where = f"{label}:{item.get('StartLine', '?')}"
        commit = str(item.get("Commit", ""))[:10]
        if commit:
            where += f" @ {commit}"
        findings.append(Finding(section, "error", where,
                                str(item.get("RuleID", "secret"))))
    return findings


def gitleaks_worktree(root: Path, work: Path, report: Path,
                      files: int) -> tuple[list[Finding], str]:
    exe = tool_path("gitleaks")
    proc = run_tool([exe, "dir", str(work), "--no-banner", "--redact",
                     "--max-target-megabytes", "5", "--exit-code", "0",
                     "--report-format", "json", "--report-path", str(report),
                     "--log-level", "error"])
    if proc.returncode not in (0, 1):
        raise GateError(f"gitleaks dir failed: {proc.stderr.strip()[:200]}")
    data = read_json(report, "gitleaks dir") or []
    return gitleaks_findings("secrets/worktree", data, work), f"{files} files"


def gitleaks_history(root: Path, report: Path) -> tuple[list[Finding], str]:
    exe = tool_path("gitleaks")
    proc = run_tool([exe, "git", str(root), "--no-banner", "--redact",
                     "--exit-code", "0", "--report-format", "json",
                     "--report-path", str(report), "--log-level", "error"])
    if proc.returncode not in (0, 1):
        raise GateError(f"gitleaks git failed: {proc.stderr.strip()[:200]}")
    data = read_json(report, "gitleaks git") or []
    _, count = git(root, "rev-list", "--all", "--count")
    return (gitleaks_findings("secrets/history", data),
            f"{count.strip() or '?'} commits")


def value_leaks(root: Path, env_values: list[tuple[str, str]],
                files: list[str]) -> list[Finding]:
    """A verbatim `.env` value in a publishable file cannot be a generic rule."""
    findings = []
    for rel in files:
        try:
            data = (root / rel).read_bytes()
        except OSError:
            continue
        if b"\x00" in data[:4096]:
            continue
        text = data.decode("utf-8", "replace")
        for key, offset in value_hits(text, env_values):
            line = text.count("\n", 0, offset) + 1
            findings.append(Finding("secrets/worktree", "error",
                                    f"{rel}:{line}", f"value of .env key {key}"))
    return findings


def value_hits(text: str, env_values: list[tuple[str, str]]) -> list[tuple[str, int]]:
    found = []
    for key, value in env_values:
        start = 0
        while True:
            index = text.find(value, start)
            if index < 0:
                break
            found.append((key, index))
            start = index + len(value)
    return found


def env_values(root: Path) -> list[tuple[str, str]]:
    if root != ROOT:
        return []
    return [(key, value) for key, value in audit.read_env()
            if audit.classify(value) == "set" and len(value) >= 8]


def value_leaks_history(root: Path,
                        env_values: list[tuple[str, str]]) -> list[Finding]:
    """Check reachable and unreachable blobs for known values only."""
    findings = []
    _, objects = git(root, "rev-list", "--all", "--objects")
    paths: dict[str, str] = {}
    for line in objects.splitlines():
        sha, _, path = line.partition(" ")
        if path:
            paths.setdefault(sha, path)
    proc = subprocess.Popen(["git", "cat-file", "--batch-all-objects", "--batch"],
                            cwd=root, stdout=subprocess.PIPE)
    assert proc.stdout is not None
    try:
        while True:
            header = proc.stdout.readline()
            if not header:
                break
            sha, kind, size = header.split()
            body = proc.stdout.read(int(size))
            proc.stdout.read(1)
            if kind != b"blob" or not body or b"\x00" in body[:4096]:
                continue
            text = body.decode("utf-8", "replace")
            for key, offset in value_hits(text, env_values):
                where = paths.get(sha.decode(), "<unreachable>")
                findings.append(Finding(
                    "secrets/history", "error", f"{where} @ {sha.decode()[:10]}",
                    f"value of .env key {key}"))
    finally:
        proc.stdout.close()
        proc.wait()
    return findings


# ------------------------------------------------------------ vulnerabilities

RUFF_RULES = "S102,S301,S307,S501,S506,S602,S605,S608"


def project_relative(filename: str, root: Path = ROOT) -> str:
    try:
        return Path(filename).resolve().relative_to(root).as_posix()
    except (ValueError, OSError):
        return filename


def ruff_findings(data: list[dict], root: Path = ROOT) -> list[Finding]:
    return [Finding("vulnerabilities/python", "error",
                    f"{project_relative(str(item.get('filename')), root)}:"
                    f"{item.get('location', {}).get('row')}",
                    f"{item.get('code')} {item.get('message', '')[:90]}")
            for item in data]


def ruff_scan() -> tuple[list[Finding], str]:
    exe = tool_path("ruff")
    with tempfile.TemporaryDirectory(prefix="gate-", dir=ROOT / "scratch") as tmp:
        report = Path(tmp) / "ruff.json"
        proc = run_tool([exe, "check", "--no-cache", "--output-format", "json",
                         "--select", RUFF_RULES, "--output-file", str(report),
                         "cinemelb", "scripts"])
        if proc.returncode not in (0, 1):
            raise GateError(f"ruff failed: {proc.stderr.strip()[:200]}")
        data = read_json(report, "ruff")
    return ruff_findings(data or []), f"rules {RUFF_RULES}"


JS_SINKS = [
    ("eval() executes a string",
     re.compile(r"(?<![\w.])eval\s*\(")),
    ("new Function() compiles a string into code",
     re.compile(r"\bnew\s+Function\s*\(")),
    ("document.write() injects raw HTML",
     re.compile(r"\bdocument\.write\s*\(")),
]


def js_findings(text: str) -> list[tuple[str, int]]:
    return [(message, match.start())
            for message, rx in JS_SINKS
            for match in rx.finditer(text)]


def js_scan() -> tuple[list[Finding], str]:
    path = ROOT / "cinemelb" / "web" / "app.js"
    text = path.read_text(encoding="utf-8")
    findings = [Finding("vulnerabilities/js", "error",
                        f"cinemelb/web/app.js:{text.count(chr(10), 0, offset) + 1}",
                        message)
                for message, offset in js_findings(text)]
    return findings, "3 reviewed sinks"


def run_invariants() -> list[Finding]:
    static.failures.clear()
    static.check_sanitize()
    static.check_mapview()
    static.check_app_js()
    static.check_export()
    static.check_serve()
    return [Finding("vulnerabilities/invariants", "error", "check_security", item)
            for item in static.failures]


# --------------------------------------------------------------- supply chain

def direct_dependencies(root: Path) -> list[str]:
    try:
        data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return []
    names = list(data.get("project", {}).get("dependencies", []))
    for group in data.get("dependency-groups", {}).values():
        names.extend(item for item in group if isinstance(item, str))
    return names


def check_lock(root: Path) -> tuple[list[Finding], str]:
    lock_path = root / "uv.lock"
    if not lock_path.exists():
        if (root / "pyproject.toml").exists():
            return [Finding("supply-chain/lock", "error", "uv.lock",
                            "pyproject exists but the dependency graph is not locked")], "missing"
        return [], "no lock"
    try:
        lock = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return [Finding("supply-chain/lock", "error", "uv.lock",
                        f"cannot parse lock: {exc}")], "unreadable"
    packages = {str(item.get("name", "")).lower()
                for item in lock.get("package", []) if item.get("name")}
    findings = []
    if not packages:
        findings.append(Finding("supply-chain/lock", "error", "uv.lock",
                                "lock has no [[package]] entries"))
    missing = []
    for spec in direct_dependencies(root):
        name = re.split(r"[<>=!~\[ ;]", spec, maxsplit=1)[0].strip().lower()
        if name and name not in packages:
            missing.append(name)
    if missing:
        findings.append(Finding("supply-chain/lock", "error", "uv.lock",
                                "direct dependencies not locked: " + ", ".join(missing)))
    return findings, f"{len(packages)} packages, {len(direct_dependencies(root))} direct"


# ---------------------------------------------------------------------- bundle

def check_bundle(root: Path, env_values: list[tuple[str, str]]) -> list[Finding]:
    path = root / "dist" / "data" / "bundle.json"
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [Finding("bundle", "warn", "dist/data/bundle.json",
                        f"unreadable: {exc}")]
    findings = [Finding("bundle", "error", str(path.relative_to(root)),
                        f"value of .env key {key}")
                for key, _ in value_hits(text, env_values)]
    if SCRIPT_URL.search(text):
        findings.append(Finding("bundle", "error", str(path.relative_to(root)),
                                "script URL in the exported bundle"))
    return findings


def check_env_state(root: Path) -> list[Finding]:
    findings = []
    if root != ROOT:
        return findings
    env = root / ".env"
    if env.exists():
        ignored = subprocess.run(["git", "check-ignore", "-q", ".env"],
                                 cwd=root).returncode == 0
        tracked = subprocess.run(["git", "ls-files", "--error-unmatch", ".env"],
                                 cwd=root, capture_output=True).returncode == 0
        if not ignored:
            findings.append(Finding("git/env", "error", ".env",
                                    "present but not gitignored"))
        if tracked:
            findings.append(Finding("git/env", "error", ".env", "tracked by git"))
    _, remote = git(root, "remote", "-v")
    if re.search(r"https?://[^/\s]+@", remote):
        findings.append(Finding("git/env", "error", "git remote",
                                "remote URL embeds credentials"))
    return findings


# ------------------------------------------------------------------------ run

def run(root: Path = ROOT, *, history: bool = True,
        max_history_seconds: float = MAX_HISTORY_SECONDS
        ) -> tuple[list[Section], list[Finding]]:
    sections: list[Section] = []
    findings: list[Finding] = []
    known_values = env_values(root)
    files = publishable_files(root)

    started = time.monotonic()
    work, handle = snapshot(root, files)
    try:
        with tempfile.TemporaryDirectory(prefix="gate-", dir=root / "scratch") as tmp:
            secrets, detail = gitleaks_worktree(
                root, work, Path(tmp) / "worktree.json", len(files))
        secrets += value_leaks(root, known_values, files)
        findings += secrets
        sections.append(Section("secrets/worktree",
                                "fail" if secrets else "pass", detail,
                                time.monotonic() - started))
    finally:
        handle.cleanup()

    if history:
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="gate-", dir=root / "scratch") as tmp:
            hist, detail = gitleaks_history(root, Path(tmp) / "history.json")
        # Gitleaks cannot know the operator's local values; check them
        # verbatim, including unreachable blobs.
        hist += value_leaks_history(root, known_values)
        findings += hist
        elapsed = time.monotonic() - started
        status = "fail" if hist else "pass"
        if elapsed > max_history_seconds:
            status = "fail"
            detail += f"; budget {max_history_seconds:.0f}s exceeded"
            findings.append(Finding("secrets/history", "error", "budget",
                                    f"history scan took {elapsed:.1f}s"))
        sections.append(Section("secrets/history", status, detail, elapsed))

    started = time.monotonic()
    py, py_detail = ruff_scan()
    findings += py
    sections.append(Section("vulnerabilities/python",
                            "fail" if py else "pass", py_detail,
                            time.monotonic() - started))

    started = time.monotonic()
    js, js_detail = js_scan()
    findings += js
    sections.append(Section("vulnerabilities/js",
                            "fail" if js else "pass", js_detail,
                            time.monotonic() - started))

    started = time.monotonic()
    invariants = run_invariants()
    findings += invariants
    sections.append(Section("vulnerabilities/invariants",
                            "fail" if invariants else "pass", "",
                            time.monotonic() - started))

    started = time.monotonic()
    lock, lock_state = check_lock(root)
    findings += lock
    sections.append(Section("supply-chain/lock", "fail" if lock else "pass",
                            lock_state, time.monotonic() - started))

    started = time.monotonic()
    bundle = check_bundle(root, known_values)
    findings += bundle
    sections.append(Section("bundle", "fail" if bundle else "pass",
                            "dist/data/bundle.json"
                            if (root / "dist" / "data" / "bundle.json").exists()
                            else "no build",
                            time.monotonic() - started))

    env = check_env_state(root)
    findings += env
    sections.append(Section("git/env", "fail" if env else "pass", ""))
    return sections, findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-history", action="store_true",
                        help="skip the git object scan (faster, less thorough)")
    parser.add_argument("--max-history-seconds", type=float, default=MAX_HISTORY_SECONDS)
    parser.add_argument("--json", type=Path, help="write a machine-readable report")
    args = parser.parse_args()

    started = time.monotonic()
    try:
        sections, findings = run(history=not args.no_history,
                                 max_history_seconds=args.max_history_seconds)
    except GateError as exc:
        print(f"security gate could not complete: {exc}")
        return 2
    except Exception as exc:  # a broken scanner must never read as clean
        print(f"security gate could not complete: {type(exc).__name__}: {exc}")
        return 2

    errors = [item for item in findings if item.severity == "error"]
    warnings = [item for item in findings if item.severity == "warn"]
    elapsed = time.monotonic() - started

    print("security gate (deterministic, offline)")
    print("  secrets: gitleaks | python: ruff S-rules | js: reviewed sinks | invariants: project rules")
    for section in sections:
        tag = {"pass": "PASS", "fail": "FAIL", "warn": "WARN"}[section.status]
        detail = f"  {section.detail}" if section.detail else ""
        print(f"{tag:4} {section.name:26}{detail}")

    if findings:
        print(f"\n{len(errors)} error(s), {len(warnings)} warning(s)")
        for item in sorted(findings, key=lambda f: (f.severity != "error", f.section,
                                                    f.where, f.message)):
            print(f"  [{item.severity}] {item.section}: {item.where}: {item.message}")

    print(f"\nRESULT: {'FAIL' if errors else 'clean'} in {elapsed:.1f}s")

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps({
            "ok": not errors,
            "elapsed_seconds": round(elapsed, 3),
            "sections": [asdict(section) for section in sections],
            "findings": [asdict(item) for item in findings],
        }, indent=2), encoding="utf-8")

    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
