"""The ``purr`` command."""

import argparse
import asyncio
import sys
from pathlib import Path

from loguru import logger

from purr import __version__
from purr.app import Purr
from purr.config import Config


def _setup_logging(config: Config, *, verbose: bool) -> None:
    config.data_dir.mkdir(parents=True, exist_ok=True)
    logger.remove()
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
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging on stderr")
    parser.add_argument("--version", action="version", version=f"purr {__version__}")
    args = parser.parse_args(argv)

    config = Config.load(data_dir=args.data_dir)
    _setup_logging(config, verbose=args.verbose)
    asyncio.run(Purr(config).run())
