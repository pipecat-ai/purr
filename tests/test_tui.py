"""Drive the TUI with Textual's pilot, on top of a real Purr process and a fake GitHub."""

import asyncio
import subprocess
from collections.abc import Callable

from textual.widgets import Input, OptionList

from purr.app import Purr
from purr.config import Config
from purr.tui import PurrApp
from purr.tui.screens import AddRepoScreen, ConfirmScreen, MainScreen
from tests.fakes import FakeAgent, FakeGitHub, FakeRecapAgent, FakeRepos, FakeWriter, remote


def make_app(tmp_path, cwd=None) -> tuple[PurrApp, FakeGitHub]:
    github = FakeGitHub()
    config = Config(data_dir=tmp_path / "data", poll_interval=3600)
    purr = Purr(
        config,
        github=github,
        agent=FakeAgent(),
        repos=FakeRepos(tmp_path / "data"),
        writer=FakeWriter(),
        recap_agent=FakeRecapAgent(),
        handle_sigint=False,
    )
    return PurrApp(purr, cwd=cwd), github


async def until(pilot, condition: Callable[[], bool], timeout: float = 5) -> None:
    async def wait():
        while not condition():
            await pilot.pause(0.02)

    await asyncio.wait_for(wait(), timeout)


def home(app: PurrApp) -> bool:
    return isinstance(app.screen, MainScreen) and app.location is None


def in_repo(app: PurrApp, repo: str | None = None) -> bool:
    return (
        isinstance(app.screen, MainScreen)
        and app.location is not None
        and (repo is None or app.main.repo.repo == repo)
    )


def pr_ids(app: PurrApp) -> list[str]:
    options = app.main.repo.query_one(OptionList)
    ids = [options.get_option_at_index(i).id for i in range(options.option_count)]
    return [i for i in ids if i]


async def test_repos_navigation_and_live_updates(tmp_path):
    app, github = make_app(tmp_path)
    github.open["pipecat-ai/pipecat"] = [remote(1, title="Message bus"), remote(2)]
    github.assigned = [remote(7, repo="daily-co/daily-x", title="Win CI fix")]

    async with app.run_test(size=(120, 40)) as pilot:
        # Home lists the repo with a PR assigned to you, even though it isn't watched.
        await until(pilot, lambda: app.repo_names == ["daily-co/daily-x"])
        assert home(app)

        # Adding: a repo GitHub doesn't know is rejected in the dialog...
        await pilot.press("plus")
        assert isinstance(app.screen, AddRepoScreen)
        await pilot.press(*"nobody/nothing", "enter")
        await pilot.pause(0.1)
        assert isinstance(app.screen, AddRepoScreen)
        # ...a real one is added, by URL too, and the list stays alphabetical.
        app.screen.query_one(Input).value = "https://github.com/Pipecat-AI/pipecat"
        await pilot.press("enter")
        await until(pilot, lambda: home(app))
        await until(pilot, lambda: app.repo_names == ["daily-co/daily-x", "pipecat-ai/pipecat"])

        # 2 opens pipecat; its PRs arrive with the first poll.
        await pilot.press("2")
        await until(pilot, lambda: in_repo(app))
        assert in_repo(app, "pipecat-ai/pipecat")
        await until(pilot, lambda: len(pr_ids(app)) == 2)

        # A PR found by a later poll shows up without doing anything.
        github.updates["pipecat-ai/pipecat"] = [remote(3, title="New one")]
        await app.purr.runner.get_worker("github").poll()
        await until(pilot, lambda: "pipecat-ai/pipecat#3" in pr_ids(app))

        # Jumping between repos, back and forth with ctrl+o, and home.
        await pilot.press("1")
        await until(pilot, lambda: in_repo(app, "daily-co/daily-x"))
        await pilot.press("ctrl+o")
        await until(pilot, lambda: in_repo(app, "pipecat-ai/pipecat"))
        await pilot.press("escape")
        await until(pilot, lambda: home(app))
        await pilot.press("ctrl+o")
        await until(pilot, lambda: in_repo(app))
        await pilot.press("0")
        await until(pilot, lambda: home(app))

        # Removing pipecat (row 2) after confirming.
        await pilot.press("down", "minus")
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.press("y")
        await until(pilot, lambda: app.repo_names == ["daily-co/daily-x"])


