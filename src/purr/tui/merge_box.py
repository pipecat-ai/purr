"""The merge box: where a PR stands for merging, once you press m (see purr.merging)."""

import time
from collections.abc import Sequence
from datetime import UTC, datetime

from rich.text import Text

from purr.merging import WORKING_OUT, check_concerns, working_out
from purr.messages import MergeReadiness
from purr.models import PR, Check, CheckState, MergeCheck
from purr.tui.working import elapsed_text

# GitHub's names for its merge buttons, by merge method.
_MERGE_VERBS = {"merge": "Merge", "squash": "Squash and merge", "rebase": "Rebase and merge"}


def render_merge(pr: PR, readiness: MergeReadiness, now: datetime | None = None) -> Text:
    """Where the PR stands for merging: what blocks it, or what you'd be merging past.
    While it waits on GitHub (see settling), each check, as GitHub's page lists them, and
    whether GitHub is still working out if it can be merged."""
    if readiness.blockers:
        text = Text(f"Can't merge #{pr.number} yet:", style="bold")
        for blocker in readiness.blockers:
            text.append(f"\n  ✗ {blocker}", style="red")
        text.append("\n[m] check again", style="bold")
        text.append("   [x] close", style="dim")
        return text
    check = readiness.check
    verb = _MERGE_VERBS.get(check.method, "Merge")
    text = Text(f"{verb} #{pr.number} into {check.base}?", style="bold")
    text.append(f"\n  ✓ you approved it at {check.head_sha[:7]}", style="green")
    concerns = readiness.concerns
    if settling(readiness):
        text.append_text(_waiting_lines(check, now or datetime.now(UTC)))
        listed = {*check_concerns(check), WORKING_OUT}
        concerns = [concern for concern in concerns if concern not in listed]
    elif passed := sum(run.state == CheckState.PASSED for run in check.checks):
        text.append(f"\n  ✓ {passed} check{'s' if passed != 1 else ''} passed", style="green")
    for concern in concerns:
        text.append(f"\n  ⚠ {concern}", style="yellow")
    text.append("\n")
    if check.waiting:  # a: GitHub's "Approve and run workflows"
        text.append("[a] approve and run CI   ", style="bold")
    text.append("[m] merge anyway" if readiness.concerns else "[m] merge", style="bold")
    text.append("   [x] leave it", style="dim")
    return text


SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"  # a check still running, a frame every SPIN_SECONDS
SPIN_SECONDS = 0.1
MAX_CHECKS_LISTED = 12  # past these, those that passed or were skipped are a line
_CHECK_MARKS = {
    CheckState.PASSED: ("✓", "green"),
    CheckState.FAILED: ("✗", "red"),
    CheckState.SKIPPED: ("–", "dim"),
}


def checks_running(check: MergeCheck) -> bool:
    return any(run.state == CheckState.RUNNING for run in check.checks)


def settling(merge: MergeReadiness | None) -> bool:
    """The merge box waits on GitHub: nothing blocks the merge, but a check is still
    running, or GitHub is still working out whether it can be merged. Its spinners turn,
    and GitHub's asked again until it isn't (PRKeys._follow_merge)."""
    if merge is None or merge.blockers:
        return False
    return checks_running(merge.check) or working_out(merge.check)


def can_run_ci(merge: MergeReadiness | None) -> bool:
    """The merge box offers to approve the workflows GitHub holds (a): nothing blocks the
    merge, and some wait."""
    return bool(merge and not merge.blockers and merge.check.waiting)


def _spinner() -> str:
    return SPINNER[int(time.monotonic() / SPIN_SECONDS) % len(SPINNER)]


def _waiting_lines(check: MergeCheck, now: datetime) -> Text:
    """The checks (see _check_lines), then a spinner while GitHub works out whether it
    can be merged, as its page says whether there are conflicts below the checks."""
    text = _check_lines(check.checks, now)
    if working_out(check):
        text.append(f"\n  {_spinner()} ", style="yellow")
        text.append("GitHub is working out whether it can be merged")
    return text


def _check_lines(checks: Sequence[Check], now: datetime) -> Text:
    """Each check while CI runs, in GitHub's order: a spinner on those still running, a
    mark on the rest, and how long each took (or has, so far)."""
    listed = list(checks)
    if len(listed) > MAX_CHECKS_LISTED:
        listed = [run for run in checks if run.state in (CheckState.RUNNING, CheckState.FAILED)]
    spinner = _spinner()
    text = Text()
    for run in listed:
        running = run.state == CheckState.RUNNING
        mark, style = (spinner, "yellow") if running else _CHECK_MARKS[run.state]
        text.append(f"\n  {mark} ", style=style)
        text.append(run.name, style="red" if run.state == CheckState.FAILED else "")
        if run.required:
            text.append(" (required)", style="dim")
        if took := _took(run, now):
            text.append(f"  {took}", style="dim")
    if passed := sum(run.state == CheckState.PASSED for run in checks if run not in listed):
        text.append(f"\n  ✓ {passed} more passed", style="green")
    return text


def _took(run: Check, now: datetime) -> str:
    """How long a check took, or has so far if it's running: ``1m 12s``; "" if unknown."""
    if not run.started or not (run.finished or run.state == CheckState.RUNNING):
        return ""
    try:
        start = datetime.fromisoformat(run.started)
        end = datetime.fromisoformat(run.finished) if run.finished else now
    except ValueError:
        return ""
    return elapsed_text(max(0.0, (end - start).total_seconds()))
