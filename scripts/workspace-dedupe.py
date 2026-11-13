#!/usr/bin/env python3
"""Share disk blocks between the Mac workspace and its Linux mirror.

Mutagen writes every file it synchronizes as new data, so the two trees take
twice the space. Once a day this script finds files that are identical on both
sides at the same relative path and makes the Linux copy share the Mac copy's
blocks (a filesystem clone). Contents never change; only the Linux side is
touched. See docs/architecture/workspace-dedupe.md.

  macOS (APFS): clonefile() to a temporary name outside the synced trees,
                re-check both files, then rename over the Linux copy.
  Linux (Btrfs, XFS, bcachefs, ZFS 2.2+): FIDEDUPERANGE; the kernel compares
                the ranges under lock and shares them only if identical.

Usage:
  python3 scripts/workspace-dedupe.py run [--dry-run] [--min-size BYTES] [--quiet-hours H]
  python3 scripts/workspace-dedupe.py install     # macOS: run daily at 04:00 (launchd)
  python3 scripts/workspace-dedupe.py uninstall
"""
import argparse
import ctypes
import errno
import fcntl
import filecmp
import importlib.util
import json
import os
import plistlib
import shutil
import stat
import struct
import subprocess
import sys
import time
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "workspace_sync", Path(__file__).resolve().with_name("workspace-sync.py"))
sync = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sync)

MIB = 1 << 20
WORKDIR = sync.LINUX_WORKSPACE.parent  # ~/.cc-runtime: same volume, outside both trees
LABEL = "io.cc-runtime.workspace-dedupe"

# Linux: _IOWR(0x94, 54, struct file_dedupe_range) with a 24-byte header.
FIDEDUPERANGE = 0xC0189436
DEDUPE_CHUNK = 16 * MIB  # Btrfs caps a single request at 16 MiB
_RANGE = "=QQHHI"   # src_offset, src_length, dest_count, reserved1, reserved2
_INFO = "=qQQiI"    # dest_fd, dest_offset, bytes_deduped, status, reserved
FILE_DEDUPE_RANGE_DIFFERS = 1


class Unsupported(Exception):
    pass


def identity(st):
    """What must not change between checking a file and replacing it."""
    return (st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def state_key(alpha_st, beta_st):
    return [alpha_st.st_ino, alpha_st.st_size, alpha_st.st_mtime_ns,
            beta_st.st_ino, beta_st.st_size, beta_st.st_mtime_ns]


# ---- macOS -----------------------------------------------------------------

_clonefile = None


def clonefile(src, dst):
    global _clonefile
    if _clonefile is None:
        libc = ctypes.CDLL(None, use_errno=True)
        _clonefile = libc.clonefile
        _clonefile.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint32]
        _clonefile.restype = ctypes.c_int
    if _clonefile(os.fsencode(str(src)), os.fsencode(str(dst)), 0) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), str(dst))


# ---- Linux -----------------------------------------------------------------

def dedupe_range(src, dst, length):
    """Share dst's blocks with src. Return False if the contents differ."""
    with open(src, "rb") as source, open(dst, "rb+") as target:
        offset = 0
        while offset < length:
            size = min(DEDUPE_CHUNK, length - offset)
            buffer = bytearray(struct.pack(_RANGE, offset, size, 1, 0, 0) +
                               struct.pack(_INFO, target.fileno(), offset, 0, 0, 0))
            fcntl.ioctl(source.fileno(), FIDEDUPERANGE, buffer)
            _, _, done, status, _ = struct.unpack(_INFO, bytes(buffer[struct.calcsize(_RANGE):]))
            if status == FILE_DEDUPE_RANGE_DIFFERS:
                return False
            if status < 0:
                raise OSError(-status, os.strerror(-status), str(dst))
            if done == 0:
                raise OSError(errno.EIO, "the kernel deduplicated nothing", str(dst))
            offset += done
    return True


# ---- strategies --------------------------------------------------------------

class CloneReplace:
    """macOS: clone the Mac file and rename it over the Linux copy."""
    name = "clonefile"

    def __init__(self, tmpdir, clone=clonefile):
        self.tmpdir = Path(tmpdir)
        self.clone = clone
        self.counter = 0

    def probe(self):
        a, b = self.tmpdir / "probe-a", self.tmpdir / "probe-b"
        for path in (a, b):
            if path.exists():
                path.unlink()
        a.write_bytes(b"x" * 65536)
        try:
            self.clone(a, b)
        except OSError as error:
            raise Unsupported(f"cannot clone files in {self.tmpdir}: {error}") from None
        finally:
            for path in (a, b):
                if path.exists():
                    path.unlink()

    def apply(self, alpha, beta, alpha_st, beta_st):
        """Return 'shared', 'differs' or 'changed'."""
        if not filecmp.cmp(alpha, beta, shallow=False):
            return "differs"
        self.counter += 1
        temporary = self.tmpdir / f"{os.getpid()}-{self.counter}"
        try:
            self.clone(alpha, temporary)
            # Keep the Linux copy's own mode and times: git in the container
            # caches them in its index.
            os.chmod(temporary, stat.S_IMODE(beta_st.st_mode))
            os.utime(temporary, ns=(beta_st.st_atime_ns, beta_st.st_mtime_ns))
            if (identity(os.lstat(alpha)) != identity(alpha_st)
                    or identity(os.lstat(beta)) != identity(beta_st)):
                return "changed"
            os.rename(temporary, beta)
            return "shared"
        finally:
            if os.path.lexists(temporary):
                os.unlink(temporary)


