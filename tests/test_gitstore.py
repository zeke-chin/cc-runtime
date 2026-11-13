import os
import shutil
import tempfile
import unittest
from pathlib import Path

from helpers import age, git, make_config

from cc_runtime import checks, config, gitstore, sync


class GitStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        base = Path(self.temporary.name)
        self.mac, self.linux, self.store = base / "mac", base / "linux", base / "git"
        for path in (self.mac, self.linux, self.store):
            path.mkdir()
        self.layout = gitstore.Layout(self.mac, self.linux, self.store,
                                      config.IgnoreList(config.NAME_IGNORES))

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

    def test_externalize_from_mac_shares_git_data_with_linux(self):
        self.repo(self.mac, "g/foo")
        self.mirror("g/foo")
        self.assertFalse((self.linux / "g/foo/.git").exists())
        changed, message = gitstore.externalize(self.layout, "g/foo")
        self.assertTrue(changed, message)
        link = self.mac / "g/foo/.git"
        self.assertEqual(Path(os.readlink(link)), self.store / "g/foo.git")
        os.symlink(os.readlink(link), self.linux / "g/foo/.git")  # the sync copies the link
        self.assertEqual(git(self.linux / "g/foo", "rev-parse", "--show-toplevel").strip(),
                         str((self.linux / "g/foo").resolve()))
        self.assertEqual(git(self.linux / "g/foo", "status", "--porcelain"), "")
        (self.linux / "g/foo/b.txt").write_text("b\n")
        git(self.linux / "g/foo", "add", "b.txt")
        git(self.linux / "g/foo", "commit", "-qm", "from linux")
        self.assertIn("from linux", git(self.mac / "g/foo", "log", "--oneline"))
        self.assertEqual(gitstore.status(self.layout).anomalies, [])

    def test_externalize_repository_created_in_linux_tree(self):
        self.repo(self.linux, "new")
        changed, message = gitstore.externalize(self.layout, self.layout.rel(str(self.linux / "new")))
        self.assertTrue(changed, message)
        self.assertTrue((self.linux / "new/.git").is_symlink())
        self.assertIn("linux tree", message)

    def test_guards(self):
        self.repo(self.mac, "both")
        self.repo(self.linux, "both")
        self.assertIn("both trees", gitstore.externalize(self.layout, "both")[1])
        busy = self.repo(self.mac, "busy")
        (busy / ".git/index.lock").write_text("")
        self.assertIn("lock file", gitstore.externalize(self.layout, "busy")[1])
        tool = self.repo(self.mac, "tool")
        (tool / ".git/cursor/crepe").mkdir(parents=True)
        (tool / ".git/cursor/crepe/build.lock").write_text("")  # an editor's file, not git's
        age(tool / ".git")
        self.assertTrue(gitstore.externalize(self.layout, "tool")[0])
        reflock = self.repo(self.mac, "reflock")
        (reflock / ".git/refs/heads/main.lock").write_text("")
        age(reflock / ".git")
        self.assertIn("refs/heads/main.lock", gitstore.externalize(self.layout, "reflock")[1])
        fresh = self.repo(self.mac, "fresh")
        (fresh / "a.txt").write_text("changed\n")
        git(fresh, "add", "a.txt")
        self.assertIn("changed", gitstore.externalize(self.layout, "fresh")[1])
        self.repo(self.mac, "empty", commit=False)
        self.assertIn("no commit", gitstore.externalize(self.layout, "empty")[1])
        self.repo(self.mac, "taken")
        (self.store / "taken.git").mkdir()
        self.assertIn("already exists", gitstore.externalize(self.layout, "taken")[1])
        wt = self.repo(self.mac, "wt")
        git(wt, "worktree", "add", "-q", "-b", "side", str(self.mac / "wt-side"))
        age(wt / ".git")
        self.assertIn("worktrees", gitstore.externalize(self.layout, "wt")[1])
        lib = self.repo(self.mac, "lib")
        sup = self.repo(self.mac, "sup")
        git(sup, "submodule", "add", "-q", str(lib), "lib")
        git(sup, "commit", "-qm", "sub")
        age(sup / ".git")
        self.assertIn("submodules", gitstore.externalize(self.layout, "sup")[1])
        self.assertEqual(sorted(self.store.iterdir()), [self.store / "taken.git", self.store / "tool.git"])

    def test_restore_moves_data_back_and_removes_both_links(self):
        self.repo(self.mac, "g/foo")
        self.mirror("g/foo")
        gitstore.externalize(self.layout, "g/foo")
        os.symlink(os.readlink(self.mac / "g/foo/.git"), self.linux / "g/foo/.git")
        age(self.store / "g/foo.git")
        changed, message = gitstore.restore(self.layout, "g/foo")
        self.assertTrue(changed, message)
        self.assertTrue((self.mac / "g/foo/.git").is_dir())
        self.assertFalse((self.mac / "g/foo/.git").is_symlink())
        self.assertFalse(os.path.lexists(self.linux / "g/foo/.git"))
        self.assertEqual(list(self.store.iterdir()), [])  # empty parents removed
        self.assertEqual(git(self.mac / "g/foo", "status", "--porcelain"), "")

    def test_status_reports_copy_rename_delete_and_dangling(self):
        for rel in ("copy", "moved", "gone", "dangling"):
            self.repo(self.mac, rel)
            gitstore.externalize(self.layout, rel)
        shutil.copytree(self.mac / "copy", self.mac / "copy2", symlinks=True)
        (self.mac / "moved").rename(self.mac / "moved2")
        shutil.rmtree(self.mac / "gone")
        shutil.rmtree(self.store / "dangling.git")
        text = "\n".join(gitstore.status(self.layout).anomalies)
        self.assertIn("shared by several working trees: copy, copy2", text)
        self.assertIn("moved2: link target", text)
        self.assertIn("gone.git: no working tree links to it", text)
        self.assertIn("dangling: link target", text)

    def test_scan_skips_ignored_directories(self):
        self.repo(self.mac, "app")
        self.repo(self.mac, "app/node_modules/dep")
        self.assertEqual(gitstore.scan_tree(self.mac, self.layout.ignores), {"app": "dir"})


class TreeAndCheckTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.cfg = make_config(self.base)
        self.mac, self.linux = self.cfg.workspace, self.cfg.linux_workspace
        self.ignores = config.IgnoreList(config.NAME_IGNORES)

    def tearDown(self):
        self.temporary.cleanup()

    def test_compare_trees(self):
        for root in (self.mac, self.linux):
            (root / "p/node_modules").mkdir(parents=True)
            (root / "p/a.txt").write_text("same\n")
            os.symlink("a.txt", root / "p/l")
        (self.mac / "p/node_modules/x").write_text("mac only, ignored\n")
        (self.mac / "p/.git").mkdir()  # a real .git directory is never synchronized
        self.assertEqual(sync.compare(str(self.mac), str(self.linux), self.ignores, content=True), [])
        (self.linux / "p/a.txt").write_text("diff\n")
        (self.linux / "p/b.txt").write_text("b\n")
        self.assertEqual(sync.compare(str(self.mac), str(self.linux), self.ignores, content=True),
                         [f"only in {self.linux}: p/b.txt", "content differs: p/a.txt"])

    def test_layout_and_tracked_files_and_virtualenvs(self):
        self.assertEqual(checks.layout_errors(self.cfg), [])
        nested = make_config(self.base / "n")
        object.__setattr__(nested, "runtime", nested.workspace / "inside")
        self.assertTrue(checks.layout_errors(nested))
        app = self.mac / "app"
        app.mkdir()
        git(app, "init", "-q")
        (app / "target").mkdir()
        (app / "target/keep.txt").write_text("tracked\n")
        git(app, "add", "-f", "target/keep.txt")
        git(app, "commit", "-qm", "track ignored")
        self.assertEqual(checks.tracked_violations(self.mac, self.ignores), [("app", "target/keep.txt")])
        for name in (".venv-old", ".venv"):
            (app / name).mkdir()
            (app / name / "pyvenv.cfg").write_text("")
        self.assertEqual(checks.stray_virtualenvs(self.mac, self.ignores), ["app/.venv-old"])

    def test_container_state_mounts(self):
        ignores = config.IgnoreList(["/github_work/cc-runtime/tailscale/state/"])
        containers = [{"Name": "/db", "Mounts": [
            {"Type": "bind", "RW": True, "Source": "/w/app/data/pg"},
            {"Type": "bind", "RW": False, "Source": "/w/app/schema.sql"},
            {"Type": "bind", "RW": True, "Source": "/w/github_work/cc-runtime/tailscale/state"},
            {"Type": "bind", "RW": True, "Source": "/w"},
            {"Type": "volume", "RW": True, "Source": "/var/lib/docker/x"}]}]
        self.assertEqual(checks.container_state_mounts(Path("/w"), ignores, inspect=lambda: containers),
                         [("db", "app/data/pg")])


if __name__ == "__main__":
    unittest.main()
