"""Drive the TUI with Textual's pilot, on top of a real Purr process and a fake GitHub."""

import asyncio
import subprocess
from collections.abc import Callable

from textual.widgets import Input, OptionList

from purr.app import Purr
from purr.config import Config
from purr.messages import DismissAction, WatchRepo
from purr.models import PRDetails, PRRef, PRState
from purr.store import Store
from purr.tui import PurrApp
from purr.tui.dialogs import AddRepoScreen, ConfirmScreen
from purr.tui.lists import HEADING
from purr.tui.prompt import HistorySearchScreen
from purr.tui.screens import MainScreen
from tests.fakes import (
    FakeAgent,
    FakeGitHub,
    FakeLead,
    FakeRecapAgent,
    FakeRepos,
    FakeSuggester,
    FakeWriter,
    remote,
)


def make_app(tmp_path, cwd=None, **settings) -> tuple[PurrApp, FakeGitHub]:
    github = FakeGitHub()
    config = Config(data_dir=tmp_path / "data", poll_interval=3600, **settings)
    purr = Purr(
        config,
        github=github,
        agent=FakeAgent(),
        repos=FakeRepos(tmp_path / "data"),
        writer=FakeWriter(github),
        recap_agent=FakeRecapAgent(),
        lead=FakeLead(),
        suggester=FakeSuggester(),
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
    return [i for i in ids if i and not i.startswith(HEADING)]  # the PRs, not the groups


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
        # ...a real one is added, by URL too. Watched repos come first.
        app.screen.query_one(Input).value = "https://github.com/Pipecat-AI/pipecat"
        await pilot.press("enter")
        await until(pilot, lambda: home(app))
        await until(pilot, lambda: app.repo_names == ["pipecat-ai/pipecat", "daily-co/daily-x"])

        # 1 opens pipecat; its PRs arrive with the first poll.
        await pilot.press("1")
        await until(pilot, lambda: in_repo(app))
        assert in_repo(app, "pipecat-ai/pipecat")
        await until(pilot, lambda: len(pr_ids(app)) == 2)

        # A PR found by a later poll shows up without doing anything.
        github.updates["pipecat-ai/pipecat"] = [remote(3, title="New one")]
        await app.purr.runner.get_worker("github").poll()
        await until(pilot, lambda: "pipecat-ai/pipecat#3" in pr_ids(app))

        # Jumping between repos, back and forth with ctrl+o, and home.
        await pilot.press("2")
        await until(pilot, lambda: in_repo(app, "daily-co/daily-x"))
        await pilot.press("ctrl+o")
        await until(pilot, lambda: in_repo(app, "pipecat-ai/pipecat"))
        await pilot.press("escape")
        await until(pilot, lambda: home(app))
        await pilot.press("ctrl+o")
        await until(pilot, lambda: in_repo(app))
        await pilot.press("0")
        await until(pilot, lambda: home(app))

        # Removing pipecat (the first row) after confirming.
        app.main.repos.select_repo("pipecat-ai/pipecat")
        await pilot.press("minus")
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


def option_texts_of(screen) -> list[str]:
    """What a screen's list says, a line per option (a table's cells, side by side)."""
    from rich.console import Console

    console = Console(width=200, color_system=None)
    items = screen.query_one(OptionList)
    lines = []
    for option in items.options:
        with console.capture() as capture:
            console.print(option.prompt)
        lines.append(capture.get())
    return lines


def footer(app: PurrApp) -> set[str]:
    """What the footer's keys say they do."""
    from purr.tui.keybar import KeyBar

    return {what for bar in app.screen.query(KeyBar) for _, what in bar.keys}


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
        suggester=FakeSuggester(),
        handle_sigint=False,
    )
    app = PurrApp(purr)
    github.open["pipecat-ai/pipecat"] = [remote(1, title="Message bus"), remote(2)]

    async with app.run_test(size=(140, 45)) as pilot:
        await until(pilot, lambda: home(app))
        await app.bus.request(WatchRepo(repo="pipecat-ai/pipecat"))
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
        # What it found, and how deep it went (the plan shows only while it's under way).
        await until(pilot, lambda: "review: found 1 issue" in header(app))
        assert "Full review: found 1 issue" in header(app) and "Planned" not in header(app)

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

        # o opens the PR's pane to read it; enter there (or right away on the list)
        # goes to its prompt, and a follow-up continues the session. esc goes back
        # a step at a time.
        await until(pilot, lambda: reviewed not in app.purr.runner.get_worker("reviews").running)
        session = app.main.repo.session
        await pilot.press("o")
        assert focused(app) == "transcript" and session.has_focus_within
        await pilot.press("enter")
        assert focused(app) == "prompt"
        # The agent's done: the empty prompt suggests what to ask next, and tab
        # (or →) takes it, as in Claude Code.
        prompt = session.query_one("#prompt")
        await until(pilot, lambda: prompt.suggested == "Is the race on shutdown real?")
        assert "Is the race on shutdown real?" in str(prompt.placeholder)
        await pilot.press("tab")
        assert prompt.text == "Is the race on shutdown real?" and focused(app) == "prompt"
        # Once: nothing more after the cursor (TextArea's own suggestion), nor from →.
        assert prompt.suggestion == ""
        await pilot.press("right")
        assert prompt.text == "Is the race on shutdown real?"
        await pilot.press("ctrl+u")
        assert prompt.text == ""
        await pilot.press(*"is it safe?", "enter")
        await until(pilot, lambda: agent.runs[-1]["prompt"].endswith("is it safe?"))
        assert "> is it safe?" in transcript(app)

        # The prompt keeps a history: up and down, like a shell...
        await until(pilot, lambda: reviewed not in app.purr.runner.get_worker("reviews").running)
        await pilot.press(*"tests?", "enter")
        await until(pilot, lambda: agent.runs[-1]["prompt"].endswith("tests?"))
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
        await until(pilot, lambda: agent.runs[-1]["prompt"].endswith("first\nsecond\nthird"))
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
        # Reading the pane, ctrl+v and alt+v (meta+v) page down and up, as in Emacs.
        log.focus()
        await pilot.press("ctrl+v")
        await until(pilot, lambda: log.scroll_y == bottom)
        await pilot.press("alt+v")
        await until(pilot, lambda: log.scroll_y < bottom)
        await pilot.press("ctrl+v")
        await until(pilot, lambda: log.scroll_y == bottom)
        prompt.focus()

        # The footer has the keys for where you are: typing, the prompt's...
        await until(pilot, lambda: {"Back", "Search history"} <= footer(app))
        assert not footer(app) & {"Home", "Review", "Approve", "Ask"}

        # esc leaves the prompt for the PR's pane; esc again goes home, and what
        # you'd typed is there when you're back.
        await pilot.press("escape")
        assert in_repo(app) and focused(app) == "transcript"
        # ...reading the PR, the pane's...
        await until(
            pilot, lambda: {"Home", "Ask", "Review", "Thinking", "Claude Code"} <= footer(app)
        )
        # (the repo's keys, the list's too; not those that work everywhere: ? has them)
        assert {"Mark", "Search"} <= footer(app) and not footer(app) & {"Back", "Settings", "Quit"}
        await pilot.press("t")  # the pane's keys work from the list too, and back
        assert app.full_thinking
        await pilot.press("tab", "t", "tab")
        assert not app.full_thinking and focused(app) == "transcript"
        # z zooms the PR's pane, and back: the focus stays where it was, here or the list.
        await pilot.press("z")
        assert not app.main.repo.query_one("#prs-pane").display
        await pilot.press("z")
        assert app.main.repo.query_one("#prs-pane").display and focused(app) == "transcript"
        await pilot.press("tab", "z")
        assert focused(app) == "transcript"
        await pilot.press("z")
        assert focused(app) == "prs"
        await pilot.press("tab")
        await pilot.press("escape")
        await until(pilot, lambda: home(app))
        await pilot.press("o")
        await until(pilot, lambda: in_repo(app))
        assert focused(app) == "prs" and prompt.text == "first\nsecond\nthird"
        # ...and in the list, the list's.
        await until(pilot, lambda: {"Home", "PR", "Search", "Mark", "Approve"} <= footer(app))
        assert {"Claude Code", "Thinking"} <= footer(app) and not footer(app) & {"Ask", "Quit"}

        # The / suggestions close first.
        await pilot.press("enter")
        assert focused(app) == "prompt"
        prompt.set_text("")
        await pilot.press("slash")
        await until(pilot, lambda: session.active())
        await pilot.press("escape")
        assert not session.active() and in_repo(app) and focused(app) == "prompt"
        await pilot.press("escape", "escape")
        await until(pilot, lambda: home(app))


async def test_alerts_and_the_switcher(tmp_path):
    app, github = make_app(tmp_path)
    github.open["pipecat-ai/pipecat"] = [remote(1, title="Message bus")]
    github.assigned = [remote(7, repo="daily-co/daily-x", title="Win CI fix")]

    async with app.run_test(size=(140, 45)) as pilot:
        alert = app.main.query_one("#alert")
        await app.bus.request(WatchRepo(repo="pipecat-ai/pipecat"))
        await until(pilot, lambda: app.repo_names == ["pipecat-ai/pipecat", "daily-co/daily-x"])
        await pilot.press("1")
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

        # Into another repo: its PRs show, never the last one's, even for a moment.
        session = app.main.repo.session
        await until(pilot, lambda: session.pr is not None)
        await app.open_repo("daily-co/daily-x")
        assert [pr.number for pr in app.main.repo._prs.values()] == [7]
        assert session.pr is None or session.pr.number == 7
        await until(pilot, lambda: getattr(session.pr, "number", None) == 7)


