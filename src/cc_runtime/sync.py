"""The Mutagen session that mirrors the Mac workspace into the Linux workspace."""
from __future__ import annotations

import filecmp
import os
import stat
from dataclasses import dataclass, field
from typing import Dict, List, Tuple

from .config import SESSION, Config, IgnoreList
from .util import CommandError, require, run

# Scanning, staging and reconciling are normal while files change; these are not.
UNHEALTHY_WORDS = ("Paused", "Halted", "Connecting", "Disconnected", "rescan")


def create_command(cfg: Config, *, alpha=None, beta=None, name: str = SESSION,
                   binary: str = "mutagen") -> List[str]:
    command = [binary, "sync", "create", f"--name={name}",
               "--symlink-mode=posix-raw",
               "--default-file-mode=0644", "--default-directory-mode=0755",
               "--ignore-vcs"]
    command += [f"--ignore={entry}" for entry in cfg.ignores().entries]
    return command + [str(alpha or cfg.workspace), str(beta or cfg.linux_workspace)]


@dataclass
class SessionState:
    exists: bool
    daemon_running: bool = True
    status: str = ""
    connected: bool = False
    conflicts: int = 0
    problems: int = 0
    ignores: List[str] = field(default_factory=list)
    raw: str = ""

    @property
    def healthy(self) -> bool:
        return (self.exists and self.connected and bool(self.status)
                and not any(word in self.status for word in UNHEALTHY_WORDS))

    @property
    def ok(self) -> bool:
        return self.healthy and not self.conflicts and not self.problems


def parse_session(raw: str) -> SessionState:
    lines = raw.splitlines()
    state = SessionState(exists=True, raw=raw)
    state.conflicts = sum(1 for line in lines if line.startswith("\t(alpha) "))
    state.problems = sum(1 for line in lines if line.strip().lower().endswith("problems:"))
    state.connected = raw.count("Connected: Yes") == 2
    status = next((line for line in lines if line.startswith("Status:")), "")
    state.status = status[len("Status:"):].strip()
    collecting = False
    for line in lines:
        if line.strip() == "Ignores:":
            collecting = True
            continue
        if collecting:
            if line.startswith("\t\t"):
                state.ignores.append(line.strip())
            else:
                collecting = False
    return state


def session_state(name: str = SESSION) -> SessionState:
    # Never let a status query start a stopped daemon: a stopped daemon must be visible.
    env = dict(os.environ, MUTAGEN_DISABLE_AUTOSTART="1")
    result = run([require("mutagen"), "sync", "list", "--long", name],
                 capture=True, check=False, env=env)
    raw = (result.stdout or "") + (result.stderr or "")
    if result.returncode != 0:
        return SessionState(exists=False, daemon_running="daemon" not in raw.lower(), raw=raw)
    return parse_session(raw)


def ignore_drift(cfg: Config, state: SessionState) -> Tuple[List[str], List[str]]:
    """(entries only in the configuration, entries only in the running session)."""
    wanted = set(cfg.ignores().entries)
    actual = set(state.ignores)
    return sorted(wanted - actual), sorted(actual - wanted)


def create(cfg: Config) -> None:
    if session_state().exists:
        raise CommandError(f"session {SESSION!r} already exists")
    for path in (cfg.workspace, cfg.linux_workspace, cfg.git_store):
        if not path.is_dir():
            raise CommandError(f"{path} does not exist")
    run(create_command(cfg, binary=require("mutagen")))


def flush() -> None:
    run([require("mutagen"), "sync", "flush", SESSION], capture=True, check=False)


def snapshot(root: str, ignores: IgnoreList) -> Dict[str, tuple]:
    """Relative path -> (kind, size, link target, executable), outside the ignore list.

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
            if ignores.matches(rel):
                continue
            if os.path.islink(path):
                entries[rel] = ("link", 0, os.readlink(path), False)
            elif name != ".git":
                entries[rel] = ("dir", 0, None, False)
                kept.append(name)
        dirs[:] = kept
        for name in files:
            rel = f"{rel_dir}/{name}" if rel_dir else name
            if ignores.matches(rel):
                continue
            path = os.path.join(directory, name)
            info = os.lstat(path)
            if stat.S_ISLNK(info.st_mode):
                entries[rel] = ("link", 0, os.readlink(path), False)
            else:
                entries[rel] = ("file", info.st_size, None, bool(info.st_mode & stat.S_IXUSR))
    return entries


def compare(alpha: str, beta: str, ignores: IgnoreList, content: bool = False) -> List[str]:
    """Human-readable differences between the two trees outside the ignore list."""
    a, b = snapshot(alpha, ignores), snapshot(beta, ignores)
    differences = [f"only in {alpha}: {rel}" for rel in sorted(a.keys() - b.keys())]
    differences += [f"only in {beta}: {rel}" for rel in sorted(b.keys() - a.keys())]
    for rel in sorted(a.keys() & b.keys()):
        if a[rel] != b[rel]:
            differences.append(f"differs: {rel}: {a[rel]} != {b[rel]}")
        elif content and a[rel][0] == "file" and not filecmp.cmp(
                os.path.join(alpha, rel), os.path.join(beta, rel), shallow=False):
            differences.append(f"content differs: {rel}")
    return differences
