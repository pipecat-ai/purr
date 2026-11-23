"""Record the README's demo GIF: Purr, scripted, on made-up PRs.

Nothing real is involved: GitHub, the review agent and the writer are fakes (the
tests' fakes, and a scripted agent here), and Purr's config and data go in a
temporary directory. Each step is a screenshot (Textual's SVG), rendered to PNG
with headless Chrome and put together with ImageMagick.

    uv run python scripts/record_demo.py [docs/purr.gif]

Needs ``google-chrome`` (or ``chromium``) and ``magick`` on your PATH.
"""

import asyncio
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOME = Path(tempfile.mkdtemp(prefix="purr-demo-"))
os.environ["HOME"] = str(HOME)  # paths show as ~/..., as on your machine
os.environ["PURR_CONFIG_DIR"] = str(HOME / ".config" / "purr")  # never your own
os.environ["PURR_DATA_DIR"] = str(HOME / ".local" / "share" / "purr")
sys.path.insert(0, str(ROOT))

from loguru import logger  # noqa: E402

from purr.app import Purr  # noqa: E402
from purr.config import Config  # noqa: E402
from purr.messages import WatchRepo  # noqa: E402
from purr.models import FileChange, Finding, PRDetails, PRRef, Severity  # noqa: E402
from purr.review.agent import AgentResult  # noqa: E402
from purr.tui import PurrApp  # noqa: E402
from purr.tui.diffview import CommentScreen, DiffView  # noqa: E402
from tests.fakes import (  # noqa: E402
    FakeGitHub,
    FakeLead,
    FakeRecapAgent,
    FakeRepos,
    FakeSuggester,
    FakeWriter,
    remote,
)

SIZE = (140, 36)  # the terminal: columns, rows
WIDTH = 1400  # the GIF's, in pixels
REPO = "pipecat-ai/pipecat"
NOW = datetime.now(UTC)


def ago(**delta: float) -> str:
    return (NOW - timedelta(**delta)).isoformat().replace("+00:00", "Z")


# -- The PRs ------------------------------------------------------------------------

FIX = 7012  # the one the demo is about: reviewed, needs you
PRS = [
    remote(FIX, title="Cancel pending function calls on interruption", author="alice",
           created_at=ago(days=2), assignees=("me",), base="main"),
    remote(7008, title="Add Deepgram Flux STT service", author="bob", created_at=ago(hours=5),
           assignees=("me",)),
    remote(7015, title="Fix websocket reconnect backoff", author="carol",
           created_at=ago(hours=3), requested=("me",)),
    remote(6999, title="RTVI: client-ready handshake timeout", author="dave",
           created_at=ago(days=4), assignees=("erin",)),
    remote(7004, title="Update Gemini Live model list", author="frank", created_at=ago(days=1)),
    remote(7002, title="Add Sarvam TTS voices", author="grace", created_at=ago(days=1, hours=3)),
    remote(6996, title="Example: phone bot with Twilio", author="heidi", created_at=ago(days=5)),
    remote(6991, title="Bump aiohttp to 3.13", author="ivan", created_at=ago(days=6)),
]  # fmt: skip

DESCRIPTION = """\
When the user interrupts, pending function calls are now cancelled instead of
running to completion, and the LLM is told so (`FunctionCallCancelFrame`).

Fixes #6981."""

DIFF = """\
diff --git a/src/pipecat/services/llm_service.py b/src/pipecat/services/llm_service.py
--- a/src/pipecat/services/llm_service.py
+++ b/src/pipecat/services/llm_service.py
@@ -398,21 +398,30 @@ class LLMService(AIService):
     async def _handle_interruptions(self, frame: InterruptionFrame):
-        for runner in self._function_call_runners:
-            await runner.wait()
+        for runner in list(self._function_call_runners):
+            if runner.cancel_on_interruption:
+                await self._cancel_function_call(runner)
         await super()._handle_interruptions(frame)

+    async def _cancel_function_call(self, runner: FunctionCallRunner):
+        runner.task.cancel()
+        try:
+            await runner.task
+        except asyncio.CancelledError:
+            pass
+        await self.push_frame(FunctionCallCancelFrame(runner.function_name, runner.tool_call_id))
+        self._function_call_runners.discard(runner)
+
     async def _run_function_call(self, runner: FunctionCallRunner):
         result = await runner.handler(runner.params)
-        await self._push_result(runner, result)
+        self._function_call_results[runner.tool_call_id] = result
+        await self._push_result(runner, result)
         self._function_call_runners.discard(runner)
diff --git a/tests/test_llm_service.py b/tests/test_llm_service.py
--- a/tests/test_llm_service.py
+++ b/tests/test_llm_service.py
@@ -210,3 +210,15 @@ async def test_function_call_result():
     assert result.result == {"temperature": 21}
+
+
+async def test_interruption_cancels_function_call():
+    service = make_service(slow_weather)
+    await service.run_function_call("get_weather", {"city": "Barcelona"})
+    await service.interrupt()
+    assert service.pushed(FunctionCallCancelFrame)
"""

