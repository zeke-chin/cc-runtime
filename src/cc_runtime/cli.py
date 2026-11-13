"""cc-runtime: one entry point for the development runtime.

  status      dashboard: sync session, scheduled jobs, git data, disk, containers
  up          start services (prepares homes, machine identities and caches)
  compose     any docker compose command for this deployment
  sync        the Mutagen session between the Mac workspace and the Linux mirror
  git         keep repositories' git data in the shared store
  dedupe      share identical files between the two trees
  schedule    install or remove the launchd jobs
  job         run a scheduled job now, or show its log
  seed-home   seed a new machine's home from an existing one
  check       consistency checks (sync ignore list, gateway configs)
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import List

from rich.console import Console
from rich.table import Table
from rich.text import Text

from . import checks, config, dedupe, deploy, gitstore, jobs, seed, sync
from .util import CommandError, human_bytes

console = Console()
err = Console(stderr=True)


# ---- status ----------------------------------------------------------------------

def cmd_status(cfg, args) -> int:
    from . import status
    if args.watch is None:
        console.print(status.render(cfg, args.git))
        return 0
    from rich.live import Live
    with Live(status.render(cfg, args.git), console=console, screen=True,
              refresh_per_second=1) as live:
        try:
            while True:
                time.sleep(args.watch)
                live.update(status.render(cfg, args.git))
        except KeyboardInterrupt:
            pass
    return 0


# ---- deployment --------------------------------------------------------------------

def cmd_up(cfg, args) -> int:
    deploy.up(cfg, args.services, pull=args.pull, force_recreate=args.force_recreate,
              allow_orphans=args.allow_orphans)
    return 0


def cmd_compose(cfg, args) -> int:
    if not args.args:
        raise CommandError("give a docker compose command, e.g. `cc-runtime compose ps`")
    return deploy.compose_passthrough(cfg, args.args)


def cmd_seed_home(cfg, args) -> int:
    seed.seed(Path(args.source), Path(args.destination), dry_run=args.dry_run,
              credentials_only=args.credentials_only, out=console.print)
    return 0


# ---- sync ----------------------------------------------------------------------------

def cmd_sync(cfg, args) -> int:
    if args.action == "create":
        sync.create(cfg)
        console.print(f"[green]Created session {sync.SESSION!r}.[/] Watch the first sync with "
                      "`cc-runtime sync status`.")
        return 0
    if args.action == "ignores":
        for entry in cfg.ignores().entries:
            console.print(entry, highlight=False)
        return 0
    if args.action == "compare":
        sync.flush()
        with console.status("Comparing the two trees…"):
            differences = sync.compare(str(cfg.workspace), str(cfg.linux_workspace),
                                       cfg.ignores(), args.content)
        for line in differences[:200]:
            console.print(line, highlight=False)
        if len(differences) > 200:
            console.print(f"… and {len(differences) - 200} more")
        if differences:
            console.print(f"[red]{len(differences)} difference(s)[/]")
            return 1
        console.print("[green]Trees match outside the ignore list.[/]")
        return 0
    state = sync.session_state()
    if not state.exists:
        if not state.daemon_running:
            console.print("[red]The Mutagen daemon is not running.[/] Start it with `mutagen daemon start`.")
        else:
            console.print(f"[red]Session {sync.SESSION!r} does not exist.[/] Create it with "
                          "`cc-runtime sync create`.")
        return 1
    if args.verbose:
        console.print(state.raw.strip(), highlight=False)
    missing, extra = sync.ignore_drift(cfg, state)
    color = "green" if state.ok else "red"
    console.print(f"[{color}]{state.status}[/] · conflicts {state.conflicts} · problems {state.problems}")
    if missing or extra:
        console.print("[yellow]The session's ignore list differs from the configuration; "
                      "recreate the session to apply it.[/]")
    return 0 if state.ok else 1


# ---- git -------------------------------------------------------------------------------

def _git_targets(layout, args) -> List[str]:
    if args.all == bool(args.repos):
        raise CommandError("give repositories or --all, not both")
    if args.all:
        repos = gitstore.scan(layout)
        if args.action == "externalize":
            return [r for r, e in repos.items() if "dir" in e.values()]
        return [r for r, e in repos.items() if e["mac"] == "link"]
    return [layout.rel(r) for r in args.repos]


def cmd_git(cfg, args) -> int:
    layout = gitstore.Layout.from_config(cfg)
    try:
        layout.check()
    except ValueError as error:
        raise CommandError(str(error)) from None
    if args.action == "status":
        with console.status("Scanning both trees…"):
            report = gitstore.status(layout)
        table = Table(show_header=False, box=None)
        for key in sorted(report.counts):
            table.add_row(f"{report.counts[key]:6d}", key)
        console.print(table)
        if args.list:
            for rel in report.externalized:
                console.print(f"  {rel}", highlight=False)
        for line in report.warnings:
            console.print(f"[yellow]warning[/]  {line}", highlight=False)
        for line in report.anomalies:
            console.print(f"[red]anomaly[/]  {line}", highlight=False)
        if not report.anomalies:
            console.print("[green]No anomalies.[/]")
        return 1 if report.anomalies else 0
    action = gitstore.externalize if args.action == "externalize" else gitstore.restore
    failed = 0
    for rel in sorted(_git_targets(layout, args)):
        try:
            changed, message = action(layout, rel, args.dry_run)
        except (OSError, ValueError) as error:
            changed, message = False, f"{rel}: error: {error}"
        style = "green" if changed else "yellow"
        console.print(Text(message, style), highlight=False)
        # With --all, skips (busy, unsupported) are expected; explicit requests must succeed.
        if not changed and not args.all and not message.endswith("already externalized"):
            failed += 1
    return 1 if failed else 0


# ---- dedupe ------------------------------------------------------------------------------

def cmd_dedupe(cfg, args) -> int:
    if not args.force:
        state = sync.session_state()
        if not state.ok:
            console.print(f"[yellow]Not deduplicating: the sync session is not healthy "
                          f"({state.status or 'not running'}, {state.conflicts} conflicts).[/] "
                          "Use --force to run anyway.")
            return 2
    try:
        with console.status("Comparing files…"):
            stats = dedupe.run(cfg, dry_run=args.dry_run, min_size=args.min_size,
                               quiet_hours=args.quiet_hours, log=lambda m: console.print(m, style="red"))
    except dedupe.Unsupported as error:
        raise CommandError(str(error)) from None
    table = Table(show_header=False, box=None)
    if args.dry_run:
        table.add_row("would share", f"{stats['would_share']} files, {human_bytes(stats['would_share_bytes'])}")
    else:
        table.add_row("shared now", f"{stats['shared']} files, {human_bytes(stats['shared_bytes'])}")
    table.add_row("already shared", f"{stats['already']} files, {human_bytes(stats['already_bytes'])}")
    table.add_row("skipped", f"{stats['recent']} changed recently, {stats['differs']} differ, "
                             f"{stats['size_differs']} size differs, {stats['missing']} one side only, "
                             f"{stats['small']} small")
    table.add_row("errors", Text(str(stats["errors"]), "red" if stats["errors"] else "green"))
    table.add_row("took", f"{stats['seconds']}s using {stats['method']}")
    console.print(table)
    if not args.dry_run and stats["shared_bytes"]:
        console.print("[dim]Freed space appears in df once local snapshots holding the old "
                      "blocks are removed.[/]")
    return 1 if stats["errors"] else 0


# ---- schedule and jobs ----------------------------------------------------------------------

def _job_names(args) -> List[str]:
    if args.all == bool(args.jobs):
        raise CommandError("give job names or --all, not both")
    names = list(jobs.JOBS) if args.all else args.jobs
    for name in names:
        jobs.get(name)
    return names


def cmd_schedule(cfg, args) -> int:
    if args.action == "list":
        table = Table("job", "schedule", "launchd", "description")
        for job in jobs.JOBS.values():
            present, loaded = jobs.scheduled(job.name)
            state = "[green]loaded[/]" if loaded else "[yellow]file only[/]" if present else "[dim]off[/]"
            table.add_row(job.name, job.schedule_text(), state, job.description)
        console.print(table)
        return 0
    names = _job_names(args)
    if args.action == "install":
        jobs.install(cfg, names)
        for name in names:
            console.print(f"[green]Installed[/] {name} ({jobs.get(name).schedule_text()}): "
                          f"{jobs.plist_path(name)}")
    else:
        jobs.uninstall(names)
        for name in names:
            console.print(f"Removed {name}.")
    return 0


def cmd_job(cfg, args) -> int:
    if args.action == "log":
        path = cfg.log_dir / f"{args.name}.log"
        jobs.get(args.name)
        if not path.exists():
            console.print(f"[dim]No log yet: {path}[/]")
            return 0
        for line in path.read_text().splitlines()[-args.lines:]:
            console.print_json(line) if args.json else console.print(line, highlight=False)
        return 0
    record = jobs.run_job(cfg, args.name)
    console.print_json(json.dumps(record, ensure_ascii=False))
    return record["exit"]


# ---- checks ------------------------------------------------------------------------------------

def cmd_check(cfg, args) -> int:
    if args.what == "proxy":
        failed = False
        for name, lines in checks.proxy_differences(cfg.root / "proxy"):
            if not lines:
                console.print(f"[green]ok[/]    {name}")
                continue
            failed = True
            console.print(f"[red]DIFF[/]  {name} differs from config.example.yaml outside "
                          "GATEWAY-SPECIFIC blocks:")
            for line in lines:
                console.print("      " + line, highlight=False, markup=False)
        return 1 if failed else 0
    ignores = cfg.ignores()
    failed = False
    for error in checks.layout_errors(cfg):
        console.print(f"[red]ERROR[/]  layout: {error}")
        failed = True
    with console.status("Listing tracked files in every repository…"):
        violations = checks.tracked_violations(cfg.workspace, ignores)
    for rel, name in violations:
        console.print(f"[red]ERROR[/]  tracked file is ignored: {rel}/{name}", highlight=False)
        failed = True
    try:
        mounts = checks.container_state_mounts(cfg.workspace, ignores)
    except Exception as error:
        mounts = []
        console.print(f"[yellow]WARN[/]   could not list container mounts: {error}")
    for name, rel in mounts:
        console.print(f"[red]ERROR[/]  container {name} mounts {rel} writable; add an ignore entry",
                      highlight=False)
        failed = True
    for rel in checks.stray_virtualenvs(cfg.workspace, ignores):
        console.print(f"[yellow]WARN[/]   virtual environment will be synchronized: {rel}", highlight=False)
    console.print("[red]FAILED[/]" if failed else "[green]OK[/]")
    return 1 if failed else 0


# ---- parser ------------------------------------------------------------------------------------

def parser() -> argparse.ArgumentParser:
    main = argparse.ArgumentParser(prog="cc-runtime", description=__doc__,
                                   formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = main.add_subparsers(dest="command", required=True, metavar="COMMAND")

    p = sub.add_parser("status", help="dashboard of the whole runtime")
    p.add_argument("--git", action="store_true", help="also scan every repository (slower)")
    p.add_argument("--watch", type=float, nargs="?", const=5, metavar="SECONDS",
                   help="refresh continuously (default every 5 seconds)")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("up", help="start services")
    p.add_argument("services", nargs="*", help="only these services (and their network-init helpers)")
    p.add_argument("--pull", action="store_true", help="pull images first")
    p.add_argument("--force-recreate", action="store_true")
    p.add_argument("--allow-orphans", action="store_true",
                   help="start even if containers not in compose.yaml are running")
    p.set_defaults(func=cmd_up)

    p = sub.add_parser("compose", help="run docker compose for this deployment")
    p.add_argument("args", nargs=argparse.REMAINDER)
    p.set_defaults(func=cmd_compose)

    p = sub.add_parser("sync", help="the Mutagen sync session")
    p.add_argument("action", choices=["status", "create", "compare", "ignores"])
    p.add_argument("--content", action="store_true", help="compare: also compare file contents")
    p.add_argument("-v", "--verbose", action="store_true", help="status: show Mutagen's full output")
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("git", help="git data in the shared store")
    git_sub = p.add_subparsers(dest="action", required=True)
    g = git_sub.add_parser("status", help="state of every repository; exit 1 on anomalies")
    g.add_argument("--list", action="store_true", help="list externalized repositories")
    for name, text in (("externalize", "move .git into the shared store"),
                       ("restore", "move git data back into the Mac tree")):
        g = git_sub.add_parser(name, help=text)
        g.add_argument("repos", nargs="*", help="paths relative to the workspace, or absolute")
        g.add_argument("--all", action="store_true")
        g.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_git)

    p = sub.add_parser("dedupe", help="share identical files between the two trees")
    p.add_argument("--dry-run", action="store_true", help="only report what would be shared")
    p.add_argument("--min-size", type=int, default=dedupe.MIB, help="skip smaller files (bytes)")
    p.add_argument("--quiet-hours", type=float, default=24,
                   help="skip files changed on either side more recently")
    p.add_argument("--force", action="store_true", help="run even if the sync session is not healthy")
    p.set_defaults(func=cmd_dedupe)

    p = sub.add_parser("schedule", help="launchd jobs")
    p.add_argument("action", choices=["list", "install", "uninstall"])
    p.add_argument("jobs", nargs="*", help=f"job names: {', '.join(jobs.JOBS)}")
    p.add_argument("--all", action="store_true")
    p.set_defaults(func=cmd_schedule)

    p = sub.add_parser("job", help="run a job now, or show its log")
    p.add_argument("action", choices=["run", "log"])
    p.add_argument("name", choices=list(jobs.JOBS))
    p.add_argument("-n", "--lines", type=int, default=10, help="log: number of runs to show")
    p.add_argument("--json", action="store_true", help="log: pretty-print each record")
    p.set_defaults(func=cmd_job)

    p = sub.add_parser("seed-home", help="seed a new machine's home from an existing one")
    p.add_argument("source")
    p.add_argument("destination")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--credentials-only", action="store_true",
                   help="copy only the shared credentials into an existing home")
    p.set_defaults(func=cmd_seed_home)

    p = sub.add_parser("check", help="consistency checks")
    p.add_argument("what", choices=["ignores", "proxy"])
    p.set_defaults(func=cmd_check)
    return main


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    cfg = config.load()
    try:
        return args.func(cfg, args) or 0
    except CommandError as error:
        err.print(f"[red]Error:[/] {error}", highlight=False)
        return 1
    except KeyboardInterrupt:
        return 130
