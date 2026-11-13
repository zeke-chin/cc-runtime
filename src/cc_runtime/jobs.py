"""Scheduled jobs: one registry, one runner, launchd as the scheduler.

Every job runs through `cc-runtime job run NAME`, which takes a per-job lock,
records the outcome in <runtime>/state/jobs/NAME.json (shown by `status`) and
appends it to <runtime>/logs/NAME.log. `schedule install` writes one launchd
agent per job that calls that command; nothing else needs to stay running.
"""
from __future__ import annotations

import fcntl
import json
import os
import plistlib
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from . import dedupe, gitstore, sync
from .config import Config
from .util import CommandError, run, which

LABEL_PREFIX = "io.cc-runtime."
LAUNCHD_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"


@dataclass(frozen=True)
class Job:
    name: str
    description: str
    run: Callable[[Config], Tuple[int, dict]]
    calendar: Optional[Dict[str, int]] = None  # launchd StartCalendarInterval
    interval: Optional[int] = None             # seconds between runs

    def schedule_text(self) -> str:
        if self.calendar:
            return f"daily {self.calendar.get('Hour', 0):02d}:{self.calendar.get('Minute', 0):02d}"
        return f"every {self.interval // 60} min" if self.interval and self.interval >= 60 \
            else f"every {self.interval}s"


def _dedupe(cfg: Config) -> Tuple[int, dict]:
    state = sync.session_state()
    if not state.ok:
        # Do not touch the mirror while the two sides may legitimately differ.
        return 2, {"skipped": f"sync session not healthy ({state.status or 'not running'}, "
                              f"{state.conflicts} conflicts)"}
    errors: List[str] = []
    try:
        stats = dedupe.run(cfg, log=errors.append)
    except dedupe.Unsupported as error:
        return 3, {"error": str(error)}
    if errors:
        stats["error_samples"] = errors[:5]
    return (1 if stats["errors"] else 0), stats


def _externalize(cfg: Config) -> Tuple[int, dict]:
    layout = gitstore.Layout.from_config(cfg)
    layout.check()
    results = gitstore.externalize_all(layout)
    moved = [rel for rel, changed, _ in results if changed]
    return 0, {"externalized": moved, "skipped": len(results) - len(moved)}


JOBS: Dict[str, Job] = {job.name: job for job in [
    Job("dedupe", "share identical files between the two trees (filesystem clones)",
        _dedupe, calendar={"Hour": 4, "Minute": 0}),
    Job("externalize", "move new repositories' git data into the shared store",
        _externalize, interval=300),
]}


def get(name: str) -> Job:
    if name not in JOBS:
        raise CommandError(f"unknown job {name!r}; known: {', '.join(JOBS)}")
    return JOBS[name]


def record_path(cfg: Config, name: str) -> Path:
    return cfg.state_dir / "jobs" / f"{name}.json"


def last_run(cfg: Config, name: str) -> Optional[dict]:
    try:
        return json.loads(record_path(cfg, name).read_text())
    except (OSError, ValueError):
        return None


def run_job(cfg: Config, name: str) -> dict:
    job = get(name)
    (cfg.state_dir / "jobs").mkdir(parents=True, exist_ok=True)
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    with open(cfg.state_dir / "jobs" / f"{name}.lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"job": name, "exit": 0, "summary": {"skipped": "already running"}}
        started = time.time()
        try:
            code, summary = job.run(cfg)
        except Exception as error:  # recorded, so a failing scheduled job is visible
            code, summary = 1, {"error": f"{type(error).__name__}: {error}"}
        record = {"job": name, "started": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(started)),
                  "seconds": round(time.time() - started, 1), "exit": code, "summary": summary}
        temporary = record_path(cfg, name).with_suffix(".tmp")
        temporary.write_text(json.dumps(record, ensure_ascii=False))
        temporary.replace(record_path(cfg, name))
        with open(cfg.log_dir / f"{name}.log", "a") as log:
            log.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


# ---- launchd ------------------------------------------------------------------

def label(name: str) -> str:
    return LABEL_PREFIX + name


def plist_path(name: str) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{label(name)}.plist"


def program_arguments(cfg: Config, name: str) -> List[str]:
    uv = which("uv")
    if not uv:
        raise CommandError("uv is required for scheduled jobs (https://docs.astral.sh/uv/)")
    # --frozen: use the committed lock file, never resolve over the network at 04:00.
    return [uv, "run", "--project", str(cfg.root), "--frozen", "--quiet",
            "cc-runtime", "job", "run", name]


def plist(cfg: Config, name: str) -> dict:
    job = get(name)
    data = {
        "Label": label(name),
        "ProgramArguments": program_arguments(cfg, name),
        "EnvironmentVariables": {"PATH": LAUNCHD_PATH, "HOME": str(Path.home())},
        "StandardOutPath": str(cfg.log_dir / f"{name}.launchd.log"),
        "StandardErrorPath": str(cfg.log_dir / f"{name}.launchd.log"),
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "Nice": 10,
    }
    if job.calendar:
        data["StartCalendarInterval"] = job.calendar
    if job.interval:
        data["StartInterval"] = job.interval
    return data


def _require_macos() -> None:
    if sys.platform != "darwin":
        raise CommandError("scheduling uses launchd and is macOS only; on Linux run "
                           "`cc-runtime job run NAME` from a systemd timer")


def install(cfg: Config, names: List[str]) -> None:
    _require_macos()
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    domain = f"gui/{os.getuid()}"
    for name in names:
        path = plist_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(plistlib.dumps(plist(cfg, name)))
        run(["launchctl", "bootout", domain, str(path)], capture=True, check=False)
        run(["launchctl", "bootstrap", domain, str(path)])


def uninstall(names: List[str]) -> None:
    _require_macos()
    domain = f"gui/{os.getuid()}"
    for name in names:
        path = plist_path(name)
        run(["launchctl", "bootout", domain, str(path)], capture=True, check=False)
        if path.exists():
            path.unlink()


def scheduled(name: str) -> Tuple[bool, bool]:
    """(agent file present, agent loaded in launchd)."""
    if sys.platform != "darwin":
        return False, False
    loaded = run(["launchctl", "print", f"gui/{os.getuid()}/{label(name)}"],
                 capture=True, check=False).returncode == 0
    return plist_path(name).exists(), loaded
