"""Keep each repository's git data in one shared store outside both trees.

Mutagen does not synchronize real .git directories. Externalizing a repository
renames its .git directory to <store>/<relative path>.git and leaves an absolute
symlink in its place; Mutagen copies that symlink to the other tree, so both
trees work on the same git data, coordinated by git's own locks.
"""
from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .config import Config, IgnoreList

QUIET_SECONDS = 120  # skip repositories whose git data changed more recently


class Layout:
    def __init__(self, mac: Path, linux: Path, store: Path, ignores: IgnoreList):
        self.mac, self.linux, self.store = Path(mac), Path(linux), Path(store)
        self.ignores = ignores

    @classmethod
    def from_config(cls, cfg: Config) -> "Layout":
        return cls(cfg.workspace, cfg.linux_workspace, cfg.git_store, cfg.ignores())

    def trees(self) -> List[Tuple[str, Path]]:
        return [("mac", self.mac), ("linux", self.linux)]

    def target(self, rel: str) -> Path:
        return self.store / (rel + ".git")

    def rel(self, argument: str) -> str:
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
            raise ValueError(f"invalid repository path: {argument}")
        return rel

    def check(self) -> None:
        if not self.mac.is_dir():
            raise ValueError(f"{self.mac} does not exist")
        for root in (self.mac, self.linux):
            if root == self.store or root in self.store.parents or self.store in root.parents:
                raise ValueError(f"the git store must be outside {root}")


def kind(path) -> Optional[str]:
    if os.path.islink(path):
        return "link"
    if os.path.isdir(path):
        return "dir"
    if os.path.exists(path):
        return "file"
    return None


def scan_tree(root: Path, ignores: IgnoreList) -> Dict[str, Optional[str]]:
    """Workspace-relative repository path -> kind of its .git entry."""
    found = {}
    if not root.is_dir():
        return found
    for directory, dirs, files in os.walk(root):
        rel = os.path.relpath(directory, root)
        rel = "" if rel == "." else rel.replace(os.sep, "/")
        if ".git" in dirs or ".git" in files:
            found[rel] = kind(os.path.join(directory, ".git"))
        dirs[:] = sorted(d for d in dirs if d != ".git"
                         and not ignores.matches(f"{rel}/{d}" if rel else d))
    found.pop("", None)  # the workspace root itself is never a repository
    return found


def scan(layout: Layout) -> Dict[str, Dict[str, Optional[str]]]:
    repos: Dict[str, Dict[str, Optional[str]]] = {}
    for side, root in layout.trees():
        for rel, entry in scan_tree(root, layout.ignores).items():
            repos.setdefault(rel, {"mac": None, "linux": None})[side] = entry
    return repos


def unsupported(gitdir, worktree) -> Optional[str]:
    """Why the repository must not be externalized, or None.

    Submodules resolve core.worktree against the git directory's real path, and
    linked worktrees record absolute paths that the other side cannot see (a
    `git gc` there would prune them). Both break once the data moves.
    """
    if (Path(worktree) / ".gitmodules").exists() or (Path(gitdir) / "modules").is_dir():
        return "submodules"
    worktrees = Path(gitdir) / "worktrees"
    if worktrees.is_dir() and any(worktrees.iterdir()):
        return "linked worktrees"
    return None


def busy(gitdir, now: Optional[float] = None) -> Optional[str]:
    """Why the git data may be in use right now, or None."""
    gitdir = Path(gitdir)
    now = now or time.time()
    # Only where git itself takes locks: tools keep their own long-lived files in
    # .git (Cursor leaves .git/cursor/crepe/build.lock).
    with os.scandir(gitdir) as entries:
        locks = [e.name for e in entries if e.name.endswith(".lock") and e.is_file(follow_symlinks=False)]
    if locks:
        return f"lock file {locks[0]}"
    for directory, _, files in os.walk(gitdir / "refs"):
        for name in files:
            if name.endswith(".lock"):
                return f"lock file {os.path.relpath(os.path.join(directory, name), gitdir)}"
    objects = gitdir / "objects"
    if objects.is_dir():
        if any(name.startswith("incoming-") for name in os.listdir(objects)):
            return "objects are being received"
        pack = objects / "pack"
        if pack.is_dir() and any(name.startswith(("tmp_pack_", "tmp_idx_")) for name in os.listdir(pack)):
            return "a pack transfer is in progress"
    markers = [gitdir, gitdir / "index", gitdir / "HEAD", gitdir / "packed-refs",
               gitdir / "ORIG_HEAD", gitdir / "logs" / "HEAD", objects,
               objects / "pack", gitdir / "refs" / "heads"]
    newest = max((p.stat().st_mtime for p in markers if p.exists()), default=0)
    if now - newest < QUIET_SECONDS:
        return f"git data changed {int(now - newest)}s ago"
    head = subprocess.run(["git", f"--git-dir={gitdir}", "rev-parse", "-q", "--verify", "HEAD"],
                          capture_output=True, text=True)
    if head.returncode != 0:
        return "no commit yet (possibly still cloning)"
    return None


