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
  python3 scripts/workspace-sync.py compare [--content]
                    # compare both trees outside the ignore list (flushes the session first)
"""
import argparse
import filecmp
import os
import stat
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
    # Live state of other containers (writable bind mounts), found by check-sync-ignores.py.
    "/github_work/shopify-lens/data/shopify_lens_pg_data/",
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
    """Return (exists, healthy, conflicts, problems, raw)."""
    # Do not let the query start a stopped daemon: a stopped daemon must be visible.
    env = dict(os.environ, MUTAGEN_DISABLE_AUTOSTART="1")
    result = subprocess.run([mutagen(), "sync", "list", "--long", name],
                            capture_output=True, text=True, env=env)
    raw = result.stdout + result.stderr
    if result.returncode != 0:
        return False, False, 0, 0, raw
    lines = raw.splitlines()
    conflicts = sum(1 for line in lines if line.startswith("\t(alpha) "))  # one per conflict
    problems = sum(1 for line in lines if line.strip().lower().endswith("problems:"))
    connected = raw.count("Connected: Yes") == 2
    status = next((line for line in lines if line.startswith("Status:")), "")
    # Scanning, staging and reconciling are normal while files change; these are not.
    bad = ("Paused", "Halted", "Connecting", "Disconnected", "rescan")
    healthy = connected and bool(status) and not any(word in status for word in bad)
    return True, healthy, conflicts, problems, raw


def snapshot(root):
    """Map relative path -> (kind, size, symlink target, executable) outside the ignore list.

    Real .git directories are skipped (never synchronized); .git symlinks are compared.
    """
    entries = {}
    for directory, dirs, files in os.walk(root):
        rel_dir = os.path.relpath(directory, root)
        rel_dir = "" if rel_dir == "." else rel_dir.replace(os.sep, "/")
        kept = []
        for name in dirs:
            rel = f"{rel_dir}/{name}" if rel_dir else name
            path = os.path.join(directory, name)
            if is_ignored(rel):
                continue
            if os.path.islink(path):
                entries[rel] = ("link", 0, os.readlink(path), False)
            elif name == ".git":
                continue
            else:
                entries[rel] = ("dir", 0, None, False)
                kept.append(name)
        dirs[:] = kept
        for name in files:
            rel = f"{rel_dir}/{name}" if rel_dir else name
            if is_ignored(rel):
                continue
            path = os.path.join(directory, name)
            info = os.lstat(path)
            if stat.S_ISLNK(info.st_mode):
                entries[rel] = ("link", 0, os.readlink(path), False)
            else:
                entries[rel] = ("file", info.st_size, None, bool(info.st_mode & stat.S_IXUSR))
    return entries


def compare(alpha, beta, content=False):
    """Return a list of human-readable differences between the two trees."""
    a, b = snapshot(alpha), snapshot(beta)
    differences = [f"only in {alpha}: {rel}" for rel in sorted(a.keys() - b.keys())]
    differences += [f"only in {beta}: {rel}" for rel in sorted(b.keys() - a.keys())]
    for rel in sorted(a.keys() & b.keys()):
        if a[rel] != b[rel]:
            differences.append(f"differs: {rel}: {a[rel]} != {b[rel]}")
        elif content and a[rel][0] == "file" and not filecmp.cmp(
                os.path.join(alpha, rel), os.path.join(beta, rel), shallow=False):
            differences.append(f"content differs: {rel}")
    return differences


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("create")
    sub.add_parser("status")
    sub.add_parser("ignores")
    compare_parser = sub.add_parser("compare")
    compare_parser.add_argument("--content", action="store_true", help="also compare file contents")
    args = parser.parse_args()
    if args.command == "compare":
        subprocess.run([mutagen(), "sync", "flush", SESSION], check=False, capture_output=True)
        differences = compare(str(WORKSPACE), str(LINUX_WORKSPACE), args.content)
        for line in differences[:200]:
            print(line)
        if len(differences) > 200:
            print(f"... and {len(differences) - 200} more")
        print(f"{len(differences)} difference(s)" if differences else "Trees match outside the ignore list.")
        return 1 if differences else 0
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
        if "daemon" in raw.lower():
            print("NOT RUNNING: the Mutagen daemon is not running. Start it with 'mutagen daemon start'.")
        else:
            print(f"Session {SESSION!r} does not exist. Create it with 'workspace-sync.py create'.")
        return 1
    print(raw.strip())
    if not healthy:
        print(f"\nNOT HEALTHY: session {SESSION!r} is paused, halted or disconnected.")
        return 1
    if conflicts or problems:
        print(f"\nATTENTION: {conflicts} conflict(s), {problems} problem section(s).")
        return 1
    print(f"\nOK: session {SESSION!r} is running without conflicts.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