DETAILS = PRDetails(
    body=DESCRIPTION,
    base="main",
    head="cancel-function-calls",
    additions=21,
    deletions=3,
    changed_files=2,
    files=(
        FileChange("src/pipecat/services/llm_service.py", 15, 3),
        FileChange("tests/test_llm_service.py", 6, 0),
    ),
    checks="SUCCESS",
)


# -- The agent, scripted --------------------------------------------------------------

LLM = "src/pipecat/services/llm_service.py"

REVIEW = [
    {"kind": "thinking", "text": "**Reading the PR and the issue it fixes.**"},
    {"kind": "tool_use", "id": "1", "name": "Bash",
     "input": {"command": f"gh pr diff {FIX} --repo {REPO}"}},
    {"kind": "tool_result", "tool_use_id": "1", "is_error": False, "text": "(2 files)"},
    {"kind": "tool_use", "id": "2", "name": "Read", "input": {"file_path": LLM}},
    {"kind": "tool_result", "tool_use_id": "2", "is_error": False, "text": "(612 lines)"},
    {"kind": "thinking", "text": "**Checking what happens if the result lands mid-cancel.**"},
    {"kind": "tool_use", "id": "3", "name": "Grep",
     "input": {"pattern": "_function_call_results", "path": "src/pipecat"}},
    {"kind": "tool_result", "tool_use_id": "3", "is_error": False, "text": "(4 matches)"},
    {"kind": "text", "text": (
        "The cancellation is right, but one ordering still leaks: if the interruption "
        "arrives while `_push_result` is running, the result is already in "
        "`_function_call_results`, so the next turn's context gets a result for a call "
        "the user cancelled. The new test doesn't cover that order."
    )},
]  # fmt: skip

FINDINGS = [
    Finding(Severity.ISSUE, "A result pushed during an interruption still reaches the context",
            LLM, 415),
    Finding(Severity.SUGGESTION, "No test for an interruption between the result and its push",
            "tests/test_llm_service.py", 216),
]  # fmt: skip

PROPOSAL = {
    "body": "Nice fix. One ordering still leaks a cancelled call's result into the context.",
    "comments": [
        {"path": LLM, "line": 415,
         "body": "If the interruption lands here, the result is stored but the call was "
                 "cancelled. Store it after `_push_result`, or drop it on cancel?"},
        {"path": "tests/test_llm_service.py", "line": 216,
         "body": "Could we add the case where the result arrives during the interruption?"},
    ],
}  # fmt: skip

ANSWER = [
    {"kind": "thinking", "text": "**Tracing the two tasks' order.**"},
    {"kind": "tool_use", "id": "4", "name": "Read",
     "input": {"file_path": LLM, "offset": 400, "limit": 40}},
    {"kind": "tool_result", "tool_use_id": "4", "is_error": False, "text": "(40 lines)"},
    {"kind": "text", "text": (
        "Yes: `_run_function_call` stores the result, then awaits `_push_result`. A "
        "cancel during that await leaves the stored result behind, and the next "
        "`LLMContext` update picks it up. Storing it after the push fixes it."
    )},
]  # fmt: skip


class DemoSuggester(FakeSuggester):
    """What you might ask next: a question first, then a nudge to wrap it up."""

    LINES = ["Is the cancelled result really added to the context?", "Suggest the fix in a comment"]

    async def suggest(self, notes):
        self.notes.append(notes)
        return self.LINES[min(len(self.notes), len(self.LINES)) - 1]


