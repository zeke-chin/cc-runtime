"""workspace-dedupe.py: file selection, safety checks and the Linux kernel path."""
import importlib.util
import io
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("workspace_dedupe", ROOT / "scripts/workspace-dedupe.py")
dedupe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dedupe)

MIB = 1 << 20
DAY = 24 * 3600


def write(path, data, age=2 * DAY):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    past = time.time() - age
    os.utime(path, (past, past))


class CopyStrategy(dedupe.CloneReplace):
    """CloneReplace with a plain copy as the 'clone', for filesystems without clones."""
    def __init__(self, tmpdir, clone=None):
        super().__init__(tmpdir, clone=clone or (lambda src, dst: shutil.copyfile(src, dst)))


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        base = Path(self.temporary.name)
        self.a, self.b, self.tmp = base / "mac", base / "linux", base / "work"
        for path in (self.a, self.b, self.tmp):
            path.mkdir()
        self.strategy = CopyStrategy(self.tmp)

    def tearDown(self):
        self.temporary.cleanup()

    def both(self, rel, data, age=2 * DAY, beta_data=None):
        write(self.a / rel, data, age)
        write(self.b / rel, beta_data if beta_data is not None else data, age)

    def run_once(self, state=None, **kwargs):
        # ctime cannot be set back; treat it as current unless a test needs otherwise.
        kwargs.setdefault("quiet_seconds", 0)
        return dedupe.deduplicate(self.a, self.b, self.strategy, state or {}, log=lambda *_: None, **kwargs)

    def test_selects_only_eligible_pairs(self):
        big = os.urandom(2 * MIB)
        self.both("p/data.bin", big)
        self.both("p/small.txt", b"tiny")
        self.both("p/node_modules/dep.bin", big)            # ignored: never examined
        self.both("p/changed.bin", big, beta_data=os.urandom(2 * MIB))
        self.both("p/size.bin", big, beta_data=big + b"x")
        write(self.b / "p/only-linux.bin", big)
        stats, state = self.run_once()
        self.assertEqual((stats["shared"], stats["differs"], stats["size_differs"], stats["missing"]),
                         (1, 1, 1, 1))
        self.assertEqual(stats["small"], 1)
        self.assertEqual(list(state), ["p/data.bin"])
        self.assertEqual((self.b / "p/data.bin").read_bytes(), big)

    def test_recently_changed_files_wait(self):
        self.both("p/data.bin", os.urandom(2 * MIB))
        stats, state = self.run_once(quiet_seconds=DAY)  # ctime is now: too recent
        self.assertEqual((stats["recent"], stats["shared"]), (1, 0))
        stats, _ = self.run_once(quiet_seconds=DAY, now=time.time() + 2 * DAY)
        self.assertEqual(stats["shared"], 1)

    def test_state_skips_already_shared_pairs_and_notices_changes(self):
        self.both("p/data.bin", os.urandom(2 * MIB))
        _, state = self.run_once()
        stats, state = self.run_once(state=state)
        self.assertEqual((stats["already"], stats["shared"]), (1, 0))
        write(self.a / "p/data.bin", os.urandom(2 * MIB))  # the Mac side changed
        stats, _ = self.run_once(state=state)
        self.assertEqual((stats["already"], stats["differs"]), (0, 1))

    def test_dry_run_changes_nothing(self):
        self.both("p/data.bin", os.urandom(2 * MIB))
        before = os.stat(self.b / "p/data.bin").st_ino
        stats, state = self.run_once(dry_run=True)
        self.assertEqual((stats["would_share"], stats["would_share_bytes"], state), (1, 2 * MIB, {}))
        self.assertEqual(os.stat(self.b / "p/data.bin").st_ino, before)

    def test_keeps_linux_mode_and_mtime(self):
        self.both("p/tool.bin", os.urandom(2 * MIB))
        os.chmod(self.b / "p/tool.bin", 0o755)
        os.chmod(self.a / "p/tool.bin", 0o600)
        os.utime(self.b / "p/tool.bin", (1_700_000_000, 1_700_000_000))
        self.run_once()
        st = os.stat(self.b / "p/tool.bin")
        self.assertEqual((st.st_mode & 0o777, int(st.st_mtime)), (0o755, 1_700_000_000))
        self.assertEqual(os.stat(self.a / "p/tool.bin").st_mode & 0o777, 0o600)  # Mac side untouched

    def test_gives_up_when_a_file_changes_during_the_clone(self):
        data = os.urandom(2 * MIB)
        self.both("p/data.bin", data)
        target = self.b / "p/data.bin"

        def racing_clone(src, dst):
            shutil.copyfile(src, dst)
            with open(target, "ab") as f:  # someone writes the Linux copy meanwhile
                f.write(b"new")
        self.strategy = CopyStrategy(self.tmp, clone=racing_clone)
        stats, state = self.run_once()
        self.assertEqual((stats["changed"], stats["shared"], state), (1, 0, {}))
        self.assertEqual(target.read_bytes(), data + b"new")  # the write survives
        self.assertEqual(list(self.tmp.iterdir()), [])        # no temporary left behind

    def test_git_directories_are_skipped(self):
        self.both("p/.git/objects/pack/big.pack", os.urandom(2 * MIB))
        stats, _ = self.run_once()
        self.assertEqual(stats["examined"], 0)


