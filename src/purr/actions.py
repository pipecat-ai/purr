"""Executing approved actions on GitHub.

This is the only place Purr writes to GitHub, and it only runs after the user
approved a pending action (or, for assignment, asked for a review; for a merge,
pressed ``m`` and confirmed). Writes go through ``gh``, as the user.

What Purr posts says so: the review's text and each reply in a thread end with
a small signature ("Sent from Purr.", linking to Purr, by default). Inline
comments don't; they belong to the review, whose text already says it. An
approval with nothing else to say is "LGTM".
"""

import asyncio
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

from loguru import logger

from purr.models import ActionKind, DraftComment, PendingAction, PRRef

_REVIEW_EVENTS = {
    ActionKind.APPROVE: "APPROVE",
    ActionKind.REQUEST_CHANGES: "REQUEST_CHANGES",
    ActionKind.COMMENT: "COMMENT",
}


class ActionError(Exception):
    """GitHub refused or couldn't be reached."""


class GitHubWriter(Protocol):
    async def submit_review(
        self, ref: PRRef, *, event: str, body: str, comments: list[dict], commit_id: str
    ) -> str: ...

    async def assign_self(self, ref: PRRef) -> None: ...

    async def reply_to_thread(self, thread_id: str, body: str) -> str: ...

    async def edit_review_comment(self, comment_id: str, body: str) -> str: ...

    async def merge(self, ref: PRRef, *, method: str, sha: str) -> None: ...


@dataclass
class Posted:
    """What posting an action did. ``warnings`` lists parts that didn't go through."""

    url: str
    warnings: list[str] = field(default_factory=list)


def signed(text: str, signature: str) -> str:
    """``text`` with Purr's signature at the end, in small print (none: as it is)."""
    if not signature:
        return text
    mark = f"<sub>{signature}</sub>"
    return f"{text.rstrip()}\n\n{mark}" if text.strip() else mark


def unsigned(text: str, signature: str) -> str:
    """``text`` without Purr's signature at its end, if it has it (to edit it again)."""
    mark = f"<sub>{signature}</sub>"
    return text.removesuffix(mark).rstrip() if signature and text.endswith(mark) else text


def _folded(body: str, comments: list[dict]) -> str:
    """Inline comments written into the review body, for when GitHub won't place them."""
    notes = "\n".join(f"- `{c['path']}:{c['line']}`: {c['body']}" for c in comments)
    return f"{body.strip()}\n\n{notes}" if body.strip() else notes


async def execute(
    writer: GitHubWriter,
    action: PendingAction,
    *,
    head_sha: str,
    signature: str = "",
    mine: Sequence[DraftComment] = (),
) -> Posted:
    """Post an approved action: the review, with your inline comments (``mine``) next to
    the agent's, then any replies in existing threads.

    The review's text and each reply end with ``signature`` (none: unsigned).
    Raises ActionError if the review itself can't be posted. A reply that fails
    after the review went up is a warning instead, so retrying can't post the
    review twice.
    """
    event = _REVIEW_EVENTS.get(action.kind)
    if event is None:
        raise ActionError(f"Purr can't {action.kind.value.replace('_', ' ')} yet")
    replies = [
        r for r in action.payload.get("replies") or [] if r.get("thread_id") and r.get("body")
    ]
    url = await _review(
        writer, action, event, head_sha, mine, only_replies=bool(replies), signature=signature
    )
    warnings = []
    for reply in replies:
        try:
            body = signed(reply["body"], signature)
            reply_url = await writer.reply_to_thread(reply["thread_id"], body)
            url = url or reply_url
        except ActionError as e:
            warnings.append(f"A reply in a review thread didn't go through: {e}")
    return Posted(url=url, warnings=warnings)


async def post_comments(
    writer: GitHubWriter, ref: PRRef, mine: Sequence[DraftComment], *, signature: str = ""
) -> str:
    """Your inline comments as a review of their own (a comment), on the commit they're on
    (all the same one). Raises ActionError if GitHub doesn't take it."""
    return await writer.submit_review(
        ref,
        event="COMMENT",
        body=signed("", signature),
        comments=[comment.for_github() for comment in mine],
        commit_id=mine[0].head_sha,
    )


