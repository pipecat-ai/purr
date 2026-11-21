"""The ``purr`` command."""

import argparse
import asyncio
import dataclasses
import sys
from pathlib import Path

from loguru import logger

from purr import __version__
from purr.config import Config


def _setup_logging(config: Config, *, verbose: bool, stderr: bool) -> None:
    config.data_dir.mkdir(parents=True, exist_ok=True)
    logger.remove()
    if stderr:
        logger.add(sys.stderr, level="DEBUG" if verbose else "INFO")
    logger.add(config.log_path, level="DEBUG", rotation="10 MB", retention=3)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="purr", description="Review GitHub pull requests with Claude."
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        help="where to keep the database, logs and worktrees "
        "(default: $PURR_DATA_DIR or ~/.local/share/purr)",
    )
    parser.add_argument(
        "--headless", action="store_true", help="run without the UI, logging to stderr"
    )
    parser.add_argument(
        "--no-auto-review",
        action="store_true",
        help="don't start reviews of PRs assigned to you on your behalf",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging on stderr")
    parser.add_argument("--version", action="version", version=f"purr {__version__}")
    args = parser.parse_args(argv)

    config = Config.load(data_dir=args.data_dir)
    if args.no_auto_review:
        config = dataclasses.replace(config, auto_review=False)
    # With the UI up, stderr belongs to the terminal UI: log to the file only.
    _setup_logging(config, verbose=args.verbose, stderr=args.headless)

    from purr.app import Purr

    if args.headless:
        asyncio.run(Purr(config).run())
    else:
        from purr.tui import PurrApp

        PurrApp(Purr(config, handle_sigint=False), cwd=Path.cwd()).run()
