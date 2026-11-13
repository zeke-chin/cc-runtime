"""Start the deployment with Docker Compose, preserving each machine's identity.

Before Compose creates a development machine, its persistent pieces are
prepared on the host: the home directory is seeded from the image once, the
machine-id and the SSH host keys are kept (migrated from an existing container
of the same service, or created for a new machine), and the cache volume and
its subpath exist.
"""
from __future__ import annotations

import json
import re
import tempfile
import uuid
from pathlib import Path
from typing import Dict, List, Optional

from .config import Config
from .util import CommandError, require, run

SSH_TARGET = "/var/lib/cc-runtime/ssh"
HOME_TARGET = "/home/dev"
# Old machine directory -> its new place. Starting the new service before the
# move would create a fresh identity, so it is refused.
LEGACY_MACHINE_DIRS = {"dev-runtime": "dev-runtimes/dev-PA"}
CREATING_SUBCOMMANDS = {"up", "create", "run"}


def compose(cfg: Config, *args: str, capture: bool = False, check: bool = True):
    command = [require("docker"), "compose", "--project-directory", str(cfg.root),
               "-f", str(cfg.root / "compose.yaml")]
    result = run(command + list(args), capture=capture, check=check)
    return result.stdout if capture else result


def model(cfg: Config) -> dict:
    return json.loads(compose(cfg, "config", "--format", "json", capture=True))


def dev_services(project: dict) -> List[str]:
    """Services that are development machines (they keep SSH host keys)."""
    return [name for name, service in project["services"].items()
            if any(v.get("target") == SSH_TARGET for v in service.get("volumes", []))]


def with_helpers(project: dict, services: List[str]) -> List[str]:
    """Add each machine's network-init helper, which must run after it starts."""
    result = list(services)
    for name in services:
        helper = f"{name}-network-init"
        if helper in project["services"] and helper not in result:
            result.append(helper)
    return result


def project_containers(project: dict) -> List[dict]:
    docker = require("docker")
    ids = run([docker, "ps", "-q", "--filter", f"label=com.docker.compose.project={project['name']}"],
              capture=True).stdout.split()
    if not ids:
        return []
    return json.loads(run([docker, "inspect", *ids], capture=True).stdout)


def running_orphans(project: dict) -> List[dict]:
    """Running containers of this project whose service is not in compose.yaml."""
    return [c for c in project_containers(project)
            if c["Config"]["Labels"].get("com.docker.compose.service") not in project["services"]]


def address_conflicts(project: dict, services: List[str], orphans: List[dict]) -> List[str]:
    taken = {}
    for container in orphans:
        for network in container["NetworkSettings"]["Networks"].values():
            if network.get("IPAddress"):
                taken[network["IPAddress"]] = container["Name"].lstrip("/")
    conflicts = []
    for name in services:
        for network in (project["services"][name].get("networks") or {}).values():
            address = (network or {}).get("ipv4_address")
            if address in taken:
                conflicts.append(f"{name} needs {address}, still used by {taken[address]}")
    return conflicts


# ---- per-machine state ----------------------------------------------------------

def mount_source(service: dict, target: str) -> Optional[dict]:
    return next((m for m in service.get("volumes", []) if m.get("target") == target), None)


def check_legacy(cfg: Config, name: str, service: dict) -> None:
    """Refuse to create a machine whose state still sits in its old directory."""
    sources = [Path(m["source"]).resolve() for m in service.get("volumes", [])
               if m.get("type") == "bind" and m.get("source")]
    for old, new in LEGACY_MACHINE_DIRS.items():
        new_dir = (cfg.root / new).resolve()
        uses_new = any(src == new_dir or new_dir in src.parents for src in sources)
        if uses_new and (cfg.root / old).exists() and not new_dir.exists():
            raise CommandError(
                f"{name}: {old}/ still holds this machine's home and identity; move it to {new}/ "
                f"(with the old container stopped) before starting {name}")


def image_ids(image: str):
    docker = require("docker")
    try:
        run([docker, "image", "inspect", "--format", "{{.Id}}", image], capture=True)
    except Exception:
        raise CommandError(f"image {image!r} is not available locally; run with --pull") from None
    ids = run([docker, "run", "--rm", "--network", "none", "--entrypoint", "/bin/sh", image,
               "-c", "id -u dev; id -g dev"], capture=True).stdout.split()
    if len(ids) != 2 or not all(i.isdecimal() for i in ids):
        raise CommandError("could not determine the dev user's UID/GID from the image")
    return int(ids[0]), int(ids[1])