async def test_news_survives_a_restart_and_shows_in_bold(tmp_path):
    from purr.models import PRRef

    app, github = make_app(tmp_path)
    github.open["pipecat-ai/pipecat"] = [remote(1, title="Message bus"), remote(2, title="Docs")]
    async with app.run_test(size=(140, 45)) as pilot:
        await app.bus.request(WatchRepo(repo="pipecat-ai/pipecat"))
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
        await app.bus.request(WatchRepo(repo="pipecat-ai/pipecat"))
        await until(pilot, lambda: "pipecat-ai/pipecat" in app.repo_names)
        await pilot.press("1")
        await until(pilot, lambda: len(pr_ids(app)) == 19)

        # Columns, and the PR someone else took in its own group, before the pool.
        assert pr_ids(app)[0] == "pipecat-ai/pipecat#2"
        lines = option_texts(app)
        assert lines[0].split() == ["#", "TITLE", "ASSIGNED", "AUTHOR", "OPENED"]
        assert "OTHERS REVIEWING (1)" in "\n".join(lines)
        row = next(line for line in lines if "Message bus" in line)
        assert "#1" in row and "alice" in row

        # The first PR (newest) is selected; go to #1 and its description shows.
        app.main.repo.select("pipecat-ai/pipecat#1", repo="pipecat-ai/pipecat")
        await until(pilot, lambda: "Fixes a race in the bus." in transcript(app))
        assert "template stuff" not in transcript(app)
        # Then where it changes things (the diff itself is d's).
        await until(pilot, lambda: "1 file changed: src/bus.py" in transcript(app))
        assert "self.lock" not in transcript(app)
        header = str(app.main.repo.session.query_one("#pr-header").render())
        assert "main ← fix-bus" in header and "+22 −4 in 1 file" in header
        assert "✓ checks pass" in header
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


async def test_every_page_says_its_keys(tmp_path):
    from purr.tui.keyhelp import KeysScreen

    app, github = make_app(tmp_path)
    github.open["pipecat-ai/pipecat"] = [remote(1, title="Message bus")]
    github.diffs[__import__("purr.models").models.PRRef("pipecat-ai/pipecat", 1)] = (
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x\n+y\n"
    )

    async def keys(*where: str) -> str:
        """What ? lists, here: its sections and lines."""
        await until(pilot, lambda: "Keys" in footer(app))  # every page's footer says ?
        await pilot.press("question_mark")
        await until(pilot, lambda: isinstance(app.screen, KeysScreen))
        shown = str(app.screen._groups)
        await pilot.press("escape")
        await until(pilot, lambda: not isinstance(app.screen, KeysScreen))
        for section in where:
            assert section in shown, section
        return shown

    async with app.run_test(size=(140, 45)) as pilot:
        await app.bus.request(WatchRepo(repo="pipecat-ai/pipecat"))
        await until(pilot, lambda: "pipecat-ai/pipecat" in app.repo_names)
        home_keys = await keys("Home", "Everywhere")
        assert "Watch repo" in home_keys and "Repo 1–9" in home_keys and "1–9" in home_keys
        assert "Find" in home_keys and "News" in home_keys

        await pilot.press("1")
        await until(pilot, lambda: len(pr_ids(app)) == 1)
        list_keys = await keys("The PR list", "The repo", "Everywhere")
        assert "Mark" in list_keys and "Thinking" in list_keys  # hidden from its footer

        await pilot.press("o")
        pane_keys = await keys("The PR", "The repo", "Everywhere")
        assert "Page down" in pane_keys and "Ask" in pane_keys

        await pilot.press("d")
        await until(pilot, lambda: app.main.repo.diff_shown)
        diff_keys = await keys("The diff", "The repo", "Everywhere")
        assert "Select down" in diff_keys and "Ask" in diff_keys
        assert "Latest news" not in diff_keys  # ctrl+n moves in the diff


async def wheel(pilot, widget, *, down: bool = True, times: int = 5) -> None:
    """The mouse wheel over a widget, as the terminal sends it."""
    from textual import events

    x, y = widget.region.x + 5, widget.region.y + 5
    kind = events.MouseScrollDown if down else events.MouseScrollUp
    for _ in range(times):
        event = kind(None, x, y, 0, 0, 0, False, False, False, screen_x=x, screen_y=y)
        pilot.app.post_message(event)
        await pilot.pause(0.03)


async def test_the_mouse_scrolls_the_list_and_the_diff(tmp_path):
    from purr.models import PRRef

    app, github = make_app(tmp_path)
    github.open["pipecat-ai/pipecat"] = [remote(n) for n in range(1, 80)]
    lines = "".join(f" line {i}\n" for i in range(200))
    for n in range(1, 80):
        github.diffs[PRRef("pipecat-ai/pipecat", n)] = (
            f"diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1,200 +1,200 @@\n{lines}"
        )
    async with app.run_test(size=(140, 40)) as pilot:
        await app.bus.request(WatchRepo(repo="pipecat-ai/pipecat"))
        await until(pilot, lambda: "pipecat-ai/pipecat" in app.repo_names)
        await pilot.press("1")
        await until(pilot, lambda: len(pr_ids(app)) == 79)
        repo = app.main.repo

        # The wheel scrolls the list, and it stays there when the list is drawn again.
        prs = repo.query_one("#prs")
        await wheel(pilot, prs)
        scrolled = prs.scroll_offset.y
        assert scrolled > 0
        app.data_version += 1
        await repo.reload()
        await pilot.pause()
        assert prs.scroll_offset.y == scrolled

        # The diff: the wheel scrolls it, and the cursor comes along.
        await pilot.press("d", "down", "tab")  # (past the conversation, at the top)
        view = repo.diff.view
        await until(pilot, lambda: len(view._rows) > 100)
        await wheel(pilot, view, times=10)
        top = view.scroll_offset.y
        assert top > 0 and view.cursor >= top
        # A click puts the cursor on a line; shift+click selects up to another.
        await pilot.click("#diff", offset=(20, 3))
        clicked = view.cursor
        assert clicked == top + 3 and view.anchor is None
        await pilot.click("#diff", offset=(20, 6), shift=True)
        assert view.anchor == clicked and view.cursor == top + 6
        assert len(view.selected()) == 4
        # What it shows stays put when what's said on it changes (no jump to the cursor).
        await repo.diff.reload()
        assert view.scroll_offset.y == top


async def test_the_list_stops_at_its_ends_and_shows_its_headings(tmp_path):
    app, github = make_app(tmp_path)
    taken = [remote(n, assignees=("bob",)) for n in range(100, 140)]
    github.open["pipecat-ai/pipecat"] = [*taken, *[remote(n) for n in range(1, 41)]]
    async with app.run_test(size=(140, 30)) as pilot:
        await app.bus.request(WatchRepo(repo="pipecat-ai/pipecat"))
        await until(pilot, lambda: "pipecat-ai/pipecat" in app.repo_names)
        await pilot.press("1")
        await until(pilot, lambda: len(pr_ids(app)) == 80)
        prs = app.main.repo.query_one("#prs")
        lines = prs._index_to_line
        first = prs.highlighted

        # No wrapping round: down at the bottom, and up at the top, stay put.
        await pilot.press("end", "down")
        assert prs.highlighted == len(prs.options) - 1 and prs.scroll_offset.y > 0
        # Up to the pool's first PR: its heading comes into view with it.
        heading = prs.get_option_index(f"{HEADING}{PRState.NOT_REVIEWED}")
        while prs.highlighted != heading + 1:
            await pilot.press("up")
        assert lines[heading] >= prs.scroll_offset.y
        # And at the very top, the first group's heading and the columns.
        await pilot.press("home", "up")
        assert prs.highlighted == first - 1 and prs.scroll_offset.y == 0  # its heading


