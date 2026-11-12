#!/usr/bin/env python3
"""Check the workspace sync ignore list against the real workspace.

Runs on the Mac. Four checks (see docs/architecture/dependency-isolation.md):
  1. The two sync endpoints and the git store do not contain each other, and the
     ignore list keeps this deployment's per-machine state out of the mirror.
  2. No file tracked by git falls under the ignore list (that would be source
     that never reaches the other side).
  3. Warn about Python virtual environments (pyvenv.cfg) outside ignored
     directories: non-standard names such as .venv-mac would be synchronized.
  4. Every path inside the workspace that a container mounts writable (a
     database directory, a service's state) is ignored. Syncing live state
     would copy it mid-write, and a change on the Linux side would flow back
     into it. Mounts of the whole workspace (the old layout) are not counted.
Exit status 1 if check 1, 2 or 4 fails; warnings alone do not fail.

Usage: python3 scripts/check-sync-ignores.py [--workspace DIR] [--linux DIR] [--store DIR]
"""
import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

_here = Path(__file__).resolve()


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, _here.with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sync = _load("workspace_sync", "workspace-sync.py")
store = _load("git_store", "git-store.py")


def contains(a, b):
    return a == b or a in b.parents


def check_layout(workspace, linux, git_store):
    errors = []
    for x, y in ((workspace, linux), (workspace, git_store), (linux, git_store)):
        if contains(x, y) or contains(y, x):
            errors.append(f"{x} and {y} must not contain each other")
    if "/github_work/cc-runtime/dev-runtimes/" not in sync.IGNORES:
        errors.append("the ignore list must exclude /github_work/cc-runtime/dev-runtimes/")
    return errors


def tracked_violations(workspace):
    """Tracked files under ignored paths, per repository in the workspace."""
    found = []
    for rel, entry in sorted(store.scan_tree(workspace).items()):
        if entry not in ("dir", "link"):
            continue  # submodule checkouts are covered by their superproject's listing
        result = subprocess.run(["git", "-C", str(workspace / rel), "-c", "core.quotepath=off",
                                 "ls-files", "-z"], capture_output=True)
        if result.returncode != 0:
            found.append((rel, f"git ls-files failed: {result.stderr.decode(errors='replace').strip()}"))
            continue
        for name in result.stdout.decode(errors="surrogateescape").split("\0"):
            if name and sync.is_ignored(f"{rel}/{name}"):
                found.append((rel, name))
    return found


def stray_virtualenvs(workspace):
    found = []
    for directory, dirs, files in os.walk(workspace):
        rel = os.path.relpath(directory, workspace)
        rel = "" if rel == "." else rel
        dirs[:] = [d for d in dirs if d != ".git" and not sync.is_ignored(f"{rel}/{d}" if rel else d)]
        if "pyvenv.cfg" in files:
            found.append(rel)
            dirs[:] = []
    return found


def container_state_mounts(workspace, inspect=None):
    """Writable bind mounts of containers that point inside the workspace and are not ignored."""
    if inspect is None:
        def inspect():
            docker = shutil.which("docker") or "/usr/local/bin/docker"
            ids = subprocess.run([docker, "ps", "-aq"], capture_output=True, text=True, check=True).stdout.split()
            if not ids:
                return []
            return json.loads(subprocess.run([docker, "inspect", *ids], capture_output=True,
                                             text=True, check=True).stdout)
    found = []
    for container in inspect():
        for mount in container.get("Mounts", []):
            if mount.get("Type") != "bind" or not mount.get("RW"):
                continue
            source = Path(mount["Source"])
            if source == workspace or workspace not in source.parents:
                continue
            rel = source.relative_to(workspace).as_posix()
            if not sync.is_ignored(rel + "/"):
                found.append((container.get("Name", "?").lstrip("/"), rel))
    return sorted(set(found))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--workspace", default=str(sync.WORKSPACE))
    parser.add_argument("--linux", default=str(sync.LINUX_WORKSPACE))
    parser.add_argument("--store", default=str(sync.GIT_STORE))
    args = parser.parse_args(argv)
    workspace = Path(args.workspace)
    failed = False
    errors = check_layout(workspace, Path(args.linux), Path(args.store))
    for error in errors:
        print(f"ERROR  layout: {error}")
    failed |= bool(errors)
    violations = tracked_violations(workspace)
    for rel, name in violations:
        print(f"ERROR  tracked file is ignored: {rel}/{name}")
    failed |= bool(violations)
    try:
        mounts = container_state_mounts(workspace)
    except (OSError, subprocess.CalledProcessError, ValueError) as error:
        mounts = []
        print(f"WARN   could not list container mounts: {error}")
    for name, rel in mounts:
        print(f"ERROR  container {name} mounts {rel} writable; add an ignore entry")
    failed |= bool(mounts)
    for rel in stray_virtualenvs(workspace):
        print(f"WARN   virtual environment will be synchronized: {rel} (rename it, or add an ignore entry)")
    print("FAILED" if failed else "OK")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
