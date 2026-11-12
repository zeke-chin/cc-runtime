#!/usr/bin/env python3
"""Seed a new dev machine's home from an existing one, without its identity.

Copies tools, toolchains, package caches, shell/editor configuration and the
tool credentials the user chose to share (CREDENTIALS).
Never copies machine identity: SSH host keys, machine-id, telemetry IDs, the
whole ~/.claude directory and ~/.claude.json, shell/editor history or session
state. The new machine gets its own SSH host keys, machine-id and Claude login.

Run on the Docker host. On macOS (APFS) files are cloned copy-on-write with
`cp -c`: instant, no extra disk space, and each side changes independently.
On Linux `cp --reflink=auto` is used. Hard links are never used.

Usage:
  python3 scripts/seed-dev-home.py dev-runtime/home dev-california-runtime/home
  python3 scripts/seed-dev-home.py --dry-run SRC DEST
  python3 scripts/seed-dev-home.py --credentials-only SRC DEST   # add to an existing home
"""
import argparse
import platform
import shutil
import subprocess
import sys
from pathlib import Path

# Paths relative to the home directory. Missing paths are skipped.
COPY = [
    # Shell and editor configuration (no history).
    ".bashrc", ".bash_logout", ".profile", ".zshrc", ".zshenv", ".zimrc", ".zim",
    ".vimrc", ".face", ".face.icon",
    # Git identity is the user's, not the machine's.
    ".gitconfig", ".gitconfig-weimob",
    # SSH: who may log in and which hosts are trusted. Client keys: see CREDENTIALS.
    ".ssh/authorized_keys", ".ssh/config", ".ssh/known_hosts",
    # User-installed tools and runtimes.
    ".local/bin", ".local/lib", ".local/share/uv", ".local/share/ms-playwright",
    # Toolchains and package caches (content only, no identity).
    ".rustup", ".cargo", "go/pkg", ".bun", ".npm/_cacache", ".local/share/pnpm",
    # Tool preferences without credentials.
    ".config/btop", ".config/gh/config.yml", ".config/user-dirs.dirs",
    ".config/user-dirs.locale", ".config/mimeapps.list",
]

# Account credentials the user chose to share between dev machines (2026-11-12).
# Never copied over an existing file.
CREDENTIALS = [
    ".git-credentials", ".config/gh/hosts.yml", ".config/context7",
    ".config/fast-context", ".config/salescli", ".tavily",
    ".ssh/id_ed25519", ".ssh/id_ed25519.pub", ".ssh/id_rsa", ".ssh/id_rsa.pub",
]

# Removed from the destination after copying (inside copied trees).
EXCLUDE = [
    ".local/share/uv/credentials",
    ".cargo/credentials", ".cargo/credentials.toml",
    ".npm/_cacache/tmp",
]

# Never copied; listed so the decision is explicit and reviewable.
NOT_COPIED = {
    ".claude.json": "Claude userID, machineID and oauthAccount",
    ".claude/": "entire Claude Code config, login, skills, plugins and history",
    ".aliyun": "telemetry instance ID and logs (no stored credentials)",
    ".config/go/telemetry": "Go telemetry state",
    ".local/state, .config/herdr, .servercc": "session state",
    ".local/share/zoxide, .zhistory, .viminfo": "usage history",
    ".local/share/pki, .Xauthority, .config/tigervnc": "machine-local secrets (regenerated)",
    ".zcompdump*, .zcompcache": "regenerated caches",
}


def claude_current_version(source):
    """Copy only the Claude Code version that ~/.local/bin/claude points to."""
    link = source / ".local/bin/claude"
    if not link.is_symlink():
        return []
    target = Path(str(link.readlink()).replace("/home/dev/", "", 1))
    return [target.as_posix()] if (source / target).exists() else []


def clone(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    if platform.system() == "Darwin":
        command = ["cp", "-c", "-pR", str(source), str(destination)]
    else:
        command = ["cp", "-a", "--reflink=auto", str(source), str(destination)]
    subprocess.run(command, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--credentials-only", action="store_true",
                        help="Copy only CREDENTIALS into an existing home, never overwriting")
    args = parser.parse_args()
    source, destination = args.source.resolve(strict=True), args.destination.resolve()
    if destination == source or source in destination.parents:
        raise ValueError("Destination must be outside the source home")
    if args.credentials_only:
        if not destination.is_dir():
            raise ValueError(f"Destination home does not exist: {destination}")
        paths = CREDENTIALS
    else:
        if destination.exists() and any(destination.iterdir()):
            raise ValueError(f"Destination is not empty: {destination}; refusing to merge homes")
        paths = COPY + claude_current_version(source) + CREDENTIALS
    for rel in paths:
        if not (source / rel).exists() and not (source / rel).is_symlink():
            print(f"skip   {rel} (missing)")
            continue
        if (destination / rel).exists() or (destination / rel).is_symlink():
            print(f"keep   {rel} (already exists)")
            continue
        print(f"copy   {rel}")
        if not args.dry_run:
            clone(source / rel, destination / rel)
    for rel in EXCLUDE:
        path = destination / rel
        if path.exists() or path.is_symlink():
            print(f"remove {rel}")
            if not args.dry_run:
                shutil.rmtree(path) if path.is_dir() and not path.is_symlink() else path.unlink()
    print("\nNot copied (machine identity, history or state):")
    for rel, reason in NOT_COPIED.items():
        print(f"  {rel}: {reason}")
    if not args.dry_run:
        destination.chmod(0o755)
        if (destination / ".ssh").exists():
            (destination / ".ssh").chmod(0o700)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)
