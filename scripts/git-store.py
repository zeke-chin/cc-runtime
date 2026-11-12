#!/usr/bin/env python3
"""Move each repository's git data out of the synced trees into one shared store.

Runs on the Mac. The Mac workspace and its Linux mirror are kept in sync by
Mutagen, which does not copy real .git directories. Externalizing a repository
renames its .git directory to <store>/<relative path>.git and leaves an absolute
symlink in its place; Mutagen copies that symlink to the other tree, so both
trees use the same git data. See docs/architecture/dependency-isolation.md.

Usage:
  python3 scripts/git-store.py status
  python3 scripts/git-store.py externalize <repo>... | --all   [--dry-run]
  python3 scripts/git-store.py restore <repo>... | --all       [--dry-run]

<repo> is a path relative to the workspace root, or an absolute path inside
either tree. Paths can be overridden for tests with --workspace, --linux and
--store (or CC_WORKSPACE, CC_LINUX_WORKSPACE, CC_GIT_STORE).
"""
import argparse
import importlib.util
import os
import subprocess
import sys
import time
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "workspace_sync", Path(__file__).resolve().with_name("workspace-sync.py"))
sync = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sync)

QUIET_SECONDS = 120  # skip repositories whose git data changed more recently


class Layout:
    def __init__(self, workspace, linux, store):
        self.mac = Path(workspace)
        self.linux = Path(linux)
        self.store = Path(store)

    def trees(self):
        return (("mac", self.mac), ("linux", self.linux))

    def target(self, rel):
        return self.store / (rel + ".git")

    def rel(self, argument):
        path = Path(argument)
        if not path.is_absolute():
            rel = path.as_posix().strip("/")
        else:
            for _, root in self.trees():
                try:
                    rel = path.relative_to(root).as_posix()
                    break
                except ValueError:
                    continue
            else:
                raise ValueError(f"{argument} is not inside {self.mac} or {self.linux}")
        if rel in ("", ".") or ".." in rel.split("/"):
            raise ValueError(f"Invalid repository path: {argument}")
        return rel


def kind(path):
    if os.path.islink(path):
        return "link"
    if os.path.isdir(path):
        return "dir"
    if os.path.exists(path):
        return "file"
    return None


def scan_tree(root):
    """Map workspace-relative repository path -> kind of its .git entry."""
    found = {}
    if not root.is_dir():
        return found
    for directory, dirs, files in os.walk(root):
        rel = os.path.relpath(directory, root)
        rel = "" if rel == "." else rel.replace(os.sep, "/")
        if rel and sync.is_ignored(rel):
            dirs[:] = []
            continue
        if ".git" in dirs or ".git" in files:
            found[rel] = kind(os.path.join(directory, ".git"))
        dirs[:] = sorted(d for d in dirs if d != ".git" and not sync.is_ignored(
            (rel + "/" + d) if rel else d))
    found.pop("", None)  # the workspace root itself is never a repository
    return found


def scan(layout):
    repos = {}
    for side, root in layout.trees():
        for rel, entry in scan_tree(root).items():
            repos.setdefault(rel, {"mac": None, "linux": None})[side] = entry
    return repos


def unsupported(gitdir, worktree):
    """Reason the repository must not be externalized, or None."""
    if (Path(worktree) / ".gitmodules").exists() or (Path(gitdir) / "modules").is_dir():
        return "submodules"
    worktrees = Path(gitdir) / "worktrees"
    if worktrees.is_dir() and any(worktrees.iterdir()):
        return "linked worktrees"
    return None


