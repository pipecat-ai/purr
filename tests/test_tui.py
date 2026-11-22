"""Drive the TUI with Textual's pilot, on top of a real Purr process and a fake GitHub."""

import asyncio
import subprocess
from collections.abc import Callable

from textual.widgets import Input, OptionList

from purr.app import Purr
from purr.config import Config
from purr.store import Store
from purr.tui import PurrApp
from purr.tui.prompt import HistorySearchScreen
from purr.tui.screens import AddRepoScreen, ConfirmScreen, MainScreen
from tests.fakes import (
    FakeAgent,
    FakeGitHub,
    FakeLead,
    FakeRecapAgent,
    FakeRepos,
    FakeWriter,
    remote,
)


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
        lead=FakeLead(),
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


def focused(app: PurrApp) -> str | None:
    return app.focused.id if app.focused else None


def header(app: PurrApp) -> str:
    return str(app.main.repo.session.query_one("#pr-header").render())


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
        lead=FakeLead(),
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
        # What it found, and what the lead planned (you asked: high priority).
        await until(pilot, lambda: "Review: found 1 issue" in header(app))
        assert "Full review · high priority · Looks real." in header(app)

        # a posts the proposed review.
        await pilot.press("a")
        await until(pilot, lambda: writer.reviews)
        assert writer.reviews[0]["event"] == "REQUEST_CHANGES"
        await until(pilot, lambda: not decision(app))

        # Next review asks for permission first; a allows it, like approving.
        agent.ask = ("Bash", {"command": "uv run pytest"}, "not pre-approved")
        await pilot.press("r")
        await until(pilot, lambda: "[a] allow" in decision(app))
        assert "uv run pytest" in decision(app)
        await pilot.press("a")
        await until(pilot, lambda: agent.answers == [True])
        agent.ask = None

        # enter (or o) opens the PR's pane, enter again goes to its prompt, and a
        # follow-up continues the session. esc goes back a step at a time.
        await until(pilot, lambda: reviewed not in app.purr.runner.get_worker("reviews").running)
        session = app.main.repo.session
        await pilot.press("enter")
        assert focused(app) == "transcript" and session.has_focus_within
        await pilot.press("enter")
        assert focused(app) == "prompt"
        await pilot.press(*"is it safe?", "enter")
        await until(pilot, lambda: agent.runs[-1]["prompt"] == "is it safe?")
        assert "> is it safe?" in transcript(app)

        # The prompt keeps a history: up and down, like a shell...
        await until(pilot, lambda: reviewed not in app.purr.runner.get_worker("reviews").running)
        await pilot.press(*"tests?", "enter")
        await until(pilot, lambda: agent.runs[-1]["prompt"] == "tests?")
        prompt = app.main.repo.session.query_one("#prompt")
        await pilot.press(*"draft")
        await pilot.press("up")
        assert prompt.text == "tests?"
        await pilot.press("up", "up")
        assert prompt.text == "is it safe?"
        await pilot.press("down", "down")
        assert prompt.text == "draft"
        # ...readline keys move around the line...
        await pilot.press("ctrl+a", "ctrl+f", "ctrl+f")
        assert prompt.cursor_location == (0, 2)
        await pilot.press("ctrl+e", "ctrl+b")
        assert prompt.cursor_location == (0, 4)
        # ...and ctrl+r searches everything asked, on any PR.
        while len(await app.purr.store.prompts()) < 2:  # recorded by the reviews worker
            await pilot.pause(0.02)
        await pilot.press("ctrl+u", "ctrl+r")
        await until(pilot, lambda: isinstance(app.screen, HistorySearchScreen))
        await pilot.press(*"safe", "enter")
        await until(pilot, lambda: isinstance(app.screen, MainScreen))
        assert prompt.text == "is it safe?" and focused(app) == "prompt"

        # Several lines: ctrl+j, or a backslash at the end, starts a new one.
        await until(pilot, lambda: reviewed not in app.purr.runner.get_worker("reviews").running)
        await pilot.press("ctrl+u", *"first", "ctrl+j", *"second\\", "enter", *"third", "enter")
        await until(pilot, lambda: agent.runs[-1]["prompt"] == "first\nsecond\nthird")
        assert prompt.text == ""
        # ↑ on the first line goes back in history; on a later line, up a line.
        await pilot.press("up")
        assert prompt.text == "first\nsecond\nthird"

        # Page up and down scroll the transcript while typing.
        log = app.main.repo.session.query_one("#transcript")
        for i in range(60):
            log.write(f"line {i}")
        await pilot.pause(0.05)
        bottom = log.scroll_y
        await pilot.press("pageup")
        await until(pilot, lambda: log.scroll_y < bottom)
        assert focused(app) == "prompt"

        await pilot.press("escape")
        assert focused(app) == "transcript"
        await pilot.press("escape")
        assert focused(app) == "prs" and not session.has_focus_within


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
        await until(pilot, lambda: "daily-x#7 review: found 1 issue" in str(alert.render()))
        await pilot.press("ctrl+n")
        await until(pilot, lambda: in_repo(app, "daily-co/daily-x"))
        await until(pilot, lambda: app.main.repo.selected_pr() is not None)
        assert app.main.repo.selected_pr().number == 7
        await until(pilot, lambda: not app.alerts)
        assert "🔔" not in str(alert.render())

        # Seen is remembered (the next run won't show it as news).
        async def seen_stored() -> bool:
            return await app.purr.store.unseen() == []

        while not await seen_stored():
            await pilot.pause(0.02)

        # ctrl+p finds a PR by its title and jumps to it.
        await pilot.press("ctrl+p")
        await pilot.press(*"message bus")
        await pilot.pause(0.3)
        await pilot.press("enter")
        await until(pilot, lambda: in_repo(app, "pipecat-ai/pipecat"))
        await until(pilot, lambda: getattr(app.main.repo.selected_pr(), "number", None) == 1)

        # No news elsewhere, but daily-x#7 still needs you: a short signal, and
        # 0 goes to the repo list, which says where.
        await until(pilot, lambda: "⚠ 1 needs you in other repos" in str(alert.render()))
        await pilot.press("0")
        await until(pilot, lambda: home(app))
        assert str(alert.render()) == ""