class KernelDedupe:
    """Linux: FIDEDUPERANGE; nothing is replaced, the kernel verifies contents."""
    name = "FIDEDUPERANGE"

    def __init__(self, tmpdir, dedupe=dedupe_range):
        self.tmpdir = Path(tmpdir)
        self.dedupe = dedupe

    def probe(self):
        a, b = self.tmpdir / "probe-a", self.tmpdir / "probe-b"
        try:
            a.write_bytes(b"x" * 65536)
            b.write_bytes(b"x" * 65536)
            self.dedupe(a, b, 65536)
        except OSError as error:
            raise Unsupported(f"cannot deduplicate files in {self.tmpdir}: {error}") from None
        finally:
            for path in (a, b):
                if path.exists():
                    path.unlink()

    def apply(self, alpha, beta, alpha_st, beta_st):
        return "shared" if self.dedupe(alpha, beta, alpha_st.st_size) else "differs"


def default_strategy(tmpdir):
    if sys.platform == "darwin":
        return CloneReplace(tmpdir)
    if sys.platform.startswith("linux"):
        return KernelDedupe(tmpdir)
    raise Unsupported(f"unsupported platform: {sys.platform}")


# ---- selection ----------------------------------------------------------------

def candidates(beta_root):
    """Relative paths of regular files in the Linux tree outside the ignore list."""
    for directory, dirs, files in os.walk(beta_root):
        rel_dir = os.path.relpath(directory, beta_root)
        rel_dir = "" if rel_dir == "." else rel_dir.replace(os.sep, "/")
        dirs[:] = [d for d in dirs if d != ".git"
                   and not sync.is_ignored(f"{rel_dir}/{d}" if rel_dir else d)]
        for name in files:
            rel = f"{rel_dir}/{name}" if rel_dir else name
            if name != ".git" and not sync.is_ignored(rel):
                yield rel


def deduplicate(alpha_root, beta_root, strategy, state, *, min_size=MIB,
                quiet_seconds=24 * 3600, dry_run=False, now=None, log=print):
    """Return (stats, new_state)."""
    now = now if now is not None else time.time()
    stats = {"examined": 0, "shared": 0, "shared_bytes": 0, "already": 0, "already_bytes": 0,
             "small": 0, "missing": 0, "size_differs": 0, "recent": 0, "differs": 0,
             "changed": 0, "errors": 0, "would_share": 0, "would_share_bytes": 0}
    new_state = {}
    for rel in candidates(beta_root):
        alpha, beta = os.path.join(alpha_root, rel), os.path.join(beta_root, rel)
        try:
            beta_st = os.lstat(beta)
            if beta_st.st_size < min_size:
                stats["small"] += 1
                continue
            stats["examined"] += 1
            try:
                alpha_st = os.lstat(alpha)
            except FileNotFoundError:
                stats["missing"] += 1
                continue
            if not (stat.S_ISREG(alpha_st.st_mode) and stat.S_ISREG(beta_st.st_mode)):
                stats["missing"] += 1
                continue
            if alpha_st.st_size != beta_st.st_size:
                stats["size_differs"] += 1
                continue
            if state.get(rel) == state_key(alpha_st, beta_st):
                stats["already"] += 1
                stats["already_bytes"] += beta_st.st_size
                new_state[rel] = state[rel]
                continue
            newest = max(alpha_st.st_mtime, alpha_st.st_ctime, beta_st.st_mtime, beta_st.st_ctime)
            if now - newest < quiet_seconds:
                stats["recent"] += 1
                continue
            if dry_run:
                if filecmp.cmp(alpha, beta, shallow=False):
                    stats["would_share"] += 1
                    stats["would_share_bytes"] += beta_st.st_size
                else:
                    stats["differs"] += 1
                continue
            result = strategy.apply(alpha, beta, alpha_st, beta_st)
            stats[result] += 1
            if result == "shared":
                stats["shared_bytes"] += beta_st.st_size
                new_state[rel] = state_key(os.lstat(alpha), os.lstat(beta))
        except OSError as error:
            stats["errors"] += 1
            log(f"error: {rel}: {error}")
    return stats, new_state


