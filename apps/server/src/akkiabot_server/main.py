"""Command-line entry point of the AkkiaBot server.

The bot runtime and HTTP API arrive in later milestones (M1/M2). For now the
entry point only reports the version, which is what the container smoke test
relies on.
"""

import argparse
from collections.abc import Sequence

from akkiabot_server import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="akkiabot", description="AkkiaBot server")
    parser.add_argument("--version", action="version", version=f"akkiabot {__version__}")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    build_parser().parse_args(argv)
    print(f"akkiabot {__version__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
