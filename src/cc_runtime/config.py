"""Paths and settings, read from the environment and the repository's .env file."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parents[2]

# Matched by name at any depth: platform-specific dependencies, build output and caches.
# Each side of the sync keeps its own copy of these.
NAME_IGNORES = [
    "node_modules", ".venv", ".venv-mac", "target", ".next", ".nuxt", ".output", ".svelte-kit",
    ".turbo", ".parcel-cache", ".gradle", ".dart_tool", ".angular", ".tox", ".nox",
    "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache",
]

LOCAL_IGNORES_FILE = "sync-ignores.local"
SESSION = "linux-workspace"


def read_env_file(path: Path) -> Dict[str, str]:
    values = {}
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return values
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def _path(value: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(value)))


class IgnoreList:
    """Mutagen-style ignores: '/anchored/path/' or a name matched at any depth."""

    def __init__(self, entries: List[str]):
        self.entries = list(entries)
        self.anchored = [e if e.endswith("/") else e + "/" for e in entries if e.startswith("/")]
        self.names = {e.rstrip("/") for e in entries if not e.startswith("/")}

    def matches(self, rel: str) -> bool:
        parts = [p for p in rel.strip("/").split("/") if p]
        if any(p in self.names for p in parts):
            return True
        path = "/" + "/".join(parts) + "/"
        return any(path.startswith(entry) for entry in self.anchored)


@dataclass(frozen=True)
class Config:
    root: Path
    workspace: Path
    runtime: Path
    downloads: Path
    env: Dict[str, str] = field(default_factory=dict, compare=False)

    @property
    def linux_workspace(self) -> Path:
        return self.runtime / "linux-workspace"

    @property
    def git_store(self) -> Path:
        return self.runtime / "git"

    @property
    def state_dir(self) -> Path:
        return self.runtime / "state"

    @property
    def log_dir(self) -> Path:
        return self.runtime / "logs"

    @property
    def tmp_dir(self) -> Path:
        return self.runtime / "tmp"

    def repo_rel(self) -> Optional[str]:
        """This repository's path inside the workspace, if it is inside."""
        try:
            return self.root.resolve().relative_to(self.workspace.resolve()).as_posix()
        except (ValueError, OSError):
            return None

    def deployment_ignores(self) -> List[str]:
        """Per-machine state of this deployment, kept out of the Linux mirror."""
        rel = self.repo_rel()
        if rel is None:
            return []
        return [f"/{rel}/dev-runtimes/", f"/{rel}/tailscale/state/"]

    def local_ignores(self) -> List[str]:
        entries = []
        try:
            lines = (self.root / LOCAL_IGNORES_FILE).read_text().splitlines()
        except OSError:
            return entries
        for line in lines:
            line = line.split("#", 1)[0].strip()
            if line:
                entries.append(line)
        return entries

    def ignores(self) -> IgnoreList:
        seen, entries = set(), []
        for entry in self.deployment_ignores() + self.local_ignores() + NAME_IGNORES:
            if entry not in seen:
                seen.add(entry)
                entries.append(entry)
        return IgnoreList(entries)


def load(root: Path = ROOT, environ: Optional[Dict[str, str]] = None) -> Config:
    environ = dict(os.environ if environ is None else environ)
    values = read_env_file(root / ".env")
    values.update({k: v for k, v in environ.items() if k in (
        "WORKSPACE_DIR", "RUNTIME_DIR", "DOWNLOADS_DIR")})
    return Config(
        root=root,
        workspace=_path(values.get("WORKSPACE_DIR", "~/workspace")),
        runtime=_path(values.get("RUNTIME_DIR", "~/.cc-runtime")),
        downloads=_path(values.get("DOWNLOADS_DIR", "~/Downloads")),
        env=values,
    )