async def test_news_survives_a_restart_and_shows_in_bold(tmp_path):
    from purr.models import PRRef

    app, github = make_app(tmp_path)
    github.open["pipecat-ai/pipecat"] = [remote(1, title="Message bus"), remote(2, title="Docs")]
    async with app.run_test(size=(140, 45)) as pilot:
        await app.bus.add_repo("pipecat-ai/pipecat")
        await until(pilot, lambda: "pipecat-ai/pipecat" in app.repo_names)
    # Something happens to #1 while Purr isn't running.
    store = await Store.open(tmp_path / "data" / "purr.db")
    await store.record_activity("review.finished", pr=PRRef("pipecat-ai/pipecat", 1))
    await store.close()

    app, github = make_app(tmp_path)
    github.open["pipecat-ai/pipecat"] = [remote(1, title="Message bus"), remote(2, title="Docs")]
    async with app.run_test(size=(140, 45)) as pilot:
        await until(pilot, lambda: "pipecat-ai/pipecat#1" in app.alerts)
        assert "pipecat#1 review: nothing found" in str(app.main.query_one("#alert").render())
        # In the repo, #2 (the newest) is selected; #1 is bold until you look at it.
        await pilot.press("1")
        await until(pilot, lambda: len(pr_ids(app)) == 2)

        def bold(title: str) -> bool:
            repo = app.main.repo
            pr = next(pr for pr in repo._prs.values() if pr.title == title)
            return "bold" in str(repo._prompt(pr).columns[4]._cells[0].style)

        assert bold("Message bus") and not bold("Docs")
        await pilot.press("n")
        await until(pilot, lambda: not bold("Message bus"))
        assert "pipecat-ai/pipecat#1" not in app.alerts


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
    github.diffs[PRRef("pipecat-ai/pipecat", 1)] = (
        "diff --git a/src/bus.py b/src/bus.py\n--- a/src/bus.py\n+++ b/src/bus.py\n"
        "@@ -1 +1 @@\n-    pass\n+    self.lock = asyncio.Lock()\n"
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
        # Then its diff, after the description.
        await until(pilot, lambda: "+    self.lock = asyncio.Lock()" in transcript(app))
        assert transcript(app).index("Fixes a race") < transcript(app).index("src/bus.py")
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


async def test_moving_around_and_searching(tmp_path):
    app, github = make_app(tmp_path)
    github.open["pipecat-ai/pipecat"] = [
        remote(1, title="Message bus"),
        remote(2, title="Zoom STT fix"),
        remote(3, title="Bus shutdown race"),
    ]
    async with app.run_test(size=(160, 45)) as pilot:
        await app.bus.add_repo("pipecat-ai/pipecat")
        await until(pilot, lambda: "pipecat-ai/pipecat" in app.repo_names)
        await pilot.press("o")  # o opens the repo, like enter
        await until(pilot, lambda: len(pr_ids(app)) == 3)
        repo = app.main.repo

        def shown() -> int | None:
            return repo.session.pr.number if repo.session.pr else None

        # n and p move through the list, newest first...
        await until(pilot, lambda: shown() == 3)
        await pilot.press("n")
        await until(pilot, lambda: shown() == 2)
        # ...and from the PR's pane too, which keeps the focus.
        await pilot.press("o")
        assert focused(app) == "transcript"
        await pilot.press("n")
        await until(pilot, lambda: shown() == 1)
        await pilot.press("p", "p")
        await until(pilot, lambda: shown() == 3)
        assert focused(app) == "transcript"

        # tab switches panes, from the prompt too; shift+tab the same.
        await pilot.press("tab")
        assert focused(app) == "prs"
        await pilot.press("tab")
        assert focused(app) == "transcript"
        await pilot.press("enter")
        assert focused(app) == "prompt"
        await pilot.press("shift+tab")
        assert focused(app) == "prs"
        await pilot.press("o")
        assert focused(app) == "transcript" and shown() == 3

        # / searches the list (title, author, number), and esc clears it.
        await pilot.press("slash")
        assert focused(app) == "search"
        await pilot.press(*"bus")
        await until(pilot, lambda: pr_ids(app) == ["pipecat-ai/pipecat#3", "pipecat-ai/pipecat#1"])
        await pilot.press("enter")
        assert focused(app) == "prs"
        await pilot.press("n")
        await until(pilot, lambda: shown() == 1)
        await pilot.press("escape")
        await until(pilot, lambda: len(pr_ids(app)) == 3)
        assert in_repo(app) and not repo.query_one("#search").display
        await pilot.press("slash", *"nothing like it")
        await until(pilot, lambda: pr_ids(app) == [])
        assert "No PRs match" in "\n".join(option_texts(app))
        await pilot.press("escape")
        await until(pilot, lambda: len(pr_ids(app)) == 3)
        await pilot.press("escape")
        await until(pilot, lambda: home(app))


async def test_choosing_the_review_skill(tmp_path):
    from purr.tui.screens import InstructionsScreen, ReviewSkillScreen
    from tests.fakes import make_plugin

    app, github = make_app(tmp_path)
    agent = app.purr.agent
    app.purr._repos.repo_plugin = make_plugin(
        tmp_path / "plugin",
        {"prose-review": "Check the prose", "security-review": "Look for security problems"},
    )
    github.open["pipecat-ai/pipecat"] = [remote(1, title="Message bus")]

    async with app.run_test(size=(140, 45)) as pilot:
        await until(pilot, lambda: home(app))
        await app.bus.add_repo("pipecat-ai/pipecat")
        await until(pilot, lambda: "pipecat-ai/pipecat" in app.repo_names)
        await pilot.press("1")
        await until(pilot, lambda: len(pr_ids(app)) == 1)

        # Neither skill is clearly for reviewing PRs: the review asks, and waits.
        await pilot.press("r")
        await until(pilot, lambda: isinstance(app.screen, ReviewSkillScreen))
        assert not agent.runs
        await pilot.press("down", "enter")
        await until(pilot, lambda: agent.runs)
        assert agent.runs[0]["skill"] == "pipecat:security-review"
        assert await app.purr.store.review_skill("pipecat-ai/pipecat") == "pipecat:security-review"

        # i, then s, changes it: back to Purr's own review.
        await until(pilot, lambda: isinstance(app.screen, MainScreen))
        await pilot.press("i")
        assert isinstance(app.screen, InstructionsScreen)
        await pilot.press("s")
        await until(pilot, lambda: isinstance(app.screen, ReviewSkillScreen))
        items = app.screen.query_one(OptionList)
        assert items.highlighted == 1  # what reviews follow now
        await pilot.press("down", "enter")
        await until(pilot, lambda: isinstance(app.screen, MainScreen))
        for _ in range(100):
            if await app.purr.store.review_skill("pipecat-ai/pipecat") == "":
                break
            await pilot.pause(0.02)
        assert await app.purr.store.review_skill("pipecat-ai/pipecat") == ""


async def test_always_allowing_from_the_keyboard(tmp_path):
    from purr.tui.screens import AlwaysScreen

    app, github = make_app(tmp_path)
    agent = app.purr.agent
    agent.ask = ("Bash", {"command": "uv run pytest -x"}, "not pre-approved")
    github.open["pipecat-ai/pipecat"] = [
        remote(1, title="Message bus", author_association="MEMBER")
    ]

    async with app.run_test(size=(140, 45)) as pilot:
        await until(pilot, lambda: home(app))
        await app.bus.add_repo("pipecat-ai/pipecat")
        await until(pilot, lambda: "pipecat-ai/pipecat" in app.repo_names)
        await pilot.press("1")
        await until(pilot, lambda: len(pr_ids(app)) == 1)
        await pilot.press("r")
        await until(pilot, lambda: "[A] always" in decision(app))

        # A, then r: this repo, from now on.
        await pilot.press("A")
        await until(pilot, lambda: isinstance(app.screen, AlwaysScreen))
        await pilot.press("r")
        await until(pilot, lambda: agent.answers == [True])
        assert app.purr.grants.rules("pipecat-ai/pipecat") == ["Bash(uv run pytest:*)"]

        # The next review's same request isn't asked about.
        await until(pilot, lambda: not decision(app) or "[a] approve" in decision(app))
        await pilot.press("r")
        await until(pilot, lambda: len(agent.answers) == 2)
        assert agent.answers == [True, True]
        assert "allowed by your permission Bash(uv run pytest:*)" in transcript(app)