def busy(gitdir, now=None):
    """Reason the git data may be in use right now, or None."""
    gitdir = Path(gitdir)
    now = now or time.time()
    for directory, dirs, files in os.walk(gitdir):
        if Path(directory) == gitdir / "objects":
            # Loose objects are many; only look at in-flight transfers.
            dirs[:] = [d for d in dirs if d == "pack" or d.startswith("incoming-")]
        for name in files:
            if name.endswith(".lock"):
                return f"lock file {os.path.relpath(os.path.join(directory, name), gitdir)}"
            if name.startswith("tmp_pack_") or name.startswith("tmp_idx_"):
                return "a pack transfer is in progress"
        for name in dirs:
            if name.startswith("incoming-"):
                return "objects are being received"
    markers = [gitdir, gitdir / "index", gitdir / "HEAD", gitdir / "packed-refs",
               gitdir / "ORIG_HEAD", gitdir / "logs" / "HEAD", gitdir / "objects",
               gitdir / "objects" / "pack", gitdir / "refs" / "heads"]
    newest = max((p.stat().st_mtime for p in markers if p.exists()), default=0)
    if now - newest < QUIET_SECONDS:
        return f"git data changed {int(now - newest)}s ago"
    head = subprocess.run(["git", f"--git-dir={gitdir}", "rev-parse", "-q", "--verify", "HEAD"],
                          capture_output=True, text=True)
    if head.returncode != 0:
        return "no commit yet (possibly still cloning)"
    return None


def externalize(layout, rel, dry_run=False):
    """Return (changed, message)."""
    target = layout.target(rel)
    entries = {side: kind(root / rel / ".git") for side, root in layout.trees()}
    real = [side for side, entry in entries.items() if entry == "dir"]
    if not real:
        if "link" in entries.values():
            return False, f"{rel}: already externalized"
        return False, f"{rel}: no .git directory"
    if len(real) == 2:
        return False, f"{rel}: refused, both trees have their own .git directory"
    side = real[0]
    other = "linux" if side == "mac" else "mac"
    if entries[other] is not None:
        return False, f"{rel}: refused, the {other} tree has a .git {entries[other]}"
    root = dict(layout.trees())[side]
    gitdir = root / rel / ".git"
    reason = unsupported(gitdir, root / rel)
    if reason:
        return False, f"{rel}: skipped, has {reason} (not supported, see the design document)"
    worktree_config = subprocess.run(["git", "config", "-f", str(gitdir / "config"), "core.worktree"],
                                     capture_output=True, text=True)
    if worktree_config.stdout.strip():
        return False, f"{rel}: skipped, core.worktree is set"
    reason = busy(gitdir)
    if reason:
        return False, f"{rel}: skipped, {reason}"
    if os.path.lexists(target):
        return False, f"{rel}: refused, {target} already exists"
    if dry_run:
        return True, f"{rel}: would move {gitdir} -> {target}"
    target.parent.mkdir(parents=True, exist_ok=True)
    os.rename(gitdir, target)
    try:
        os.symlink(str(target), str(gitdir))
    except OSError:
        os.rename(target, gitdir)
        raise
    return True, f"{rel}: externalized from the {side} tree -> {target}"


def restore(layout, rel, dry_run=False):
    link = layout.mac / rel / ".git"
    if kind(link) != "link":
        return False, f"{rel}: not externalized in the mac tree"
    target = Path(os.readlink(link))
    if not target.is_absolute() or layout.store not in target.parents or not target.is_dir():
        return False, f"{rel}: refused, link target {target} is not a directory in {layout.store}"
    reason = busy(target)
    if reason:
        return False, f"{rel}: skipped, {reason}"
    if dry_run:
        return True, f"{rel}: would move {target} -> {link}"
    other = layout.linux / rel / ".git"
    os.unlink(link)
    try:
        os.rename(target, link)
    except OSError:
        os.symlink(str(target), str(link))
        raise
    # The sync would delete it as well; doing it here does not depend on the session.
    if kind(other) == "link" and Path(os.readlink(other)) == target:
        os.unlink(other)
    parent = target.parent
    while parent != layout.store and parent.is_dir() and not any(parent.iterdir()):
        parent.rmdir()
        parent = parent.parent
    return True, f"{rel}: restored to {link}"


def stored_gitdirs(store):
    found = set()
    if not store.is_dir():
        return found
    for directory, dirs, _ in os.walk(store):
        if directory.endswith(".git") and Path(directory) != store:
            found.add(Path(directory))
            dirs[:] = []
    return found