async def test_launching_in_a_checkout_offers_to_watch_it(tmp_path):
    checkout = tmp_path / "pipecat"
    checkout.mkdir()
    subprocess.run(["git", "init", "-q", str(checkout)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(checkout),
            "remote",
            "add",
            "upstream",
            "https://github.com/pipecat-ai/pipecat.git",
        ],
        check=True,
    )
    app, github = make_app(tmp_path, cwd=checkout)
    github.open["pipecat-ai/pipecat"] = [remote(1)]

    async with app.run_test(size=(120, 40)) as pilot:
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        await pilot.press("y")
        await until(pilot, lambda: in_repo(app))
        assert in_repo(app, "pipecat-ai/pipecat")
        await until(pilot, lambda: pr_ids(app) == ["pipecat-ai/pipecat#1"])

    # Next time, it opens straight away.
    app, github = make_app(tmp_path, cwd=checkout)
    async with app.run_test(size=(120, 40)) as pilot:
        await until(pilot, lambda: in_repo(app))
        assert in_repo(app, "pipecat-ai/pipecat")


def transcript(app: PurrApp) -> str:
    log = app.main.repo.session.query_one("#transcript")
    return "\n".join(strip.text for strip in log.lines)


def decision(app: PurrApp) -> str:
    box = app.main.repo.session.query_one("#pr-decision")
    return str(box.render()) if box.display else ""


async def test_reviewing_from_the_keyboard(tmp_path):
    github = FakeGitHub()
    agent, writer = FakeAgent(), FakeWriter()
    config = Config(data_dir=tmp_path / "data", poll_interval=3600)
    purr = Purr(
        config,
        github=github,
        agent=agent,
        repos=FakeRepos(tmp_path / "data"),
        writer=writer,
        recap_agent=FakeRecapAgent(),
        handle_sigint=False,
    )
    app = PurrApp(purr)
    github.open["pipecat-ai/pipecat"] = [remote(1, title="Message bus"), remote(2)]

    async with app.run_test(size=(140, 45)) as pilot:
        await until(pilot, lambda: home(app))
        await app.bus.add_repo("pipecat-ai/pipecat")
        await until(pilot, lambda: "pipecat-ai/pipecat" in app.repo_names)
        await pilot.press("1")
        await until(pilot, lambda: len(pr_ids(app)) == 2)

        # r reviews the selected PR and assigns you (it came from the pool).
        await pilot.press("r")
        await until(pilot, lambda: "Approve" in decision(app) or "request changes" in decision(app))
        assert agent.runs[0]["pr"].number in (1, 2)
        reviewed = agent.runs[0]["pr"]
        assert writer.assigned == [reviewed]
        assert "Bash(gh pr diff)" in transcript(app)
        assert "[a] approve" in decision(app)

        # a posts the proposed review.
        await pilot.press("a")
        await until(pilot, lambda: writer.reviews)
        assert writer.reviews[0]["event"] == "REQUEST_CHANGES"
        await until(pilot, lambda: not decision(app))

        # Next review asks for permission first; y allows it.
        agent.ask = ("Bash", {"command": "uv run pytest"}, "not pre-approved")
        await pilot.press("r")
        await until(pilot, lambda: "[y] allow" in decision(app))
        assert "uv run pytest" in decision(app)
        await pilot.press("y")
        await until(pilot, lambda: agent.answers == [True])

        # enter goes to the prompt; a follow-up continues the session.
        await until(pilot, lambda: reviewed not in app.purr.runner.get_worker("reviews").running)
        await pilot.press("enter")
        await pilot.press(*"is it safe?", "enter")
        await until(pilot, lambda: agent.runs[-1]["prompt"] == "is it safe?")
        assert "> is it safe?" in transcript(app)


async def test_alerts_and_the_switcher(tmp_path):
    app, github = make_app(tmp_path)
    github.open["pipecat-ai/pipecat"] = [remote(1, title="Message bus")]
    github.assigned = [remote(7, repo="daily-co/daily-x", title="Win CI fix")]

    async with app.run_test(size=(140, 45)) as pilot:
        alert = app.main.query_one("#alert")
        await app.bus.add_repo("pipecat-ai/pipecat")
        await until(pilot, lambda: app.repo_names == ["daily-co/daily-x", "pipecat-ai/pipecat"])
        await pilot.press("2")
        await until(pilot, lambda: in_repo(app, "pipecat-ai/pipecat"))

        # The assigned PR elsewhere gets reviewed; that shows up as an alert here.
        await until(pilot, lambda: "daily-x#7 review done" in str(alert.render()))
        await pilot.press("ctrl+n")
        await until(pilot, lambda: in_repo(app, "daily-co/daily-x"))
        await until(pilot, lambda: app.main.repo.selected_pr() is not None)
        assert app.main.repo.selected_pr().number == 7
        await until(pilot, lambda: not app.alerts)
        assert "🔔" not in str(alert.render())

        # ctrl+p finds a PR by its title and jumps to it.
        await pilot.press("ctrl+p")
        await pilot.press(*"message bus")
        await pilot.pause(0.3)
        await pilot.press("enter")
        await until(pilot, lambda: in_repo(app, "pipecat-ai/pipecat"))
        await until(pilot, lambda: getattr(app.main.repo.selected_pr(), "number", None) == 1)