async def test_folding_a_group(tmp_path):
    app, github = make_app(tmp_path)
    github.open["pipecat-ai/pipecat"] = [remote(1), remote(2), remote(3, assignees=("bob",))]
    async with app.run_test(size=(140, 30)) as pilot:
        await app.bus.request(WatchRepo(repo="pipecat-ai/pipecat"))
        await until(pilot, lambda: "pipecat-ai/pipecat" in app.repo_names)
        await pilot.press("1")
        await until(pilot, lambda: len(pr_ids(app)) == 3)
        repo = app.main.repo
        prs = repo.query_one("#prs")
        heading = f"{HEADING}{PRState.NOT_REVIEWED}"

        def on(option_id: str) -> bool:
            return prs.highlighted_option.id == option_id

        # The cursor starts on the first PR; down, and it's on the next group's heading.
        await until(pilot, lambda: on("pipecat-ai/pipecat#3"))
        await pilot.press("down")
        assert on(heading)
        await until(pilot, lambda: "Fold/open" in footer(app) and "PR" not in footer(app))
        # tab folds it, the cursor staying on it; tab again opens it.
        await pilot.press("tab")
        assert pr_ids(app) == ["pipecat-ai/pipecat#3"] and on(heading)
        await pilot.press("tab")
        assert len(pr_ids(app)) == 3 and on(heading)
        # shift+tab is still the PR's pane.
        await pilot.press("shift+tab")
        assert repo.session.has_focus_within
        await pilot.press("shift+tab")
        assert on(heading)
        # enter folds or opens it too.
        await pilot.press("enter")
        assert pr_ids(app) == ["pipecat-ai/pipecat#3"]
        # On a PR, tab is still the PR's pane; n and p go from PR to PR, over headings.
        await pilot.press("up")
        assert on("pipecat-ai/pipecat#3")
        await until(pilot, lambda: "Fold/open" not in footer(app) and "PR" in footer(app))
        await pilot.press("tab")
        assert repo.session.has_focus_within
        await pilot.press("tab", "n")
        assert on("pipecat-ai/pipecat#3")  # nothing after it: #1 and #2 are folded
        # Going to a PR in a folded group (an alert, ctrl+p) opens it.
        repo.select("pipecat-ai/pipecat#1", repo="pipecat-ai/pipecat")
        await until(pilot, lambda: on("pipecat-ai/pipecat#1"))
        assert len(pr_ids(app)) == 3
        await pilot.press("p")
        assert on("pipecat-ai/pipecat#2")
        # Searching shows every match, folded or not.
        await pilot.press("p", "p", "tab", "slash", "2")
        await until(pilot, lambda: pr_ids(app) == ["pipecat-ai/pipecat#2"])


async def test_zooming_rewraps_the_pr(tmp_path, monkeypatch):
    # Textual's console, which RichLog measures what it writes against, is as wide as
    # the terminal: headless, COLUMNS (80 unless said).
    monkeypatch.setenv("COLUMNS", "160")
    app, github = make_app(tmp_path)
    github.open["pipecat-ai/pipecat"] = [remote(1)]
    github.details[PRRef("pipecat-ai/pipecat", 1)] = PRDetails(body="word " * 200)
    async with app.run_test(size=(160, 40)) as pilot:
        await app.bus.request(WatchRepo(repo="pipecat-ai/pipecat"))
        await until(pilot, lambda: "pipecat-ai/pipecat" in app.repo_names)
        await pilot.press("1")
        await until(pilot, lambda: len(pr_ids(app)) == 1)
        log = app.main.repo.session.query_one("#transcript")

        def widest() -> int:
            return max((line.cell_length for line in log.lines), default=0)

        await until(pilot, lambda: "word word" in transcript(app))
        narrow = widest()
        assert narrow <= log.size.width
        # Zoomed, the description fills the width; and back, it fits the pane again.
        await pilot.press("tab", "z")
        await until(pilot, lambda: widest() > narrow + 40)
        await pilot.press("z")
        await until(pilot, lambda: widest() <= log.size.width)
        assert focused(app) == "transcript"


async def test_the_theme_setting(tmp_path, monkeypatch):
    from purr.tui import theme

    monkeypatch.setattr(theme, "_palette", theme.palette())  # back as it was, after
    app, _ = make_app(tmp_path)
    assert app.theme == "solarized-dark" and theme.palette().code == "solarized-dark"
    nord, github = make_app(tmp_path / "nord", theme="nord")
    # The theme, and Purr's own colors from it: its primary color for numbers, ...
    assert nord.theme == "nord" and nord.current_theme.name == "nord"
    colors = theme.palette()
    assert colors.number.lower() == "#88c0d0" and colors.code == "nord"
    github.open["pipecat-ai/pipecat"] = [remote(7)]
    async with nord.run_test(size=(120, 30)) as pilot:
        await nord.bus.request(WatchRepo(repo="pipecat-ai/pipecat"))
        await until(pilot, lambda: "pipecat-ai/pipecat" in nord.repo_names)
        await pilot.press("1")
        await until(pilot, lambda: nord.main.repo.selected_pr() is not None)
        row = nord.main.repo._prompt(nord.main.repo.selected_pr())
        number = row.columns[2]._cells[0]
        assert colors.number.lower() in str(number.style).lower()


async def test_esc_unmarks_marked_prs_first(tmp_path):
    app, github = make_app(tmp_path)
    github.open["pipecat-ai/pipecat"] = [remote(1), remote(2)]
    async with app.run_test(size=(140, 30)) as pilot:
        await app.bus.request(WatchRepo(repo="pipecat-ai/pipecat"))
        await until(pilot, lambda: "pipecat-ai/pipecat" in app.repo_names)
        await pilot.press("1")
        await until(pilot, lambda: len(pr_ids(app)) == 2)
        repo = app.main.repo
        await pilot.press("space", "space")
        assert len(repo._marked) == 2
        await until(pilot, lambda: "Unmark" in footer(app))
        # esc (or ctrl+g) lets go of the marks, before going home.
        await pilot.press("escape")
        assert not repo._marked and in_repo(app)
        await until(pilot, lambda: "Home" in footer(app) and "Unmark" not in footer(app))
        await pilot.press("space", "ctrl+g")
        assert not repo._marked and in_repo(app)
        await pilot.press("escape")
        await until(pilot, lambda: home(app))


async def test_moving_around_and_searching(tmp_path):
    app, github = make_app(tmp_path)
    github.open["pipecat-ai/pipecat"] = [
        remote(1, title="Message bus"),
        remote(2, title="Zoom STT fix"),
        remote(3, title="Bus shutdown race"),
    ]
    async with app.run_test(size=(160, 45)) as pilot:
        await app.bus.request(WatchRepo(repo="pipecat-ai/pipecat"))
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

        # / searches the list (title, author, number).
        bus = ["pipecat-ai/pipecat#3", "pipecat-ai/pipecat#1"]
        await pilot.press("slash")
        assert focused(app) == "search"
        await pilot.press(*"bus")
        await until(pilot, lambda: pr_ids(app) == bus)
        await pilot.press("enter")
        assert focused(app) == "prs"
        await pilot.press("n")
        await until(pilot, lambda: shown() == 1)

        # esc in the search box goes back to the list, keeping the search, which is still
        # there after going home and back...
        await pilot.press("slash", "escape")
        assert in_repo(app) and focused(app) == "prs" and pr_ids(app) == bus
        await pilot.press("0")
        await until(pilot, lambda: home(app))
        await pilot.press("o")
        await until(pilot, lambda: in_repo(app))
        assert pr_ids(app) == bus and repo.query_one("#search").value == "bus"
        assert repo.query_one("#search").display and focused(app) == "prs"
        # ...until esc clears it: every PR again, the cursor on the one it was on. esc
        # again goes home.
        await until(pilot, lambda: "Clear search" in footer(app))
        await pilot.press("escape")
        await until(pilot, lambda: len(pr_ids(app)) == 3)
        assert in_repo(app) and not repo.query_one("#search").display and shown() == 1
        await until(pilot, lambda: "Home" in footer(app))
        await pilot.press("escape")
        await until(pilot, lambda: home(app))
        await pilot.press("o")
        await until(pilot, lambda: in_repo(app))

        # Deleting it clears it too.
        await pilot.press("slash", *"bus")
        await until(pilot, lambda: pr_ids(app) == bus)
        await pilot.press("ctrl+u")
        await until(pilot, lambda: len(pr_ids(app)) == 3)
        await pilot.press("enter")
        assert focused(app) == "prs" and not repo.query_one("#search").display
        await pilot.press("slash", *"nothing like it")
        await until(pilot, lambda: pr_ids(app) == [])
        assert "No PRs match" in "\n".join(option_texts(app))
        await pilot.press("ctrl+u", "enter")
        await until(pilot, lambda: len(pr_ids(app)) == 3)
        await pilot.press("escape")
        await until(pilot, lambda: home(app))


async def test_choosing_the_review_skill(tmp_path):
    from purr.tui.dialogs import ReviewSkillScreen, SettingsScreen
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
        await app.bus.request(WatchRepo(repo="pipecat-ai/pipecat"))
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
        assert isinstance(app.screen, SettingsScreen)
        await pilot.press("s")
        await until(pilot, lambda: isinstance(app.screen, ReviewSkillScreen))
        items = app.screen.query_one(OptionList)
        assert items.highlighted == 1  # what reviews follow now
        await pilot.press("down", "enter")
        await until(pilot, lambda: isinstance(app.screen, SettingsScreen))
        await pilot.press("escape")
        await until(pilot, lambda: isinstance(app.screen, MainScreen))
        for _ in range(100):
            if await app.purr.store.review_skill("pipecat-ai/pipecat") == "":
                break
            await pilot.pause(0.02)
        assert await app.purr.store.review_skill("pipecat-ai/pipecat") == ""


async def test_always_allowing_from_the_keyboard(tmp_path):
    from purr.tui.dialogs import AlwaysScreen

    app, github = make_app(tmp_path)
    agent = app.purr.agent
    agent.ask = ("Bash", {"command": "uv run pytest -x"}, "", ["Bash(uv run pytest:*)"])
    github.open["pipecat-ai/pipecat"] = [remote(1, title="Message bus")]

    async with app.run_test(size=(140, 45)) as pilot:
        await until(pilot, lambda: home(app))
        await app.bus.request(WatchRepo(repo="pipecat-ai/pipecat"))
        await until(pilot, lambda: "pipecat-ai/pipecat" in app.repo_names)
        await pilot.press("1")
        await until(pilot, lambda: len(pr_ids(app)) == 1)
        await pilot.press("r")
        await until(pilot, lambda: "[A] always" in decision(app))

        # A shows Claude Code's suggested rule; r keeps it for this repo.
        await pilot.press("A")
        await until(pilot, lambda: isinstance(app.screen, AlwaysScreen))
        assert "Bash(uv run pytest:*)" in str(app.screen.query("Label")[1].render())
        await pilot.press("r")
        await until(pilot, lambda: agent.answers == [True])
        assert agent.remembered == ["repo"]


