"""Deterministic cinemelb release gate.

Run after committing, from the repository root:

    uv run python scripts/release.py --dry-run   # every check, no release
    uv run python scripts/release.py             # checks -> push -> deploy -> live hash check

The script refuses a dirty working tree, runs the full T0-T3 ladder, rebuilds
``dist``, and only then pushes the current branch and deploys that tree to the
Cloudflare Pages project. A failed check stops before anything leaves the
machine. The deploy is pinned to a wrangler version so a release is repeatable.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ROOT / "scratch"
LIVE = "https://cinemelb.pages.dev/"
PROJECT = "cinemelb"
PAGES_BRANCH = "main"          # Cloudflare Pages production branch, not the git branch
WRANGLER_VERSION = "4.166.0"
COMMAND_TIMEOUT = 900
LIVE_TIMEOUT = 60
ASSET_RE = re.compile(r"assets/app\.[0-9a-f]+\.(?:css|js)")

DEPLOY_ROOT = SCRATCH / "wrangler-pages"
DEPLOY_LOG = SCRATCH / "wrangler-logs"
DEPLOY_CACHE = SCRATCH / "wrangler-cache"

PY = sys.executable
NODE = shutil.which("node")
WRANGLER = shutil.which("wrangler")


def deploy_env() -> dict[str, str]:
    env = os.environ.copy()
    env["WRANGLER_LOG_PATH"] = str(DEPLOY_LOG)
    env["WRANGLER_CACHE_DIR"] = str(DEPLOY_CACHE)
    env["XDG_CACHE_HOME"] = str(DEPLOY_CACHE)
    return env


def fail(step: str, detail: str = "") -> None:
    message = f"[fail] {step}"
    if detail:
        message += f": {detail}"
    print(message)
    raise SystemExit(1)


def run(name: str, command: list[str], cwd: Path = ROOT, *,
        soft: bool = False, env: dict[str, str] | None = None) -> int:
    print(f"\n== {name}")
    print("$ " + " ".join(str(part) for part in command))
    try:
        result = subprocess.run(command, cwd=cwd, timeout=COMMAND_TIMEOUT, env=env)
    except subprocess.TimeoutExpired:
        fail(name, f"timed out after {COMMAND_TIMEOUT}s")
    except OSError as exc:
        fail(name, str(exc))
    if result.returncode != 0:
        if soft:
            print(f"[warn] {name} exited {result.returncode}")
            return result.returncode
        fail(name, f"exit {result.returncode}")
    print(f"[ok] {name}")
    return 0


def output(command: list[str], cwd: Path = ROOT,
           env: dict[str, str] | None = None, label: str = "git") -> str:
    try:
        result = subprocess.run(command, cwd=cwd, text=True, capture_output=True,
                                timeout=120, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        fail(label, str(exc))
    if result.returncode != 0:
        fail(label, result.stderr.strip() or f"exit {result.returncode}")
    return result.stdout.strip()


def clean_tree() -> tuple[str, str]:
    status = output(["git", "status", "--porcelain", "--untracked-files=all"])
    if status:
        print("working tree is not clean:")
        print(status)
        fail("clean tree", "commit or stash before releasing")
    branch = output(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    if not branch or branch == "HEAD":
        fail("branch", "detached HEAD")
    head = output(["git", "rev-parse", "HEAD"])
    print(f"[ok] clean tree on {branch} at {head[:12]}")
    return branch, head


def publish_staging() -> bool:
    """Publish a fully built dist.building if the final rename was refused.

    Windows ACLs and some scanners intermittently refuse a non-empty directory
    rename with WinError 5. write_site has already written and validated the
    marker, so copying the staging tree is the same publish step without the
    atomic rename. A partial/failed build has no marker and is never published.
    """
    staging = ROOT / "dist.building"
    out = ROOT / "dist"
    if not (staging / "index.html").is_file() or not (staging / ".cinemelb-site").is_file():
        return False
    try:
        if out.exists():
            shutil.rmtree(out)
        shutil.copytree(staging, out)
    except OSError as exc:
        print(f"[warn] could not publish the staging tree: {exc}")
        return False
    shutil.rmtree(staging, ignore_errors=True)
    print("[warn] export rename was refused; copied the validated staging tree")
    return True


def build_dist() -> None:
    code = run("build dist", [PY, "-m", "cinemelb", "export", "--out", "dist"], soft=True)
    if code != 0 and not publish_staging():
        fail("build dist", f"exit {code}")
    if not (ROOT / "dist" / "index.html").is_file():
        fail("build dist", "dist/index.html is missing")
    print("[ok] dist is ready")


def wait_for_live(expected: set[str]) -> tuple[bool, str]:
    deadline = time.monotonic() + LIVE_TIMEOUT
    last = "no response"
    while time.monotonic() < deadline:
        try:
            request = urllib.request.Request(LIVE, headers={"User-Agent": "cinemelb-release"})
            with urllib.request.urlopen(request, timeout=15) as response:
                html = response.read().decode("utf-8", "replace")
            found = set(ASSET_RE.findall(html))
            if found == expected:
                return True, ""
            last = f"live {sorted(found)} != expected {sorted(expected)}"
        except Exception as exc:  # report the last network failure
            last = str(exc)
        time.sleep(2)
    return False, last


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="run every check but skip push, deploy and the live check")
    args = parser.parse_args()

    if not NODE:
        fail("node", "not found on PATH")
    if not WRANGLER:
        fail("wrangler", f"not found on PATH; install wrangler@{WRANGLER_VERSION}")

    branch, head = clean_tree()
    SCRATCH.mkdir(parents=True, exist_ok=True)
    DEPLOY_ROOT.mkdir(parents=True, exist_ok=True)
    DEPLOY_LOG.mkdir(parents=True, exist_ok=True)
    DEPLOY_CACHE.mkdir(parents=True, exist_ok=True)
    (DEPLOY_ROOT / "package.json").write_text("{}\n", encoding="utf-8")

    version_text = output([WRANGLER, "--version"], cwd=DEPLOY_ROOT,
                          env=deploy_env(), label="wrangler")
    version = re.search(r"(\d+\.\d+\.\d+)", version_text)
    if not version or version.group(1) != WRANGLER_VERSION:
        fail("wrangler", f"expected {WRANGLER_VERSION}, found {version_text or 'nothing'}")
    print(f"[ok] wrangler {WRANGLER_VERSION}")

    for name, command in (
        ("syntax", [NODE, "--check", "cinemelb/web/app.js"]),
        ("tokens", [PY, "scripts/check_tokens.py", "--coverage", "--literals", "--motion"]),
        ("contrast", [PY, "scripts/check_contrast.py"]),
        ("manifest", [PY, "scripts/check_manifest.py"]),
    ):
        run(name, command)

    build_dist()

    for name, command in (
        ("bundle", [PY, "scripts/check_bundle.py", "dist/data/bundle.json",
                    "--titles", "--against-db"]),
        ("budget", [PY, "scripts/check_budget.py", "dist"]),
        ("security", [PY, "scripts/security_gate.py"]),
        ("unit tests", [PY, "-m", "unittest", "discover", "-s", "tests"]),
    ):
        run(name, command)

    # Chromium writes debug.log into its working directory; scratch/ is ignored.
    for name in ("layout", "states", "search", "film", "alias"):
        run(f"ui {name}", [PY, str(ROOT / "tests" / "ui" / f"{name}.py")], cwd=SCRATCH)

    if args.dry_run:
        print("\n[dry-run] all checks passed; push and deploy skipped")
        return 0

    run("push", ["git", "push", "origin", branch])
    remote = output(["git", "ls-remote", "origin", f"refs/heads/{branch}"])
    remote_head = remote.split()[0] if remote else ""
    if remote_head != head:
        fail("push", f"origin/{branch} is {remote_head[:12] or 'missing'}, expected {head[:12]}")
    print(f"[ok] origin/{branch} is {head[:12]}")

    # Wrangler writes logs, its Pages temp dir and its cache under the user's
    # home by default; this sandbox denies those writes to Python children.
    # scratch/ is writable, so the deploy runs from a scratch package root with
    # log/cache overrides. One bounded retry separates a transient lock from a
    # real deploy failure.
    deploy = [WRANGLER, "pages", "deploy", str(ROOT / "dist"),
              "--project-name", PROJECT, "--branch", PAGES_BRANCH]
    env = deploy_env()
    code = run("deploy", deploy, cwd=DEPLOY_ROOT, env=env, soft=True)
    if code != 0:
        print("[warn] deploy failed; retrying once")
        run("deploy retry", deploy, cwd=DEPLOY_ROOT, env=env)

    expected = set(ASSET_RE.findall((ROOT / "dist" / "index.html").read_text(encoding="utf-8")))
    if len(expected) < 2:
        fail("live check", "dist/index.html has no hashed assets")
    print(f"\nexpected live assets: {', '.join(sorted(expected))}")
    ok, detail = wait_for_live(expected)
    if not ok:
        fail("live check", detail)
    print("[ok] live asset hashes match")
    print(f"\nreleased {head[:12]} to {LIVE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