def initialize_home(image: str, home: Path, uid: int, gid: int) -> None:
    """Seed the bind-mounted home from the image once, without overwriting user files."""
    home.mkdir(parents=True, exist_ok=True)
    script = '''set -eu
if [ ! -f /persistent-home/.cc-runtime-home-initialized ]; then
    for path in /home/dev/.[!.]* /home/dev/..?* /home/dev/*; do
        [ -e "$path" ] || [ -L "$path" ] || continue
        # Never seed per-machine runtime state that an image may contain.
        case "${path##*/}" in workspace|.cache|.claude|.claude.json) continue ;; esac
        cp -a -n "$path" /persistent-home/
    done
    mkdir -p /persistent-home/workspace
    chown "$1:$2" /persistent-home /persistent-home/workspace
    touch /persistent-home/.cc-runtime-home-initialized
fi
'''
    run([require("docker"), "run", "--rm", "--network", "none", "--user", "0:0",
         "--mount", f"type=bind,source={home},target=/persistent-home",
         "--entrypoint", "/bin/sh", image, "-c", script, "init", str(uid), str(gid)])


def existing_container(cfg: Config, service: str) -> Optional[str]:
    ids = compose(cfg, "ps", "--all", "--quiet", service, capture=True).strip().splitlines()
    if len(ids) > 1:
        raise CommandError(f"expected at most one {service} container")
    return ids[0] if ids else None


