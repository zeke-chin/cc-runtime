"""Exercise migration against a stopped container without touching the deployment."""
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("dev", ROOT / "scripts/dev.py")
dev = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dev)


@unittest.skipUnless(os.environ.get("CC_RUNTIME_DOCKER_TEST") == "1", "opt-in Docker integration")
class IdentityTests(unittest.TestCase):
    def test_migrate_stopped_container_and_preserve_existing_keys(self):
        image = os.environ.get("CC_RUNTIME_TEST_IMAGE", "ghcr.io/zeke-chin/cc-runtime:latest")
        name = "cc-identity-test-" + uuid.uuid4().hex[:10]
        try:
            subprocess.run(["docker", "run", "--name", name, "--network", "none",
                            "--user", "0:0", "--entrypoint", "/bin/sh", image, "-c",
                            "rm -f /etc/ssh/ssh_host_*; ssh-keygen -A"],
                           check=True, stdout=subprocess.DEVNULL)
            with tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary) / "keys"
                model = {"services": {
                    "dev": {"volumes": [{"type": "bind", "source": str(directory),
                                          "target": "/var/lib/cc-runtime/ssh"}]},
                    "tailscale": {"volumes": [{"type": "bind",
                        "source": str(Path(temporary) / "tailscale-state"),
                        "target": "/var/lib/tailscale"}]}}}
                with patch.object(dev, "compose", return_value=name):
                    dev.initialize_identity(model, image)
                keys = sorted(directory.glob("ssh_host_*_key"))
                self.assertEqual(len(keys), 3)
                original = {p.name: p.read_bytes() for p in keys}
                self.assertTrue(all(p.stat().st_mode & 0o777 == 0o600 for p in keys))
                with patch.object(dev, "compose", side_effect=AssertionError("Must reuse saved keys")):
                    dev.initialize_identity(model, image)
                self.assertEqual({p.name: p.read_bytes() for p in keys}, original)
                # A damaged existing identity must fail, not silently rotate.
                keys[0].write_text("invalid key\n")
                with self.assertRaises(subprocess.CalledProcessError):
                    dev.initialize_identity(model, image)
                self.assertEqual(keys[0].read_text(), "invalid key\n")
        finally:
            subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL)


class MachineIdTests(unittest.TestCase):
    def model(self, path):
        return {"services": {"dev": {"volumes": [
            {"type": "bind", "source": str(path), "target": "/etc/machine-id"}]}}}

    def test_new_machine_gets_fresh_id_and_existing_id_is_kept(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "machine-id"
            with patch.object(dev, "compose", return_value=""):
                dev.initialize_machine_id(self.model(path), "dev")
                first = path.read_text()
                self.assertRegex(first, r"^[0-9a-f]{32}\n$")
                dev.initialize_machine_id(self.model(path), "dev")
            self.assertEqual(path.read_text(), first)

    def test_existing_container_id_is_migrated(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "machine-id"
            def fake_run(args, **_):
                Path(args[-1]).write_text("0123456789abcdef0123456789abcdef\n")
            with patch.object(dev, "compose", return_value="abc123\n"), \
                    patch.object(dev, "run", side_effect=fake_run):
                dev.initialize_machine_id(self.model(path), "dev")
            self.assertEqual(path.read_text(), "0123456789abcdef0123456789abcdef\n")

    def test_invalid_existing_file_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "machine-id"
            path.write_text("")
            with self.assertRaisesRegex(ValueError, "not a valid"):
                dev.initialize_machine_id(self.model(path), "dev")