async def test_a_shell_in_the_worktree_and_claude_code(tmp_path, monkeypatch):
    monkeypatch.setattr("purr.tui.pr_keys.claude_cli", lambda: "claude")  # not this machine's
    app, github = make_app(tmp_path)
    github.open["pipecat-ai/pipecat"] = [remote(1, title="Message bus")]
    ran: list[tuple] = []

    async def run_outside(command, cwd, env=None, *, banner=""):
        ran.append((command, cwd, env or {}, banner))
        return 0

    app.run_outside = run_outside
    worktree = str(tmp_path / "data/worktrees/pipecat-ai/pipecat/1")

    async with app.run_test(size=(140, 45)) as pilot:
        await until(pilot, lambda: home(app))
        await app.bus.request(WatchRepo(repo="pipecat-ai/pipecat"))
        await until(pilot, lambda: "pipecat-ai/pipecat" in app.repo_names)
        await pilot.press("1")
        await until(pilot, lambda: len(pr_ids(app)) == 1)

        # ! works before any review: the worktree is made for it.
        await pilot.press("exclamation_mark")
        await until(pilot, lambda: ran)
        command, cwd, env, banner = ran[-1]
        assert cwd == worktree and env["PURR_PR"] == "pipecat-ai/pipecat#1"
        assert "Exit the shell to go back to Purr." in banner

        # After a review, the header says where its worktree is; c opens its session there.
        await pilot.press("r")
        await until(pilot, lambda: "[a] approve" in decision(app))
        assert header(app).endswith(f"\nWorktree {worktree}")  # just where, no key hints
        await pilot.press("c")
        await until(pilot, lambda: len(ran) == 2)
        command, cwd, env, banner = ran[-1]
        assert command[:2] == ["claude", "--resume"] and cwd == worktree
        assert "/exit goes back to Purr." in banner

        # No Claude Code to run, or it doesn't start: a dialog says so.
        from purr.tui.dialogs import NoticeScreen

        monkeypatch.setattr("purr.tui.pr_keys.claude_cli", lambda: None)
        await pilot.press("c")
        await until(pilot, lambda: isinstance(app.screen, NoticeScreen))
        await pilot.press("escape")
        monkeypatch.setattr("purr.tui.pr_keys.claude_cli", lambda: "claude")

        async def not_found(command, cwd, env=None, *, banner=""):
            return None  # it couldn't be started

        app.run_outside = not_found
        await pilot.press("c")
        await until(pilot, lambda: isinstance(app.screen, NoticeScreen))


async def test_slash_commands(tmp_path):
    from purr.models import PRRef
    from purr.tui.prompt import PromptArea

    app, github = make_app(tmp_path)
    agent = app.purr.agent
    github.open["pipecat-ai/pipecat"] = [remote(1, title="Message bus")]
    ran: list[tuple] = []

    async def run_outside(command, cwd, env=None, *, banner=""):
        ran.append((command, cwd))
        return 0

    app.run_outside = run_outside

    def suggested() -> list[str]:
        box = app.main.repo.session.query_one("#suggestions", OptionList)
        if not box.display:
            return []
        return [str(box.get_option_at_index(i).prompt) for i in range(box.option_count)]

    async with app.run_test(size=(140, 45)) as pilot:
        await until(pilot, lambda: home(app))
        await app.bus.request(WatchRepo(repo="pipecat-ai/pipecat"))
        await until(pilot, lambda: "pipecat-ai/pipecat" in app.repo_names)
        await pilot.press("1")
        await until(pilot, lambda: len(pr_ids(app)) == 1)
        await pilot.press("r")
        await until(pilot, lambda: "[a] approve" in decision(app))
        prompt = app.main.repo.session.query_one(PromptArea)
        prompt.focus()

        # / shows what you can type; tab fills in the highlighted one, enter runs it.
        await pilot.press("slash", "c", "o")
        await until(pilot, lambda: len(suggested()) == 3)
        assert suggested()[0].startswith("/cost")
        await pilot.press("down", "tab")
        assert prompt.text == "/compact "
        prompt.clear()
        await pilot.press("slash", "c", "o", "s", "enter")  # fills it in...
        assert prompt.text == "/cost "
        await pilot.press("enter")  # ...and runs it
        await until(pilot, lambda: "1 runs" in transcript(app))
        assert len(agent.runs) == 1  # Purr's own: the agent never saw it

        await pilot.press(*"/model opus", "enter")
        await until(pilot, lambda: "uses opus" in transcript(app))
        assert (await app.purr.store.get_pr(PRRef("pipecat-ai/pipecat", 1))).model == "opus"

        await pilot.press(*"/help", "enter")
        await until(pilot, lambda: "Purr's commands" in transcript(app))
        await pilot.press(*"/shell", "enter")
        await until(pilot, lambda: ran)

        # Anything else goes to the agent as typed.
        await pilot.press(*"/compact", "enter")
        await until(pilot, lambda: len(agent.runs) == 2)
        assert agent.runs[1]["prompt"] == "/compact"
        assert agent.runs[1]["model"] == "opus"

        # /review quick starts over, quick.
        await until(pilot, lambda: "[a] approve" in decision(app))
        await pilot.press(*"/review quick", "enter")
        await until(pilot, lambda: len(agent.runs) == 3)
        assert agent.runs[2]["plan"].depth == "quick" and agent.runs[2]["resume"] is False


async def test_quitting_asks_first(tmp_path):
    app, _ = make_app(tmp_path)
    quits = []

    async def quit_() -> None:
        quits.append(True)

    app.action_quit = quit_
    async with app.run_test(size=(140, 45)) as pilot:
        await until(pilot, lambda: home(app))
        for key in ("q", "ctrl+q", "ctrl+c"):  # ctrl+q asks too; ctrl+c never quits
            await pilot.press(key)
            if key != "ctrl+c":
                await until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
                await pilot.press("n")
            await until(pilot, lambda: home(app))
        assert quits == []
        await pilot.press("q")
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        await pilot.press("y")
        await until(pilot, lambda: quits == [True])


async def test_home_says_what_watching_means(tmp_path):
    from textual.widgets import Static

    app, github = make_app(tmp_path)
    async with app.run_test(size=(140, 45)) as pilot:
        await until(pilot, lambda: home(app))
        hint = app.main.repos.query_one("#empty", Static)
        # Nothing at all: the message alone, in the middle.
        await pilot.pause(0.2)
        assert hint.display and hint.has_class("alone")
        assert "No repos yet" in str(hint.render())

        # A PR assigned to you, in a repo you don't watch: listed under NOT WATCHED.
        github.assigned = [remote(7, repo="daily-co/daily-x", assignees=("me",))]
        github.open["pipecat-ai/pipecat"] = [remote(1)]
        await app.bus.request(WatchRepo(repo="pipecat-ai/pipecat"))
        await app.purr.runner.get_worker("github").poll()
        await until(pilot, lambda: app.repo_names == ["pipecat-ai/pipecat", "daily-co/daily-x"])
        await until(pilot, lambda: not hint.display)
        repo_list = app.main.repos.query_one(OptionList)
        prompts = [repo_list.get_option_at_index(i).prompt for i in range(repo_list.option_count)]
        rows = [str(getattr(prompt, "title", prompt)) for prompt in prompts]  # a heading's
        headings = [row for row in rows if "WATCHED (" in row]
        assert headings[0].startswith("● WATCHED (1)")
        assert headings[1].startswith("○ NOT WATCHED (1)")
        assert "only the PRs assigned to you" in headings[1]
        app.main.repos.select_repo("daily-co/daily-x")

        # + on it: ready to watch.
        await pilot.press("plus")
        await until(pilot, lambda: isinstance(app.screen, AddRepoScreen))
        assert app.screen.query_one(Input).value == "daily-co/daily-x"


async def test_the_pr_pane_never_keeps_another_prs_transcript(tmp_path):
    from purr.models import PRDetails

    app, github = make_app(tmp_path)
    github.open["pipecat-ai/pipecat"] = [remote(1, title="Message bus"), remote(2, title="Docs")]
    async with app.run_test(size=(140, 45)) as pilot:
        await app.bus.request(WatchRepo(repo="pipecat-ai/pipecat"))
        await until(pilot, lambda: "pipecat-ai/pipecat" in app.repo_names)
        await pilot.press("1")
        await until(pilot, lambda: len(pr_ids(app)) == 2)
        session = app.main.repo.session
        one, two = (await app.purr.store.list_prs(repo="pipecat-ai/pipecat"))[::-1]
        app.details.put(one, PRDetails(body="All about the message bus"))
        app.details.put(two, PRDetails(body="All about the docs"))
        await session.show(one)
        assert "All about the message bus" in transcript(app)

        # A show cut short after switching PRs, before drawing: the next one draws.
        session.pr = two
        await session.show(two)
        assert "All about the docs" in transcript(app)
        assert "message bus" not in transcript(app)

        # enter on a PR in the list: straight to its prompt.
        app.main.repo.focus_main()
        await pilot.press("enter")
        assert focused(app) == "prompt"


