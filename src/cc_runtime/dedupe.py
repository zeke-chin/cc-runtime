"""Share disk blocks between the Mac workspace and its Linux mirror.

Mutagen writes every file it synchronizes as new data, so the two trees take
twice the space. Files identical on both sides at the same relative path are
made to share the Mac copy's blocks (a filesystem clone). Contents never
change; only the Linux side is touched.

  macOS (APFS): clonefile() to a temporary name outside the synced trees,
                re-check both files, then rename over the Linux copy.
  Linux (Btrfs, XFS, bcachefs, ZFS 2.2+): FIDEDUPERANGE; the kernel compares
                the ranges under lock and shares them only if identical.
"""
from __future__ import annotations

import ctypes
import errno
import fcntl
import filecmp
import json
import os
import shutil
import stat
import struct
import sys
import time
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

from .config import Config, IgnoreList

MIB = 1 << 20

# Linux: _IOWR(0x94, 54, struct file_dedupe_range) with a 24-byte header.
FIDEDUPERANGE = 0xC0189436
DEDUPE_CHUNK = 16 * MIB  # Btrfs caps a single request at 16 MiB
_RANGE = "=QQHHI"   # src_offset, src_length, dest_count, reserved1, reserved2
_INFO = "=qQQiI"    # dest_fd, dest_offset, bytes_deduped, status, reserved
FILE_DEDUPE_RANGE_DIFFERS = 1


class Unsupported(Exception):
    pass


def identity(st) -> tuple:
    """What must not change between checking a file and replacing it."""
    return (st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def state_key(alpha_st, beta_st) -> list:
    return [alpha_st.st_ino, alpha_st.st_size, alpha_st.st_mtime_ns,
            beta_st.st_ino, beta_st.st_size, beta_st.st_mtime_ns]


_clonefile = None


def clonefile(src, dst) -> None:
    global _clonefile
    if _clonefile is None:
        libc = ctypes.CDLL(None, use_errno=True)
        _clonefile = libc.clonefile
        _clonefile.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint32]
        _clonefile.restype = ctypes.c_int
    if _clonefile(os.fsencode(str(src)), os.fsencode(str(dst)), 0) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), str(dst))


def dedupe_range(src, dst, length: int) -> bool:
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


class CloneReplace:
    """macOS: clone the Mac file and rename it over the Linux copy."""
    name = "clonefile"

    def __init__(self, tmpdir, clone: Callable = clonefile):
        self.tmpdir = Path(tmpdir)
        self.clone = clone
        self.counter = 0

    def probe(self) -> None:
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

    def apply(self, alpha, beta, alpha_st, beta_st) -> str:
        """Return 'shared', 'differs' or 'changed'."""
        if not filecmp.cmp(alpha, beta, shallow=False):
            return "differs"
        self.counter += 1
        temporary = self.tmpdir / f"{os.getpid()}-{self.counter}"
        try:
            self.clone(alpha, temporary)
            # Keep the Linux copy's own mode and times: git in the container caches them.
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

    def __init__(self, tmpdir, dedupe: Callable = dedupe_range):
        self.tmpdir = Path(tmpdir)
        self.dedupe = dedupe

    def probe(self) -> None:
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

    def apply(self, alpha, beta, alpha_st, beta_st) -> str:
        return "shared" if self.dedupe(alpha, beta, alpha_st.st_size) else "differs"


def default_strategy(tmpdir):
    if sys.platform == "darwin":
        return CloneReplace(tmpdir)
    if sys.platform.startswith("linux"):
        return KernelDedupe(tmpdir)
    raise Unsupported(f"unsupported platform: {sys.platform}")


def candidates(beta_root, ignores: IgnoreList):
    """Relative paths of regular files in the Linux tree outside the ignore list."""
    for directory, dirs, files in os.walk(beta_root):
        rel_dir = os.path.relpath(directory, beta_root)
        rel_dir = "" if rel_dir == "." else rel_dir.replace(os.sep, "/")
        dirs[:] = [d for d in dirs if d != ".git"
                   and not ignores.matches(f"{rel_dir}/{d}" if rel_dir else d)]
        for name in files:
            rel = f"{rel_dir}/{name}" if rel_dir else name
            if name != ".git" and not ignores.matches(rel):
                yield rel


def deduplicate(alpha_root, beta_root, strategy, state: Dict[str, list], ignores: IgnoreList, *,
                min_size: int = MIB, quiet_seconds: float = 24 * 3600, dry_run: bool = False,
                now: Optional[float] = None, log: Callable = print) -> Tuple[dict, Dict[str, list]]:
    now = now if now is not None else time.time()
    stats = {"examined": 0, "shared": 0, "shared_bytes": 0, "already": 0, "already_bytes": 0,
             "small": 0, "missing": 0, "size_differs": 0, "recent": 0, "differs": 0,
             "changed": 0, "errors": 0, "would_share": 0, "would_share_bytes": 0}
    new_state: Dict[str, list] = {}
    for rel in candidates(beta_root, ignores):
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


def load_state(path: Path) -> Dict[str, list]:
    try:
        data = json.loads(Path(path).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}  # missing or damaged: everything is compared again


def save_state(path: Path, state: Dict[str, list]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(state, separators=(",", ":")))
    temporary.replace(path)


def run(cfg: Config, *, dry_run: bool = False, min_size: int = MIB, quiet_hours: float = 24,
        alpha: Optional[Path] = None, beta: Optional[Path] = None, strategy=None,
        log: Callable = print) -> dict:
    """Deduplicate once and return the statistics. Raises Unsupported."""
    alpha = Path(alpha or cfg.workspace)
    beta = Path(beta or cfg.linux_workspace)
    for path in (alpha, beta):
        if not path.is_dir():
            raise Unsupported(f"{path} does not exist")
    tmpdir = cfg.tmp_dir / "dedupe"
    tmpdir.mkdir(parents=True, exist_ok=True)
    if len({os.stat(p).st_dev for p in (alpha, beta, tmpdir)}) != 1:
        raise Unsupported("the two trees and the runtime directory must be on the same filesystem")
    strategy = strategy or default_strategy(tmpdir)
    strategy.probe()
    state_path = cfg.state_dir / "dedupe.json"
    free_before = shutil.disk_usage(beta).free
    started = time.time()
    stats, new_state = deduplicate(alpha, beta, strategy, load_state(state_path), cfg.ignores(),
                                   min_size=min_size, quiet_seconds=quiet_hours * 3600,
                                   dry_run=dry_run, log=log)
    if not dry_run:
        save_state(state_path, new_state)
    stats.update(method=strategy.name, dry_run=dry_run, seconds=round(time.time() - started, 1),
                 free_gained_bytes=shutil.disk_usage(beta).free - free_before)
    return stats
