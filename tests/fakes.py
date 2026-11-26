"""Test doubles shared across tests."""

from dataclasses import replace

from purr.git import Unpushed
from purr.github import RemotePR, Snapshot, Standing
from purr.models import ActionKind, ActionStatus, Verdict


async def give_verdict(store, ref, kind=ActionKind.APPROVE, *, head="abc") -> None:
    """You approved the PR (or requested changes) through Purr, at ``head``."""
    action = await store.add_pending_action(ref, kind=kind, description="Verdict")
    await store.transition_action(
        action.id, expected=ActionStatus.PENDING, new=ActionStatus.EXECUTED, posted_head=head
    )


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
        self.merge_checks: dict = {}  # PRRef -> MergeCheck
        self.details_calls: list[list] = []
        self.diffs: dict = {}  # PRRef -> unified diff
        self.diff_calls: list = []
        self.updates: dict[str, list[RemotePR]] = {}
        self.assigned: list[RemotePR] = []
        self.requested: list[RemotePR] = []
        self.involved: list[RemotePR] = []  # PRs you commented on or reviewed
        self.conversations: dict = {}  # PRRef -> [Reply], what standing() answers
        self.verdicts: dict = {}  # PRRef -> Verdict, your latest on GitHub
        self.standing_calls: list[list] = []
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
            requested=list(self.requested),
            involved=list(self.involved),
        )

    async def viewer_login(self):
        return "me"

    async def pr_statuses(self, refs):
        """From ``statuses``: PRRef -> PRStatus, what the PR itself says (left out: unread)."""
        refs = list(refs)
        self.status_calls = [*getattr(self, "status_calls", []), refs]
        statuses = getattr(self, "statuses", {})
        return {ref: statuses[ref] for ref in refs if ref in statuses}

    async def pr_details(self, refs):
        refs = list(refs)
        self.details_calls.append(refs)
        return {ref: self.details[ref] for ref in refs if ref in self.details}

    async def pr_diff(self, ref):
        self.diff_calls.append(ref)
        return self.diffs.get(ref, "")

    async def file_at(self, repo, path, sha):
        """From ``files``: (repo, path) -> text."""
        from purr.github import GitHubError

        if (repo, path) not in getattr(self, "files", {}):
            raise GitHubError("Not Found (HTTP 404)")
        return self.files[(repo, path)]

    async def pr_head(self, ref):
        """From ``heads``: PRRef -> PRHead (None: GitHub doesn't show it)."""
        return getattr(self, "heads", {}).get(ref)

    async def merge_check(self, ref):
        """From ``merge_checks``; set ``merge_gate`` to hold it until it's set."""
        if gate := getattr(self, "merge_gate", None):
            await gate.wait()
        return self.merge_checks.get(ref)

    async def standing(self, refs):
        refs = list(refs)
        self.standing_calls.append(refs)
        return {
            ref: Standing(list(self.conversations.get(ref, [])), self.verdicts.get(ref))
            for ref in refs
        }

    async def review_threads(self, ref):
        return list(getattr(self, "threads", []))

    async def history(self, repo):
        """From ``past``: the PastPRs that say what you know of a repo."""
        return list(getattr(self, "past", []))


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

    async def local(self, pr):
        """From ``locals``: PRRef -> Local (what the agent committed, not pushed)."""
        return getattr(self, "locals", {}).get(pr.ref)

    async def local_diff(self, pr, local):
        """From ``local_diffs``: PRRef -> the diff with what's local."""
        return getattr(self, "local_diffs", {}).get(pr.ref)

    async def moved(self, pr, local):
        return getattr(self, "moves", {}).get(pr.ref, "")

    async def file_at(self, pr, path, commit=""):
        return getattr(self, "files", {}).get((pr.ref, path))

    async def numstat(self, pr):
        """From ``stats``: PRRef -> [FileChange]; None (not synced) for the rest."""
        return getattr(self, "stats", {}).get(pr.ref)

    async def new_commits(self, pr, since):
        return getattr(self, "commits", None)

    async def unpushed(self, ref, onto, base=""):
        """From ``commits_made``: [(sha, subject)], newest first, replacing ``replaces`` of
        the branch's (a rebase); ``unpushed_error`` raises."""
        if isinstance(error := getattr(self, "unpushed_error", None), Exception):
            raise error
        return Unpushed(list(getattr(self, "commits_made", [])), getattr(self, "replaces", 0))

    async def push(self, ref, commit, *, repo, branch, onto):
        """Records ``(ref, commit, repo, branch, onto)`` in ``pushed``; ``push_error`` raises.
        Set ``push_gate`` to hold it until it's set."""
        if gate := getattr(self, "push_gate", None):
            await gate.wait()
        if isinstance(error := getattr(self, "push_error", None), Exception):
            raise error
        self.pushed = [*getattr(self, "pushed", []), (ref, commit, repo, branch, onto)]

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

    Each run reads the diff, submits one finding and proposes requesting changes
    (or ``proposes``: ``(kind, description, payload)``; what's refused goes to
    ``refused``). Set ``gate`` to hold runs until it's set, ``fail_with`` to make
    them raise, ``ask`` to ask permission for something first (answers go to
    ``answers``).
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
        self.first: list[dict] = []  # entries each run sends before its own
        self.remembered: list[str | None] = []
        self.entries: dict[str, list[dict]] = {}
        self.findings: dict = {}  # PRRef -> what its review finds (default: one race)
        self.proposes: tuple | None = None  # (kind, description, payload)
        self.refused: list[str] = []  # why proposals were refused

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
        from purr.review.agent import AgentResult, Refused

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
        for entry in self.first:  # what the scripted run says before its own steps
            await callbacks.on_message(entry)
        self.threads = await callbacks.on_threads()
        entry = {"kind": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "gh pr diff"}}
        self.entries.setdefault(session_id, []).append(entry)
        await callbacks.on_message(entry)
        race = [Finding(severity=Severity.ISSUE, text="Race on shutdown", file="bus.py", line=42)]
        findings = self.findings.get(pr.ref, race)
        await callbacks.on_submit(
            "Clean refactor, one race." if findings else "Looks good.", findings
        )
        proposal = self.proposes or (
            ActionKind.REQUEST_CHANGES,
            "Post 1 comment and request changes",
            {"body": "", "comments": [{"path": "bus.py", "line": 42, "body": "Race here"}]},
        )
        try:
            await callbacks.on_propose(*proposal)
        except Refused as e:
            self.refused.append(str(e))
        return AgentResult(
            text="Done.",
            cost_usd=0.25,
            turns=4,
            duration_ms=1500,
            api_duration_ms=1200,
            model="claude-opus-5-5",
            usage={"input_tokens": 10, "output_tokens": 20, "cache_read_input_tokens": 300},
        )


