"""cc-runtime: manage the containerized development runtime."""
import sys


def main() -> None:
    from .cli import main as run
    sys.exit(run())
