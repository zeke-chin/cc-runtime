"""git-store.py, workspace-sync.py and check-sync-ignores.py against temporary trees."""
import importlib.util
import io
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
from contextlib import redirect_stdout

ROOT = Path(__file__).resolve().parents[1]


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


store = load("git_store", "git-store.py")
sync = load("workspace_sync", "workspace-sync.py")
checker = load("check_sync_ignores", "check-sync-ignores.py")

GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
       "-c", "init.defaultBranch=main", "-c", "protocol.file.allow=always"]


def git(cwd, *args):
    return subprocess.run(GIT + list(args), cwd=cwd, check=True, capture_output=True, text=True).stdout


def age(path, seconds=600):
    """Make a repository's git data look idle."""
    past = time.time() - seconds
    for directory, dirs, files in os.walk(path):
        for name in dirs + files:
            os.utime(os.path.join(directory, name), (past, past), follow_symlinks=False)
    os.utime(path, (past, past))


class GitStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        base = Path(self.temporary.name)
        self.mac, self.linux, self.store = base / "mac", base / "linux", base / "git"
        for path in (self.mac, self.linux, self.store):
            path.mkdir()
        self.layout = store.Layout(self.mac, self.linux, self.store)

    def tearDown(self):
        self.temporary.cleanup()

    def repo(self, root, rel, commit=True):
        path = root / rel
        path.mkdir(parents=True)
        git(path, "init", "-q")
        (path / "a.txt").write_text("a\n")
        if commit:
            git(path, "add", "a.txt")
            git(path, "commit", "-qm", "init")
        age(path / ".git")
        return path

    def mirror(self, rel):
        """What the sync session does: copy sources and the .git symlink to the other tree."""
        for src, dst in ((self.mac, self.linux), (self.linux, self.mac)):
            if (src / rel).is_dir() and not (dst / rel).exists():
                shutil.copytree(src / rel, dst / rel, symlinks=True,
                                ignore=lambda d, names: [n for n in names if n == ".git"
                                                         and not os.path.islink(os.path.join(d, n))])

    def test_ignore_matching(self):
        self.assertTrue(sync.is_ignored("a/b/node_modules"))
        self.assertTrue(sync.is_ignored("a/node_modules/x/y.js"))
        self.assertTrue(sync.is_ignored("github_work/cc-runtime/dev-runtimes/dev-CA/home"))
        self.assertTrue(sync.is_ignored("rust_work/.cache"))
        self.assertFalse(sync.is_ignored("other/rust_work/.cache"))
        self.assertFalse(sync.is_ignored("a/bin/tool"))
        self.assertFalse(sync.is_ignored("a/node_modules_backup"))
        command = sync.create_command(Path("/a"), Path("/b"), "x")
        self.assertIn("--ignore-vcs", command)
        self.assertIn("--symlink-mode=posix-raw", command)
        self.assertNotIn("--ignore=.git", command)

    def test_externalize_from_mac_shares_git_data_with_linux(self):
        self.repo(self.mac, "g/foo")
        self.mirror("g/foo")
        self.assertFalse((self.linux / "g/foo/.git").exists())
        changed, message = store.externalize(self.layout, "g/foo")
        self.assertTrue(changed, message)
        link = self.mac / "g/foo/.git"
        self.assertTrue(link.is_symlink())
        self.assertEqual(Path(os.readlink(link)), self.store / "g/foo.git")
        os.symlink(os.readlink(link), self.linux / "g/foo/.git")  # the sync copies the link
        self.assertEqual(git(self.linux / "g/foo", "rev-parse", "--show-toplevel").strip(),
                         str((self.linux / "g/foo").resolve()))
        self.assertEqual(git(self.linux / "g/foo", "status", "--porcelain"), "")
        (self.linux / "g/foo/b.txt").write_text("b\n")
        git(self.linux / "g/foo", "add", "b.txt")
        git(self.linux / "g/foo", "commit", "-qm", "from linux")
        self.assertIn("from linux", git(self.mac / "g/foo", "log", "--oneline"))
        self.assertEqual(store.status(self.layout, out=lambda *_: None), 0)

    def test_externalize_repository_created_in_linux_tree(self):
        self.repo(self.linux, "new")
        changed, message = store.externalize(self.layout, self.layout.rel(str(self.linux / "new")))
        self.assertTrue(changed, message)
        self.assertTrue((self.linux / "new/.git").is_symlink())
        self.assertIn("linux tree", message)

    def test_guards(self):
        self.repo(self.mac, "both"); self.repo(self.linux, "both")
        self.assertIn("both trees", store.externalize(self.layout, "both")[1])
        busy = self.repo(self.mac, "busy")
        (busy / ".git/index.lock").write_text("")
        self.assertIn("lock file", store.externalize(self.layout, "busy")[1])
        tool = self.repo(self.mac, "tool")
        (tool / ".git/cursor/crepe").mkdir(parents=True)
        (tool / ".git/cursor/crepe/build.lock").write_text("")  # Cursor's own file, not git's
        age(tool / ".git")
        self.assertTrue(store.externalize(self.layout, "tool")[0])
        reflock = self.repo(self.mac, "reflock")
        (reflock / ".git/refs/heads/main.lock").write_text("")
        age(reflock / ".git")
        self.assertIn("refs/heads/main.lock", store.externalize(self.layout, "reflock")[1])
        fresh = self.repo(self.mac, "fresh")
        (fresh / "a.txt").write_text("changed\n")
        git(fresh, "add", "a.txt")
        self.assertIn("changed", store.externalize(self.layout, "fresh")[1])
        self.repo(self.mac, "empty", commit=False)
        self.assertIn("no commit", store.externalize(self.layout, "empty")[1])
        self.repo(self.mac, "taken")
        (self.store / "taken.git").mkdir()
        self.assertIn("already exists", store.externalize(self.layout, "taken")[1])
        wt = self.repo(self.mac, "wt")
        git(wt, "worktree", "add", "-q", "-b", "side", str(self.mac / "wt-side"))
        age(wt / ".git")
        self.assertIn("worktrees", store.externalize(self.layout, "wt")[1])
        lib = self.repo(self.mac, "lib")
        sup = self.repo(self.mac, "sup")
        git(sup, "submodule", "add", "-q", str(lib), "lib")
        git(sup, "commit", "-qm", "sub")
        age(sup / ".git")
        self.assertIn("submodules", store.externalize(self.layout, "sup")[1])
        self.assertEqual(sorted(self.store.iterdir()), [self.store / "taken.git", self.store / "tool.git"])

    def test_restore_moves_data_back_and_removes_both_links(self):
        self.repo(self.mac, "g/foo")
        self.mirror("g/foo")
        store.externalize(self.layout, "g/foo")
        os.symlink(os.readlink(self.mac / "g/foo/.git"), self.linux / "g/foo/.git")
        age(self.store / "g/foo.git")
        changed, message = store.restore(self.layout, "g/foo")
        self.assertTrue(changed, message)
        self.assertTrue((self.mac / "g/foo/.git").is_dir())
        self.assertFalse((self.mac / "g/foo/.git").is_symlink())
        self.assertFalse(os.path.lexists(self.linux / "g/foo/.git"))
        self.assertEqual(list(self.store.iterdir()), [])  # empty parents removed
        self.assertEqual(git(self.mac / "g/foo", "status", "--porcelain"), "")

    def test_status_reports_copy_rename_delete_and_dangling(self):
        for rel in ("copy", "moved", "gone", "dangling"):
            self.repo(self.mac, rel)
            store.externalize(self.layout, rel)
        shutil.copytree(self.mac / "copy", self.mac / "copy2", symlinks=True)
        (self.mac / "moved").rename(self.mac / "moved2")
        shutil.rmtree(self.mac / "gone")
        shutil.rmtree(self.store / "dangling.git")
        lines = []
        anomalies = store.status(self.layout, out=lines.append)
        text = "\n".join(lines)
        self.assertIn("shared by several working trees: copy, copy2", text)
        self.assertIn("moved2: link target", text)
        self.assertIn("gone.git: no working tree links to it", text)
        self.assertIn("dangling: link target", text)
        self.assertGreaterEqual(anomalies, 4)

    def test_scan_skips_ignored_directories(self):
        self.repo(self.mac, "app")
        self.repo(self.mac, "app/node_modules/dep")
        self.assertEqual(store.scan_tree(self.mac), {"app": "dir"})

    def test_cli_requires_targets(self):
        self.repo(self.mac, "x")
        with self.assertRaises(SystemExit):
            with redirect_stdout(io.StringIO()):
                store.main(["--workspace", str(self.mac), "--linux", str(self.linux),
                            "--store", str(self.store), "externalize"])
        with redirect_stdout(io.StringIO()):
            code = store.main(["--workspace", str(self.mac), "--linux", str(self.linux),
                               "--store", str(self.store), "externalize", "--all", "--dry-run"])
        self.assertEqual(code, 0)
        self.assertTrue((self.mac / "x/.git").is_dir())

    def test_check_sync_ignores(self):
        self.assertEqual(checker.check_layout(self.mac, self.linux, self.store), [])
        self.assertTrue(checker.check_layout(self.mac, self.mac / "inner", self.store))
        app = self.repo(self.mac, "app")
        (app / "target").mkdir()
        (app / "target/keep.txt").write_text("tracked\n")
        git(app, "add", "-f", "target/keep.txt")
        git(app, "commit", "-qm", "track ignored")
        self.assertEqual(checker.tracked_violations(self.mac), [("app", "target/keep.txt")])
        (app / ".venv-old").mkdir()
        (app / ".venv-old/pyvenv.cfg").write_text("")
        (app / ".venv").mkdir()
        (app / ".venv/pyvenv.cfg").write_text("")
        self.assertEqual(checker.stray_virtualenvs(self.mac), ["app/.venv-old"])


    def test_container_state_mounts(self):
        ws = Path("/w")
        containers = [{"Name": "/db", "Mounts": [
            {"Type": "bind", "RW": True, "Source": "/w/app/data/pg"},
            {"Type": "bind", "RW": False, "Source": "/w/app/schema.sql"},
            {"Type": "bind", "RW": True, "Source": "/w/github_work/cc-runtime/tailscale/state"},
            {"Type": "bind", "RW": True, "Source": "/w"},
            {"Type": "volume", "RW": True, "Source": "/var/lib/docker/x"}]}]
        self.assertEqual(checker.container_state_mounts(ws, inspect=lambda: containers),
                         [("db", "app/data/pg")])

if __name__ == "__main__":
    unittest.main()
