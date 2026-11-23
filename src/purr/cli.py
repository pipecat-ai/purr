"""The ``purr`` command."""

import argparse
import asyncio
import dataclasses
import subprocess
import sys
from pathlib import Path

from loguru import logger

from purr import __version__
from purr.claude import NO_CLAUDE_CODE, claude_cli
from purr.config import Config, SettingsError


def _setup_logging(config: Config, *, verbose: bool, stderr: bool) -> None:
    config.data_dir.mkdir(parents=True, exist_ok=True)
    logger.remove()
    if stderr:
        logger.add(sys.stderr, level="DEBUG" if verbose else "INFO")
    logger.add(config.log_path, level="DEBUG", rotation="10 MB", retention=3)


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    if args.login:
        sys.exit(_login())
    try:
        config = _config(args)
    except SettingsError as e:
        sys.exit(f"purr: {e}")
    # With the UI up, stderr belongs to the terminal UI: log to the file only.
    _setup_logging(config, verbose=args.verbose, stderr=args.headless)

    from purr.app import Purr

    if args.headless:
        asyncio.run(Purr(config).run())
    else:
        from purr.tui import PurrApp

        PurrApp(Purr(config, handle_sigint=False), cwd=Path.cwd()).run()


def _parser() -> argparse.ArgumentParser:
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
    parser.add_argument(
        "--max-reviews",
        type=int,
        metavar="N",
        help="reviews running at once; the rest wait in line (default: 3)",
    )
    parser.add_argument(
        "--permission-mode",
        metavar="MODE",
        help="Claude Code's permission mode for reviews: auto (default), acceptEdits, default",
    )
    parser.add_argument(
        "--sandbox",
        action="store_true",
        help="run review agents' commands in Claude Code's sandbox (see the README)",
    )
    parser.add_argument(
        "--login",
        action="store_true",
        help="log in to Claude (with the Claude Code Purr comes with), then exit",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging on stderr")
    parser.add_argument("--version", action="version", version=f"purr {__version__}")
    return parser


def _login() -> int | str:
    """Log in to Claude, as Claude Code does: reviews use that login (or ANTHROPIC_API_KEY)."""
    if not (claude := claude_cli()):
        return f"purr: no Claude Code to log in with. {NO_CLAUDE_CODE}"
    return subprocess.call([claude, "auth", "login"])


def _config(args: argparse.Namespace) -> Config:
    """Purr's settings (settings.toml), with what the command line says over them."""
    config = Config.load(data_dir=args.data_dir)
    if args.no_auto_review:
        config = dataclasses.replace(config, auto_review=False)
    if args.max_reviews:
        config = dataclasses.replace(config, max_reviews=max(1, args.max_reviews))
    if args.permission_mode:
        config = dataclasses.replace(config, permission_mode=args.permission_mode)
    if args.sandbox:
        config = dataclasses.replace(config, sandbox=True)
    return config