def dedupe_capable_dir():
    """A directory where FIDEDUPERANGE works (e.g. Btrfs), or None."""
    for base in filter(None, [os.environ.get("CC_DEDUPE_TEST_DIR"), str(Path.home() / ".cache"),
                              tempfile.gettempdir()]):
        if not os.path.isdir(base) or not os.access(base, os.W_OK):
            continue
        directory = tempfile.mkdtemp(prefix="cc-dedupe-test-", dir=base)
        try:
            dedupe.KernelDedupe(directory).probe()
            return directory
        except dedupe.Unsupported:
            shutil.rmtree(directory)
    return None


@unittest.skipUnless(os.uname().sysname == "Linux", "Linux kernel interface")
class KernelDedupeTests(unittest.TestCase):
    def setUp(self):
        self.directory = dedupe_capable_dir()
        if not self.directory:
            self.skipTest("no filesystem with FIDEDUPERANGE available")

    def tearDown(self):
        shutil.rmtree(self.directory)

    def extents(self, path):
        try:
            out = subprocess.run(["filefrag", "-v", path], capture_output=True, text=True, check=True).stdout
        except (OSError, subprocess.CalledProcessError):
            return None
        rows = [line.split() for line in out.splitlines() if line.strip()[:1].isdigit() and ":" in line]
        return [row[3] for row in rows]  # physical start of each extent

    def test_shares_identical_files_and_refuses_different_ones(self):
        a, b, c = (os.path.join(self.directory, name) for name in "abc")
        data = os.urandom(40 * MIB)  # crosses the 16 MiB request limit
        for path in (a, b):
            Path(path).write_bytes(data)
        Path(c).write_bytes(data[:-1] + b"!")
        os.sync()
        self.assertTrue(dedupe.dedupe_range(a, b, len(data)))
        self.assertFalse(dedupe.dedupe_range(a, c, len(data)))
        self.assertEqual(Path(b).read_bytes(), data)
        self.assertEqual(Path(c).read_bytes()[-1:], b"!")
        os.sync()
        ea, eb = self.extents(a), self.extents(b)
        if ea is not None:
            self.assertEqual(ea, eb)  # same physical blocks


@unittest.skipUnless(os.uname().sysname == "Darwin", "macOS clonefile")
class CloneFileTests(unittest.TestCase):
    def test_clone_replaces_linux_copy_with_identical_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            for name in ("mac", "linux", "work"):
                (base / name).mkdir()
            data = os.urandom(8 * MIB)
            write(base / "mac/d.bin", data)
            write(base / "linux/d.bin", data)
            os.chmod(base / "linux/d.bin", 0o640)
            before = os.stat(base / "linux/d.bin")
            strategy = dedupe.CloneReplace(base / "work")
            strategy.probe()
            stats, _ = dedupe.deduplicate(base / "mac", base / "linux", strategy, {},
                                          quiet_seconds=0, log=lambda *_: None)
            after = os.stat(base / "linux/d.bin")
            self.assertEqual(stats["shared"], 1)
            self.assertNotEqual(after.st_ino, before.st_ino)
            self.assertEqual((after.st_mode & 0o777, after.st_mtime_ns), (0o640, before.st_mtime_ns))
            self.assertEqual((base / "linux/d.bin").read_bytes(), data)
            self.assertEqual(list((base / "work").iterdir()), [])

class CommandTests(unittest.TestCase):
    def test_unsupported_filesystem_exits_without_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            for name in ("mac", "linux", "work"):
                (base / name).mkdir()
            write(base / "mac/x.bin", b"x" * 2 * MIB)
            write(base / "linux/x.bin", b"x" * 2 * MIB)
            inode = os.stat(base / "linux/x.bin").st_ino

            class NoClones(CopyStrategy):
                def probe(self):  # what ext4 or NTFS report
                    raise dedupe.Unsupported("cannot clone files: Operation not supported")
            err = io.StringIO()
            with patch.object(dedupe, "default_strategy", NoClones), \
                    redirect_stdout(io.StringIO()), redirect_stderr(err):
                code = dedupe.main(["run", "--alpha", str(base / "mac"), "--beta", str(base / "linux"),
                                    "--workdir", str(base / "work"), "--skip-session-check"])
            self.assertEqual(code, 3)
            self.assertIn("Error:", err.getvalue())
            self.assertEqual(os.stat(base / "linux/x.bin").st_ino, inode)


if __name__ == "__main__":
    unittest.main()
