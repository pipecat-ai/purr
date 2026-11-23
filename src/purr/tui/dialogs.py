"""Purr's dialogs: modal screens over the main one."""

from typing import TYPE_CHECKING, cast

from rich.table import Table
from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Label, OptionList, Static
from textual.widgets.option_list import Option

from purr.jobs import PurrError
from purr.messages import WatchRepo
from purr.review.skills import Skill

if TYPE_CHECKING:
    from purr.tui.app import PurrApp


class AddRepoScreen(ModalScreen[str | None]):
    """Ask for a repo to watch and check it on GitHub."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, repo: str = ""):
        super().__init__()
        self._repo = repo  # filled in, ready to watch

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label("[b]Watch a repo[/b]")
            yield Input(value=self._repo, placeholder="owner/name or GitHub URL")
            yield Static(id="error")
            yield Label("enter add · esc cancel", classes="hint")

    @on(Input.Submitted)
    def submit(self, event: Input.Submitted) -> None:
        if event.value.strip():
            self.query_one("#error", Static).update(Text("Checking GitHub…", style="dim"))
            self.add(event.value)

    @work(exclusive=True)
    async def add(self, value: str) -> None:
        try:
            name = (await cast("PurrApp", self.app).bus.request(WatchRepo(repo=value))).repo
        except PurrError as e:
            self.query_one("#error", Static).update(Text(str(e), style="red"))
            return
        self.dismiss(name)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfirmScreen(ModalScreen[bool]):
    """A yes/no question."""

    BINDINGS = [
        Binding("y", "answer(True)", "Yes"),
        Binding("n", "answer(False)", "No"),
        Binding("escape", "answer(False)", "No", show=False),
    ]

    def __init__(self, question: str, detail: str = ""):
        super().__init__()
        self._question = question
        self._detail = detail

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(self._question)
            if self._detail:
                yield Label(self._detail, classes="hint")
            yield Label("y yes · n no", classes="hint")

    def action_answer(self, yes: bool) -> None:
        self.dismiss(yes)


class NoticeScreen(ModalScreen[None]):
    """Something you should know (it went wrong), until you close it."""

    BINDINGS = [Binding("escape,enter", "close", "Close")]

    def __init__(self, message: str, detail: str = ""):
        super().__init__()
        self._message = message
        self._detail = detail

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(self._message)
            if self._detail:
                yield Label(self._detail, classes="hint")
            yield Label("enter or esc closes", classes="hint")

    def action_close(self) -> None:
        self.dismiss(None)


class AlwaysScreen(ModalScreen[str | None]):
    """Where to always allow a rule: this repo (``repo``), everywhere (``all``), or not (None)."""

    BINDINGS = [
        Binding("r", "pick('repo')", "This repo"),
        Binding("e", "pick('all')", "Everywhere"),
        Binding("escape", "pick('')", "Cancel", show=False),
    ]

    def __init__(self, rule: str, repo: str):
        super().__init__()
        self._rule, self._repo = rule, repo

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label("Always allow")
            yield Label(Text(self._rule, style="bold yellow"))
            yield Label(Text.from_markup(f"[b]r[/b]  in {self._repo}'s reviews"))
            yield Label(
                Text.from_markup("[b]e[/b]  everywhere: your Claude Code settings, Claude Code too")
            )
            yield Label("It's Claude Code's own rule. Change them with i, then p.", classes="hint")
            yield Label("r this repo · e everywhere · esc cancel", classes="hint")

    def action_pick(self, scope: str) -> None:
        self.dismiss(scope or None)


class InstructionsScreen(ModalScreen):
    """Which instructions to edit: how you like reviews, or how you like code.

    Also your standing permissions (``permissions``), Purr's settings
    (``settings``), and, inside a repo, which of its skills its reviews follow
    (``skill``).
    """

    BINDINGS = [
        Binding("r", "pick('review')", "Review style"),
        Binding("c", "pick('code')", "Code style"),
        Binding("p", "pick('permissions')", "Permissions"),
        Binding("s", "pick('skill')", "Review skill"),
        Binding("o", "pick('settings')", "Settings"),
        Binding("escape", "pick('')", "Cancel", show=False),
    ]

    def __init__(self, repo: str | None):
        super().__init__()
        self._repo = repo

    def compose(self) -> ComposeResult:
        scope = f"for [b]{self._repo}[/b]" if self._repo else "for [b]all repos[/b]"
        with Vertical(classes="dialog"):
            yield Label(f"Your instructions {scope}")
            yield Label("[b]r[/b]  review style: how reviews should go")
            yield Label("[b]c[/b]  code style: what good code looks like to you")
            yield Label("[b]p[/b]  permissions: what agents may do without asking")
            if self._repo:
                yield Label("[b]s[/b]  review skill: which of the repo's skills reviews follow")
            yield Label("[b]o[/b]  Purr's settings: models, polling, signature… (all repos)")
            yield Label("Opens in $EDITOR. Repo files add to the general ones.", classes="hint")
            yield Label("a key above opens it · esc cancel", classes="hint")

    def check_action(self, action: str, parameters: tuple) -> bool:
        return self._repo is not None or parameters != ("skill",)

    def action_pick(self, kind: str) -> None:
        from purr.review.instructions import Kind

        self.dismiss(
            kind if kind in ("skill", "permissions", "settings") else Kind(kind) if kind else None
        )


class ReviewSkillScreen(ModalScreen[str | None]):
    """Which of a repo's skills its reviews follow, if any.

    Dismisses with the skill, ``""`` for Purr's own review, or None (escape):
    leave it as it is, and when asking, ask again next time.

    Args:
        repo: The repo.
        skills: Its skills that may be for reviewing.
        chosen: What its reviews follow now ('' for Purr's own, None: not decided).
        asking: Purr is asking (reviews are waiting), rather than you changing it.
    """

    BINDINGS = [Binding("escape", "cancel", "Not now")]

    def __init__(
        self, repo: str, skills: list[Skill], chosen: str | None = None, *, asking: bool = False
    ):
        super().__init__()
        self._repo = repo
        self._choices = [skill.name for skill in skills] + [""]
        self._skills = skills
        self._chosen = chosen
        self._asking = asking

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog", id="review-skill"):
            if self._asking:
                yield Label(
                    f"[b]{self._repo}[/b] has skills that may be how it reviews PRs. "
                    "Should Purr's reviews there follow one?"
                )
            else:
                yield Label(f"What Purr's reviews in [b]{self._repo}[/b] follow")
            yield OptionList(id="review-skill-items")
            later = "ask me next time" if self._asking else "cancel"
            yield Label(f"enter chooses · esc {later} · you can change it with i", classes="hint")

    def on_mount(self) -> None:
        options = []
        for skill in [*self._skills, Skill(name="", description="", likely=False)]:
            name = skill.name
            title = f"/{name}" if name else "Purr's own review"
            description = (
                skill.description
                if name
                else "Its general review, holding the PR to the repo's CLAUDE.md / AGENTS.md"
            )
            # Descriptions wrap in their own column, under the name.
            row = Table.grid(padding=(0, 1))
            row.add_column(width=1)
            row.add_column(ratio=1)
            row.add_row("✓" if name == self._chosen else "", Text(title, style="bold"))
            row.add_row("", Text(description, style="dim"))
            options.append(Option(row))
        items = self.query_one(OptionList)
        items.set_options(options)
        items.highlighted = (
            self._choices.index(self._chosen) if self._chosen in self._choices else 0
        )
        items.focus()

    @on(OptionList.OptionSelected, "#review-skill-items")
    def _picked(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(self._choices[event.option_index])

    def action_cancel(self) -> None:
        self.dismiss(None)
