"""SQLite persistence for PRs, review sessions, findings and pending actions."""

import json
import uuid
from collections.abc import AsyncIterator, Iterable, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

from purr.models import (
    PR,
    ActionKind,
    ActionStatus,
    Activity,
    Finding,
    GHState,
    PendingAction,
    PRRef,
    PRState,
    Recap,
    Repo,
    RepoSummary,
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
    """
    ALTER TABLE prs ADD COLUMN gh_state TEXT NOT NULL DEFAULT 'open'
        CHECK (gh_state IN ('open', 'closed', 'merged'));

    CREATE TABLE repos (
        name      TEXT PRIMARY KEY COLLATE NOCASE,
        added_at  TEXT NOT NULL,
        synced_at TEXT
    );

    CREATE TABLE activity (
        id     INTEGER PRIMARY KEY,
        at     TEXT NOT NULL,
        kind   TEXT NOT NULL,
        repo   TEXT,
        number INTEGER,
        data   TEXT NOT NULL DEFAULT '{}'
    );
    CREATE INDEX activity_at ON activity (at);
    CREATE INDEX activity_pr ON activity (repo, number);
    """,
    """
    CREATE TABLE meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );

    CREATE TABLE recaps (
        id         INTEGER PRIMARY KEY,
        since      TEXT NOT NULL,
        until      TEXT NOT NULL,
        headline   TEXT NOT NULL,
        items      TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    """,
    """
    ALTER TABLE reviews ADD COLUMN head_sha TEXT;
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
        gh_state=GHState(row["gh_state"]),
        assigned_to_me=bool(row["assigned_to_me"]),
        updated_at=row["updated_at"],
    )


def _repo(row: aiosqlite.Row) -> Repo:
    return Repo(name=row["name"], added_at=row["added_at"], synced_at=row["synced_at"])


def _recap(row: aiosqlite.Row) -> Recap:
    return Recap(
        id=row["id"],
        since=row["since"],
        until=row["until"],
        headline=row["headline"],
        items=json.loads(row["items"]),
        created_at=row["created_at"],
    )


def _activity(row: aiosqlite.Row) -> Activity:
    pr = PRRef(row["repo"], row["number"]) if row["repo"] is not None else None
    return Activity(
        id=row["id"], at=row["at"], kind=row["kind"], pr=pr, data=json.loads(row["data"])
    )


_UPSERT_PR = """
    INSERT INTO prs (repo, number, title, author, url, head_sha, assigned_to_me, gh_state,
                     updated_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT (repo, number) DO UPDATE SET
        title = excluded.title,
        author = excluded.author,
        url = excluded.url,
        head_sha = excluded.head_sha,
        assigned_to_me = excluded.assigned_to_me,
        gh_state = excluded.gh_state,
        updated_at = excluded.updated_at
"""


def _upsert_params(pr: PR) -> tuple:
    return (
        pr.repo,
        pr.number,
        pr.title,
        pr.author,
        pr.url,
        pr.head_sha,
        int(pr.assigned_to_me),
        pr.gh_state,
        pr.updated_at,
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
        head_sha=row["head_sha"],
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
        row = await self._fetchone(f"{_UPSERT_PR} RETURNING *", _upsert_params(pr))
        await self._db.commit()
        return _pr(row)

    async def get_pr(self, ref: PRRef) -> PR | None:
        row = await self._fetchone(
            "SELECT * FROM prs WHERE repo = ? AND number = ?", (ref.repo, ref.number)
        )
        return _pr(row) if row else None

    async def list_prs(
        self, state: PRState | None = None, *, repo: str | None = None, open_only: bool = False
    ) -> list[PR]:
        """PRs in display order: needs you, reviewing, not reviewed, done; newest first."""
        clauses, params = [], []
        if state:
            clauses.append("state = ?")
            params.append(state)
        if repo:
            clauses.append("repo = ?")
            params.append(repo)
        if open_only:
            clauses.append("gh_state = ?")
            params.append(GHState.OPEN)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
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

    async def open_prs(self) -> list[PR]:
        """PRs that are open on GitHub, in no particular order."""
        rows = await self._fetchall("SELECT * FROM prs WHERE gh_state = ?", (GHState.OPEN,))
        return [_pr(row) for row in rows]

    async def apply_sync(
        self,
        *,
        upserts: Iterable[PR] = (),
        deletes: Iterable[PRRef] = (),
        gh_states: Mapping[PRRef, GHState] | None = None,
    ) -> None:
        """Apply one poll's worth of changes to the PR table, in a single transaction."""
        try:
            await self._db.executemany(_UPSERT_PR, [_upsert_params(pr) for pr in upserts])
            await self._db.executemany(
                "DELETE FROM prs WHERE repo = ? AND number = ?",
                [(ref.repo, ref.number) for ref in deletes],
            )
            await self._db.executemany(
                "UPDATE prs SET gh_state = ? WHERE repo = ? AND number = ?",
                [(state, ref.repo, ref.number) for ref, state in (gh_states or {}).items()],
            )
        except BaseException:
            await self._db.rollback()
            raise
        await self._db.commit()

    # -- Repos -----------------------------------------------------------------

    async def add_repo(self, name: str) -> Repo:
        """Start watching ``owner/name``. Adding a watched repo again is a no-op."""
        await self._db.execute(
            "INSERT INTO repos (name, added_at) VALUES (?, ?) ON CONFLICT (name) DO NOTHING",
            (name, _now()),
        )
        await self._db.commit()
        repo = await self.get_repo(name)
        assert repo is not None
        return repo

    async def get_repo(self, name: str) -> Repo | None:
        row = await self._fetchone("SELECT * FROM repos WHERE name = ?", (name,))
        return _repo(row) if row else None

    async def list_repos(self) -> list[Repo]:
        """Watched repos, alphabetically (case-insensitive)."""
        rows = await self._fetchall("SELECT * FROM repos ORDER BY name COLLATE NOCASE")
        return [_repo(row) for row in rows]

    async def repo_summaries(self) -> list[RepoSummary]:
        """The repo list: watched repos plus any other repo with an open PR Purr tracks
        (assigned to you, or reviewed), alphabetically, with per-state counts."""
        rows = await self._fetchall(
            """
            WITH names (name, watched) AS (
                SELECT name, 1 FROM repos
                UNION
                SELECT DISTINCT repo, 0 FROM prs
                WHERE gh_state = 'open' AND repo NOT IN (SELECT name FROM repos)
            )
            SELECT
                names.name,
                names.watched,
                COALESCE(SUM(prs.state = 'needs_you'), 0) AS needs_you,
                COALESCE(SUM(prs.state = 'reviewing'), 0) AS reviewing,
                COALESCE(SUM(prs.state = 'not_reviewed'), 0) AS not_reviewed,
                (SELECT MAX(at) FROM activity WHERE activity.repo = names.name) AS last_activity
            FROM names
            LEFT JOIN prs ON prs.repo = names.name AND prs.gh_state = 'open'
            GROUP BY names.name, names.watched
            ORDER BY names.name COLLATE NOCASE
            """
        )
        return [
            RepoSummary(
                name=row["name"],
                watched=bool(row["watched"]),
                needs_you=row["needs_you"],
                reviewing=row["reviewing"],
                not_reviewed=row["not_reviewed"],
                last_activity=row["last_activity"],
            )
            for row in rows
        ]

    async def remove_repo(self, name: str) -> bool:
        """Stop watching a repo and drop its pool.

        PRs assigned to you, or that Purr has started reviewing, are kept.
        Returns whether the repo was being watched.
        """
        cursor = await self._db.execute("DELETE FROM repos WHERE name = ?", (name,))
        removed = cursor.rowcount > 0
        await self._db.execute(
            """
            DELETE FROM prs
            WHERE repo = ? COLLATE NOCASE AND state = ? AND assigned_to_me = 0
            """,
            (name, PRState.NOT_REVIEWED),
        )
        await self._db.commit()
        return removed

    async def mark_synced(self, names: Iterable[str], *, at: str | None = None) -> None:
        """Record a successful sync of these repos, as of ``at`` (default: now)."""
        at = at or _now()
        await self._db.executemany(
            "UPDATE repos SET synced_at = ? WHERE name = ?", [(at, name) for name in names]
        )
        await self._db.commit()

    # -- Activity --------------------------------------------------------------

    async def record_activity(
        self, kind: str, *, pr: PRRef | None = None, data: dict | None = None
    ) -> Activity:
        cursor = await self._db.execute(
            "INSERT INTO activity (at, kind, repo, number, data) VALUES (?, ?, ?, ?, ?)",
            (
                _now(),
                kind,
                pr.repo if pr else None,
                pr.number if pr else None,
                json.dumps(data or {}),
            ),
        )
        await self._db.commit()
        row = await self._fetchone("SELECT * FROM activity WHERE id = ?", (cursor.lastrowid,))
        return _activity(row)

    async def activity(
        self, *, since: str | None = None, pr: PRRef | None = None
    ) -> list[Activity]:
        """Activity entries, oldest first; optionally only after ``since`` or for one PR."""
        clauses, params = [], []
        if since:
            clauses.append("at > ?")
            params.append(since)
        if pr:
            clauses += ["repo = ?", "number = ?"]
            params += [pr.repo, pr.number]
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = await self._fetchall(f"SELECT * FROM activity {where} ORDER BY id", params)
        return [_activity(row) for row in rows]

    # -- Meta and recaps --------------------------------------------------------

    async def get_meta(self, key: str) -> str | None:
        row = await self._fetchone("SELECT value FROM meta WHERE key = ?", (key,))
        return row["value"] if row else None

    async def set_meta(self, key: str, value: str) -> None:
        await self._db.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?)"
            " ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await self._db.commit()

    async def add_recap(self, *, since: str, until: str, headline: str, items: list[dict]) -> Recap:
        cursor = await self._db.execute(
            "INSERT INTO recaps (since, until, headline, items, created_at) VALUES (?, ?, ?, ?, ?)",
            (since, until, headline, json.dumps(items), _now()),
        )
        await self._db.commit()
        recap = await self.get_recap(cursor.lastrowid)
        assert recap is not None
        return recap

    async def get_recap(self, recap_id: int) -> Recap | None:
        row = await self._fetchone("SELECT * FROM recaps WHERE id = ?", (recap_id,))
        return _recap(row) if row else None

    async def latest_recap(self) -> Recap | None:
        row = await self._fetchone("SELECT * FROM recaps ORDER BY id DESC LIMIT 1")
        return _recap(row) if row else None

    # -- Reviews ---------------------------------------------------------------

    _REVIEW_SELECT = """
        SELECT reviews.*, prs.repo, prs.number
        FROM reviews JOIN prs ON prs.id = reviews.pr_id
    """

    async def start_review(
        self, ref: PRRef, *, worktree_path: str | None = None, head_sha: str | None = None
    ) -> Review:
        pr_id = await self._pr_id(ref)
        cursor = await self._db.execute(
            "INSERT INTO reviews (pr_id, worktree_path, head_sha, status, started_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (pr_id, worktree_path, head_sha, ReviewStatus.RUNNING, _now()),
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
        self,
        review_id: int,
        *,
        summary: str,
        findings: Iterable[Finding],
        head_sha: str | None = None,
    ) -> Review:
        """Record a review's result; ``head_sha`` is the commit it looked at, if it moved."""
        await self._db.execute(
            "UPDATE reviews SET status = ?, summary = ?, finished_at = ?,"
            " head_sha = COALESCE(?, head_sha) WHERE id = ?",
            (ReviewStatus.FINISHED, summary, _now(), head_sha, review_id),
        )
        # A follow-up turn can submit the review again; the latest one wins.
        await self._db.execute("DELETE FROM findings WHERE review_id = ?", (review_id,))
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
