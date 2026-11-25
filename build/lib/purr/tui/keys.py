"""The repo's keys, and which each of its panes shows in the footer.

They all work from either pane (they're about the PR: selected in the list, shown
in its pane), so RepoView binds them all, hidden; each pane binds the ones its
footer shows, in its order, running the repo view's action. A new key: a line in
``REPO_KEYS``, and its action in the panes' lists where it belongs.
"""

from dataclasses import replace

from textual.binding import Binding

_NEXT = Binding.Group("Next/prev", compact=True)

# By action: RepoView's.
REPO_KEYS = {
    "open": Binding("o", "open", "GitHub"),  # the PR's page, in your browser
    "search": Binding("slash", "search", "Search", key_display="/"),
    "review": Binding("r", "review", "Review"),
    "skip": Binding("s", "skip", "Skip"),  # up next: not for you
    "mark": Binding("space", "mark", "Mark"),
    # Whatever the decision box offers: do or dismiss a proposal, allow or deny a
    # permission request, merge. The box says them, with what each does, so the footer
    # doesn't; ? lists them, a saying what it does (see MainScreen.describe_key).
    "approve": Binding("a", "approve", "Accept", show=False),
    "always": Binding("A", "always", "Always allow", show=False),
    "dismiss": Binding("x", "dismiss", "Dismiss", show=False),
    "merge": Binding("m", "merge", "Merge", show=False),
    "move(1)": Binding("n", "move(1)", "Next", group=_NEXT),
    "move(-1)": Binding("p", "move(-1)", "Previous", group=_NEXT),
    "diff": Binding("d", "diff", "Diff"),
    "pager": Binding("D", "pager", "Pager"),
    "thinking": Binding("t", "thinking", "Thinking"),
    "zoom": Binding("z", "zoom", "Zoom"),
    "open_claude": Binding("c", "open_claude", "Claude Code"),  # also /claude
    "shell": Binding("exclamation_mark", "shell", "Shell"),
    # Two panes, so either way it's the other one (typing included: tab never
    # indents the prompt).
    "switch_pane": Binding("tab,shift+tab", "switch_pane", "Switch pane"),
}


def pane_keys(actions: list[str], **labels: str) -> list[Binding]:
    """A pane's footer: the repo's keys for ``actions``, in that order, each running
    the repo view's action (``action_repo``); ``labels`` rename some, by action."""
    return [_in_pane(REPO_KEYS[action], labels.get(action)) for action in actions]


def _in_pane(key: Binding, label: str | None) -> Binding:
    return replace(key, action=f"repo('{key.action}')", description=label or key.description)
