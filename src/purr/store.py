"""SQLite persistence for PRs, review sessions, findings and pending actions."""

import json
import uuid
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

from purr.models import (
    PR,
    ActionKind,
    ActionStatus,
    Finding,
    PendingAction,
    PRRef,
    PRState,
    Review,
    ReviewStatus,
    Severity,
)

# Each entry upgrades the schema by one version (tracked in PRAGMA user_version).
# Append new migrations; never edit one that has shipped.
MIGRATIONS: list[str] = [
    """
    CREATE TABLE prs (
        id             INTEGER PRIMARY KEY,
        repo           TEXT NOT NULL,
        number         INTEGER NOT NULL,
        title          TEXT NOT NULL,
        author         TEXT NOT NULL,
        url            TEXT NOT NULL,
        head_sha       TEXT NOT NULL,
        state          TEXT NOT NULL DEFAULT 'not_reviewed'
                       CHECK (state IN ('not_reviewed', 'reviewing', 'needs_you', 'done')),
        assigned_to_me INTEGER NOT NULL DEFAULT 0,
        updated_at     TEXT NOT NULL,
        UNIQUE (repo, number)
    );

    CREATE TABLE reviews (
        id            INTEGER PRIMARY KEY,
        pr_id         INTEGER NOT NULL REFERENCES prs (id) ON DELETE CASCADE,
        session_id    TEXT,
        worktree_path TEXT,
        status        TEXT NOT NULL CHECK (status IN ('running', 'finished', 'failed')),
        summary       TEXT,
        error         TEXT,
        started_at    TEXT NOT NULL,
        finished_at   TEXT
    );
    CREATE INDEX reviews_pr ON reviews (pr_id);

    CREATE TABLE findings (
        id        INTEGER PRIMARY KEY,
        review_id INTEGER NOT NULL REFERENCES reviews (id) ON DELETE CASCADE,
        file      TEXT,
        line      INTEGER,
        severity  TEXT NOT NULL CHECK (severity IN ('issue', 'suggestion', 'nit')),
        text      TEXT NOT NULL
    );
    CREATE INDEX findings_review ON findings (review_id);

    CREATE TABLE pending_actions (
        id          TEXT PRIMARY KEY,
        pr_id       INTEGER NOT NULL REFERENCES prs (id) ON DELETE CASCADE,
        kind        TEXT NOT NULL
                    CHECK (kind IN ('comment', 'request_changes', 'approve', 'merge')),
        description TEXT NOT NULL,
        payload     TEXT NOT NULL,
        status      TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'approved', 'dismissed', 'executed', 'failed')),
        created_at  TEXT NOT NULL,
        resolved_at TEXT
    );
    CREATE INDEX pending_actions_pr ON pending_actions (pr_id);
    """,
]