async def test_each_step_in_the_transcript_stands_apart(tmp_path):
    app, github = make_app(tmp_path)
    github.open["pipecat-ai/pipecat"] = [remote(1, title="Message bus")]
    async with app.run_test(size=(140, 45)) as pilot:
        await app.bus.request(WatchRepo(repo="pipecat-ai/pipecat"))
        await until(pilot, lambda: "pipecat-ai/pipecat" in app.repo_names)
        await pilot.press("1")
        await until(pilot, lambda: len(pr_ids(app)) == 1)
        session = app.main.repo.session
        await until(pilot, lambda: session.pr is not None and session._drawn is not None)
        ref = str(session.pr.ref)
        for entry in [
            {"kind": "user", "text": "is the race real?"},
            {"kind": "thinking", "text": "Look at shutdown first."},
            {"kind": "tool_use", "name": "Read", "input": {"file_path": "src/bus.py"}},
            {"kind": "tool_result", "tool_use_id": "t", "is_error": False, "text": "class Bus:"},
            {"kind": "text", "text": "Yes: stop() doesn't wait."},
        ]:
            session.append(ref, entry)
        await pilot.pause(0.1)
        lines = [line.rstrip() for line in transcript(app).splitlines()]
        start = next(i for i, line in enumerate(lines) if line.startswith("> is the race real?"))
        steps = lines[start:]
        assert steps[1] == ""  # a blank line between steps...
        assert "Look at shutdown first." in steps[2] and steps[3] == ""
        assert "Read(src/bus.py)" in steps[4]
        assert "⎿ class Bus:" in steps[5]  # ...but a tool's result stays with its call
        assert steps[6] == "" and "Yes: stop() doesn't wait." in steps[7]


async def test_a_review_at_work_keeps_moving(tmp_path):
    """While the agent works, its PR's mark spins and the PR pane counts the time;
    both stop when it's waiting on you, and when it's done."""
    import re

    from purr.tui.working import FRAMES, VERBS, elapsed_text, render_working

    assert [elapsed_text(s) for s in (0, 50, 62, 3725)] == ["0s", "50s", "1m 2s", "1h 2m"]
    line = render_working(since=100.0, now=162.5, frame=3).plain
    assert line.startswith(FRAMES[3]) and line.endswith("… (1m 2s)")
    line = render_working(since=100.0, now=162.5, frame=3, background=2).plain
    assert line.endswith("… (1m 2s · 2 in the background)")
    assert any(verb in line for verb in VERBS)

    app, github = make_app(tmp_path)
    agent = app.purr.agent
    github.open["pipecat-ai/pipecat"] = [remote(1, title="Message bus")]
    async with app.run_test(size=(140, 45)) as pilot:
        await app.bus.request(WatchRepo(repo="pipecat-ai/pipecat"))
        await until(pilot, lambda: "pipecat-ai/pipecat" in app.repo_names)
        await pilot.press("1")
        await until(pilot, lambda: len(pr_ids(app)) == 1)
        working = app.main.repo.session.query_one("#working")

        def mark() -> str:  # the PR's row, as drawn: its first character
            [row] = [t for t in option_texts(app) if "#1 " in t]
            return row.strip()[0]

        agent.gate = asyncio.Event()
        await pilot.press("r")
        await until(pilot, lambda: working.display)
        assert re.search(r"… \(\d+s\)$", str(working.render()))
        await until(pilot, lambda: mark() in FRAMES)
        first = mark()
        await until(pilot, lambda: mark() != first)  # it moves

        # Tasks out in the background say so.
        from purr.messages import SessionMessage
        from purr.tui.bridge import BusEvent

        ref = str(app.main.repo.session.pr.ref)
        for running in (2, 1):
            message = {"kind": "background", "running": running}
            app.post_message(BusEvent(SessionMessage(source="reviews", pr=ref, message=message)))
        await until(pilot, lambda: str(working.render()).endswith("s · 1 in the background)"))

        # Waiting on you: still.
        agent.ask = ("Bash", {"command": "uv run pytest"}, "not pre-approved")
        agent.gate.set()
        await until(pilot, lambda: "[a] allow" in decision(app))
        assert not working.display and mark() not in FRAMES

        # Done: still.
        await pilot.press("a")
        await until(pilot, lambda: "review: found 1 issue" in header(app))
        await until(pilot, lambda: not working.display and mark() not in FRAMES)


async def test_the_latest_done_prs_come_before_the_pool(tmp_path):
    from purr.models import PRRef, PRState

    names = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel", "india"]
    app, github = make_app(tmp_path)
    github.open["pipecat-ai/pipecat"] = [
        remote(i, title=f"Change {name}") for i, name in enumerate(names, start=1)
    ]
    async with app.run_test(size=(140, 45)) as pilot:
        await app.bus.request(WatchRepo(repo="pipecat-ai/pipecat"))
        await until(pilot, lambda: "pipecat-ai/pipecat" in app.repo_names)
        await pilot.press("1")
        await until(pilot, lambda: len(pr_ids(app)) == 9)
        for number in range(1, 8):  # #7 finished last
            await app.purr.store.set_pr_state(PRRef("pipecat-ai/pipecat", number), PRState.DONE)
            await asyncio.sleep(0.005)
        repo = app.main.repo
        await repo.reload()

        def numbers() -> list[int]:
            return [int(i.rsplit("#", 1)[1]) for i in pr_ids(app)]

        await until(pilot, lambda: numbers() == [7, 6, 5, 4, 3, 9, 8])
        text = "\n".join(option_texts(app))
        assert text.index("DONE (7)") < text.index("NOT REVIEWED (2)")
        assert "the latest 5; / finds the others" in text

        # Searching finds the others, and the one you picked stays when you clear it...
        await pilot.press("slash", *"alpha", "enter")
        await until(pilot, lambda: numbers() == [1])
        await pilot.press("slash", "ctrl+u", "enter")
        await until(pilot, lambda: numbers() == [7, 6, 5, 4, 3, 1, 9, 8])
        # ...as does one you go to (an alert, ctrl+p).
        await app.goto_pr(PRRef("pipecat-ai/pipecat", 2))
        await until(pilot, lambda: numbers() == [7, 6, 5, 4, 3, 2, 9, 8])


async def test_selecting_text_in_the_pr_pane_copies_it(tmp_path, monkeypatch):
    """Drag over the PR's description or conversation: it's selected, and copied, as
    terminals do. ctrl+c copies too, and never quits."""
    import purr.tui.app

    monkeypatch.setattr(purr.tui.app, "copy_command", lambda: None)  # never your clipboard
    monkeypatch.delenv("VTE_VERSION", raising=False)
    app, github = make_app(tmp_path)
    github.open["pipecat-ai/pipecat"] = [remote(1, title="Message bus")]
    async with app.run_test(size=(140, 45)) as pilot:
        await app.bus.request(WatchRepo(repo="pipecat-ai/pipecat"))
        await until(pilot, lambda: "pipecat-ai/pipecat" in app.repo_names)
        await pilot.press("1")
        await until(pilot, lambda: len(pr_ids(app)) == 1)
        session = app.main.repo.session
        await until(pilot, lambda: session.pr is not None and session._drawn is not None)
        session.append(str(session.pr.ref), {"kind": "text", "text": "Yes: stop() doesn't wait."})
        await pilot.pause(0.1)
        log = session.query_one("#transcript")
        [index] = [i for i, line in enumerate(log.lines) if "stop() doesn't" in line.text]
        x, y = log.lines[index].text.index("stop()"), index - log.scroll_offset.y
        await pilot.mouse_down(log, offset=(x, y))
        await pilot.hover(log, offset=(x + 5, y))  # over the ")": it's included
        await pilot.mouse_up(log, offset=(x + 5, y))
        await pilot.pause()
        assert app.screen.get_selected_text() == "stop()"
        assert app.clipboard == "stop()"

        app.copy_to_clipboard("")  # what ctrl+c copies, now
        await pilot.press("ctrl+c")
        assert app.is_running and app.clipboard == "stop()"

        app.screen.clear_selection()
        await pilot.press("ctrl+c")  # nothing to copy: still nothing quits
        await pilot.pause()
        assert app.is_running


async def test_the_colors_are_solarized(tmp_path):
    app, _ = make_app(tmp_path)
    async with app.run_test(size=(140, 45)):
        assert app.theme == "solarized-dark"
        # Purr's named colors ("green" in a transcript line) are Solarized's too.
        assert app.ansi_theme.ansi_colors[2] == (0x85, 0x99, 0x00)


async def test_the_agents_reasoning_is_a_line_per_thought(tmp_path):
    """In full it's ten times what the agent says: a line each, unless you press t."""
    app, github = make_app(tmp_path)
    github.open["pipecat-ai/pipecat"] = [remote(1, title="Message bus")]
    async with app.run_test(size=(140, 45)) as pilot:
        await app.bus.request(WatchRepo(repo="pipecat-ai/pipecat"))
        await until(pilot, lambda: "pipecat-ai/pipecat" in app.repo_names)
        await pilot.press("1")
        await until(pilot, lambda: len(pr_ids(app)) == 1)
        session = app.main.repo.session
        await until(pilot, lambda: session.pr is not None and session._drawn is not None)
        thought = "Checking the lock file. The bump looks deliberate: the changelog says so."
        session.append(str(session.pr.ref), {"kind": "thinking", "text": thought})
        await pilot.pause(0.1)
        assert "Checking the lock file…" in transcript(app)
        assert "looks deliberate" not in transcript(app)
        await pilot.press("t")
        await until(pilot, lambda: "looks deliberate" in transcript(app))
        await pilot.press("t")
        await until(pilot, lambda: "looks deliberate" not in transcript(app))