class DemoAgent:
    """Reviews #7012 as scripted, answers one question, and keeps reviewing #7008."""

    def __init__(self):
        self.entries: dict[str, list[dict]] = {}
        self.forever = asyncio.Event()  # #7008's review never ends

    def session_exists(self, session_id, worktree):
        return session_id in self.entries

    def history(self, session_id, worktree):
        return list(self.entries.get(session_id, []))

    async def run(self, *, pr, session_id, resume, prompt, callbacks, **_):
        said = self.entries.setdefault(session_id, [])
        if pr.number != FIX:
            return await self._keep_reviewing(said, callbacks)
        if resume:
            said.append({"kind": "user", "text": prompt})
        for entry in ANSWER if resume else REVIEW:
            await asyncio.sleep(0.05)
            said.append(entry)
            await callbacks.on_message(entry)
        if not resume:
            await callbacks.on_submit("Fixes the leak, but one ordering still leaks.", FINDINGS)
        # Writing set the proposal aside: the agent proposes again, as it's told to.
        await callbacks.on_propose(_request_changes(), "Request changes", PROPOSAL)
        return AgentResult(text="", cost_usd=0.38 if not resume else 0.06, turns=9)

    async def _keep_reviewing(self, said, callbacks):
        entry = {"kind": "tool_use", "id": "9", "name": "Read",
                 "input": {"file_path": "src/pipecat/services/deepgram/flux.py"}}  # fmt: skip
        said.append(entry)
        await callbacks.on_message(entry)
        await self.forever.wait()
        return AgentResult()


def _request_changes():
    from purr.models import ActionKind

    return ActionKind.REQUEST_CHANGES


# -- Recording ------------------------------------------------------------------------


class Recorder:
    """Screenshots, each shown for its own time."""

    def __init__(self, app: PurrApp, pilot, folder: Path):
        self.app, self.pilot, self.folder = app, pilot, folder
        self.frames: list[tuple[Path, float]] = []

    async def shot(self, seconds: float) -> None:
        await self.pilot.pause(0.25)  # let it draw
        name = f"{len(self.frames):03}.svg"
        self.app.save_screenshot(filename=name, path=str(self.folder))
        self.frames.append((self.folder / name, seconds))

    async def type(self, text: str, every: int = 4) -> None:
        """Type it a few keys at a time, a frame each."""
        for start in range(0, len(text), every):
            await self.pilot.press(*text[start : start + every])
            await self.shot(0.12)


async def until(pilot, condition, timeout: float = 10) -> None:
    async def wait():
        while not condition():
            await pilot.pause(0.05)

    await asyncio.wait_for(wait(), timeout)


def make_app() -> tuple[PurrApp, FakeGitHub]:
    github = FakeGitHub()
    github.repos = {REPO, "pipecat-ai/pipecat-flows"}
    github.open[REPO] = PRS
    github.open["pipecat-ai/pipecat-flows"] = [
        remote(311, repo="pipecat-ai/pipecat-flows", title="Typed node functions",
               author="judy", created_at=ago(days=1)),
    ]  # fmt: skip
    github.assigned = [pr for pr in PRS if "me" in pr.assignees]
    github.requested = [pr for pr in PRS if "me" in pr.requested]
    ref = PRRef(REPO, FIX)
    github.details[ref] = DETAILS
    github.diffs[ref] = DIFF
    data = Path(os.environ["PURR_DATA_DIR"])
    config = Config(
        data_dir=data, config_dir=Path(os.environ["PURR_CONFIG_DIR"]), poll_interval=3600
    )
    purr = Purr(
        config,
        github=github,
        agent=DemoAgent(),
        repos=FakeRepos(data),
        writer=FakeWriter(),
        recap_agent=FakeRecapAgent(),
        lead=FakeLead(),
        suggester=DemoSuggester(),
        handle_sigint=False,
    )
    return PurrApp(purr), github