# "Needs you" first, then in-flight reviews, then the pool, then done.
_STATE_ORDER = """
    CASE prs.state
        WHEN 'needs_you' THEN 0
        WHEN 'reviewing' THEN 1
        WHEN 'not_reviewed' THEN 2
        ELSE 3
    END
"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _pr(row: aiosqlite.Row) -> PR:
    return PR(
        id=row["id"],
        repo=row["repo"],
        number=row["number"],
        title=row["title"],
        author=row["author"],
        url=row["url"],
        head_sha=row["head_sha"],
        state=PRState(row["state"]),
        assigned_to_me=bool(row["assigned_to_me"]),
        updated_at=row["updated_at"],
    )


def _review(row: aiosqlite.Row) -> Review:
    return Review(
        id=row["id"],
        pr=PRRef(row["repo"], row["number"]),
        status=ReviewStatus(row["status"]),
        session_id=row["session_id"],
        worktree_path=row["worktree_path"],
        summary=row["summary"],
        error=row["error"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
    )


def _finding(row: aiosqlite.Row) -> Finding:
    return Finding(
        id=row["id"],
        severity=Severity(row["severity"]),
        text=row["text"],
        file=row["file"],
        line=row["line"],
    )


def _action(row: aiosqlite.Row) -> PendingAction:
    return PendingAction(
        id=row["id"],
        pr=PRRef(row["repo"], row["number"]),
        kind=ActionKind(row["kind"]),
        description=row["description"],
        payload=json.loads(row["payload"]),
        status=ActionStatus(row["status"]),
        created_at=row["created_at"],
        resolved_at=row["resolved_at"],
    )


class Store:
    """Async access to Purr's SQLite database.

    One connection per process; aiosqlite serializes access on its own thread,
    so every worker can share the same ``Store``.
    """

    def __init__(self, db: aiosqlite.Connection):
        self._db = db

    @classmethod
    async def open(cls, path: Path | str) -> "Store":
        """Open (creating if needed) the database at ``path`` and migrate it."""
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        db = await aiosqlite.connect(path)
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON")
        await db.execute("PRAGMA journal_mode = WAL")
        store = cls(db)
        await store._migrate()
        return store

    @classmethod
    @asynccontextmanager
    async def connect(cls, path: Path | str) -> AsyncIterator["Store"]:
        store = await cls.open(path)
        try:
            yield store
        finally:
            await store.close()

    async def close(self) -> None:
        await self._db.close()

    async def schema_version(self) -> int:
        async with self._db.execute("PRAGMA user_version") as cursor:
            (version,) = await cursor.fetchone()
        return version

    async def _migrate(self) -> None:
        version = await self.schema_version()
        for target, script in enumerate(MIGRATIONS[version:], start=version + 1):
            # One transaction per migration, version bump included.
            await self._db.executescript(
                f"BEGIN;\n{script}\nPRAGMA user_version = {target};\nCOMMIT;"
            )

    async def _fetchone(self, sql: str, params: Iterable = ()) -> aiosqlite.Row | None:
        async with self._db.execute(sql, tuple(params)) as cursor:
            return await cursor.fetchone()

    async def _fetchall(self, sql: str, params: Iterable = ()) -> list[aiosqlite.Row]:
        async with self._db.execute(sql, tuple(params)) as cursor:
            return list(await cursor.fetchall())

    async def _pr_id(self, ref: PRRef) -> int:
        row = await self._fetchone(
            "SELECT id FROM prs WHERE repo = ? AND number = ?", (ref.repo, ref.number)
        )
        if row is None:
            raise KeyError(f"unknown PR {ref}")
        return row["id"]

    # -- PRs -------------------------------------------------------------------

    async def upsert_pr(self, pr: PR) -> PR:
        """Insert or refresh a PR's GitHub metadata. Never changes its review state."""
        row = await self._fetchone(
            """
            INSERT INTO prs (repo, number, title, author, url, head_sha, assigned_to_me,
                             updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (repo, number) DO UPDATE SET
                title = excluded.title,
                author = excluded.author,
                url = excluded.url,
                head_sha = excluded.head_sha,
                assigned_to_me = excluded.assigned_to_me,
                updated_at = excluded.updated_at
            RETURNING *
            """,
            (
                pr.repo,
                pr.number,
                pr.title,
                pr.author,
                pr.url,
                pr.head_sha,
                int(pr.assigned_to_me),
                pr.updated_at,
            ),
        )
        await self._db.commit()
        return _pr(row)

    async def get_pr(self, ref: PRRef) -> PR | None:
        row = await self._fetchone(
            "SELECT * FROM prs WHERE repo = ? AND number = ?", (ref.repo, ref.number)
        )
        return _pr(row) if row else None

    async def list_prs(self, state: PRState | None = None) -> list[PR]:
        """PRs in display order: needs you, reviewing, not reviewed, done; newest first."""
        where, params = ("WHERE state = ?", (state,)) if state else ("", ())
        rows = await self._fetchall(
            f"SELECT * FROM prs {where} ORDER BY {_STATE_ORDER}, updated_at DESC", params
        )
        return [_pr(row) for row in rows]

    async def set_pr_state(self, ref: PRRef, state: PRState) -> None:
        cursor = await self._db.execute(
            "UPDATE prs SET state = ? WHERE repo = ? AND number = ?",
            (state, ref.repo, ref.number),
        )
        await self._db.commit()
        if cursor.rowcount == 0:
            raise KeyError(f"unknown PR {ref}")

    # -- Reviews ---------------------------------------------------------------

    _REVIEW_SELECT = """
        SELECT reviews.*, prs.repo, prs.number
        FROM reviews JOIN prs ON prs.id = reviews.pr_id
    """

    async def start_review(self, ref: PRRef, *, worktree_path: str | None = None) -> Review:
        pr_id = await self._pr_id(ref)
        cursor = await self._db.execute(
            "INSERT INTO reviews (pr_id, worktree_path, status, started_at) VALUES (?, ?, ?, ?)",
            (pr_id, worktree_path, ReviewStatus.RUNNING, _now()),
        )
        await self._db.commit()
        return await self._get_review(cursor.lastrowid)

    async def _get_review(self, review_id: int) -> Review:
        row = await self._fetchone(f"{self._REVIEW_SELECT} WHERE reviews.id = ?", (review_id,))
        if row is None:
            raise KeyError(f"unknown review {review_id}")
        return _review(row)

    async def set_review_session(self, review_id: int, session_id: str) -> None:
        """Remember the Agent SDK session so the review can be resumed later."""
        await self._db.execute(
            "UPDATE reviews SET session_id = ? WHERE id = ?", (session_id, review_id)
        )
        await self._db.commit()

    async def finish_review(
        self, review_id: int, *, summary: str, findings: Iterable[Finding]
    ) -> Review:
        await self._db.execute(
            "UPDATE reviews SET status = ?, summary = ?, finished_at = ? WHERE id = ?",
            (ReviewStatus.FINISHED, summary, _now(), review_id),
        )
        await self._db.executemany(
            "INSERT INTO findings (review_id, file, line, severity, text) VALUES (?, ?, ?, ?, ?)",
            [(review_id, f.file, f.line, f.severity, f.text) for f in findings],
        )
        await self._db.commit()
        return await self._get_review(review_id)

    async def fail_review(self, review_id: int, *, error: str) -> Review:
        await self._db.execute(
            "UPDATE reviews SET status = ?, error = ?, finished_at = ? WHERE id = ?",
            (ReviewStatus.FAILED, error, _now(), review_id),
        )
        await self._db.commit()
        return await self._get_review(review_id)

    async def latest_review(self, ref: PRRef) -> Review | None:
        row = await self._fetchone(
            f"""{self._REVIEW_SELECT}
            WHERE prs.repo = ? AND prs.number = ?
            ORDER BY reviews.id DESC LIMIT 1""",
            (ref.repo, ref.number),
        )
        return _review(row) if row else None

    async def interrupted_reviews(self) -> list[Review]:
        """Reviews left running by the previous process, to resume on startup."""
        rows = await self._fetchall(
            f"{self._REVIEW_SELECT} WHERE reviews.status = ? ORDER BY reviews.id",
            (ReviewStatus.RUNNING,),
        )
        return [_review(row) for row in rows]

    async def findings(self, review_id: int) -> list[Finding]:
        rows = await self._fetchall(
            "SELECT * FROM findings WHERE review_id = ? ORDER BY id", (review_id,)
        )
        return [_finding(row) for row in rows]

    # -- Pending actions -------------------------------------------------------

    _ACTION_SELECT = """
        SELECT pending_actions.*, prs.repo, prs.number
        FROM pending_actions JOIN prs ON prs.id = pending_actions.pr_id
    """

    async def add_pending_action(
        self, ref: PRRef, *, kind: ActionKind, description: str, payload: dict | None = None
    ) -> PendingAction:
        pr_id = await self._pr_id(ref)
        action_id = uuid.uuid4().hex
        await self._db.execute(
            """
            INSERT INTO pending_actions (id, pr_id, kind, description, payload, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (action_id, pr_id, kind, description, json.dumps(payload or {}), _now()),
        )
        await self._db.commit()
        action = await self.get_pending_action(action_id)
        assert action is not None
        return action

    async def get_pending_action(self, action_id: str) -> PendingAction | None:
        row = await self._fetchone(
            f"{self._ACTION_SELECT} WHERE pending_actions.id = ?", (action_id,)
        )
        return _action(row) if row else None

    async def pending_actions(self, ref: PRRef | None = None) -> list[PendingAction]:
        """Actions still awaiting a decision, oldest first."""
        sql = f"{self._ACTION_SELECT} WHERE pending_actions.status = ?"
        params: tuple = (ActionStatus.PENDING,)
        if ref:
            sql += " AND prs.repo = ? AND prs.number = ?"
            params += (ref.repo, ref.number)
        rows = await self._fetchall(f"{sql} ORDER BY pending_actions.created_at", params)
        return [_action(row) for row in rows]

    async def transition_action(
        self, action_id: str, *, expected: ActionStatus, new: ActionStatus
    ) -> PendingAction | None:
        """Move an action from ``expected`` to ``new`` status, atomically.

        Returns the updated action, or None if it wasn't in ``expected`` status.
        This is what stops one action being approved twice (say, from the TUI
        and by voice at the same time).
        """
        cursor = await self._db.execute(
            "UPDATE pending_actions SET status = ?, resolved_at = ? WHERE id = ? AND status = ?",
            (new, _now(), action_id, expected),
        )
        await self._db.commit()
        if cursor.rowcount == 0:
            return None
        return await self.get_pending_action(action_id)
