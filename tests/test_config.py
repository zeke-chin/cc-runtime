import tempfile
import unittest
from pathlib import Path

from helpers import make_config

from cc_runtime import config, sync

SESSION_OUTPUT = """--------------------------------------------------------------------------------
Name: linux-workspace
Configuration:
\tSymbolic link mode: POSIX Raw
\tIgnores:
\t\t/github_work/cc-runtime/dev-runtimes/
\t\t/github_work/cc-runtime/tailscale/state/
\t\t/rust_work/.cache/
\t\tnode_modules
\tIgnore VCS mode: Ignore
Alpha:
\tURL: /Users/you/workspace
\tConnected: Yes
Beta:
\tURL: /Users/you/.cc-runtime/linux-workspace
\tConnected: Yes
Conflicts:
\t(alpha) app/a.txt (File (x) -> File (y))
\t(beta)  app/a.txt (File (x) -> File (z))

Status: Watching for changes
--------------------------------------------------------------------------------
"""


class ConfigTests(unittest.TestCase):
    def test_env_file_and_environment(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".env").write_text('# comment\nPASSWORD=x\nWORKSPACE_DIR="/w s"\nRUNTIME_DIR=/r\n')
            cfg = config.load(root, environ={"RUNTIME_DIR": "/override"})
            self.assertEqual((cfg.workspace, cfg.runtime), (Path("/w s"), Path("/override")))
            self.assertEqual(cfg.linux_workspace, Path("/override/linux-workspace"))
            self.assertEqual(cfg.git_store, Path("/override/git"))
            self.assertEqual(config.load(root / "missing", environ={}).runtime,
                             Path("~/.cc-runtime").expanduser())

    def test_ignore_list_composition(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            repo = base / "mac" / "github_work" / "cc-runtime"
            repo.mkdir(parents=True)
            (repo / config.LOCAL_IGNORES_FILE).write_text("# local\n/big/data/  # comment\nnode_modules\n")
            cfg = make_config(base, root=repo)
            entries = cfg.ignores().entries
            self.assertEqual(entries[:3], ["/github_work/cc-runtime/dev-runtimes/",
                                           "/github_work/cc-runtime/tailscale/state/", "/big/data/"])
            self.assertEqual(entries.count("node_modules"), 1)
            # A repository outside the workspace adds no anchored entries.
            self.assertEqual(make_config(base, root=base / "elsewhere").deployment_ignores(), [])

    def test_matching(self):
        ignores = config.IgnoreList(["/rust_work/.cache/", "/a/b", "node_modules"])
        self.assertTrue(ignores.matches("x/node_modules/y.js"))
        self.assertTrue(ignores.matches("rust_work/.cache"))
        self.assertTrue(ignores.matches("a/b/c"))
        self.assertFalse(ignores.matches("other/rust_work/.cache"))
        self.assertFalse(ignores.matches("x/node_modules_backup"))
        self.assertFalse(ignores.matches("a/bc"))


class SessionParsingTests(unittest.TestCase):
    def test_parse_and_drift(self):
        state = sync.parse_session(SESSION_OUTPUT)
        self.assertTrue(state.connected)
        self.assertEqual((state.status, state.conflicts, state.problems), ("Watching for changes", 1, 0))
        self.assertTrue(state.healthy)
        self.assertFalse(state.ok)  # a conflict needs attention
        self.assertEqual(state.ignores[0], "/github_work/cc-runtime/dev-runtimes/")
        self.assertEqual(len(state.ignores), 4)
        paused = sync.parse_session(SESSION_OUTPUT.replace("Watching for changes", "[Paused]"))
        self.assertFalse(paused.healthy)
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            repo = base / "mac" / "github_work" / "cc-runtime"
            repo.mkdir(parents=True)
            missing, extra = sync.ignore_drift(make_config(base, root=repo), state)
            self.assertIn("target", missing)
            self.assertEqual(extra, ["/rust_work/.cache/"])

    def test_create_command(self):
        with tempfile.TemporaryDirectory() as temporary:
            command = sync.create_command(make_config(Path(temporary)))
        self.assertIn("--ignore-vcs", command)
        self.assertIn("--symlink-mode=posix-raw", command)
        self.assertNotIn("--ignore=.git", command)


if __name__ == "__main__":
    unittest.main()
