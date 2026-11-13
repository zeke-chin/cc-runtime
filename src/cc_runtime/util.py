"""Small helpers shared by the commands."""
from __future__ import annotations

import os
import shutil
import subprocess
from typing import List, Optional

SEARCH_PATH = ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin",
               os.path.expanduser("~/.local/bin"), os.path.expanduser("~/.cargo/bin")]


class CommandError(Exception):
    """A user-facing error: printed without a traceback."""


def which(name: str) -> Optional[str]:
    found = shutil.which(name)
    if found:
        return found
    for directory in SEARCH_PATH:
        candidate = os.path.join(directory, name)
        if os.access(candidate, os.X_OK):
            return candidate
    return None


def require(name: str) -> str:
    path = which(name)
    if not path:
        raise CommandError(f"{name} is not installed or not on PATH")
    return path


def run(args: List[str], *, capture: bool = False, check: bool = True, input: Optional[str] = None,
        env: Optional[dict] = None) -> subprocess.CompletedProcess:
    return subprocess.run(args, check=check, text=True, input=input, env=env,
                          stdout=subprocess.PIPE if capture else None,
                          stderr=subprocess.PIPE if capture else None)


def human_bytes(value: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(value) < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"