def status(layout, out=print):
    """Print the state of every repository; return the number of anomalies."""
    repos = scan(layout)
    anomalies, warnings, counts = [], [], {}
    users = {}
    for rel in sorted(repos):
        entries = repos[rel]
        mac, linux = entries["mac"], entries["linux"]
        if "file" in (mac, linux) and "link" not in (mac, linux) and "dir" not in (mac, linux):
            counts["gitfile (submodule or worktree checkout)"] = counts.get(
                "gitfile (submodule or worktree checkout)", 0) + 1
            continue
        if "link" in (mac, linux):
            targets = {Path(os.readlink(layout_root / rel / ".git"))
                       for side, layout_root in layout.trees() if entries[side] == "link"}
            if len(targets) > 1:
                anomalies.append(f"{rel}: the two trees link to different git directories")
            if "dir" in (mac, linux):
                anomalies.append(f"{rel}: one tree has a link, the other a real .git directory")
            elif None in (mac, linux):
                missing = "linux" if linux is None else "mac"
                warnings.append(f"{rel}: link only in one tree; the {missing} tree has not synced it yet")
            for target in targets:
                users.setdefault(target, set()).add(rel)
                if not target.is_dir():
                    anomalies.append(f"{rel}: link target {target} does not exist")
                    continue
                if target != layout.target(rel):
                    anomalies.append(f"{rel}: link target {target} does not match the path; "
                                     f"expected {layout.target(rel)} (moved or copied?)")
                root = layout.mac if mac == "link" else layout.linux
                reason = unsupported(target, root / rel)
                if reason:
                    anomalies.append(f"{rel}: externalized but now has {reason}")
            counts["externalized"] = counts.get("externalized", 0) + 1
            continue
        side = "both" if (mac == "dir" and linux == "dir") else ("mac" if mac == "dir" else "linux")
        root = layout.mac if mac == "dir" else layout.linux
        reason = unsupported(root / rel / ".git", root / rel)
        key = f"not externalized, {reason} (stays)" if reason else f"not externalized ({side} tree)"
        counts[key] = counts.get(key, 0) + 1
        if side == "both":
            warnings.append(f"{rel}: both trees have their own .git directory")
    for target, rels in sorted(users.items()):
        if len(rels) > 1:
            anomalies.append(f"{target}: shared by several working trees: {', '.join(sorted(rels))} "
                             f"(copied with cp -R?)")
    for orphan in sorted(stored_gitdirs(layout.store) - set(users)):
        anomalies.append(f"{orphan}: no working tree links to it (project deleted?)")
    for key in sorted(counts):
        out(f"{counts[key]:6d}  {key}")
    for line in warnings:
        out(f"WARNING  {line}")
    for line in anomalies:
        out(f"ANOMALY  {line}")
    if not anomalies:
        out("No anomalies.")
    return len(anomalies)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--workspace", default=os.environ.get("CC_WORKSPACE", str(sync.WORKSPACE)))
    parser.add_argument("--linux", default=os.environ.get("CC_LINUX_WORKSPACE", str(sync.LINUX_WORKSPACE)))
    parser.add_argument("--store", default=os.environ.get("CC_GIT_STORE", str(sync.GIT_STORE)))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    for name in ("externalize", "restore"):
        command = sub.add_parser(name)
        command.add_argument("repos", nargs="*")
        command.add_argument("--all", action="store_true")
        command.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    layout = Layout(args.workspace, args.linux, args.store)
    for path in (layout.mac, layout.store):
        if not path.is_dir():
            raise SystemExit(f"Error: {path} does not exist")
    for root in (layout.mac, layout.linux):
        if root == layout.store or root in layout.store.parents or layout.store in root.parents:
            raise SystemExit(f"Error: the git store must be outside {root}")
    if args.command == "status":
        return 1 if status(layout) else 0
    if args.all == bool(args.repos):
        parser.error("give repositories or --all, not both")
    if args.all:
        repos = scan(layout)
        if args.command == "externalize":
            rels = [r for r, e in repos.items() if "dir" in e.values()]
        else:
            rels = [r for r, e in repos.items() if e["mac"] == "link"]
    else:
        rels = [layout.rel(r) for r in args.repos]
    action = externalize if args.command == "externalize" else restore
    failed = 0
    for rel in sorted(rels):
        changed, message = action(layout, rel, args.dry_run)
        print(message)
        # With --all, skips (busy, unsupported) are expected; explicit requests must succeed.
        if not changed and not args.all and not message.endswith("already externalized"):
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)