async def test_coming_back_shows_a_recap(tmp_path):
    from datetime import UTC, datetime, timedelta

    from purr.models import PRRef
    from purr.tui.recap import RecapScreen

    app, github = make_app(tmp_path)
    github.assigned = [remote(7, repo="daily-co/daily-x", title="Win CI fix")]
    async with app.run_test(size=(140, 45)) as pilot:
        # The assigned PR gets reviewed while we're "away".
        await until(pilot, lambda: app.repo_names == ["daily-co/daily-x"])
        ref = PRRef("daily-co/daily-x", 7)

        async def reviewed():
            return bool(await app.purr.store.pending_actions(ref))

        while not await reviewed():
            await pilot.pause(0.02)
        app._last_active = datetime.now(UTC) - timedelta(hours=1)

        await pilot.press("down")  # any key: we're back
        await until(pilot, lambda: isinstance(app.screen, RecapScreen))
        recap = app.screen
        await until(pilot, lambda: recap.query_one("#recap-items").option_count > 0)
        assert "need you" in str(recap.query_one("#recap-headline").render())

        await pilot.press("enter")
        await until(pilot, lambda: in_repo(app, "daily-co/daily-x"))

        # w shows the latest recap again.
        await pilot.press("escape", "w")
        await until(pilot, lambda: isinstance(app.screen, RecapScreen))


def option_texts(app: PurrApp) -> list[str]:
    """The PR list as drawn, one line per option."""
    options = app.main.repo.query_one(OptionList)
    return [strip.text for strip in options.render_lines(options.region.reset_offset)]


async def test_looking_at_a_pr_before_reviewing_it(tmp_path):
    from purr.models import FileChange, PRDetails, PRRef

    app, github = make_app(tmp_path)
    github.open["pipecat-ai/pipecat"] = [
        remote(1, title="Message bus", author="alice", created_at="2026-11-18T10:00:00Z"),
        remote(2, title="Taken", assignees=("bob",)),
        *[remote(n) for n in range(3, 20)],
    ]
    github.details[PRRef("pipecat-ai/pipecat", 1)] = PRDetails(
        body="Fixes a race in the bus.<!-- template stuff -->",
        base="main",
        head="fix-bus",
        additions=22,
        deletions=4,
        changed_files=1,
        files=(FileChange("src/bus.py", 22, 4),),
        checks="SUCCESS",
    )

    async with app.run_test(size=(180, 45)) as pilot:
        await app.bus.add_repo("pipecat-ai/pipecat")
        await until(pilot, lambda: "pipecat-ai/pipecat" in app.repo_names)
        await pilot.press("1")
        await until(pilot, lambda: len(pr_ids(app)) == 19)

        # Columns, and the PR someone else took goes last, in its own group.
        assert pr_ids(app)[-1] == "pipecat-ai/pipecat#2"
        lines = option_texts(app)
        assert lines[0].split() == ["#", "TITLE", "ASSIGNED", "AUTHOR", "OPENED"]
        assert "OTHERS REVIEWING (1)" in "\n".join(lines)
        row = next(line for line in lines if "Message bus" in line)
        assert "#1" in row and "alice" in row

        # The first PR (newest) is selected; go to #1 and its description shows.
        app.main.repo.select("pipecat-ai/pipecat#1", repo="pipecat-ai/pipecat")
        await until(pilot, lambda: "Fixes a race in the bus." in transcript(app))
        assert "template stuff" not in transcript(app)
        assert "src/bus.py" in transcript(app)
        header = str(app.main.repo.session.query_one("#pr-header").render())
        assert "main ← fix-bus" in header and "✓ checks pass" in header
        assert "by alice" in header and "r to review it and take it" in header
        # PRs below the cursor were fetched ahead, in one batch.
        await until(pilot, lambda: len(github.details_calls) >= 2)
        assert len(github.details_calls[-1]) > 1

        # r on someone else's PR asks first.
        app.main.repo.select("pipecat-ai/pipecat#2", repo="pipecat-ai/pipecat")
        await pilot.pause(0.05)
        await pilot.press("r")
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        await pilot.press("n")
        await until(pilot, lambda: isinstance(app.screen, MainScreen))
        assert app.purr.agent.runs == []
