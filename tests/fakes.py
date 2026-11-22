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
    """Stands in for purr.github.GitHub: repos that exist, their open PRs, updates,
    assignments, and PR details."""

    def __init__(self):
        self.repos = {"pipecat-ai/pipecat", "pipecat-ai/whisker"}
        self.open: dict[str, list[RemotePR]] = {}
        self.details: dict = {}  # PRRef -> PRDetails
        self.details_calls: list[list] = []
        self.diffs: dict = {}  # PRRef -> unified diff
        self.diff_calls: list = []
        self.updates: dict[str, list[RemotePR]] = {}
        self.assigned: list[RemotePR] = []
        self.calls: list[tuple[list[str], dict[str, str]]] = []

    async def repo_name(self, name: str) -> str | None:
        return {repo.lower(): repo for repo in self.repos}.get(name.lower())

    async def snapshot(self, full=(), incremental=None, *, on_page=None):
        full, incremental = list(full), dict(incremental or {})
        self.calls.append((full, incremental))
        for repo in full:
            if on_page and self.open.get(repo):
                await on_page(repo, list(self.open[repo]))
        return Snapshot(
            open={r: list(self.open.get(r, [])) for r in full},
            assigned=list(self.assigned),
            updates={r: list(self.updates.get(r, [])) for r in incremental},
            viewer="me",
        )

    async def viewer_login(self):
        return "me"

    async def pr_statuses(self, refs):
        return {}

    async def pr_details(self, refs):
        refs = list(refs)
        self.details_calls.append(refs)
        return {ref: self.details[ref] for ref in refs if ref in self.details}

    async def pr_diff(self, ref):
        self.diff_calls.append(ref)
        return self.diffs.get(ref, "")

    async def review_threads(self, ref):
        return list(getattr(self, "threads", []))


class FakeRepos:
    """Stands in for purr.git.GitRepos: worktrees are empty directories."""

    def __init__(self, root):
        self.root = root
        self.checkouts: list = []

    def worktree_path(self, ref):
        return self.root / "worktrees" / ref.repo / str(ref.number)

    async def guidelines(self, repo):
        return getattr(self, "repo_guidelines", "")

    async def sync(self, repo, prs):
        self.synced = [*getattr(self, "synced", []), (repo, [pr.number for pr in prs])]

    async def diff(self, pr):
        return getattr(self, "diffs", {}).get(pr.ref)

    async def new_commits(self, pr, since):
        return getattr(self, "commits", None)

    async def plugin(self, repo):
        if isinstance(error := getattr(self, "plugin_error", None), Exception):
            raise error
        return getattr(self, "repo_plugin", None)

    def worktrees(self):
        return [ref for ref, _ in self.checkouts if self.worktree_path(ref).exists()]

    async def remove_worktree(self, ref):
        import shutil

        self.removed = [*getattr(self, "removed", []), ref]
        existed = self.worktree_path(ref).exists()
        shutil.rmtree(self.worktree_path(ref), ignore_errors=True)
        return existed

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
        self.ask: tuple | None = None  # (tool, input, reason[, suggested rules])
        self.answers: list[bool | None] = []
        self.insteads: list[str | None] = []  # what you wrote instead of answering
        self.read: list[str] = []  # what you wrote while it worked, as it read it
        self.reads_inbox = True  # False: like a Claude Code that can't take it mid-run
        self.remembered: list[str | None] = []
        self.entries: dict[str, list[dict]] = {}
        self.findings: dict = {}  # PRRef -> what its review finds (default: one race)

    def session_exists(self, session_id, worktree):
        return session_id in self.sessions

    def history(self, session_id, worktree):
        return list(self.entries.get(session_id, []))

    async def run(
        self,
        *,
        pr,
        worktree,
        session_id,
        resume,
        prompt,
        callbacks,
        guidelines="",
        plugin=None,
        skill=None,
        plan=None,
        model=None,
        inbox=None,
    ):
        from purr.models import ActionKind, Finding, Severity
        from purr.review.agent import AgentResult

        self.runs.append(
            {
                "pr": pr.ref,
                "session_id": session_id,
                "resume": resume,
                "prompt": prompt,
                "guidelines": guidelines,
                "plugin": plugin,
                "skill": skill,
                "plan": plan,
                "model": model,
            }
        )
        self.sessions.add(session_id)
        if self.gate:
            await self.gate.wait()
        while self.reads_inbox and inbox is not None and not inbox.empty():
            self.read.append(inbox.get_nowait())  # what you wrote meanwhile
        if self.fail_with:
            raise RuntimeError(self.fail_with)
        if self.ask:
            answer = await callbacks.on_ask(*self.ask)
            self.answers.append(answer.allowed)
            self.insteads.append(answer.instead)
            self.remembered.append(answer.remember)
        self.threads = await callbacks.on_threads()
        entry = {"kind": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "gh pr diff"}}
        self.entries.setdefault(session_id, []).append(entry)
        await callbacks.on_message(entry)
        race = [Finding(severity=Severity.ISSUE, text="Race on shutdown", file="bus.py", line=42)]
        findings = self.findings.get(pr.ref, race)
        await callbacks.on_submit(
            "Clean refactor, one race." if findings else "Looks good.", findings
        )
        await callbacks.on_propose(
            ActionKind.REQUEST_CHANGES,
            "Post 1 comment and request changes",
            {"body": "", "comments": [{"path": "bus.py", "line": 42, "body": "Race here"}]},
        )
        return AgentResult(
            text="Done.",
            cost_usd=0.25,
            turns=4,
            duration_ms=1500,
            api_duration_ms=1200,
            model="claude-opus-5-5",
            usage={"input_tokens": 10, "output_tokens": 20, "cache_read_input_tokens": 300},
        )


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


def make_plugin(root, skills: dict[str, str], *, name: str = "pipecat", commands=None):
    """A repo plugin as GitRepos.plugin builds it: ``skills`` and ``commands`` map
    names to their descriptions (or to whole frontmatter dicts)."""
    import json

    import yaml

    def write(path, meta):
        meta = meta if isinstance(meta, dict) else {"description": meta}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"---\n{yaml.safe_dump(meta)}---\n\nDo the thing.\n")

    (root / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (root / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": name}))
    for skill, meta in skills.items():
        write(root / "skills" / skill / "SKILL.md", meta)
    for command, meta in (commands or {}).items():
        write(root / "commands" / f"{command}.md", meta)
    return root


class FakeLead:
    """Stands in for purr.lead.ClaudeLead: plans what ``plans`` says, by PR.

    ``plans`` maps ``owner/repo#123`` to a plan dict; PRs not in it get a full,
    normal-priority review. Set ``fail_with`` to make planning raise.
    """

    def __init__(self):
        import re

        self._pr = re.compile(r"^## ([\w.-]+/[\w.-]+#\d+) ", re.MULTILINE)
        self.plans: dict[str, dict] = {}
        self.notes: list[str] = []
        self.fail_with: str | None = None
        self.gate = None

    async def plan(self, notes):
        self.notes.append(notes)
        if self.gate:
            await self.gate.wait()
        if self.fail_with:
            raise RuntimeError(self.fail_with)
        default = {"depth": "full", "priority": "normal", "focus": [], "why": "Looks real."}
        return {
            "plans": [
                {"pr": pr, **default, **self.plans.get(pr, {})} for pr in self._pr.findall(notes)
            ]
        }
