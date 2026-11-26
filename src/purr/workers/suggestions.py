"""The suggestions worker: what you might ask next in a PR's session (see purr.suggest)."""

import asyncio
from pathlib import Path

from loguru import logger
from pipecat.workers.base_worker import BaseWorker

from purr.jobs import handles
from purr.messages import SUGGEST_WORKER, Suggestion, SuggestPrompt
from purr.review.agent import ReviewAgent
from purr.store import Store
from purr.suggest import Suggester, suggestion_notes


class SuggestionWorker(BaseWorker):
    """Handles ``SuggestPrompt {pr}``, from the PR's session as it's saved.

    Args:
        suggester: Writes the suggestion; None: suggestions are off.
    """

    def __init__(
        self,
        name: str = SUGGEST_WORKER,
        *,
        store: Store,
        agent: ReviewAgent,
        suggester: Suggester | None,
    ):
        super().__init__(name)
        self._store = store
        self._agent = agent
        self._suggester = suggester

    @handles(SuggestPrompt)
    async def suggest(self, request: SuggestPrompt) -> Suggestion:
        # Off: no need to read the conversation.
        if not self._suggester or not (notes := await self._notes(request)):
            return Suggestion(text="")
        try:
            return Suggestion(text=await self._suggester.suggest(notes))
        except Exception as e:
            logger.warning(f"{request.pr}: no suggestion ({e})")
            return Suggestion(text="")

    async def _notes(self, request: SuggestPrompt) -> str | None:
        """What the suggester reads; None if there's no conversation to go on."""
        pr = await self._store.get_pr(request.pr)
        review = await self._store.latest_review(request.pr)
        if not pr or not review or not review.session_id or not review.worktree_path:
            return None
        history = self._agent.history
        entries = await asyncio.to_thread(history, review.session_id, Path(review.worktree_path))
        pending = await self._store.pending_actions(request.pr)
        return suggestion_notes(pr, entries, pending) if entries else None