def externalize(layout: Layout, rel: str, dry_run: bool = False) -> Tuple[bool, str]:
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
        return False, f"{rel}: skipped, has {reason} (not supported)"
    worktree = subprocess.run(["git", "config", "-f", str(gitdir / "config"), "core.worktree"],
                              capture_output=True, text=True)
    if worktree.stdout.strip():
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
    return True, f"{rel}: externalized from the {side} tree"


def restore(layout: Layout, rel: str, dry_run: bool = False) -> Tuple[bool, str]:
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
    return True, f"{rel}: restored"


def stored_gitdirs(store: Path) -> set:
    found = set()
    if not store.is_dir():
        return found
    for directory, dirs, _ in os.walk(store):
        if directory.endswith(".git") and Path(directory) != store:
            found.add(Path(directory))
            dirs[:] = []
    return found


@dataclass
class Report:
    counts: Dict[str, int] = field(default_factory=dict)
    externalized: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    anomalies: List[str] = field(default_factory=list)

    def add(self, key: str) -> None:
        self.counts[key] = self.counts.get(key, 0) + 1


def status(layout: Layout) -> Report:
    report = Report()
    repos = scan(layout)
    users: Dict[Path, set] = {}
    for rel in sorted(repos):
        entries = repos[rel]
        mac, linux = entries["mac"], entries["linux"]
        if "file" in (mac, linux) and "link" not in (mac, linux) and "dir" not in (mac, linux):
            report.add("gitfile (submodule or worktree checkout)")
            continue
        if "link" in (mac, linux):
            targets = {Path(os.readlink(root / rel / ".git"))
                       for side, root in layout.trees() if entries[side] == "link"}
            if len(targets) > 1:
                report.anomalies.append(f"{rel}: the two trees link to different git directories")
            if "dir" in (mac, linux):
                report.anomalies.append(f"{rel}: one tree has a link, the other a real .git directory")
            elif None in (mac, linux):
                missing = "linux" if linux is None else "mac"
                report.warnings.append(f"{rel}: link only in one tree; the {missing} tree has not synced it yet")
            for target in targets:
                users.setdefault(target, set()).add(rel)
                if not target.is_dir():
                    report.anomalies.append(f"{rel}: link target {target} does not exist")
                    continue
                if target != layout.target(rel):
                    report.anomalies.append(f"{rel}: link target {target} does not match the path; "
                                            f"expected {layout.target(rel)} (moved or copied?)")
                root = layout.mac if mac == "link" else layout.linux
                reason = unsupported(target, root / rel)
                if reason:
                    report.anomalies.append(f"{rel}: externalized but now has {reason}")
            report.add("externalized")
            report.externalized.append(rel)
            continue
        side = "both" if (mac == "dir" and linux == "dir") else ("mac" if mac == "dir" else "linux")
        root = layout.mac if mac == "dir" else layout.linux
        reason = unsupported(root / rel / ".git", root / rel)
        report.add(f"not externalized, {reason} (stays)" if reason else f"not externalized ({side} tree)")
        if side == "both":
            report.warnings.append(f"{rel}: both trees have their own .git directory")
    for target, rels in sorted(users.items()):
        if len(rels) > 1:
            report.anomalies.append(f"{target}: shared by several working trees: "
                                    f"{', '.join(sorted(rels))} (copied with cp -R?)")
    for orphan in sorted(stored_gitdirs(layout.store) - set(users)):
        report.anomalies.append(f"{orphan}: no working tree links to it (project deleted?)")
    return report


def externalize_all(layout: Layout, dry_run: bool = False) -> List[Tuple[str, bool, str]]:
    results = []
    for rel, entries in sorted(scan(layout).items()):
        if "dir" in entries.values():
            changed, message = externalize(layout, rel, dry_run)
            results.append((rel, changed, message))
    return results
