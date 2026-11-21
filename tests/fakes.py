"""Test doubles shared across tests."""

from purr.github import RemotePR, Snapshot


def remote(number: int, repo: str = "pipecat-ai/pipecat", **kwargs) -> RemotePR:
    return RemotePR(
        repo=repo,
        number=number,
        title=kwargs.pop("title", f"PR {number}"),
        author=kwargs.pop("author", "someone"),
        url=f"https://github.com/{repo}/pull/{number}",
        head_sha=kwargs.pop("head_sha", "abc"),
        updated_at=kwargs.pop("updated_at", "2026-11-20T10:00:00Z"),
        **kwargs,
    )


class FakeGitHub:
    """Stands in for purr.github.GitHub: repos that exist, pools, updates, assignments."""

    def __init__(self):
        self.repos = {"pipecat-ai/pipecat", "pipecat-ai/whisker"}
        self.pool: dict[str, list[RemotePR]] = {}
        self.updates: dict[str, list[RemotePR]] = {}
        self.assigned: list[RemotePR] = []
        self.calls: list[tuple[list[str], dict[str, str]]] = []

    async def repo_name(self, name: str) -> str | None:
        return {repo.lower(): repo for repo in self.repos}.get(name.lower())

    async def snapshot(self, full=(), incremental=None, *, on_page=None):
        full, incremental = list(full), dict(incremental or {})
        self.calls.append((full, incremental))
        for repo in full:
            if on_page and self.pool.get(repo):
                await on_page(repo, list(self.pool[repo]))
        return Snapshot(
            pool={r: list(self.pool.get(r, [])) for r in full},
            assigned=list(self.assigned),
            updates={r: list(self.updates.get(r, [])) for r in incremental},
            viewer="me",
        )

    async def pr_statuses(self, refs):
        return {}

    async def review_threads(self, ref):
        return list(getattr(self, "threads", []))


class FakeRepos:
    """Stands in for purr.git.GitRepos: worktrees are empty directories."""

    def __init__(self, root):
        self.root = root
        self.checkouts: list = []

    def worktree_path(self, ref):
        return self.root / "worktrees" / ref.repo / str(ref.number)

    async def checkout(self, ref, head_sha):
        path = self.worktree_path(ref)
        path.mkdir(parents=True, exist_ok=True)
        self.checkouts.append((ref, head_sha))
        return path


class FakeAgent:
    """Stands in for purr.review.agent.ClaudeReviewAgent with a scripted review.

    Each run reads the diff, submits one finding and proposes requesting changes.
    Set ``gate`` to hold runs until it's set, ``fail_with`` to make them raise,
    ``ask`` to ask permission for something first (answers go to ``answers``).
    """

    def __init__(self):
        self.runs: list[dict] = []
        self.sessions: set[str] = set()
        self.gate = None
        self.fail_with: str | None = None
        self.ask: tuple[str, dict, str] | None = None
        self.answers: list[bool] = []
        self.entries: dict[str, list[dict]] = {}

    def session_exists(self, session_id, worktree):
        return session_id in self.sessions

    def history(self, session_id, worktree):
        return list(self.entries.get(session_id, []))

    async def run(self, *, pr, worktree, session_id, resume, prompt, callbacks):
        from purr.models import ActionKind, Finding, Severity
        from purr.review.agent import AgentResult

        self.runs.append(
            {"pr": pr.ref, "session_id": session_id, "resume": resume, "prompt": prompt}
        )
        self.sessions.add(session_id)
        if self.gate:
            await self.gate.wait()
        if self.fail_with:
            raise RuntimeError(self.fail_with)
        if self.ask:
            self.answers.append(await callbacks.on_ask(*self.ask))
        self.threads = await callbacks.on_threads()
        entry = {"kind": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "gh pr diff"}}
        self.entries.setdefault(session_id, []).append(entry)
        await callbacks.on_message(entry)
        await callbacks.on_submit(
            "Clean refactor, one race.",
            [Finding(severity=Severity.ISSUE, text="Race on shutdown", file="bus.py", line=42)],
        )
        await callbacks.on_propose(
            ActionKind.REQUEST_CHANGES,
            "Post 1 comment and request changes",
            {"body": "", "comments": [{"path": "bus.py", "line": 42, "body": "Race here"}]},
        )
        return AgentResult(text="Done.")


class FakeWriter:
    """Stands in for purr.actions.GhWriter: records reviews instead of posting them."""

    def __init__(self):
        self.reviews: list[dict] = []
        self.assigned: list = []
        self.replies: list[tuple[str, str]] = []
        self.fail_with: list[str] = []  # errors to raise, one per call, before succeeding
        self.fail_replies = False

    async def submit_review(self, ref, *, event, body, comments, commit_id):
        from purr.actions import ActionError

        if self.fail_with:
            raise ActionError(self.fail_with.pop(0))
        self.reviews.append(
            {"pr": ref, "event": event, "body": body, "comments": comments, "commit_id": commit_id}
        )
        return f"https://github.com/{ref.repo}/pull/{ref.number}#review-{len(self.reviews)}"

    async def assign_self(self, ref):
        self.assigned.append(ref)

    async def reply_to_thread(self, thread_id, body):
        from purr.actions import ActionError

        if self.fail_replies:
            raise ActionError("thread is locked")
        self.replies.append((thread_id, body))
        return f"https://github.com/reply/{len(self.replies)}"


class FakeRecapAgent:
    """Stands in for purr.recap.ClaudeRecapAgent: one item per PR mentioned in the notes."""

    def __init__(self):
        self.notes: list[str] = []
        self.fail = False

    async def summarize(self, notes):
        import re

        self.notes.append(notes)
        if self.fail:
            raise RuntimeError("no recap today")
        prs = list(dict.fromkeys(re.findall(r"[\w.-]+/[\w.-]+#\d+", notes)))
        items = [{"pr": pr, "text": "Something happened", "needs_you": True} for pr in prs]
        return f"{len(prs)} PRs need you.", items
