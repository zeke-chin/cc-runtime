"""Shared test helpers. Tests import the package from src/ without installing it."""
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from cc_runtime import config  # noqa: E402

MIB = 1 << 20
DAY = 24 * 3600
GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
       "-c", "init.defaultBranch=main", "-c", "protocol.file.allow=always"]


def make_config(base: Path, root=None) -> config.Config:
    """A configuration whose workspace and runtime live under a temporary directory."""
    workspace, runtime = base / "mac", base / "runtime"
    for path in (workspace, runtime / "linux-workspace", runtime / "git"):
        path.mkdir(parents=True, exist_ok=True)
    return config.Config(root=Path(root) if root else base / "repo", workspace=workspace,
                         runtime=runtime, downloads=base / "downloads")


def git(cwd, *args) -> str:
    return subprocess.run(GIT + list(args), cwd=cwd, check=True, capture_output=True, text=True).stdout


def age(path, seconds=600) -> None:
    """Make files look idle (mtime in the past)."""
    past = time.time() - seconds
    for directory, dirs, files in os.walk(path):
        for name in dirs + files:
            os.utime(os.path.join(directory, name), (past, past), follow_symlinks=False)
    os.utime(path, (past, past))


def write(path: Path, data: bytes, age_seconds: float = 2 * DAY) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    past = time.time() - age_seconds
    os.utime(path, (past, past))
