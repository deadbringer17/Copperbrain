"""Command-line entry point for Copperbrain server and maintenance commands."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from copperbrain.adapters.repository_updates import (
    GitRepositoryUpdateAdapter,
    RepositoryUpdateAdapterError,
)
from copperbrain.services.updates import RepositoryUpdateService, UpdateRefusal


def _source_checkout_root() -> Path:
    root = Path(__file__).resolve().parents[2]
    if not (root / ".git").is_dir() or not (root / "pyproject.toml").is_file():
        raise RepositoryUpdateAdapterError(
            "Copperbrain is not running from a Git source checkout, so it cannot self-update."
        )
    return root


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="copperbrain")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser(
        "update",
        help="Safely fast-forward a clean source checkout from official origin/main.",
    )
    return parser


def _run_update() -> int:
    try:
        backend = GitRepositoryUpdateAdapter(_source_checkout_root())
        result = RepositoryUpdateService(backend).update()
    except UpdateRefusal as exc:
        print(f"Copperbrain update refused [{exc.reason}]: {exc}", file=sys.stderr)
        print(f"Next step: {exc.hint}", file=sys.stderr)
        return 2
    except RepositoryUpdateAdapterError as exc:
        print(f"Copperbrain update failed: {exc}", file=sys.stderr)
        return 1

    previous = result.previous_revision[:12]
    current = result.current_revision[:12]
    if result.outcome == "updated":
        print(f"Copperbrain updated {previous} -> {current}.")
        print("Restart Codex or open a new task to load the updated MCP server.")
    elif result.outcome == "local_ahead":
        print(f"Local main is ahead of origin/main at {current}; no update was applied.")
    else:
        print(f"Copperbrain is already up to date at {current}.")
    return 0


def main() -> None:
    """Run the MCP server by default or an explicit maintenance command."""
    parser = _build_parser()
    args = parser.parse_args()
    if args.command == "update":
        raise SystemExit(_run_update())

    from copperbrain.server import main as run_server

    run_server()


if __name__ == "__main__":
    main()
