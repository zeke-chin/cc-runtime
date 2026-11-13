"""The `status` dashboard: one screen for sync, jobs, git data, disk and containers."""
from __future__ import annotations

import json
import shutil
import sys
import time
from typing import List, Optional

from rich import box
from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from . import gitstore, jobs, sync
from .config import Config
from .util import human_bytes, run, which

OK, WARN, BAD, DIM = "green", "yellow", "red", "dim"


def _ago(stamp: Optional[str]) -> str:
    if not stamp:
        return "never"
    try:
        seconds = time.time() - time.mktime(time.strptime(stamp, "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return stamp
    for size, unit in ((86400, "d"), (3600, "h"), (60, "min")):
        if seconds >= size:
            return f"{int(seconds // size)}{unit} ago"
    return f"{int(seconds)}s ago"


def _table(title: str) -> Table:
    table = Table(title=title, title_justify="left", title_style="bold", box=box.SIMPLE_HEAD,
                  expand=True, show_edge=False, pad_edge=False)
    return table


def sync_section(cfg: Config) -> Table:
    table = _table("Sync session")
    table.add_column("item", style=DIM, width=14)
    table.add_column("state")
    state = sync.session_state()
    if not state.exists:
        if not state.daemon_running:
            table.add_row("daemon", Text("not running — start it with `mutagen daemon start`", BAD))
        else:
            table.add_row("session", Text(f"{sync.SESSION!r} does not exist — `cc-runtime sync create`", BAD))
        return table
    color = OK if state.healthy else BAD
    table.add_row("status", Text(f"{state.status}{'' if state.connected else ' (disconnected)'}", color))
    table.add_row("endpoints", f"{cfg.workspace}  ⇄  {cfg.linux_workspace}")
    table.add_row("conflicts", Text(str(state.conflicts), BAD if state.conflicts else OK))
    table.add_row("problems", Text(str(state.problems), BAD if state.problems else OK))
    missing, extra = sync.ignore_drift(cfg, state)
    if missing or extra:
        detail = []
        if missing:
            detail.append(f"not in session: {', '.join(missing)}")
        if extra:
            detail.append(f"only in session: {', '.join(extra)}")
        table.add_row("ignores", Text("differ from config — recreate the session to apply; "
                                      + "; ".join(detail), WARN))
    else:
        table.add_row("ignores", Text(f"{len(state.ignores)} entries, match config", OK))
    return table


def _job_summary(record: Optional[dict]) -> Text:
    if not record:
        return Text("—", DIM)
    summary = record.get("summary", {})
    if "error" in summary:
        return Text(summary["error"], BAD)
    if "skipped" in summary:
        return Text(f"skipped: {summary['skipped']}", WARN)
    if record["job"] == "dedupe":
        shared = summary.get("shared_bytes", 0) + summary.get("already_bytes", 0)
        text = f"{human_bytes(shared)} shared ({summary.get('shared', 0)} new, " \
               f"{summary.get('already', 0)} kept), {summary.get('recent', 0)} recent"
        if summary.get("errors"):
            return Text(text + f", {summary['errors']} errors", BAD)
        return Text(text)
    if record["job"] == "externalize":
        moved = summary.get("externalized", [])
        return Text(f"{len(moved)} externalized" + (f": {', '.join(moved[:3])}" if moved else "")
                    + f", {summary.get('skipped', 0)} skipped")
    return Text(json.dumps(summary)[:80])


def jobs_section(cfg: Config) -> Table:
    table = _table("Scheduled jobs")
    for column, kwargs in (("job", {"style": "bold", "width": 12}), ("schedule", {"width": 14}),
                           ("launchd", {"width": 12}), ("last run", {"width": 12}),
                           ("exit", {"width": 5}), ("result", {})):
        table.add_column(column, **kwargs)
    for job in jobs.JOBS.values():
        present, loaded = jobs.scheduled(job.name)
        agent = Text("loaded", OK) if loaded else Text("file only", WARN) if present else Text("off", DIM)
        record = jobs.last_run(cfg, job.name)
        code = record.get("exit") if record else None
        exit_text = Text("—", DIM) if code is None else Text(str(code), OK if code == 0 else BAD)
        table.add_row(job.name, job.schedule_text(), agent, _ago(record and record.get("started")),
                      exit_text, _job_summary(record))
    return table


def git_section(cfg: Config, full: bool) -> Table:
    table = _table("Git data")
    table.add_column("item", style=DIM, width=14)
    table.add_column("state")
    table.add_row("store", str(cfg.git_store))
    stored = gitstore.stored_gitdirs(cfg.git_store)
    table.add_row("repositories", f"{len(stored)} externalized")
    if not full:
        table.add_row("check", Text("run `cc-runtime status --git` for a full scan", DIM))
        return table
    report = gitstore.status(gitstore.Layout.from_config(cfg))
    for key in sorted(report.counts):
        table.add_row("", f"{report.counts[key]:6d}  {key}")
    for line in report.warnings:
        table.add_row(Text("warning", WARN), line)
    for line in report.anomalies:
        table.add_row(Text("anomaly", BAD), line)
    if not report.anomalies:
        table.add_row("anomalies", Text("none", OK))
    return table


def disk_section(cfg: Config) -> Table:
    table = _table("Disk")
    table.add_column("item", style=DIM, width=14)
    table.add_column("state")
    probe = cfg.runtime if cfg.runtime.exists() else cfg.workspace
    usage = shutil.disk_usage(probe)
    pct = usage.used / usage.total * 100
    table.add_row("volume", Text(f"{human_bytes(usage.used)} used, {human_bytes(usage.free)} free "
                                 f"({pct:.0f}%)", BAD if pct > 90 else WARN if pct > 80 else ""))
    if sys.platform == "darwin" and which("tmutil"):
        out = run(["tmutil", "listlocalsnapshots", "/"], capture=True, check=False).stdout or ""
        names = [line.strip() for line in out.splitlines() if line.strip().startswith("com.apple")]
        latest = names[-1].split(".")[-2] if names else "none"
        table.add_row("local snapshots", f"{len(names)} (latest {latest}) — they hold freed blocks "
                                         "until Time Machine removes them")
    return table


def containers_section(cfg: Config) -> Table:
    table = _table("Containers")
    docker = which("docker")
    if not docker:
        table.add_column("state")
        table.add_row(Text("docker not available", DIM))
        return table
    for column in ("service", "container", "state", "note"):
        table.add_column(column)
    project = cfg.root.name
    out = run([docker, "ps", "-a", "--filter", f"label=com.docker.compose.project={project}",
               "--format", "{{json .}}"], capture=True, check=False).stdout or ""
    configured = set((run([docker, "compose", "--project-directory", str(cfg.root), "config",
                           "--services"], capture=True, check=False).stdout or "").split())
    rows = [json.loads(line) for line in out.splitlines() if line.strip()]
    for row in sorted(rows, key=lambda r: r.get("Names", "")):
        labels = dict(item.split("=", 1) for item in row.get("Labels", "").split(",") if "=" in item)
        service = labels.get("com.docker.compose.service", "?")
        running = row.get("State") == "running"
        note = Text("not in compose.yaml (legacy)", WARN) if configured and service not in configured else ""
        if service.endswith("network-init") and not running:
            continue  # one-shot helpers exit by design
        table.add_row(service, row.get("Names", ""), Text(row.get("Status", ""), OK if running else BAD), note)
    return table


def render(cfg: Config, full_git: bool = False) -> Group:
    sections: List = []
    for builder in (lambda: sync_section(cfg), lambda: jobs_section(cfg),
                    lambda: git_section(cfg, full_git), lambda: disk_section(cfg),
                    lambda: containers_section(cfg)):
        try:
            sections.append(builder())
        except Exception as error:  # one broken source must not hide the rest
            sections.append(Text(f"error: {type(error).__name__}: {error}", BAD))
        sections.append(Text(""))
    header = Text(time.strftime("%Y-%m-%d %H:%M:%S"), DIM)
    return Group(Panel(Group(*sections), title="[bold]cc-runtime status", subtitle=header,
                       border_style="blue"))