async def test_writing_instead_of_approving_says_so_in_the_thread(tmp_path):
    app, github = make_app(tmp_path)
    github.open["pipecat-ai/pipecat"] = [remote(1, title="Message bus")]
    async with app.run_test(size=(140, 45)) as pilot:
        await app.bus.request(WatchRepo(repo="pipecat-ai/pipecat"))
        await until(pilot, lambda: "pipecat-ai/pipecat" in app.repo_names)
        await pilot.press("1")
        await until(pilot, lambda: len(pr_ids(app)) == 1)
        await pilot.press("r")
        await until(pilot, lambda: "[a] approve" in decision(app))
        assert "or write to change it" in decision(app)
        await until(pilot, lambda: not app.purr.runner.get_worker("reviews").running)
        await pilot.press("enter", *"is the race real?", "enter")
        await until(pilot, lambda: "> is the race real?" in transcript(app))
        lines = transcript(app).splitlines()
        [aside] = [i for i, line in enumerate(lines) if "◼ Proposal set aside:" in line]
        assert "Post 1 comment and request changes" in lines[aside]
        assert lines[aside + 2].startswith("> is the race real?")  # then what you wrote


async def test_requests_for_your_review_and_replies_to_you(tmp_path):
    from purr.models import PRRef, PRState, Reply

    app, github = make_app(tmp_path)
    asks = remote(1, title="Asks you", requested=("me",))
    github.open["pipecat-ai/pipecat"] = [
        asks,
        remote(2, title="In the pool"),
        remote(3, title="Answered you"),
    ]
    github.requested = [asks]
    async with app.run_test(size=(140, 45)) as pilot:
        await app.bus.request(WatchRepo(repo="pipecat-ai/pipecat"))
        await until(pilot, lambda: "pipecat-ai/pipecat" in app.repo_names)
        await pilot.press("1")
        await until(pilot, lambda: len(pr_ids(app)) == 3)
        store = app.purr.store
        answered = PRRef("pipecat-ai/pipecat", 3)
        review = await store.start_review(answered)  # reviewed, then answered on GitHub
        await store.finish_review(review.id, summary="Fine", findings=[])
        await store.set_pr_state(answered, PRState.NEEDS_YOU)  # as a poll leaves it
        await store.set_replies(
            answered,
            [Reply("alice", "Fixed,\nsee the test.", "2026-11-21T10:00:00Z", "", "bus.py:42")],
        )
        await app.main.repo.reload()

        # Asked of you: apart from the pool, and saying so.
        text = "\n".join(option_texts(app))
        assert text.index("NEEDS YOU (1)") < text.index("REVIEW REQUESTED (1)")
        assert text.index("REVIEW REQUESTED (1)") < text.index("Asks you")
        assert text.index("Asks you") < text.index("NOT REVIEWED (1)")
        await app.goto_pr(PRRef("pipecat-ai/pipecat", 1))
        await until(pilot, lambda: "asks for your review" in header(app))

        # Answered: what they said, until x says it needs no reply.
        await app.goto_pr(answered)
        await until(pilot, lambda: "alice replied" in decision(app))
        assert "alice · bus.py:42: Fixed, see the test." in decision(app)
        await pilot.press("x")

        async def settled() -> bool:
            pr = await store.get_pr(answered)
            return pr.state == PRState.DONE and not pr.replies

        await until_async(settled)
        await until(pilot, lambda: decision(app) == "")


async def until_async(check, timeout: float = 5) -> None:
    async def wait():
        while not await check():
            await asyncio.sleep(0.02)

    await asyncio.wait_for(wait(), timeout)


async def test_merging_with_m_after_approving(tmp_path):
    from purr.messages import ApproveAction
    from purr.models import ActionKind, Check, CheckState, MergeCheck, PRRef, PRState

    app, github = make_app(tmp_path)
    github.open["pipecat-ai/pipecat"] = [remote(7, title="Fix the bus", head_sha="abc")]
    ref = PRRef("pipecat-ai/pipecat", 7)
    github.merge_checks[ref] = MergeCheck(
        head_sha="abc",
        base="main",
        method="squash",
        mergeable="MERGEABLE",
        merge_state="CLEAN",
        checks=(
            Check("Unit Tests", CheckState.PASSED),
            Check("docs/readthedocs", CheckState.FAILED),
        ),
        assignees=("me", "alice"),
    )
    async with app.run_test(size=(140, 45)) as pilot:
        await app.bus.request(WatchRepo(repo="pipecat-ai/pipecat"))
        await until(pilot, lambda: "pipecat-ai/pipecat" in app.repo_names)
        await pilot.press("1")
        await until(pilot, lambda: pr_ids(app) == [str(ref)])
        store = app.purr.store
        await store.set_pr_state(ref, PRState.NEEDS_YOU)
        approval = await store.add_pending_action(ref, kind=ActionKind.APPROVE, description="LGTM")
        await app.bus.request(ApproveAction(action_id=approval.id))
        await app.main.repo.session.refresh_status()
        await until(pilot, lambda: "You approved it." in decision(app))

        # m: where it stands. The docs build failed and alice hasn't approved: your call.
        await pilot.press("m")
        await until(pilot, lambda: "Squash and merge #7 into main?" in decision(app))
        shown = decision(app)
        assert "✓ 1 check passed" in shown
        assert "⚠ docs/readthedocs failed" in shown and "⚠ alice hasn't approved" in shown
        assert "[m] merge anyway" in shown

        # m again: merged, at the commit you approved.
        await pilot.press("m")
        await until(pilot, lambda: app.purr._writer.merges == [(ref, "squash", "abc")])


