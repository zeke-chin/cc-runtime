import os
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from helpers import make_config

from cc_runtime import deploy
from cc_runtime.util import CommandError

DOCKER = os.environ.get("CC_RUNTIME_DOCKER_TEST") == "1"
IMAGE = os.environ.get("CC_RUNTIME_TEST_IMAGE", "ghcr.io/zeke-chin/cc-runtime:latest")


def machine(base: Path, name: str) -> dict:
    return {"image": IMAGE, "volumes": [
        {"type": "bind", "source": str(base / f"dev-runtimes/{name}/ssh-host-keys"),
         "target": deploy.SSH_TARGET},
        {"type": "bind", "source": str(base / f"dev-runtimes/{name}/machine-id"),
         "target": "/etc/machine-id"},
        {"type": "bind", "source": str(base / f"dev-runtimes/{name}/home"), "target": "/home/dev"}],
        "networks": {"private": {"ipv4_address": "172.30.0.10" if name == "dev-PA" else "172.30.0.11"}}}


class ProjectTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "repo"
        self.root.mkdir()
        self.cfg = make_config(Path(self.temporary.name), root=self.root)
        self.project = {"name": "cc-runtime", "services": {
            "dev-PA": machine(self.root, "dev-PA"), "dev-CA": machine(self.root, "dev-CA"),
            "dev-PA-network-init": {"image": "x"}, "proxy": {"image": "x"}}}

    def tearDown(self):
        self.temporary.cleanup()

    def test_dev_services_and_helpers(self):
        self.assertEqual(deploy.dev_services(self.project), ["dev-PA", "dev-CA"])
        self.assertEqual(deploy.with_helpers(self.project, ["dev-PA", "proxy"]),
                         ["dev-PA", "proxy", "dev-PA-network-init"])

    def test_legacy_directory_blocks_only_the_machine_that_moved(self):
        (self.root / "dev-runtime").mkdir()  # old layout still present
        with self.assertRaisesRegex(CommandError, "dev-runtime/ still holds"):
            deploy.check_legacy(self.cfg, "dev-PA", self.project["services"]["dev-PA"])
        deploy.check_legacy(self.cfg, "dev-CA", self.project["services"]["dev-CA"])  # unaffected
        (self.root / "dev-runtimes/dev-PA").mkdir(parents=True)
        deploy.check_legacy(self.cfg, "dev-PA", self.project["services"]["dev-PA"])  # moved: fine

    def test_address_conflicts_with_running_orphans(self):
        orphan = {"Name": "/cc-runtime-dev-1", "NetworkSettings": {"Networks": {
            "cc-runtime_private": {"IPAddress": "172.30.0.10"}}}}
        self.assertEqual(deploy.address_conflicts(self.project, ["dev-PA", "dev-CA"], [orphan]),
                         ["dev-PA needs 172.30.0.10, still used by cc-runtime-dev-1"])


class MachineIdTests(unittest.TestCase):
    def service(self, path):
        return {"volumes": [{"type": "bind", "source": str(path), "target": "/etc/machine-id"}]}

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.cfg = make_config(Path(self.temporary.name))
        self.path = Path(self.temporary.name) / "machine-id"
        patcher = patch.object(deploy, "require", return_value="docker")
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.temporary.cleanup()

    def test_new_machine_gets_fresh_id_and_existing_id_is_kept(self):
        with patch.object(deploy, "compose", return_value=""):
            deploy.initialize_machine_id(self.cfg, "dev-X", self.service(self.path))
            first = self.path.read_text()
            self.assertRegex(first, r"^[0-9a-f]{32}\n$")
            deploy.initialize_machine_id(self.cfg, "dev-X", self.service(self.path))
        self.assertEqual(self.path.read_text(), first)

    def test_existing_container_id_is_kept(self):
        def fake_run(args, **_):
            Path(args[-1]).write_text("0123456789abcdef0123456789abcdef\n")
        with patch.object(deploy, "compose", return_value="abc123\n"), \
                patch.object(deploy, "run", side_effect=fake_run):
            deploy.initialize_machine_id(self.cfg, "dev-X", self.service(self.path))
        self.assertEqual(self.path.read_text(), "0123456789abcdef0123456789abcdef\n")

    def test_invalid_existing_file_is_refused(self):
        self.path.write_text("")
        with self.assertRaisesRegex(CommandError, "not a valid"):
            deploy.initialize_machine_id(self.cfg, "dev-X", self.service(self.path))


@unittest.skipUnless(DOCKER, "opt-in Docker integration (CC_RUNTIME_DOCKER_TEST=1)")
class DockerTests(unittest.TestCase):
    def test_ssh_keys_are_migrated_from_a_stopped_container_and_then_kept(self):
        name = "cc-identity-test-" + uuid.uuid4().hex[:10]
        try:
            subprocess.run(["docker", "run", "--name", name, "--network", "none", "--user", "0:0",
                            "--entrypoint", "/bin/sh", IMAGE, "-c", "rm -f /etc/ssh/ssh_host_*; ssh-keygen -A"],
                           check=True, stdout=subprocess.DEVNULL)
            with tempfile.TemporaryDirectory() as temporary:
                cfg = make_config(Path(temporary))
                directory = Path(temporary) / "keys"
                service = {"volumes": [{"type": "bind", "source": str(directory), "target": deploy.SSH_TARGET}]}
                with patch.object(deploy, "existing_container", return_value=name):
                    deploy.initialize_ssh_keys(cfg, IMAGE, "dev-X", service)
                keys = sorted(directory.glob("ssh_host_*_key"))
                self.assertEqual(len(keys), 3)
                original = {p.name: p.read_bytes() for p in keys}
                with patch.object(deploy, "existing_container", side_effect=AssertionError("reuse saved keys")):
                    deploy.initialize_ssh_keys(cfg, IMAGE, "dev-X", service)
                self.assertEqual({p.name: p.read_bytes() for p in keys}, original)
                keys[0].write_text("invalid key\n")  # a damaged identity must fail, not rotate
                with self.assertRaises(subprocess.CalledProcessError):
                    deploy.initialize_ssh_keys(cfg, IMAGE, "dev-X", service)
        finally:
            subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL)

    def test_home_is_seeded_once_without_overwriting(self):
        image = "cc-home-test-" + uuid.uuid4().hex[:10]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            home.mkdir()
            (home / ".zshrc").write_text("keep-user-config")
            (root / "Dockerfile").write_text('''FROM python:3.12-slim
RUN mkdir -p /home/dev/.local/bin /home/dev/.cache /home/dev/workspace && \\
    echo image-config > /home/dev/.zshrc && echo image-tool > /home/dev/.local/bin/tool && \\
    echo no-copy > /home/dev/.cache/image-cache && echo no-copy > /home/dev/workspace/image-source
''')
            try:
                subprocess.run(["docker", "build", "-q", "-t", image, str(root)], check=True,
                               stdout=subprocess.DEVNULL)
                deploy.initialize_home(image, home, 1234, 1234)
                self.assertEqual((home / ".zshrc").read_text(), "keep-user-config")
                self.assertEqual((home / ".local/bin/tool").read_text().strip(), "image-tool")
                self.assertFalse((home / ".cache/image-cache").exists())
                self.assertFalse((home / "workspace/image-source").exists())
                (home / ".local/bin/tool").write_text("user-updated-tool")
                deploy.initialize_home(image, home, 1234, 1234)
                self.assertEqual((home / ".local/bin/tool").read_text(), "user-updated-tool")
            finally:
                subprocess.run(["docker", "image", "rm", image], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)


if __name__ == "__main__":
    unittest.main()
