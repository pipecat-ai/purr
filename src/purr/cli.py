"""The ``purr`` command (Typer, as Pipecat's CLI)."""

import asyncio
import dataclasses
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer
from loguru import logger
from rich.console import Console

from purr import __version__
from purr.claude import NO_CLAUDE_CODE, claude_cli
from purr.config import Config, SettingsError

app = typer.Typer(
    name="purr",
    help="ᓚᘏᗢ Purr: review GitHub pull requests with Claude, you in the loop.",
    add_completion=False,
    rich_markup_mode="rich",
)
console = Console(stderr=True)


@dataclass(frozen=True)
class Flags:
    """What the command line says over ``settings.toml`` (None: what the file says)."""

    data_dir: Path | None = None
    auto_review: bool | None = None
    max_reviews: int | None = None
    permission_mode: str | None = None
    sandbox: bool | None = None


def _version(value: bool) -> None:
    if value:
        console.print(f"ᓚᘏᗢ Purr [green]{__version__}[/green]")
        raise typer.Exit()


@app.callback(invoke_without_command=True)
def purr(
    ctx: typer.Context,
    data_dir: Annotated[
        Path | None,
        typer.Option(
            help="Where to keep the database, logs and worktrees "
            "[dim](default: $PURR_DATA_DIR or ~/.local/share/purr)[/dim]",
            show_default=False,
        ),
    ] = None,
    auto_review: Annotated[
        bool | None,
        typer.Option(
            "--auto-review/--no-auto-review",
            help="Review PRs assigned to you on your behalf [dim](default: yes)[/dim]",
            show_default=False,
        ),
    ] = None,
    max_reviews: Annotated[
        int | None,
        typer.Option(
            min=1,
            metavar="N",
            help="Reviews running at once; the rest wait in line [dim](default: 3)[/dim]",
            show_default=False,
        ),
    ] = None,
    permission_mode: Annotated[
        str | None,
        typer.Option(
            metavar="MODE",
            help="Claude Code's permission mode for reviews: auto, acceptEdits, default",
            show_default=False,
        ),
    ] = None,
    sandbox: Annotated[
        bool | None,
        typer.Option(
            "--sandbox/--no-sandbox",
            help="Run review agents' commands in Claude Code's sandbox (see the README)",
            show_default=False,
        ),
    ] = None,
    headless: Annotated[
        bool, typer.Option("--headless", help="Run without the UI, logging to stderr")
    ] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Debug logging")] = False,
    version: Annotated[
        bool,
        typer.Option("--version", callback=_version, is_eager=True, help="Show the version"),
    ] = False,
) -> None:
    """Review GitHub pull requests with Claude: Purr watches your repos, reviews what's
    assigned to you, and posts only what you approve. Settings: ~/.config/purr/settings.toml,
    which these flags win over."""
    if ctx.invoked_subcommand:
        return
    flags = Flags(data_dir, auto_review, max_reviews, permission_mode, sandbox)
    try:
        config = _config(flags)
    except SettingsError as e:
        console.print(f"[red]purr:[/red] {e}")
        raise typer.Exit(1) from e
    _run(config, headless=headless, verbose=verbose)


@app.command()
def login() -> None:
    """Log in to Claude, as Claude Code does. Reviews use that login (or ANTHROPIC_API_KEY);
    if you use Claude Code already, you're logged in."""
    if not (claude := claude_cli()):
        console.print(f"[red]purr:[/red] no Claude Code to log in with. {NO_CLAUDE_CODE}")
        raise typer.Exit(1)
    raise typer.Exit(subprocess.call([claude, "auth", "login"]))


def main() -> None:
    app()


def _config(flags: Flags) -> Config:
    """Purr's settings (settings.toml), with what the command line says over them."""
    config = Config.load(data_dir=flags.data_dir)
    said = {
        name: value
        for name, value in dataclasses.asdict(flags).items()
        if value is not None and name != "data_dir"
    }
    return dataclasses.replace(config, **said)


def _run(config: Config, *, headless: bool, verbose: bool) -> None:
    _setup_logging(config, verbose=verbose, stderr=headless)

    from purr.app import Purr

    if headless:
        asyncio.run(Purr(config).run())
        return
    from purr.tui import PurrApp

    PurrApp(Purr(config, handle_sigint=False), cwd=Path.cwd()).run()


def _setup_logging(config: Config, *, verbose: bool, stderr: bool) -> None:
    """To the log file; with the UI up, stderr belongs to it, so only headless there."""
    config.data_dir.mkdir(parents=True, exist_ok=True)
    logger.remove()
    if stderr:
        logger.add(sys.stderr, level="DEBUG" if verbose else "INFO")
    logger.add(config.log_path, level="DEBUG", rotation="10 MB", retention=3)