async def test_the_diff_its_files_and_your_comments_on_lines(tmp_path):
    from purr.models import PRDetails, PRRef
    from purr.tui.dialogs import ConfirmScreen
    from purr.tui.diffview import CommentScreen, DiffView

    app, github = make_app(tmp_path)
    agent = app.purr.agent
    github.open["pipecat-ai/pipecat"] = [
        remote(7, title="Fix the bus", head_sha="abc", assignees=("me",))
    ]
    github.assigned = list(github.open["pipecat-ai/pipecat"])  # reviewed: you can ask
    ref = PRRef("pipecat-ai/pipecat", 7)
    github.details[ref] = PRDetails(body="Fixes the bus.", additions=2, deletions=2)
    context = "".join(f" line {i}\n" for i in range(2, 80))  # long enough to start shut
    github.diffs[ref] = (
        "diff --git a/src/bus.py b/src/bus.py\n--- a/src/bus.py\n+++ b/src/bus.py\n"
        f"@@ -1,79 +1,79 @@\n-    pass\n+    self.lock = asyncio.Lock()\n{context}"
        "diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n"
        "@@ -1 +1 @@\n-old\n+new\n"
    )
    async with app.run_test(size=(140, 45)) as pilot:
        await app.bus.request(WatchRepo(repo="pipecat-ai/pipecat"))
        await until(pilot, lambda: "pipecat-ai/pipecat" in app.repo_names)
        await pilot.press("1")
        await until(pilot, lambda: pr_ids(app) == [str(ref)])

        await until(pilot, lambda: "Fixes the bus." in transcript(app))
        assert "self.lock" not in transcript(app)  # the PR's pane doesn't show the diff
        await until(pilot, lambda: "[a] approve" in decision(app))  # reviewed
        await app.bus.request(DismissAction(action_id=(await pending(app, ref))[0].id))

        # d: the diff, in the list's place, to read line by line. Its files start shut too.
        github.threads = [
            {"id": "T1", "path": "README.md", "line": 1, "side": "RIGHT", "resolved": False,
             "comments": [{"author": "carol", "body": "Say why"}]},
            {"id": "T2", "path": "README.md", "line": 1, "side": "LEFT", "resolved": False,
             "comments": [{"author": "me", "by_user": True, "id": "C9", "body": "Old, gone"}]},
        ]  # fmt: skip
        repo = app.main.repo
        await pilot.press("d")
        await until(pilot, lambda: repo.diff_shown and focused(app) == "diff")
        assert not repo.query_one("#prs-pane").display and repo.session.display
        view = repo.query_one(DiffView)
        # The PR's review threads on GitHub come in a box under their line (their file
        # opens); tab shuts one to a line, and opens it again.
        await until(pilot, lambda: rows(view, "│ Say why"))
        await pilot.press("end")  # a box is one step, highlighted whole
        assert view.current().said.thread == "T1" and view.current().text.plain.endswith("╮")
        await pilot.press("tab")
        await until(pilot, lambda: rows(view, "▸ carol ─ Say why"))
        await pilot.press("tab")
        await until(pilot, lambda: rows(view, "│ Say why"))
        # enter on it: your reply, posted to the thread on GitHub when you save it.
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, CommentScreen))
        await pilot.press(*"Will do", "enter")
        await until(pilot, lambda: app.purr._writer.replies)
        assert app.purr._writer.replies[0][0] == "T1"
        assert app.purr._writer.replies[0][1].startswith("Will do")
        # enter on a comment of yours on GitHub: the editor, with its words, to change them.
        await pilot.press("up", "up", "enter")  # +new, -old's box (yours)
        await until(pilot, lambda: isinstance(app.screen, CommentScreen))
        assert app.screen.query_one("#comment-body").text == "Old, gone"
        await pilot.press("ctrl+u", *"Gone, right?", "enter")
        await until(pilot, lambda: getattr(app.purr._writer, "edits", None))
        assert app.purr._writer.edits[0][0] == "C9"
        assert app.purr._writer.edits[0][1].startswith("Gone, right?")
        await pilot.press("end")
        await pilot.press("home", "n", "n", "tab", "home")  # and the README's file shuts
        # (first, what's said on the PR itself: the conversation, nothing yet)
        await until(pilot, lambda: len(view._rows) == 3 and view.cursor == 0)
        assert "Conversation" in view.current().text.plain
        await pilot.press("down", "tab")  # tab (or space) opens the file, and shuts it
        await until(pilot, lambda: len(view._rows) > 3)
        await pilot.press("down", "tab")
        await until(pilot, lambda: len(view._rows) == 3 and view.cursor == 1)
        await pilot.press("space")
        await until(pilot, lambda: len(view._rows) > 3)
        await pilot.press("ctrl+v")  # a page down, and back up, as in Emacs
        assert view.cursor == view.size.height
        await pilot.press("alt+v")
        assert view.cursor == 1
        await pilot.press("down", "ctrl+n", "ctrl+n", "ctrl+p", "down")  # ctrl+n/p as ↓/↑
        assert view.current().line.target() == (1, "RIGHT")  # @@, -    pass, +    self.lock
        assert focused(app) == "diff"  # not the switcher, nor news

        # z: the diff full screen, and back.
        await pilot.press("z")
        await pilot.pause()
        assert not repo.session.display and repo.diff.size.width > 130  # all the width
        await pilot.press("z")
        await pilot.pause()
        assert repo.session.display and repo.diff.size.width < 100

        # Select (shift+↑/↓, or ctrl+space and moving) and enter: ask the agent about
        # those lines, in the PR's conversation, next to them.
        await pilot.press("up", "shift+down", "shift+down")  # -    pass .. line 2
        assert [line.text for line in view.selected()] == [
            "-    pass", "+    self.lock = asyncio.Lock()", " line 2"
        ]  # fmt: skip
        await pilot.press("escape")  # esc lets the selection go first
        assert view.anchor is None and repo.diff_shown
        await pilot.press("up", "up", "ctrl+@", "down", "down")  # a mark, as in Emacs
        await pilot.press("enter")
        prompt = repo.session.query_one("#prompt")
        await until(pilot, lambda: focused(app) == "prompt")
        assert prompt.text == "About bus.py:1–2: "
        await pilot.press(*"why the lock?", "enter")
        await until(pilot, lambda: agent.runs[-1]["prompt"].endswith("why the lock?"))
        sent = agent.runs[-1]["prompt"]
        assert "src/bus.py, lines 1–2, in the base's line 1 to the PR's line 2" in sent
        assert "+    self.lock = asyncio.Lock()" in sent and "> About bus.py:1–2: why" in (
            transcript(app)
        )
        assert "purr-note" not in transcript(app)
        await until(pilot, lambda: not repo.session.state().working)
        await pilot.press("shift+tab")  # from the PR's pane back to the diff, and back
        assert focused(app) == "diff"
        await pilot.press("shift+tab")  # (in the diff, tab opens and shuts)
        assert focused(app) == "transcript"
        await pilot.press("tab")
        assert focused(app) == "diff"
        await pilot.press("home", "down", "down", "down", "down")

        # c: a comment on that line, posted on GitHub when you save it (on its own, signed,
        # at the PR's head): then it's there, in a box under the line, yours.
        writer = app.purr._writer
        await pilot.press("c")
        await until(pilot, lambda: isinstance(app.screen, CommentScreen))
        await pilot.press(*"Why a lock?", "enter")
        await until(pilot, lambda: writer.line_comments)
        _, posted, commit = writer.line_comments[0]
        assert (posted["path"], posted["line"], posted["side"]) == ("src/bus.py", 1, "RIGHT")
        assert posted["body"].startswith("Why a lock?") and commit == "abc"
        await until(pilot, lambda: rows(view, "│ Why a lock? "))
        await pilot.press("down", "enter")  # enter on your box: the editor, to change it there
        await until(pilot, lambda: isinstance(app.screen, CommentScreen))
        body = app.screen.query_one("#comment-body")
        assert body.text == "Why a lock?"  # its words (not the signature), to change
        # The ask box's keys, as in Emacs: ctrl+a/e, ctrl+b/f, alt+b/f, ctrl+k/u.
        await pilot.press("ctrl+a", "alt+f", "alt+f", "alt+f")
        assert body.cursor_location == (0, 10)
        await pilot.press("ctrl+b", "ctrl+k")
        assert body.text == "Why a loc"
        await pilot.press("ctrl+e", "ctrl+u", *"Why a lock here?", "enter")
        await until(pilot, lambda: rows(view, "│ Why a lock here?"))
        # One on the deleted line goes on the base's side; x deletes it, on GitHub, once
        # you've said so.
        await pilot.press("up", "up", "c")
        await until(pilot, lambda: isinstance(app.screen, CommentScreen))
        await pilot.press(*"Gone?", "enter")
        await until(pilot, lambda: rows(view, "│ Gone?"))
        assert [(c["line"], c["side"]) for _, c, _ in writer.line_comments] == [
            (1, "RIGHT"), (1, "LEFT")
        ]  # fmt: skip
        await pilot.press("down", "x")
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        await pilot.press("y")
        await until(pilot, lambda: not rows(view, "Gone?"))
        assert writer.deleted == [("LC2", True)]

        # A comment on several lines (a selection) goes on all of them.
        await until(pilot, lambda: view.current().line is not None)
        assert view.current().line.target() == (1, "RIGHT")  # where the deleted one was
        await pilot.press("shift+down", "c")  # over the comment under it, to line 2
        await until(pilot, lambda: isinstance(app.screen, CommentScreen))
        await pilot.press(*"Both?", "enter")
        await until(pilot, lambda: len(writer.line_comments) == 3)
        both = writer.line_comments[-1][1]
        assert (both["start_line"], both["line"]) == (1, 2)
        await until(pilot, lambda: rows(view, "│ Both?"))

        # The conversation, at the top: c there writes on the PR itself, posted when you
        # save it; enter on yours changes it there, and x deletes it (once you've said so).
        await pilot.press("home", "c")
        await until(pilot, lambda: isinstance(app.screen, CommentScreen))
        await pilot.press(*"Thanks for this!", "enter")
        await until(pilot, lambda: writer.pr_comments)
        assert writer.pr_comments[0][1].startswith("Thanks for this!")
        await until(pilot, lambda: rows(view, "│ Thanks for this!"))
        # (the PR's pane shows it too, under the description)
        await until(pilot, lambda: "Conversation (1)" in transcript(app))
        await pilot.press("down", "enter")
        await until(pilot, lambda: isinstance(app.screen, CommentScreen))
        assert app.screen.query_one("#comment-body").text == "Thanks for this!"
        await pilot.press("ctrl+u", *"Thanks!", "enter")
        await until(pilot, lambda: rows(view, "│ Thanks! "))
        assert writer.pr_edits[0][0] == "IC1"
        await pilot.press("x")
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        await pilot.press("y")
        await until(pilot, lambda: not rows(view, "Thanks!"))
        assert writer.deleted[-1] == ("IC1", False)

        # d (or esc) closes it: the list again. Nothing of yours waits to be posted: it
        # went as you saved it.
        await pilot.press("d")
        assert not repo.diff_shown and focused(app) == "prs"
        assert "your" not in decision(app)
        assert await app.purr.store.draft_comments(ref) == []


