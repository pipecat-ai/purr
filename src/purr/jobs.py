"""Jobs, typed: a request and its reply are dataclasses, and errors keep their class.

A job is a :class:`Request` for the worker that handles it (its ``worker``), which
answers with the request's reply type, or with a :class:`PurrError`. The plumbing
lives here, once:

- a worker handles a request with a method decorated with :func:`handles`, which
  takes the request and returns the reply;
- :func:`request` sends one from any worker (the TUI, the lead, a test) and returns
  the reply, or raises the error it came back with, as the same class.

On the bus they're plain JSON (pydantic encodes the dataclasses), so they could
cross a network bus if the components ever move to separate processes.
"""

import functools
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, ClassVar, Generic, TypeVar, get_args, get_origin

from loguru import logger
from pipecat.bus import BusJobRequestMessage
from pipecat.pipeline.job_context import JobError, JobGroupError, JobParams
from pipecat.pipeline.job_decorator import job
from pipecat.workers.base_worker import BaseWorker
from pydantic import TypeAdapter, ValidationError


class PurrError(Exception):
    """A request that couldn't be done. The message says why, for the user."""

    kinds: ClassVar[dict[str, type["PurrError"]]] = {}

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        PurrError.kinds[cls.__name__] = cls  # so it comes back as itself


class Invalid(PurrError):
    """The request doesn't make sense: a repo name, a depth, an option."""


class NotFound(PurrError):
    """What it's about isn't there (any more): a PR, a repo, an action, a question."""


class Busy(PurrError):
    """Not now: the agent's working, a review's already running, CI hasn't passed."""


class Failed(PurrError):
    """Tried, and it didn't work: GitHub, git, the agent."""


class Unanswered(PurrError):
    """The worker didn't answer in time."""


@dataclass
class Done:
    """The reply to a request that has nothing to say but that it's done."""


R = TypeVar("R")  # what a request is answered with


class Request(Generic[R]):
    """A job for ``worker``, answered with ``R``.

    Subclasses are dataclasses naming their reply: ``class SendMessage(Request[MessageSent])``.
    """

    worker: ClassVar[str]
    timeout: ClassVar[float] = 60  # seconds, from asking to the reply
    reply: ClassVar[type]

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        for base in getattr(cls, "__orig_bases__", ()):
            if get_origin(base) is Request:
                (cls.reply,) = get_args(base)


@functools.cache
def _adapter(kind: type) -> TypeAdapter:
    return TypeAdapter(kind)


def _error(error: PurrError) -> dict:
    return {"error": {"kind": type(error).__name__, "message": str(error)}}


def handles(kind: type[Request[R]]) -> Callable:
    """Make ``async def method(self, request) -> reply`` a worker's handler of ``kind``.

    A :class:`PurrError` it raises goes back as itself; anything else is logged and
    goes back as a plain :class:`PurrError`, so the asker hears about it at once.
    """

    def decorate(method: Callable[[Any, Any], Awaitable[R]]) -> Callable:
        @job(name=kind.__name__)
        @functools.wraps(method)
        async def handler(self: BaseWorker, message: BusJobRequestMessage) -> None:
            response = await _response(self, kind, method, message.payload)
            await self.send_job_response(message.job_id, response)

        return handler

    return decorate


async def _response(
    worker: BaseWorker,
    kind: type[Request[R]],
    method: Callable[[Any, Any], Awaitable[R]],
    payload: dict | None,
) -> dict:
    """What ``worker`` answers a request of ``kind`` with: ``method``'s reply, or the error."""
    try:
        request = _adapter(kind).validate_python((payload or {}).get("request"))
    except ValidationError as e:
        return _error(Invalid(f"Not a {kind.__name__}: {e}"))
    try:
        reply = await method(worker, request)
        return {"reply": _adapter(kind.reply).dump_python(reply, mode="json")}
    except PurrError as e:
        return _error(e)
    except Exception as e:
        logger.exception(f"{worker} couldn't do {request}")
        return _error(PurrError(str(e) or type(e).__name__))


async def request(sender: BaseWorker, req: Request[R], *, timeout: float | None = None) -> R:
    """Send ``req`` from ``sender``, and wait for its reply.

    Raises the :class:`PurrError` it came back with, or :class:`Unanswered`.
    """
    kind = type(req)
    params = JobParams(
        name=kind.__name__,
        payload={"request": _adapter(kind).dump_python(req, mode="json")},
        timeout=timeout or kind.timeout,
    )
    try:
        async with sender.job(kind.worker, params=params) as context:
            pass
    except (JobError, JobGroupError) as e:  # gone, not ready, or too slow
        raise Unanswered(f"No answer from Purr's {kind.worker} ({e})") from e
    response = context.response or {}
    if error := response.get("error"):
        raise PurrError.kinds.get(error.get("kind"), PurrError)(error.get("message", ""))
    return _adapter(kind.reply).validate_python(response.get("reply"))
