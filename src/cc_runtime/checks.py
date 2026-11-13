"""Consistency checks: the sync ignore list and the gateway configs."""
from __future__ import annotations

import difflib
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from .config import Config, IgnoreList
from .gitstore import scan_tree
from .util import which


# ---- sync ignore list ---------------------------------------------------------

def contains(a: Path, b: Path) -> bool:
    return a == b or a in b.parents


def layout_errors(cfg: Config) -> List[str]:
    errors = []
    paths = (cfg.workspace, cfg.linux_workspace, cfg.git_store)
    for i, x in enumerate(paths):
        for y in paths[i + 1:]:
            if contains(x, y) or contains(y, x):
                errors.append(f"{x} and {y} must not contain each other")
    rel = cfg.repo_rel()
    if rel is not None and not cfg.ignores().matches(f"{rel}/dev-runtimes/x"):
        errors.append("the ignore list must exclude this repository's dev-runtimes/")
    return errors


def tracked_violations(workspace: Path, ignores: IgnoreList) -> List[Tuple[str, str]]:
    """Files tracked by git that fall under the ignore list (source that would not sync)."""
    found = []
    for rel, entry in sorted(scan_tree(workspace, ignores).items()):
        if entry not in ("dir", "link"):
            continue  # submodule checkouts are covered by their superproject
        result = subprocess.run(["git", "-C", str(workspace / rel), "-c", "core.quotepath=off",
                                 "ls-files", "-z"], capture_output=True)
        if result.returncode != 0:
            found.append((rel, "git ls-files failed: "
                          + result.stderr.decode(errors="replace").strip()))
            continue
        for name in result.stdout.decode(errors="surrogateescape").split("\0"):
            if name and ignores.matches(f"{rel}/{name}"):
                found.append((rel, name))
    return found


def stray_virtualenvs(workspace: Path, ignores: IgnoreList) -> List[str]:
    """Virtual environments outside ignored names: they would be synchronized."""
    found = []
    for directory, dirs, files in os.walk(workspace):
        rel = os.path.relpath(directory, workspace)
        rel = "" if rel == "." else rel
        dirs[:] = [d for d in dirs if d != ".git" and not ignores.matches(f"{rel}/{d}" if rel else d)]
        if "pyvenv.cfg" in files:
            found.append(rel)
            dirs[:] = []
    return found


def container_state_mounts(workspace: Path, ignores: IgnoreList,
                           inspect: Optional[Callable] = None) -> List[Tuple[str, str]]:
    """Writable bind mounts of containers inside the workspace that are not ignored.

    Live state (a database directory) must not be synchronized: it would be copied
    mid-write, and a change on the Linux side would flow back into it. Mounts of the
    whole workspace are not counted.
    """
    if inspect is None:
        def inspect():
            docker = which("docker")
            if not docker:
                return []
            ids = subprocess.run([docker, "ps", "-aq"], capture_output=True, text=True,
                                 check=True).stdout.split()
            if not ids:
                return []
            return json.loads(subprocess.run([docker, "inspect", *ids], capture_output=True,
                                             text=True, check=True).stdout)
    found = set()
    for container in inspect():
        for mount in container.get("Mounts", []):
            if mount.get("Type") != "bind" or not mount.get("RW"):
                continue
            source = Path(mount["Source"])
            if source == workspace or workspace not in source.parents:
                continue
            rel = source.relative_to(workspace).as_posix()
            if not ignores.matches(rel + "/"):
                found.add((container.get("Name", "?").lstrip("/"), rel))
    return sorted(found)


# ---- gateway configs ----------------------------------------------------------

BLOCK = re.compile(r"[ ]*# ==== GATEWAY-SPECIFIC.*?# ==== END GATEWAY-SPECIFIC ====\n", re.S)
AIRPORT = re.compile(r"  - name: airport\n.*?\n\n", re.S)
# Template placeholder -> pattern accepted for the real value.
PLACEHOLDER_IPS = re.compile(r"IP-CIDR,[0-9.]+/32,DIRECT,no-resolve\n(?=  # Local LAN subnet)")
CREDENTIAL = re.compile(r"(password|username|uuid|public-key|short-id|cipher)\s*:")


def normalize_proxy(text: str) -> str:
    text = text[text.index("mode: rule\n"):]
    text = BLOCK.sub("<GATEWAY-SPECIFIC>\n", text)
    text = AIRPORT.sub("<AIRPORT>\n\n", text)
    return PLACEHOLDER_IPS.sub("<DIRECT-HOST>\n", text)


def proxy_differences(proxy_dir: Path) -> List[Tuple[str, List[str]]]:
    """(derived config, diff lines) for every config.*.yaml that differs from the template.

    Outside GATEWAY-SPECIFIC blocks, each derived config must equal
    config.example.yaml except for the airport node and documented placeholders.
    Credential-looking lines are never shown.
    """
    expected = normalize_proxy((proxy_dir / "config.example.yaml").read_text()).splitlines()
    results = []
    for path in sorted(proxy_dir.glob("config*.yaml")):
        if path.name == "config.example.yaml":
            continue
        actual = normalize_proxy(path.read_text()).splitlines()
        lines = []
        if actual != expected:
            for line in difflib.unified_diff(expected, actual, "config.example.yaml", path.name,
                                             lineterm="", n=1):
                lines.append(line[0] + "  <credential line hidden>" if CREDENTIAL.search(line) else line)
        results.append((path.name, lines))
    return results