async def story(app: PurrApp, record: Recorder) -> None:
    pilot = record.pilot
    for repo in (REPO, "pipecat-ai/pipecat-flows"):
        await app.bus.request(WatchRepo(repo=repo))
    while not await app.purr.store.pending_actions(PRRef(REPO, FIX)):
        await pilot.pause(0.05)  # #7012's review: done, and waiting on you
    await until(pilot, lambda: len(app.repo_names) == 2)
    await record.shot(2.2)  # home: your repos, and what needs you

    await pilot.press("1")
    app.main.repo.select(f"{REPO}#{FIX}", repo=REPO)
    session = app.main.repo.session
    decision = session.query_one("#pr-decision")
    await until(pilot, lambda: getattr(session.pr, "number", None) == FIX)
    await until(pilot, lambda: "Request changes?" in str(decision.render()))
    await record.shot(3.5)  # the list, grouped, and the review that needs you

    await pilot.press("d")  # the diff, in the list's place, next to the conversation
    repo = app.main.repo
    view = repo.query_one(DiffView)
    await until(pilot, lambda: repo.diff_shown and len(view._rows) > 2)
    await record.shot(2.5)
    await move_to(record, view, new=415)  # the line the agent's comment is about
    await pilot.press("shift+down")  # select it and the next: shift+↑/↓
    await record.shot(1.0)
    await pilot.press("enter")  # ask about them
    prompt = session.query_one("#prompt")
    await until(pilot, lambda: prompt.text.startswith("About llm_service.py"))
    await record.shot(1.5)
    await record.type("why store it before pushing?")
    await pilot.press("enter")
    await until(pilot, lambda: "Storing it after the push" in _transcript(app))
    await record.shot(3.5)  # the agent answers in the PR's conversation, next to the code

    await pilot.press("shift+tab")  # back to the diff, to comment on a line
    await move_to(record, view, new=404)
    await pilot.press("c")
    await until(pilot, lambda: isinstance(app.screen, CommentScreen))
    await record.type("Why not keep waiting on calls marked uninterruptible?")
    await pilot.press("enter")
    await until(pilot, lambda: not isinstance(app.screen, CommentScreen))
    await record.shot(2.5)  # your comment, in a box under the line

    await pilot.press("escape")  # the list again
    await until(pilot, lambda: "your 1 comment" in str(decision.render()))
    await record.shot(2.5)  # posting it is your call
    await pilot.press("a")
    await until(pilot, lambda: bool(app.purr._writer.reviews) and not decision.display)
    await pilot.pause(0.6)
    await record.shot(4.0)  # posted, as you, and the PR is done


async def move_to(record: Recorder, view: DiffView, *, new: int) -> None:
    """To the line numbered ``new`` in the PR's version, a frame a step."""
    target = next(i for i, row in enumerate(view._rows) if row.line and row.line.new == new)
    while view.cursor != target:
        await record.pilot.press("down" if view.cursor < target else "up")
        await record.shot(0.1)


def _transcript(app: PurrApp) -> str:
    log = app.main.repo.session.query_one("#transcript")
    return "\n".join(strip.text for strip in log.lines)


async def record(folder: Path) -> list[tuple[Path, float]]:
    app, _ = make_app()
    async with app.run_test(size=SIZE) as pilot:
        recorder = Recorder(app, pilot, folder)
        await story(app, recorder)
        app.purr.agent.forever.set()
        return recorder.frames


# -- To a GIF -------------------------------------------------------------------------


def png(svg: Path) -> Path:
    """The screenshot as a PNG, the size the SVG says."""
    size = re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', svg.read_text())
    width, height = map(float, size.groups()) if size else (1600.0, 900.0)
    out = svg.with_suffix(".png")
    chrome = shutil.which("google-chrome") or shutil.which("chromium") or "chromium"
    subprocess.run(
        [chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars",
         f"--window-size={round(width)},{round(height)}", f"--screenshot={out}", svg.as_uri()],
        check=True, capture_output=True,
    )  # fmt: skip
    return out


def gif(frames: list[tuple[Path, float]], out: Path) -> None:
    command = ["magick"]
    for svg, seconds in frames:
        command += ["-delay", str(round(seconds * 100)), str(png(svg))]
    command += ["-resize", f"{WIDTH}x", "-layers", "Optimize", "-loop", "0", str(out)]
    subprocess.run(command, check=True)


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "docs" / "purr.gif"
    folder = HOME / "frames"
    folder.mkdir()
    logger.remove()  # Purr's log: not on the terminal
    frames = asyncio.run(record(folder))
    gif(frames, out)
    print(f"{out}: {len(frames)} frames, {out.stat().st_size // 1024} KB")


if __name__ == "__main__":
    main()