async def _review(
    writer: GitHubWriter,
    action: PendingAction,
    event: str,
    head_sha: str,
    mine: Sequence[DraftComment],
    *,
    only_replies: bool,
    signature: str,
) -> str:
    body = str(action.payload.get("body") or "")
    comments = [
        {"path": c["path"], "line": int(c["line"]), "side": "RIGHT", "body": c["body"]}
        for c in action.payload.get("comments") or []
        if c.get("path") and c.get("line") and c.get("body")
    ] + [comment.for_github() for comment in mine]
    if not body and not comments and event == "COMMENT" and only_replies:
        return ""  # just replies, no review to submit
    if not body and not comments:
        # GitHub wants some text for these; an approval with nothing else to say: LGTM.
        body = "LGTM" if event == "APPROVE" else action.description
    try:
        return await writer.submit_review(
            action.pr,
            event=event,
            body=signed(body, signature),
            comments=comments,
            commit_id=head_sha,
        )
    except ActionError as e:
        if not comments or "line" not in str(e).lower():
            raise
        # A comment pointed at a line outside the diff; post them in the body instead.
        logger.warning(f"Inline comments on {action.pr} rejected ({e}); folding them into the body")
        return await writer.submit_review(
            action.pr,
            event=event,
            body=signed(_folded(body, comments), signature),
            comments=[],
            commit_id=head_sha,
        )


_REPLY = (
    "mutation($id: ID!, $body: String!) {"
    " addPullRequestReviewThreadReply(input: {pullRequestReviewThreadId: $id, body: $body})"
    " { comment { url } } }"
)
_EDIT = (
    "mutation($id: ID!, $body: String!) {"
    " updatePullRequestReviewComment(input: {pullRequestReviewCommentId: $id, body: $body})"
    " { pullRequestReviewComment { url } } }"
)


class GhWriter:
    """Writes to GitHub with the GitHub CLI.

    Args:
        gh: The GitHub CLI executable.
    """

    def __init__(self, gh: str = "gh"):
        self._gh = gh

    async def submit_review(
        self, ref: PRRef, *, event: str, body: str, comments: list[dict], commit_id: str
    ) -> str:
        request = {"event": event, "body": body, "comments": comments, "commit_id": commit_id}
        result = await self._api(f"repos/{ref.repo}/pulls/{ref.number}/reviews", request)
        return result.get("html_url", "")

    async def assign_self(self, ref: PRRef) -> None:
        """Assign the current user, so teammates see the PR is taken."""
        process = await asyncio.create_subprocess_exec(
            self._gh,
            "pr",
            "edit",
            str(ref.number),
            "--repo",
            ref.repo,
            "--add-assignee",
            "@me",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()
        if process.returncode != 0:
            raise ActionError(stderr.decode().strip() or f"gh exited with {process.returncode}")

    async def reply_to_thread(self, thread_id: str, body: str) -> str:
        data = await self._mutation(_REPLY, id=thread_id, body=body)
        return ((data.get("addPullRequestReviewThreadReply") or {}).get("comment") or {}).get(
            "url", ""
        )

    async def edit_review_comment(self, comment_id: str, body: str) -> str:
        """New words for an inline comment of yours already on GitHub (its node id)."""
        data = await self._mutation(_EDIT, id=comment_id, body=body)
        edited = data.get("updatePullRequestReviewComment") or {}
        return (edited.get("pullRequestReviewComment") or {}).get("url", "")

    async def _mutation(self, query: str, **variables: str) -> dict:
        """A GraphQL mutation through gh (-f: values as they are, never read from files)."""
        args = [self._gh, "api", "graphql", "-f", f"query={query}"]
        for name, value in variables.items():
            args += ["-f", f"{name}={value}"]
        process = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            raise ActionError(stderr.decode().strip() or f"gh exited with {process.returncode}")
        return json.loads(stdout or "{}").get("data") or {}

    async def merge(self, ref: PRRef, *, method: str, sha: str) -> None:
        """Merge the PR (``method``: merge, squash or rebase), only if its head is ``sha``."""
        request = {"merge_method": method, "sha": sha}
        await self._api(f"repos/{ref.repo}/pulls/{ref.number}/merge", request, http="PUT")

    async def _api(self, path: str, request: dict, *, http: str = "POST") -> dict:
        process = await asyncio.create_subprocess_exec(
            self._gh,
            "api",
            "--method",
            http,
            path,
            "--input",
            "-",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate(json.dumps(request).encode())
        if process.returncode != 0:
            detail = stderr.decode().strip()
            try:
                body = json.loads(stdout)
                errors = "; ".join(str(e) for e in body.get("errors", []))
                detail = f"{body.get('message', detail)} {errors}".strip()
            except (json.JSONDecodeError, AttributeError):
                pass
            raise ActionError(detail or f"gh exited with {process.returncode}")
        return json.loads(stdout or "{}")