def initialize_machine_id(cfg: Config, service_name: str, service: dict) -> None:
    """Keep the machine-id: from the file, else from the existing container, else new."""
    mount = mount_source(service, "/etc/machine-id")
    if not mount:
        return
    if mount["type"] != "bind":
        raise CommandError(f"{service_name} machine-id must use a bind mount")
    path = Path(mount["source"])
    if path.exists():
        if not re.fullmatch(r"[0-9a-f]{32}\n?", path.read_text()):
            raise CommandError(f"{service_name} machine-id is not a valid ID: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    container = existing_container(cfg, service_name)
    value = None
    if container:
        with tempfile.TemporaryDirectory(prefix="cc-machine-id-") as temporary:
            copied = Path(temporary) / "machine-id"
            run([require("docker"), "cp", "-L", f"{container}:/etc/machine-id", str(copied)])
            value = copied.read_text().strip()
        if not re.fullmatch(r"[0-9a-f]{32}", value):
            value = None
    action = "Kept the existing" if value else "Generated a new"
    path.write_text((value or uuid.uuid4().hex) + "\n")
    path.chmod(0o644)
    print(f"{action} machine-id for {service_name}.", flush=True)


def initialize_ssh_keys(cfg: Config, image: str, service_name: str, service: dict) -> None:
    """Keep the SSH host keys: validate stored ones, or migrate them from the container."""
    mount = mount_source(service, SSH_TARGET)
    if mount["type"] != "bind":
        raise CommandError("SSH host keys must use a bind mount")
    directory = Path(mount["source"]).resolve()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    docker = require("docker")
    helper = [docker, "run", "--rm", "--network", "none", "--user", "0:0",
              "--mount", f"type=bind,source={directory},target=/keys"]
    existing = run(helper + ["--entrypoint", "/bin/sh", image, "-c",
                             'find /keys -name "ssh_host_*_key" -print'], capture=True).stdout
    if existing.strip():
        run(helper + ["--entrypoint", "/bin/sh", image, "-c",
                      'set -eu; for type in rsa ecdsa ed25519; do '
                      'ssh-keygen -y -f "/keys/ssh_host_${type}_key" > /dev/null; done'])
        return  # never overwrite an established identity
    container = existing_container(cfg, service_name)
    if not container:
        return  # a new machine: the entrypoint generates keys in the directory
    mounts = json.loads(run([docker, "inspect", "--format", "{{json .Mounts}}", container],
                            capture=True).stdout)
    old = SSH_TARGET if any(m["Destination"] == SSH_TARGET for m in mounts) else "/etc/ssh"
    with tempfile.TemporaryDirectory(prefix="cc-ssh-migration-") as temporary:
        source = Path(temporary) / "ssh"
        run([docker, "cp", f"{container}:{old}", str(source)])
        if not all((source / f"ssh_host_{kind}_key").is_file() for kind in ("rsa", "ecdsa", "ed25519")):
            raise CommandError(f"{service_name} host keys are incomplete; refusing to replace its identity")
        script = '''set -eu
umask 077
for type in rsa ecdsa ed25519; do
    ssh-keygen -y -f "/source/ssh_host_${type}_key" > /dev/null
    cp "/source/ssh_host_${type}_key" "/keys/ssh_host_${type}_key"
    chown root:root "/keys/ssh_host_${type}_key"
    chmod 600 "/keys/ssh_host_${type}_key"
done
'''
        run(helper + ["--mount", f"type=bind,source={source},target=/source,readonly",
                      "--entrypoint", "/bin/sh", image, "-c", script])
    print(f"Kept the existing {service_name} SSH host keys.", flush=True)


def initialize_cache(project: dict, image: str, service: dict, uid: int, gid: int) -> None:
    """Create named volumes and their subpaths used by a development machine."""
    docker = require("docker")
    for mount in service.get("volumes", []):
        if mount.get("type") != "volume":
            continue
        volume = project.get("volumes", {}).get(mount["source"], {})
        name = volume.get("name") or f"{project['name']}_{mount['source']}"
        run([docker, "volume", "create", name], capture=True)
        subpath = (mount.get("volume") or {}).get("subpath")
        if subpath:
            run([docker, "run", "--rm", "--network", "none", "--user", "0:0",
                 "--mount", f"type=volume,source={name},target=/volume",
                 "--entrypoint", "/bin/sh", image, "-c",
                 'mkdir -p "/volume/$3" && chown "$1:$2" "/volume/$3"', "init",
                 str(uid), str(gid), subpath])


def prepare(cfg: Config, project: dict, services: Optional[List[str]] = None) -> None:
    machines = [s for s in dev_services(project) if services is None or s in services]
    if not machines:
        return
    for name in machines:
        check_legacy(cfg, name, project["services"][name])
    for name in machines:
        service = project["services"][name]
        image = service["image"]
        uid, gid = image_ids(image)
        home = mount_source(service, HOME_TARGET)
        if home:
            if home["type"] != "bind":
                raise CommandError(f"{name} home must be a bind mount")
            initialize_home(image, Path(home["source"]), uid, gid)
        initialize_machine_id(cfg, name, service)
        initialize_ssh_keys(cfg, image, name, service)
        initialize_cache(project, image, service, uid, gid)


def up(cfg: Config, services: List[str], *, pull: bool = False, force_recreate: bool = False,
       allow_orphans: bool = False) -> None:
    project = model(cfg)
    unknown = [s for s in services if s not in project["services"]]
    if unknown:
        raise CommandError(f"unknown service(s): {', '.join(unknown)}")
    targets = with_helpers(project, services) if services else list(project["services"])
    orphans = running_orphans(project)
    if orphans and not allow_orphans:
        names = ", ".join(c["Name"].lstrip("/") for c in orphans)
        conflicts = address_conflicts(project, targets, orphans)
        if conflicts or not services:
            detail = "; ".join(conflicts) if conflicts else "a full start would conflict with them"
            raise CommandError(f"running containers are not in compose.yaml ({names}): {detail}. "
                               "Stop them first, or start specific services.")
    if pull:
        compose(cfg, "pull", *[s for s in targets if "image" in project["services"][s]])
    prepare(cfg, project, targets)
    args = ["up", "-d"]
    if services:
        args.append("--no-deps")
    if force_recreate:
        args.append("--force-recreate")
    compose(cfg, *args, *targets)


def compose_passthrough(cfg: Config, args: List[str]) -> int:
    """Run any Compose command; commands that create containers prepare machines first."""
    if CREATING_SUBCOMMANDS.intersection(args):
        project = model(cfg)
        named = [a for a in args if a in project["services"]]
        prepare(cfg, project, named or None)
    return compose(cfg, *args, check=False).returncode