# ---- state, lock, log -----------------------------------------------------------

def load_state(path):
    try:
        data = json.loads(Path(path).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}  # missing or damaged: everything is compared again


def save_state(path, state):
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(state, separators=(",", ":")))
    temporary.replace(path)


def check_layout(alpha_root, beta_root, workdir):
    devices = {os.stat(p).st_dev for p in (alpha_root, beta_root, workdir)}
    if len(devices) != 1:
        raise Unsupported("the two trees and the work directory must be on the same filesystem")


def session_ok():
    exists, healthy, conflicts, problems, _ = sync.session_state()
    if not exists:
        return "the sync session does not exist or the Mutagen daemon is not running"
    if not healthy:
        return "the sync session is paused, halted or disconnected"
    if conflicts or problems:
        return f"the sync session has {conflicts} conflict(s) and {problems} problem section(s)"
    return None


def run(args):
    alpha, beta, workdir = Path(args.alpha), Path(args.beta), Path(args.workdir)
    for path in (alpha, beta):
        if not path.is_dir():
            raise SystemExit(f"Error: {path} does not exist")
    tmpdir = workdir / "dedupe-tmp"
    tmpdir.mkdir(parents=True, exist_ok=True)
    check_layout(alpha, beta, workdir)
    with open(workdir / "dedupe.lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Error: another dedupe run is in progress") from None
        if not args.skip_session_check:
            problem = session_ok()
            if problem:
                print(f"Not deduplicating: {problem}.")
                return 2
        strategy = default_strategy(tmpdir)
        strategy.probe()
        state_path = workdir / "dedupe-state.json"
        free_before = shutil.disk_usage(beta).free
        started = time.time()
        stats, new_state = deduplicate(
            alpha, beta, strategy, load_state(state_path), min_size=args.min_size,
            quiet_seconds=args.quiet_hours * 3600, dry_run=args.dry_run)
        if not args.dry_run:
            save_state(state_path, new_state)
        stats.update(method=strategy.name, dry_run=args.dry_run,
                     seconds=round(time.time() - started, 1),
                     free_gained_bytes=shutil.disk_usage(beta).free - free_before)
    line = time.strftime("%Y-%m-%dT%H:%M:%S") + " " + json.dumps(stats, ensure_ascii=False)
    with open(workdir / "dedupe.log", "a") as log:
        log.write(line + "\n")
    print(line)
    return 1 if stats["errors"] else 0


# ---- schedule (macOS) -----------------------------------------------------------

def plist_path():
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def install(args):
    if sys.platform != "darwin":
        raise SystemExit("Error: install sets up launchd and is macOS only; on Linux use a systemd timer")
    path = plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    WORKDIR.mkdir(parents=True, exist_ok=True)
    path.write_bytes(plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": ["/usr/bin/python3", str(Path(__file__).resolve()), "run"],
        "StartCalendarInterval": {"Hour": args.hour, "Minute": 0},
        "StandardOutPath": str(WORKDIR / "dedupe.launchd.log"),
        "StandardErrorPath": str(WORKDIR / "dedupe.launchd.log"),
        "LowPriorityIO": True,
        "Nice": 10,
    }))
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", domain, str(path)], capture_output=True)
    subprocess.run(["launchctl", "bootstrap", domain, str(path)], check=True)
    print(f"Installed {path}: runs daily at {args.hour:02d}:00.")
    return 0


def uninstall(args):
    path = plist_path()
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(path)], capture_output=True)
    if path.exists():
        path.unlink()
    print(f"Removed {path}.")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    run_parser = sub.add_parser("run")
    run_parser.add_argument("--dry-run", action="store_true", help="report what would be shared")
    run_parser.add_argument("--min-size", type=int, default=MIB, help="skip smaller files (bytes)")
    run_parser.add_argument("--quiet-hours", type=float, default=24,
                            help="skip files changed on either side more recently")
    run_parser.add_argument("--alpha", default=str(sync.WORKSPACE))
    run_parser.add_argument("--beta", default=str(sync.LINUX_WORKSPACE))
    run_parser.add_argument("--workdir", default=str(WORKDIR))
    run_parser.add_argument("--skip-session-check", action="store_true", help=argparse.SUPPRESS)
    install_parser = sub.add_parser("install")
    install_parser.add_argument("--hour", type=int, default=4)
    sub.add_parser("uninstall")
    args = parser.parse_args(argv)
    try:
        return {"run": run, "install": install, "uninstall": uninstall}[args.command](args)
    except Unsupported as error:
        print(f"Error: {error}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
