"""Executing approved actions on GitHub.

This is the only place Purr writes to GitHub, and it only runs after the user
approved a pending action (or, for assignment, asked for a review). Writes go
through ``gh``, as the user.
"""

import asyncio
import json
from typing import Protocol

from loguru import logger

from purr.models import ActionKind, PendingAction, PRRef

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


def _folded(body: str, comments: list[dict]) -> str:
    """Inline comments written into the review body, for when GitHub won't place them."""
    notes = "\n".join(f"- `{c['path']}:{c['line']}`: {c['body']}" for c in comments)
    return f"{body.strip()}\n\n{notes}" if body.strip() else notes


async def execute(writer: GitHubWriter, action: PendingAction, *, head_sha: str) -> str:
    """Post an approved action. Returns the URL of what was posted."""
    event = _REVIEW_EVENTS.get(action.kind)
    if event is None:
        raise ActionError(f"Purr can't {action.kind.value.replace('_', ' ')} yet")
    body = str(action.payload.get("body") or "")
    comments = [
        {"path": c["path"], "line": int(c["line"]), "side": "RIGHT", "body": c["body"]}
        for c in action.payload.get("comments") or []
        if c.get("path") and c.get("line") and c.get("body")
    ]
    if not body and not comments and event != "APPROVE":
        body = action.description  # GitHub wants some text for these
    try:
        return await writer.submit_review(
            action.pr, event=event, body=body, comments=comments, commit_id=head_sha
        )
    except ActionError as e:
        if not comments or "line" not in str(e).lower():
            raise
        # A comment pointed at a line outside the diff; post them in the body instead.
        logger.warning(f"Inline comments on {action.pr} rejected ({e}); folding them into the body")
        return await writer.submit_review(
            action.pr, event=event, body=_folded(body, comments), comments=[], commit_id=head_sha
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

    async def _api(self, path: str, request: dict) -> dict:
        process = await asyncio.create_subprocess_exec(
            self._gh,
            "api",
            "--method",
            "POST",
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
