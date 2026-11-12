#!/usr/bin/env python3
"""Define and inspect the Mutagen session that mirrors the Mac workspace for Linux.

Runs on the Mac. One two-way session keeps the source in /Users/zeke/workspace
and /Users/zeke/.cc-runtime/linux-workspace in sync. Platform-specific
dependency directories are ignored by name, so each side keeps its own.
Real .git directories are not synchronized (--ignore-vcs); a .git symlink into
the shared git store is. See docs/architecture/dependency-isolation.md.

Usage:
  python3 scripts/workspace-sync.py create     # create the session (refuses if it exists)
  python3 scripts/workspace-sync.py status     # exit 1 unless the session runs without conflicts
  python3 scripts/workspace-sync.py ignores    # print the ignore list, one entry per line
"""
import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

SESSION = "linux-workspace"
WORKSPACE = Path("/Users/zeke/workspace")
LINUX_WORKSPACE = Path("/Users/zeke/.cc-runtime/linux-workspace")
GIT_STORE = Path("/Users/zeke/.cc-runtime/git")

# Anchored to the sync root (leading "/"): runtime state of this deployment and
# large tool caches that are not source.
ANCHORED_IGNORES = [
    "/github_work/cc-runtime/dev-runtimes/",
    "/github_work/cc-runtime/tailscale/state/",
    # Machine directories of the old layout, in use until the migration ends (rollout stage 4).
    "/github_work/cc-runtime/dev-runtime/",
    "/github_work/cc-runtime/dev-california-runtime/",
    "/rust_work/.cache/",
    "/.pnpm-store/",
    # Virtual environments with non-standard names, found by check-sync-ignores.py.
    "/github_work/Confucius4-R2T2/.vnev/",
]
# Matched by name at any depth: platform-specific dependencies, build output and caches.
NAME_IGNORES = [
    "node_modules", ".venv", ".venv-mac", "target", ".next", ".nuxt", ".output", ".svelte-kit",
    ".turbo", ".parcel-cache", ".gradle", ".dart_tool", ".angular", ".tox", ".nox",
    "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache",
]
IGNORES = ANCHORED_IGNORES + NAME_IGNORES


def mutagen():
    for candidate in (shutil.which("mutagen"), "/opt/homebrew/bin/mutagen", "/usr/local/bin/mutagen"):
        if candidate and os.access(candidate, os.X_OK):
            return candidate
    raise SystemExit("Error: mutagen is not installed or not on PATH")


def is_ignored(rel):
    """Whether a workspace-relative POSIX path falls under the ignore list."""
    parts = [p for p in rel.strip("/").split("/") if p]
    if any(p in NAME_IGNORES for p in parts):
        return True
    path = "/" + "/".join(parts) + "/"
    return any(path.startswith(entry) for entry in ANCHORED_IGNORES)


def create_command(alpha=WORKSPACE, beta=LINUX_WORKSPACE, name=SESSION, binary="mutagen"):
    command = [binary, "sync", "create", f"--name={name}",
               "--symlink-mode=posix-raw",
               "--default-file-mode=0644", "--default-directory-mode=0755",
               "--ignore-vcs"]
    command += [f"--ignore={entry}" for entry in IGNORES]
    return command + [str(alpha), str(beta)]


def session_state(name=SESSION):
    """Return (exists, connected_and_watching, conflicts, problems, raw)."""
    result = subprocess.run([mutagen(), "sync", "list", "--long", name],
                            capture_output=True, text=True)
    raw = result.stdout + result.stderr
    if result.returncode != 0:
        return False, False, 0, 0, raw
    lines = raw.splitlines()
    conflicts = sum(1 for line in lines if line.startswith("\t(alpha) "))  # one per conflict
    problems = sum(1 for line in lines if line.strip().lower().endswith("problems:"))
    connected = raw.count("Connected: Yes") == 2
    status = next((line for line in lines if line.startswith("Status:")), "")
    healthy = connected and status == "Status: Watching for changes"
    return True, healthy, conflicts, problems, raw


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("create")
    sub.add_parser("status")
    sub.add_parser("ignores")
    args = parser.parse_args()
    if args.command == "ignores":
        print("\n".join(IGNORES))
        return 0
    if args.command == "create":
        if session_state()[0]:
            raise SystemExit(f"Error: session {SESSION!r} already exists")
        for path in (WORKSPACE, LINUX_WORKSPACE, GIT_STORE):
            if not path.is_dir():
                raise SystemExit(f"Error: {path} does not exist")
        subprocess.run(create_command(binary=mutagen()), check=True)
        return 0
    exists, healthy, conflicts, problems, raw = session_state()
    if not exists:
        print(f"Session {SESSION!r} does not exist (or the Mutagen daemon is not running).")
        return 1
    print(raw.strip())
    if not healthy:
        print(f"\nNOT HEALTHY: session {SESSION!r} is not connected and watching.")
        return 1
    if conflicts or problems:
        print(f"\nATTENTION: {conflicts} conflict(s), {problems} problem section(s).")
        return 1
    print(f"\nOK: session {SESSION!r} is running without conflicts.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