_VERDICT_EVENTS = {"APPROVE": ActionKind.APPROVE, "REQUEST_CHANGES": ActionKind.REQUEST_CHANGES}


class FakeWriter:
    """Stands in for purr.actions.GhWriter: records reviews instead of posting them. Given
    the FakeGitHub, the comments it posts, changes and deletes are there too, as on GitHub:
    in its review threads, and in what's said on the PR (its details)."""

    def __init__(self, github: "FakeGitHub | None" = None):
        self.github = github
        self.reviews: list[dict] = []
        self.assigned: list = []
        self.replies: list[tuple[str, str]] = []
        self.merges: list[tuple] = []  # (ref, method, sha)
        self.fail_with: list[str] = []  # errors to raise, one per call, before succeeding
        self.fail_replies = False
        self.line_comments: list[tuple] = []  # (ref, comment, commit_id)
        self.pr_comments: list[tuple] = []  # (ref, body)
        self.pr_edits: list[tuple] = []  # (comment_id, body)
        self.deleted: list[tuple] = []  # (comment_id, on_line)
        self.approved_runs: list[tuple] = []  # (repo, run_id)

    async def submit_review(self, ref, *, event, body, comments, commit_id):
        from purr.actions import ActionError

        if self.fail_with:
            raise ActionError(self.fail_with.pop(0))
        self.reviews.append(
            {"pr": ref, "event": event, "body": body, "comments": comments, "commit_id": commit_id}
        )
        if self.github and event in _VERDICT_EVENTS:  # your latest verdict, on GitHub
            self.github.verdicts[ref] = Verdict(_VERDICT_EVENTS[event], commit_id)
        return f"https://github.com/{ref.repo}/pull/{ref.number}#review-{len(self.reviews)}"

    async def assign_self(self, ref):
        self.assigned.append(ref)

    async def edit_review_comment(self, comment_id, body):
        self.edits = [*getattr(self, "edits", []), (comment_id, body)]
        for thread in getattr(self.github, "threads", []):
            for comment in thread["comments"]:
                if comment.get("id") == comment_id:
                    comment["body"] = body
        return f"https://github.com/comment/{comment_id}"

    async def merge(self, ref, *, method, sha):
        from purr.actions import ActionError

        if self.fail_with:
            raise ActionError(self.fail_with.pop(0))
        self.merges.append((ref, method, sha))

    async def approve_run(self, repo, run_id):
        """As GitHub does: the run starts, so the merge check has it running."""
        from dataclasses import replace

        from purr.actions import ActionError
        from purr.models import Check, CheckState

        if self.fail_with:
            raise ActionError(self.fail_with.pop(0))
        self.approved_runs.append((repo, run_id))
        checks = self.github.merge_checks if self.github else {}
        for ref, check in list(checks.items()):
            run = next((run for run in check.waiting if run.id == run_id), None)
            if ref.repo == repo and run:
                waiting = tuple(other for other in check.waiting if other != run)
                started = (*check.checks, Check(run.workflow, CheckState.RUNNING))
                checks[ref] = replace(check, waiting=waiting, checks=started)

    async def post_line_comment(self, ref, comment, *, commit_id):
        self._failing()
        self.line_comments.append((ref, comment, commit_id))
        n = len(self.line_comments)
        if self.github is not None:  # a thread of its own, yours
            mine = {"id": f"LC{n}", "author": "me", "by_user": True, "body": comment["body"],
                    "at": "2026-11-23T10:00:00Z"}  # fmt: skip
            thread = {"id": f"LT{n}", "path": comment["path"], "line": comment["line"],
                      "side": comment["side"], "resolved": False, "comments": [mine]}  # fmt: skip
            self.github.threads = [*getattr(self.github, "threads", []), thread]
        return f"https://github.com/{ref.repo}/pull/{ref.number}#line-{n}"

    async def post_pr_comment(self, ref, body):
        from purr.models import PRComment

        self._failing()
        self.pr_comments.append((ref, body))
        n = len(self.pr_comments)
        said = PRComment(f"IC{n}", "comment", "me", body, "2026-11-23T10:00:00Z", mine=True)
        self._on_pr(ref, lambda conversation: (*conversation, said))
        return f"https://github.com/{ref.repo}/pull/{ref.number}#comment-{n}"

    async def edit_pr_comment(self, comment_id, body):
        self._failing()
        self.pr_edits.append((comment_id, body))
        for ref in list(getattr(self.github, "details", {})):
            self._on_pr(ref, lambda said: tuple(
                replace(c, body=body) if c.id == comment_id else c for c in said
            ))  # fmt: skip
        return f"https://github.com/comment/{comment_id}"

    async def delete_comment(self, comment_id, *, on_line):
        self._failing()
        self.deleted.append((comment_id, on_line))
        if self.github is None:
            return
        for thread in getattr(self.github, "threads", []):
            thread["comments"] = [c for c in thread["comments"] if c.get("id") != comment_id]
        self.github.threads = [t for t in getattr(self.github, "threads", []) if t["comments"]]
        for ref in list(self.github.details):
            self._on_pr(ref, lambda said: tuple(c for c in said if c.id != comment_id))

    def _on_pr(self, ref, change) -> None:
        """What's said on the PR (its details at the FakeGitHub), changed."""
        from purr.models import PRDetails

        if self.github is not None:
            details = self.github.details.get(ref) or PRDetails()
            self.github.details[ref] = replace(details, conversation=change(details.conversation))

    def _failing(self):
        from purr.actions import ActionError

        if self.fail_with:
            raise ActionError(self.fail_with.pop(0))

    async def reply_to_thread(self, thread_id, body):
        from purr.actions import ActionError

        if self.fail_replies:
            raise ActionError("thread is locked")
        self.replies.append((thread_id, body))
        return f"https://github.com/reply/{len(self.replies)}"


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


class FakeSuggester:
    """Stands in for purr.suggest.ClaudeSuggester: says ``text``, keeping what it read."""

    def __init__(self, text: str = "Is the race on shutdown real?"):
        self.text = text
        self.notes: list[str] = []
        self.fail = False

    async def suggest(self, notes):
        self.notes.append(notes)
        if self.fail:
            raise RuntimeError("no idea")
        return self.text


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


class FakePicker:
    """Stands in for purr.upnext.ClaudePicker: picks the PRs in ``choose`` (numbers), or
    the first ``count`` it's offered, and keeps the notes it was given."""

    def __init__(self):
        self.notes: list[str] = []
        self.choose: list[int] | None = None
        self.fail_with: str | None = None

    async def pick(self, notes, count):
        import re

        self.notes.append(notes)
        if self.fail_with:
            raise RuntimeError(self.fail_with)
        offered = [int(n) for n in re.findall(r"^- #(\d+) ", notes, re.MULTILINE)]
        chosen = self.choose if self.choose is not None else offered[:count]
        return {"picks": [{"pr": f"#{n}", "why": f"Why #{n}"} for n in chosen]}