async def test_answering_on_the_pr_and_dropping_the_agents_comments(tmp_path):
    from purr.models import ActionKind, PRComment, PRDetails, PRRef
    from purr.tui.diffview import CommentScreen, DiffView

    app, github = make_app(tmp_path)
    github.open["pipecat-ai/pipecat"] = [remote(7, title="Fix the bus", head_sha="abc")]
    ref = PRRef("pipecat-ai/pipecat", 7)
    alice = PRComment("IC5", "comment", "alice", "Can you look?", "2026-11-20T10:00:00Z")
    bob = PRComment("R5", "review", "bob", "Ship it", "2026-11-20T11:00:00Z", state="APPROVED")
    github.details[ref] = PRDetails(body="Fixes the bus.", conversation=(alice, bob))
    github.diffs[ref] = (
        "diff --git a/bus.py b/bus.py\n--- a/bus.py\n+++ b/bus.py\n@@ -1 +1 @@\n-a\n+b\n"
    )
    async with app.run_test(size=(140, 45)) as pilot:
        await app.bus.request(WatchRepo(repo="pipecat-ai/pipecat"))
        await until(pilot, lambda: "pipecat-ai/pipecat" in app.repo_names)
        store = app.purr.store
        comments = [{"path": "bus.py", "line": 1, "body": "Nit: name it"}]
        await store.add_pending_action(
            ref, kind=ActionKind.COMMENT, description="Comment", payload={"comments": comments}
        )
        await pilot.press("1")
        await until(pilot, lambda: app.main.repo.selected_pr() is not None)
        # The PR's pane: what's said on it, under the description.
        await until(pilot, lambda: "Conversation (2)" in transcript(app))
        assert "Can you look?" in transcript(app) and "approved" in transcript(app)

        # The diff: the conversation at the top, open, a box for each.
        await pilot.press("d")
        view = app.main.repo.query_one(DiffView)
        await until(pilot, lambda: rows(view, "│ Ship it"))
        assert rows(view, "bob · ") and "approved" in rows(view, "bob · ")[0].text.plain
        # enter on someone's: your comment on the PR, to them, posted when you save it.
        await pilot.press("home", "down", "enter")
        await until(pilot, lambda: isinstance(app.screen, CommentScreen))
        assert app.screen.query_one("#comment-body").text == "@alice "
        await pilot.press(*"looking now", "enter")
        writer = app.purr._writer
        await until(pilot, lambda: writer.pr_comments)
        assert writer.pr_comments[0][1].startswith("@alice looking now")
        # x on someone's does nothing; on the agent's proposed comment, it's left out.
        await pilot.press("home", "down", "x")
        assert not writer.deleted
        await until(pilot, lambda: rows(view, "│ Nit: name it"))
        agents = [i for i, row in enumerate(view._rows) if row.said and row.said.source == "agent"]
        view._move_to(agents[0])
        await pilot.press("x")
        await until(pilot, lambda: not rows(view, "Nit: name it"))
        assert (await store.pending_actions(ref))[0].payload["comments"] == []


async def pending(app, ref):
    return await app.purr.store.pending_actions(ref)


def rows(view, text: str) -> list:
    return [row for row in view._rows if text in row.text.plain]


async def test_the_merge_box_follows_checks_still_running(tmp_path, monkeypatch):
    from dataclasses import replace

    from purr.messages import ApproveAction
    from purr.models import ActionKind, Check, CheckState, MergeCheck, PRRef

    monkeypatch.setattr("purr.tui.pr_keys.MERGE_RECHECK", 0.05)
    app, github = make_app(tmp_path)
    github.open["pipecat-ai/pipecat"] = [remote(7, title="Fix the bus", head_sha="abc")]
    ref = PRRef("pipecat-ai/pipecat", 7)
    running = MergeCheck(
        head_sha="abc",
        base="main",
        method="squash",
        mergeable="MERGEABLE",
        merge_state="CLEAN",
        checks=(Check("Unit Tests", CheckState.RUNNING),),
        assignees=("me",),
    )
    github.merge_checks[ref] = running
    async with app.run_test(size=(140, 45)) as pilot:
        await app.bus.request(WatchRepo(repo="pipecat-ai/pipecat"))
        await until(pilot, lambda: "pipecat-ai/pipecat" in app.repo_names)
        await pilot.press("1")
        await until(pilot, lambda: pr_ids(app) == [str(ref)])
        approval = await app.purr.store.add_pending_action(
            ref, kind=ActionKind.APPROVE, description="LGTM"
        )
        await app.bus.request(ApproveAction(action_id=approval.id))
        await app.main.repo.session.refresh_status()
        await until(pilot, lambda: "You approved it." in decision(app))

        await pilot.press("m")
        await until(pilot, lambda: "⚠ Unit Tests is still running" in decision(app))
        # It finishes: the box says so, without pressing anything.
        passed = (Check("Unit Tests", CheckState.PASSED),)
        github.merge_checks[ref] = replace(running, checks=passed)
        await until(pilot, lambda: "✓ 1 check passed" in decision(app))
        assert "still running" not in decision(app) and "merge anyway" not in decision(app)


async def test_replies_and_requests_are_news_as_they_happen(tmp_path):
    from purr.messages import PRChanged

    app, github = make_app(tmp_path)
    github.open["pipecat-ai/pipecat"] = [remote(1), remote(2)]
    async with app.run_test(size=(140, 45)) as pilot:
        await app.bus.request(WatchRepo(repo="pipecat-ai/pipecat"))
        await until(pilot, lambda: "pipecat-ai/pipecat" in app.repo_names)
        for number, change, by in ((1, "replied", "alice"), (2, "requested", "")):
            event = PRChanged(
                source="github", pr=f"pipecat-ai/pipecat#{number}", change=change, title="", by=by
            )
            await app.bus.send_bus_message(event)
        await until(
            pilot,
            lambda: (
                app.alerts
                == {
                    "pipecat-ai/pipecat#1": "alice replied",
                    "pipecat-ai/pipecat#2": "asks for your review",
                }
            ),
        )


async def test_choosing_a_theme(tmp_path, monkeypatch):
    from purr.config import SETTINGS_FILE, read_settings
    from purr.tui import theme
    from purr.tui.dialogs import SettingsScreen, ThemeScreen
    from purr.tui.editor import EditorArea, EditorScreen

    monkeypatch.setattr(theme, "_palette", theme.palette())  # back as it was, after
    app, github = make_app(tmp_path)
    github.open["pipecat-ai/pipecat"] = [remote(1)]
    settings = app.purr.config.config_dir / SETTINGS_FILE
    async with app.run_test(size=(140, 45)) as pilot:
        # i, then t: the themes, each shown as you move to it (Purr's own colors too);
        # esc goes back to the one you had.
        await pilot.press("i", "t")
        await until(pilot, lambda: isinstance(app.screen, ThemeScreen))
        await pilot.press("down")
        await until(pilot, lambda: app.theme == "solarized-light")
        assert theme.palette().code == "solarized-light"
        await pilot.press("escape")
        await until(pilot, lambda: app.theme == "solarized-dark")
        await until(pilot, lambda: isinstance(app.screen, SettingsScreen))  # back to the menu
        assert theme.palette().code == "solarized-dark" and not settings.exists()
        # enter keeps it, in settings.toml.
        await pilot.press("t", "down", "enter")
        await until(pilot, lambda: settings.exists())
        assert app.theme == "solarized-light" == read_settings(settings)["theme"]

        # Saving settings.toml (i, then o) with another theme in it: that one, at once.
        await until(pilot, lambda: isinstance(app.screen, SettingsScreen))
        assert "solarized-light" in "\n".join(option_texts_of(app.screen))  # as it is now
        await pilot.press("o")
        await until(pilot, lambda: isinstance(app.screen, EditorScreen))
        area = app.screen.query_one(EditorArea)
        area.text = area.text.replace('theme = "solarized-light"', 'theme = "nord"')
        await pilot.press("ctrl+s")
        await until(pilot, lambda: app.theme == "nord")
        assert theme.palette().code == "nord"
        await pilot.press("escape")
        await until(pilot, lambda: isinstance(app.screen, SettingsScreen))
        await pilot.press("escape")
        await until(pilot, lambda: isinstance(app.screen, MainScreen))

        # /theme <name> in a PR's prompt: that one, kept.
        await app.bus.request(WatchRepo(repo="pipecat-ai/pipecat"))
        await until(pilot, lambda: "pipecat-ai/pipecat" in app.repo_names)
        await pilot.press("1")
        await until(pilot, lambda: app.main.repo.session.pr is not None)
        await pilot.press("enter", *"/theme dracula", "enter")
        await until(pilot, lambda: app.theme == "dracula")
        assert read_settings(settings)["theme"] == "dracula"


async def test_editing_settings_in_purr(tmp_path):
    from purr.config import SETTINGS_FILE, read_settings
    from purr.tui.dialogs import ConfirmScreen, SettingsScreen
    from purr.tui.editor import EditorArea, EditorScreen

    app, github = make_app(tmp_path)
    path = app.purr.config.config_dir / SETTINGS_FILE
    async with app.run_test(size=(140, 45)) as pilot:
        # i, then o: settings.toml in Purr's editor, every setting there, commented out.
        await pilot.press("i")
        assert isinstance(app.screen, SettingsScreen)
        await pilot.press("o")
        await until(pilot, lambda: isinstance(app.screen, EditorScreen))
        editor = app.screen
        area = editor.query_one(EditorArea)
        status = editor.query_one("#editor-status")
        assert "# max_reviews = 3" in area.text and path.exists()

        def says() -> str:
            return str(status.render())

        # A typo isn't saved: it says why, at once, and the file's as it was.
        before = path.read_text()
        area.text = "away_aftr = 600\n" + area.text
        await pilot.press("ctrl+s")
        assert "Not saved" in says() and "unknown setting 'away_aftr'" in says()
        assert path.read_text() == before
        # Fixed, it is (Emacs's keys: ctrl+a, ctrl+k kill the line, as in the prompt).
        area.move_cursor((0, 4))
        await pilot.press("ctrl+a", "ctrl+k", *"max_reviews = 2")
        await pilot.press("ctrl+s")
        assert "Saved" in says() and read_settings(path)["max_reviews"] == 2

        # esc closes; with changes not saved, it asks first.
        area.insert("# more\n")
        await pilot.press("escape")
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        await pilot.press("n")
        await until(pilot, lambda: app.screen is editor)
        await pilot.press("escape")
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        await pilot.press("y")
        await until(pilot, lambda: isinstance(app.screen, SettingsScreen))  # where it was
        assert "# more" not in path.read_text()
        await pilot.press("escape")
        await until(pilot, lambda: isinstance(app.screen, MainScreen))
