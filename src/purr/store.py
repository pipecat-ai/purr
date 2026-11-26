"""SQLite persistence for PRs, review sessions, findings and pending actions."""

import json
import uuid
from collections.abc import AsyncIterator, Iterable, Mapping
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

from purr.models import (
    PR,
    ActionKind,
    ActionStatus,
    Activity,
    DraftComment,
    Finding,
    GHState,
    Outcome,
    PendingAction,
    PRRef,
    PRState,
    Reply,
    Repo,
    RepoSummary,
    Review,
    ReviewPlan,
    ReviewStatus,
    Severity,
    UpNext,
    Verdict,
)
from purr.news import NEWS_KINDS, news

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
    """
    ALTER TABLE prs ADD COLUMN created_at TEXT NOT NULL DEFAULT '';
    ALTER TABLE prs ADD COLUMN assignees TEXT NOT NULL DEFAULT '[]';
    """,
    # What you asked PR agents, for the prompt's history. Kept by repo and
    # number, not PR id, so it outlives the PR's row.
    """
    CREATE TABLE prompts (
        id INTEGER PRIMARY KEY,
        at TEXT NOT NULL,
        repo TEXT NOT NULL,
        number INTEGER NOT NULL,
        text TEXT NOT NULL
    );
    CREATE INDEX prompts_by_pr ON prompts (repo, number);
    """,
    """
    ALTER TABLE prs ADD COLUMN base TEXT NOT NULL DEFAULT '';
    """,
    # Every PR conversation as it happened, for analysis: each agent run (a
    # review, a re-review, a resume, one of your messages) and every message in
    # it. Claude Code's session files stay what sessions resume from. Kept by
    # repo and number, without foreign keys, like the activity log.
    """
    CREATE TABLE runs (
        id INTEGER PRIMARY KEY,
        review_id INTEGER,
        session_id TEXT,
        repo TEXT NOT NULL,
        number INTEGER NOT NULL,
        kind TEXT NOT NULL,
        queued_at TEXT NOT NULL,
        started_at TEXT,
        finished_at TEXT,
        status TEXT NOT NULL,
        error TEXT,
        model TEXT,
        cost_usd REAL,
        turns INTEGER,
        duration_ms INTEGER,
        api_duration_ms INTEGER,
        input_tokens INTEGER,
        output_tokens INTEGER,
        cache_read_tokens INTEGER,
        cache_write_tokens INTEGER
    );
    CREATE INDEX runs_by_pr ON runs (repo, number);
    CREATE TABLE messages (
        id INTEGER PRIMARY KEY,
        run_id INTEGER NOT NULL,
        at TEXT NOT NULL,
        repo TEXT NOT NULL,
        number INTEGER NOT NULL,
        kind TEXT NOT NULL,
        author TEXT NOT NULL,
        data TEXT NOT NULL
    );
    CREATE INDEX messages_by_pr ON messages (repo, number);
    CREATE INDEX messages_by_run ON messages (run_id);
    """,
    # When you last looked at each PR, so what's new since survives restarts.
    # Everything counts as seen as of the upgrade, not as years of news.
    """
    ALTER TABLE prs ADD COLUMN seen_at TEXT;
    UPDATE prs SET seen_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now');
    """,
    # Which of a repo's own skills its reviews follow (see purr.review.skills),
    # and which one each review followed.
    """
    CREATE TABLE review_skills (
        repo      TEXT PRIMARY KEY COLLATE NOCASE,
        skill     TEXT NOT NULL,  -- '' for Purr's own review
        chosen_by TEXT NOT NULL,  -- 'purr' (clearly the one) or 'you'
        chosen_at TEXT NOT NULL
    );
    ALTER TABLE reviews ADD COLUMN skill TEXT;
    """,
    # What Purr's lead planned for each review (see purr.models.ReviewPlan), as JSON.
    """
    ALTER TABLE reviews ADD COLUMN plan TEXT;
    """,
    # Who wrote the PR, relative to the repo (standing permissions only apply to
    # its owners, members and collaborators).
    """
    ALTER TABLE prs ADD COLUMN author_association TEXT NOT NULL DEFAULT '';
    """,
    # The model you chose for a PR's conversation (/model); NULL: Purr's choice.
    """
    ALTER TABLE prs ADD COLUMN model TEXT;
    """,
    # When a PR got to its state (the list shows the latest done ones). Until
    # now: its latest review's.
    """
    ALTER TABLE prs ADD COLUMN state_at TEXT;
    UPDATE prs SET state_at = (
        SELECT MAX(COALESCE(finished_at, started_at)) FROM reviews WHERE pr_id = prs.id
    );
    """,
    # Your review asked for by name, and what others said to you since your last
    # word (see purr.github.replies_to): JSON, with when you last said they need
    # no answer (x).
    """
    ALTER TABLE prs ADD COLUMN requested_from_me INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE prs ADD COLUMN replies TEXT NOT NULL DEFAULT '[]';
    ALTER TABLE prs ADD COLUMN replies_done_at TEXT;
    """,
    # PRs by others you've commented on or reviewed on GitHub: for replies to you.
    """
    ALTER TABLE prs ADD COLUMN involved INTEGER NOT NULL DEFAULT 0;
    """,
    # The PR's head when an action was posted: an approval is of that commit (m
    # merges only that).
    """
    ALTER TABLE pending_actions ADD COLUMN posted_head TEXT;
    """,
    # Inline comments you wrote in Purr, until they're posted with your review.
    """
    CREATE TABLE draft_comments (
        id          INTEGER PRIMARY KEY,
        pr_id       INTEGER NOT NULL REFERENCES prs (id) ON DELETE CASCADE,
        path        TEXT NOT NULL,
        line        INTEGER NOT NULL,
        side        TEXT NOT NULL CHECK (side IN ('LEFT', 'RIGHT')),
        body        TEXT NOT NULL,
        head_sha    TEXT NOT NULL,
        created_at  TEXT NOT NULL
    );
    CREATE INDEX draft_comments_pr ON draft_comments (pr_id);
    """,
    # A comment of yours on several lines: the first (line is the last).
    """
    ALTER TABLE draft_comments ADD COLUMN start_line INTEGER;
    ALTER TABLE draft_comments ADD COLUMN start_side TEXT;
    """,
    # PRs you opened: what you're working on.
    """
    ALTER TABLE prs ADD COLUMN authored INTEGER NOT NULL DEFAULT 0;
    """,
    # What's up next in each repo: the PRs in its pool worth looking at next (see
    # purr.upnext), best first, and why. Replaced as a whole each time.
    """
    CREATE TABLE up_next (
        repo      TEXT NOT NULL COLLATE NOCASE,
        number    INTEGER NOT NULL,
        rank      INTEGER NOT NULL,
        why       TEXT NOT NULL,
        by        TEXT NOT NULL,  -- 'lead', or 'purr' when the lead couldn't say
        picked_at TEXT NOT NULL,
        PRIMARY KEY (repo, number)
    );
    """,
    # The PRs you said weren't for you when they were up next (s): never up next
    # again, and the lead's told, so it learns what you skip. Kept with their title
    # and author once they've closed.
    """
    CREATE TABLE up_next_skipped (
        repo       TEXT NOT NULL COLLATE NOCASE,
        number     INTEGER NOT NULL,
        title      TEXT NOT NULL,
        author     TEXT NOT NULL,
        skipped_at TEXT NOT NULL,
        PRIMARY KEY (repo, number)
    );
    """,
    # No more "while you were away" recaps, nor knowing when you were last around,
    # which only they used.
    """
    DROP TABLE recaps;
    DROP TABLE meta;
    """,
    # The agent can propose pushing the commits it made to the PR's branch: a kind
    # of action SQLite's CHECK won't take without making the table again.
    """
    CREATE TABLE pending_actions_new (
        id          TEXT PRIMARY KEY,
        pr_id       INTEGER NOT NULL REFERENCES prs (id) ON DELETE CASCADE,
        kind        TEXT NOT NULL
                    CHECK (kind IN ('comment', 'request_changes', 'approve', 'merge', 'push')),
        description TEXT NOT NULL,
        payload     TEXT NOT NULL,
        status      TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'approved', 'dismissed', 'executed', 'failed')),
        created_at  TEXT NOT NULL,
        resolved_at TEXT,
        posted_head TEXT
    );
    INSERT INTO pending_actions_new
        SELECT id, pr_id, kind, description, payload, status, created_at, resolved_at,
               posted_head
        FROM pending_actions;
    DROP TABLE pending_actions;
    ALTER TABLE pending_actions_new RENAME TO pending_actions;
    CREATE INDEX pending_actions_pr ON pending_actions (pr_id);
    """,
    # Your latest verdict on each PR, so one you gave on GitHub counts too: read with the
    # replies, and set when Purr posts one. It starts from what Purr posted.
    """
    ALTER TABLE prs ADD COLUMN verdict TEXT;
    ALTER TABLE prs ADD COLUMN verdict_head TEXT;
    UPDATE prs SET (verdict, verdict_head) = (
        SELECT kind, posted_head FROM pending_actions
        WHERE pr_id = prs.id AND status = 'executed' AND kind IN ('approve', 'request_changes')
        ORDER BY resolved_at DESC, rowid DESC LIMIT 1
    );
    """,
    # Your own PRs aren't Purr's any more (you work on them elsewhere, in Claude Code
    # say): they go, with their reviews, proposals and comments (ON DELETE CASCADE).
    """
    DELETE FROM up_next WHERE (repo, number) IN (SELECT repo, number FROM prs WHERE authored);
    DELETE FROM prs WHERE authored;
    ALTER TABLE prs DROP COLUMN authored;
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

# The actions that are your verdict on a PR, for SQL: kind IN ({_VERDICT_MARKS}).
_VERDICTS = tuple(kind for kind in ActionKind if kind.verdict)
_VERDICT_MARKS = ", ".join("?" * len(_VERDICTS))


def _draft(row: aiosqlite.Row) -> DraftComment:
    return DraftComment(
        row["id"],
        row["path"],
        row["line"],
        row["side"],
        row["body"],
        row["head_sha"],
        row["start_line"],
        row["start_side"],
        row["created_at"],
    )


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _now_ms() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


# Who wrote each kind of message the agent's stream carries.
_AUTHORS = {"text": "agent", "thinking": "agent", "tool_use": "agent", "tool_result": "tool"}


def _unanswered(row: aiosqlite.Row) -> tuple[Reply, ...]:
    done = row["replies_done_at"] or ""
    return tuple(Reply(**r) for r in json.loads(row["replies"]) if r["at"] > done)


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
        requested_from_me=bool(row["requested_from_me"]),
        involved=bool(row["involved"]),
        updated_at=row["updated_at"],
        created_at=row["created_at"],
        base=row["base"],
        author_association=row["author_association"],
        assignees=tuple(json.loads(row["assignees"])),
        model=row["model"],
        state_at=row["state_at"],
        replies=_unanswered(row),
        verdict=_verdict(row),
    )


def _verdict(row: aiosqlite.Row) -> Verdict | None:
    kind, head = row["verdict"], row["verdict_head"]
    return Verdict(ActionKind(kind), head) if kind and head else None


def _repo(row: aiosqlite.Row) -> Repo:
    return Repo(name=row["name"], added_at=row["added_at"], synced_at=row["synced_at"])


def _activity(row: aiosqlite.Row) -> Activity:
    pr = PRRef(row["repo"], row["number"]) if row["repo"] is not None else None
    return Activity(
        id=row["id"], at=row["at"], kind=row["kind"], pr=pr, data=json.loads(row["data"])
    )


_UPSERT_PR = """
    INSERT INTO prs (repo, number, title, author, url, head_sha, assigned_to_me, gh_state,
                     updated_at, created_at, assignees, base, author_association,
                     requested_from_me, involved)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT (repo, number) DO UPDATE SET
        title = excluded.title,
        author = excluded.author,
        url = excluded.url,
        head_sha = excluded.head_sha,
        assigned_to_me = excluded.assigned_to_me,
        gh_state = excluded.gh_state,
        updated_at = excluded.updated_at,
        created_at = excluded.created_at,
        assignees = excluded.assignees,
        base = excluded.base,
        author_association = excluded.author_association,
        requested_from_me = excluded.requested_from_me,
        involved = MAX(prs.involved, excluded.involved)  -- once you've taken part, you have
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
        pr.created_at,
        json.dumps(list(pr.assignees)),
        pr.base,
        pr.author_association,
        int(pr.requested_from_me),
        int(pr.involved),
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
        skill=row["skill"],
        plan=ReviewPlan.from_dict(json.loads(row["plan"])) if row["plan"] else None,
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
        rows = await self._fetchall(sql, params)
        return rows[0] if rows else None

    async def _fetchall(self, sql: str, params: Iterable = ()) -> list[aiosqlite.Row]:
        """The rows, read in one go: a query left half-read while another task commits is
        an error ("SQL statements in progress"), on Python 3.14's sqlite3 at least."""
        return list(await self._db.execute_fetchall(sql, tuple(params)))

    async def _pr_id(self, ref: PRRef) -> int:
        row = await self._fetchone(
            "SELECT id FROM prs WHERE repo = ? AND number = ?", (ref.repo, ref.number)
        )
        if row is None:
            raise KeyError(f"unknown PR {ref}")
        return row["id"]

    # -- PRs -------------------------------------------------------------------

    async def pushed(self, ref: PRRef, head: str) -> None:
        """Purr pushed the agent's commits (you approved them) to the PR: they're its head
        now, and its review has seen them, so they're no new commits to re-review."""
        pr_id = await self._pr_id(ref)
        await self._db.execute("UPDATE prs SET head_sha = ? WHERE id = ?", (head, pr_id))
        await self._db.execute(
            "UPDATE reviews SET head_sha = ?"
            " WHERE id = (SELECT MAX(id) FROM reviews WHERE pr_id = ?)",
            (head, pr_id),
        )
        await self._db.commit()

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
            f"SELECT * FROM prs {where} ORDER BY {_STATE_ORDER}, created_at DESC, number DESC",
            params,
        )
        return [_pr(row) for row in rows]

    async def set_pr_model(self, ref: PRRef, model: str | None) -> None:
        """The model for everything in the PR's conversation (None: Purr's choice)."""
        await self._db.execute(
            "UPDATE prs SET model = ? WHERE repo = ? AND number = ?", (model, ref.repo, ref.number)
        )
        await self._db.commit()

    async def set_pr_state(self, ref: PRRef, state: PRState) -> None:
        cursor = await self._db.execute(
            # SET sees the row as it was: state_at moves only when the state does.
            """
            UPDATE prs SET state_at = CASE WHEN state = ? THEN state_at ELSE ? END, state = ?
            WHERE repo = ? AND number = ?
            """,
            (state, _now_ms(), state, ref.repo, ref.number),
        )
        await self._db.commit()
        if cursor.rowcount == 0:
            raise KeyError(f"unknown PR {ref}")

    async def set_replies(self, ref: PRRef, replies: Iterable[Reply]) -> None:
        """What others said to you on the PR since your last word, as GitHub has it now."""
        data = json.dumps([asdict(reply) for reply in replies])
        await self._db.execute(
            "UPDATE prs SET replies = ? WHERE repo = ? AND number = ?", (data, ref.repo, ref.number)
        )
        await self._db.commit()

    async def set_verdict(
        self, ref: PRRef, verdict: Verdict | None, *, read_at: str | None = None
    ) -> None:
        """Your latest verdict on the PR, as GitHub has it (None: you've given none, or it was
        dismissed), or as Purr just posted it. ``read_at``: when GitHub was asked; a verdict
        Purr posted since is newer than what it said, and stays."""
        kind, head = (verdict.kind, verdict.head) if verdict else (None, None)
        await self._db.execute(
            f"""
            UPDATE prs SET verdict = ?, verdict_head = ? WHERE repo = ? AND number = ?
            AND NOT EXISTS (
                SELECT 1 FROM pending_actions WHERE pr_id = prs.id AND status = ?
                AND kind IN ({_VERDICT_MARKS}) AND resolved_at >= ?
            )
            """,
            (kind, head, ref.repo, ref.number, ActionStatus.EXECUTED, *_VERDICTS, read_at),
        )
        await self._db.commit()

    async def dismiss_replies(self, ref: PRRef) -> None:
        """You said the PR's replies need no answer: the ones Purr has seen so far."""
        pr = await self.get_pr(ref)
        if pr and pr.replies:
            await self._db.execute(
                "UPDATE prs SET replies_done_at = ? WHERE repo = ? AND number = ?",
                (pr.replies[-1].at, ref.repo, ref.number),
            )
            await self._db.commit()

    async def waits_on_you(self, ref: PRRef) -> bool:
        """Something on the PR waits for you: a proposal to decide, or replies to answer."""
        pr = await self.get_pr(ref)
        return bool(pr and pr.replies) or bool(await self.pending_actions(ref))

    async def decided(self, ref: PRRef) -> bool:
        """Whether you gave your verdict on the PR as it is: approved it or requested changes,
        at its head, on GitHub or through Purr."""
        pr = await self.get_pr(ref)
        if pr and pr.verdict and pr.verdict.head == pr.head_sha:
            return True
        # Posted through Purr, even if a read of GitHub that began before says otherwise.
        row = await self._fetchone(
            f"{self._ACTION_SELECT} WHERE prs.repo = ? AND prs.number = ?"
            f" AND pending_actions.status = ? AND pending_actions.kind IN ({_VERDICT_MARKS})"
            " AND pending_actions.posted_head = prs.head_sha LIMIT 1",
            (ref.repo, ref.number, ActionStatus.EXECUTED, *_VERDICTS),
        )
        return row is not None

    async def done_without_verdict(self) -> list[PRRef]:
        """Open PRs that are done with no verdict of yours: done by the rule before (nothing
        left to decide), or your verdict was dismissed, when they need one."""
        rows = await self._fetchall(
            "SELECT repo, number FROM prs WHERE gh_state = ? AND state = ? AND verdict IS NULL",
            (GHState.OPEN, PRState.DONE),
        )
        return [PRRef(row["repo"], row["number"]) for row in rows]

    async def resting_state(self, ref: PRRef) -> PRState:
        """Where the PR goes once no agent is at it. It needs you while something waits on
        you (a proposal, replies), its review failed, or until you give your verdict on it as
        it is (approve it or request changes: a comment isn't one); then it's done. Not
        reviewed here: not reviewed."""
        review = await self.latest_review(ref)
        if await self.waits_on_you(ref) or (review and review.status == ReviewStatus.FAILED):
            return PRState.NEEDS_YOU
        if await self.decided(ref):
            return PRState.DONE
        return PRState.NOT_REVIEWED if review is None else PRState.NEEDS_YOU

    async def running(self, ref: PRRef) -> bool:
        """An agent run on the PR is queued or under way."""
        row = await self._fetchone(
            "SELECT 1 FROM runs WHERE repo = ? AND number = ? AND status IN ('queued', 'running')",
            (ref.repo, ref.number),
        )
        return row is not None

    async def open_prs(self) -> list[PR]:
        """PRs that are open on GitHub, in no particular order."""
        rows = await self._fetchall("SELECT * FROM prs WHERE gh_state = ?", (GHState.OPEN,))
        return [_pr(row) for row in rows]

    async def closed_among(self, refs: Iterable[PRRef]) -> set[PRRef]:
        """Those of ``refs`` Purr has as merged or closed."""
        wanted = set(refs)
        if not wanted:
            return set()
        rows = await self._fetchall(
            "SELECT repo, number FROM prs WHERE gh_state != ?", (GHState.OPEN,)
        )
        return {ref for row in rows if (ref := PRRef(row["repo"], row["number"])) in wanted}

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
        (assigned to you, asking for your review, or reviewed), alphabetically, with
        per-state counts."""
        rows = await self._fetchall(
            """
            WITH names (name, watched) AS (
                SELECT name, 1 FROM repos
                UNION
                -- Other repos only for a PR that concerns you (not just one you
                -- commented on once).
                SELECT DISTINCT repo, 0 FROM prs
                WHERE gh_state = 'open' AND repo NOT IN (SELECT name FROM repos)
                  AND (assigned_to_me OR requested_from_me OR state != 'not_reviewed')
            )
            SELECT
                names.name,
                names.watched,
                COALESCE(SUM(prs.state = 'not_reviewed' AND prs.assignees = '[]'
                             AND up_next.number IS NOT NULL), 0) AS up_next,
                COALESCE(SUM(prs.state = 'needs_you'), 0) AS needs_you,
                COALESCE(SUM(prs.state = 'done' AND prs.verdict = 'approve'
                             AND prs.verdict_head = prs.head_sha), 0) AS to_merge,
                COALESCE(SUM(prs.state = 'reviewing'), 0) AS reviewing,
                COALESCE(
                    SUM(prs.state = 'not_reviewed'
                        AND (prs.assigned_to_me OR prs.requested_from_me
                             OR prs.assignees = '[]')), 0
                ) AS not_reviewed,
                (SELECT MAX(at) FROM activity WHERE activity.repo = names.name) AS last_activity
            FROM names
            LEFT JOIN prs ON prs.repo = names.name AND prs.gh_state = 'open'
            LEFT JOIN up_next ON up_next.repo = prs.repo AND up_next.number = prs.number
            GROUP BY names.name, names.watched
            ORDER BY names.name COLLATE NOCASE
            """
        )
        return [
            RepoSummary(
                name=row["name"],
                watched=bool(row["watched"]),
                up_next=row["up_next"],
                needs_you=row["needs_you"],
                to_merge=row["to_merge"],
                reviewing=row["reviewing"],
                not_reviewed=row["not_reviewed"],
                last_activity=row["last_activity"],
            )
            for row in rows
        ]

    async def remove_repo(self, name: str) -> bool:
        """Stop watching a repo and drop its pool.

        PRs assigned to you, asking for your review, that you've taken part in, or that
        Purr has started reviewing, are kept.
        Returns whether the repo was being watched.
        """
        cursor = await self._db.execute("DELETE FROM repos WHERE name = ?", (name,))
        removed = cursor.rowcount > 0
        await self._db.execute(
            """
            DELETE FROM prs
            WHERE repo = ? COLLATE NOCASE AND state = ?
              AND assigned_to_me = 0 AND requested_from_me = 0 AND involved = 0
            """,
            (name, PRState.NOT_REVIEWED),
        )
        await self._db.commit()
        return removed

    async def set_up_next(self, repo: str, picks: Iterable[UpNext]) -> None:
        """What's up next in ``repo`` now, in place of what was."""
        await self._db.execute("DELETE FROM up_next WHERE repo = ?", (repo,))
        await self._db.executemany(
            "INSERT INTO up_next (repo, number, rank, why, by, picked_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            [(repo, p.pr.number, p.rank, p.why, p.by, _now()) for p in picks],
        )
        await self._db.commit()

    async def up_next(self, repo: str) -> list[UpNext]:
        """What's up next in ``repo``, best first: those still in its pool, unreviewed."""
        rows = await self._fetchall(
            """
            SELECT up_next.* FROM up_next
            JOIN prs ON prs.repo = up_next.repo AND prs.number = up_next.number
            WHERE up_next.repo = ? AND prs.gh_state = 'open' AND prs.state = 'not_reviewed'
              AND prs.assignees = '[]'
            ORDER BY up_next.rank
            """,
            (repo,),
        )
        return [UpNext(PRRef(r["repo"], r["number"]), r["why"], r["rank"], r["by"]) for r in rows]

    async def skip_up_next(self, ref: PRRef) -> None:
        """Not for you: out of what's up next, for good."""
        await self._db.execute(
            """
            INSERT OR REPLACE INTO up_next_skipped (repo, number, title, author, skipped_at)
            SELECT repo, number, title, author, ? FROM prs WHERE repo = ? AND number = ?
            """,
            (_now(), ref.repo, ref.number),
        )
        await self._db.execute(
            "DELETE FROM up_next WHERE repo = ? AND number = ?", (ref.repo, ref.number)
        )
        await self._db.commit()

    async def skipped(self, repo: str, *, limit: int = 10) -> list[PR]:
        """The PRs you said weren't for you in ``repo``, latest first (as they were then)."""
        rows = await self._fetchall(
            "SELECT * FROM up_next_skipped WHERE repo = ? ORDER BY skipped_at DESC LIMIT ?",
            (repo, limit),
        )
        return [PR(r["repo"], r["number"], r["title"], r["author"], "", "", "") for r in rows]

    async def taken(self, repo: str, *, limit: int = 10) -> list[PR]:
        """PRs in ``repo`` you had reviewed lately, latest first."""
        rows = await self._fetchall(
            """
            SELECT prs.* FROM prs JOIN reviews ON reviews.pr_id = prs.id
            WHERE prs.repo = ?
            GROUP BY prs.id ORDER BY MAX(reviews.started_at) DESC LIMIT ?
            """,
            (repo, limit),
        )
        return [_pr(row) for row in rows]

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
        row = await self._fetchone(
            "INSERT INTO activity (at, kind, repo, number, data) VALUES (?, ?, ?, ?, ?)"
            " RETURNING *",
            (
                _now(),
                kind,
                pr.repo if pr else None,
                pr.number if pr else None,
                json.dumps(data or {}),
            ),
        )
        await self._db.commit()
        return _activity(row)

    async def activity(self, *, pr: PRRef | None = None) -> list[Activity]:
        """Activity entries, oldest first: all, or one PR's."""
        where, params = ("WHERE repo = ? AND number = ?", [pr.repo, pr.number]) if pr else ("", [])
        rows = await self._fetchall(f"SELECT * FROM activity {where} ORDER BY id", params)
        return [_activity(row) for row in rows]

    # -- Conversations, for analysis ---------------------------------------------

    async def start_run(
        self, pr: PRRef, *, kind: str, review_id: int | None, session_id: str | None
    ) -> int:
        """A run is queued (reviews may wait for a slot); returns its id."""
        cursor = await self._db.execute(
            "INSERT INTO runs (review_id, session_id, repo, number, kind, queued_at, status)"
            " VALUES (?, ?, ?, ?, ?, ?, 'queued')",
            (review_id, session_id, pr.repo, pr.number, kind, _now_ms()),
        )
        await self._db.commit()
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    async def run_started(self, run_id: int) -> None:
        await self._db.execute(
            "UPDATE runs SET started_at = ?, status = 'running' WHERE id = ?", (_now_ms(), run_id)
        )
        await self._db.commit()

    async def finish_run(
        self, run_id: int, *, error: str | None = None, result: dict | None = None
    ) -> None:
        """A run ended (``error`` if it failed); ``result`` is its ``result`` entry."""
        result = result or {}
        usage = result.get("usage") or {}
        await self._db.execute(
            "UPDATE runs SET finished_at = ?, status = ?, error = ?, model = ?, cost_usd = ?,"
            " turns = ?, duration_ms = ?, api_duration_ms = ?, input_tokens = ?,"
            " output_tokens = ?, cache_read_tokens = ?, cache_write_tokens = ? WHERE id = ?",
            (
                _now_ms(),
                "failed" if error else "finished",
                error,
                result.get("model"),
                result.get("cost_usd"),
                result.get("turns"),
                result.get("duration_ms"),
                result.get("api_duration_ms"),
                usage.get("input_tokens"),
                usage.get("output_tokens"),
                usage.get("cache_read_input_tokens"),
                usage.get("cache_creation_input_tokens"),
                run_id,
            ),
        )
        await self._db.commit()

    async def interrupt_runs(self) -> None:
        """Runs cut short by a shutdown (on startup, before anything runs)."""
        await self._db.execute(
            "UPDATE runs SET status = 'interrupted' WHERE status IN ('queued', 'running')"
        )
        await self._db.commit()

    async def add_message(
        self, run_id: int, pr: PRRef, entry: dict, *, author: str | None = None
    ) -> None:
        """One message of a run: a transcript entry (see purr.review.agent), as it happened."""
        kind = str(entry.get("kind", "unknown"))
        await self._db.execute(
            "INSERT INTO messages (run_id, at, repo, number, kind, author, data)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                _now_ms(),
                pr.repo,
                pr.number,
                kind,
                author or _AUTHORS.get(kind, "purr"),
                json.dumps(entry, default=str),
            ),
        )
        await self._db.commit()

    async def runs(self, pr: PRRef) -> list[dict]:
        rows = await self._fetchall(
            "SELECT * FROM runs WHERE repo = ? AND number = ? ORDER BY id", (pr.repo, pr.number)
        )
        return [dict(row) for row in rows]

    async def messages(self, pr: PRRef) -> list[dict]:
        rows = await self._fetchall(
            "SELECT * FROM messages WHERE repo = ? AND number = ? ORDER BY id",
            (pr.repo, pr.number),
        )
        return [{**dict(row), "data": json.loads(row["data"])} for row in rows]

    # -- What's new for you ------------------------------------------------------

    async def mark_seen(self, pr: PRRef) -> None:
        """You looked at the PR: what happened until now isn't news anymore."""
        await self._db.execute(
            "UPDATE prs SET seen_at = ? WHERE repo = ? AND number = ?",
            (_now_ms(), pr.repo, pr.number),
        )
        await self._db.commit()

    async def unseen(self) -> list[tuple[PRRef, str]]:
        """Open PRs where something you'd want to know happened since you last looked.

        One per PR, its latest news ("review done", ...), oldest first.
        """
        kinds = ", ".join("?" * len(NEWS_KINDS))
        rows = await self._fetchall(
            f"""
            SELECT a.repo, a.number, a.kind, a.data
            FROM activity a JOIN prs p ON p.repo = a.repo AND p.number = a.number
            WHERE p.gh_state = 'open' AND a.kind IN ({kinds})
              AND (p.seen_at IS NULL OR julianday(a.at) > julianday(p.seen_at))
            ORDER BY a.id
            """,
            NEWS_KINDS,
        )
        latest: dict[PRRef, str] = {}
        for row in rows:
            if what := news(row["kind"], json.loads(row["data"])):
                ref = PRRef(row["repo"], row["number"])
                latest.pop(ref, None)  # keep each PR at its latest news, in order
                latest[ref] = what
        return list(latest.items())

    # -- Prompt history --------------------------------------------------------

    async def add_prompt(self, pr: PRRef, text: str) -> None:
        """Remember something you asked a PR's agent."""
        await self._db.execute(
            "INSERT INTO prompts (at, repo, number, text) VALUES (?, ?, ?, ?)",
            (_now(), pr.repo, pr.number, text),
        )
        await self._db.commit()

    async def prompts(
        self, pr: PRRef | None = None, *, limit: int = 500
    ) -> list[tuple[PRRef, str]]:
        """What you asked, newest first: on one PR, or on any."""
        where, params = ("WHERE repo = ? AND number = ?", [pr.repo, pr.number]) if pr else ("", [])
        rows = await self._fetchall(
            f"SELECT repo, number, text FROM prompts {where} ORDER BY id DESC LIMIT ?",
            [*params, limit],
        )
        return [(PRRef(row["repo"], row["number"]), row["text"]) for row in rows]

    # -- Reviews ---------------------------------------------------------------

    _REVIEW_SELECT = """
        SELECT reviews.*, prs.repo, prs.number
        FROM reviews JOIN prs ON prs.id = reviews.pr_id
    """

    async def start_review(
        self,
        ref: PRRef,
        *,
        worktree_path: str | None = None,
        head_sha: str | None = None,
        plan: ReviewPlan | None = None,
    ) -> Review:
        pr_id = await self._pr_id(ref)
        cursor = await self._db.execute(
            "INSERT INTO reviews (pr_id, worktree_path, head_sha, status, started_at, plan)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                pr_id,
                worktree_path,
                head_sha,
                ReviewStatus.RUNNING,
                _now(),
                json.dumps(plan.to_dict()) if plan else None,
            ),
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

    async def set_review_plan(self, review_id: int, plan: ReviewPlan) -> None:
        """What the lead planned for the review's latest round (a re-review replaces it)."""
        await self._db.execute(
            "UPDATE reviews SET plan = ? WHERE id = ?", (json.dumps(plan.to_dict()), review_id)
        )
        await self._db.commit()

    async def set_review_followed(self, review_id: int, skill: str | None) -> None:
        """Which of the repo's own skills the review follows (None: Purr's own review)."""
        await self._db.execute("UPDATE reviews SET skill = ? WHERE id = ?", (skill, review_id))
        await self._db.commit()

    # -- Review skills ----------------------------------------------------------------

    async def review_skill(self, repo: str) -> str | None:
        """The repo skill its reviews follow: '' for Purr's own, None if not decided yet."""
        row = await self._fetchone("SELECT skill FROM review_skills WHERE repo = ?", (repo,))
        return row["skill"] if row else None

    async def set_review_skill(self, repo: str, skill: str, *, chosen_by: str) -> None:
        """Have the repo's reviews follow ``skill`` ('' for Purr's own review).

        ``chosen_by`` is ``purr`` (it was clearly the repo's review skill) or ``you``.
        """
        await self._db.execute(
            "INSERT INTO review_skills (repo, skill, chosen_by, chosen_at) VALUES (?, ?, ?, ?)"
            " ON CONFLICT (repo) DO UPDATE SET skill = excluded.skill,"
            " chosen_by = excluded.chosen_by, chosen_at = excluded.chosen_at",
            (repo, skill, chosen_by, _now()),
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

    async def review_before(self, review_id: int) -> Review | None:
        """The PR's review before this one, if it had one."""
        row = await self._fetchone(
            f"""{self._REVIEW_SELECT}
            WHERE reviews.pr_id = (SELECT pr_id FROM reviews WHERE id = ?) AND reviews.id < ?
            ORDER BY reviews.id DESC LIMIT 1""",
            (review_id, review_id),
        )
        return _review(row) if row else None

    async def drop_review(self, review_id: int) -> None:
        """Forget a review (and its findings): the one before it is the PR's latest again.
        Its runs stay, for what they cost."""
        await self._db.execute("DELETE FROM reviews WHERE id = ?", (review_id,))
        await self._db.commit()

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

    async def outcomes(self, repo: str | None = None) -> dict[PRRef, Outcome]:
        """What each PR's latest review found, for PRs whose latest review finished."""
        where = "AND prs.repo = ?" if repo else ""
        rows = await self._fetchall(
            f"""
            SELECT prs.repo, prs.number, findings.severity, COUNT(findings.id) AS n
            FROM prs
            JOIN reviews ON reviews.id =
                (SELECT MAX(id) FROM reviews WHERE reviews.pr_id = prs.id)
            LEFT JOIN findings ON findings.review_id = reviews.id
            WHERE reviews.status = 'finished' {where}
            GROUP BY prs.id, findings.severity
            """,
            (repo,) if repo else (),
        )
        counts: dict[PRRef, list[dict]] = {}
        for row in rows:
            found = counts.setdefault(PRRef(row["repo"], row["number"]), [])
            if row["severity"]:
                found += [{"severity": row["severity"]}] * row["n"]
        return {ref: Outcome.of(found) for ref, found in counts.items()}

    async def failed(self, repo: str) -> set[PRRef]:
        """The repo's PRs whose latest review failed."""
        rows = await self._fetchall(
            """
            SELECT prs.repo, prs.number FROM prs
            JOIN reviews ON reviews.id = (SELECT MAX(id) FROM reviews WHERE reviews.pr_id = prs.id)
            WHERE prs.repo = ? AND reviews.status = ?
            """,
            (repo, ReviewStatus.FAILED),
        )
        return {PRRef(row["repo"], row["number"]) for row in rows}

    # -- Pending actions -------------------------------------------------------

    async def proposed(self, repo: str) -> dict[PRRef, ActionKind]:
        """What the agent proposes on each of the repo's PRs (the oldest, if several)."""
        rows = await self._fetchall(
            f"{self._ACTION_SELECT} WHERE prs.repo = ? AND pending_actions.status = ?"
            " ORDER BY pending_actions.created_at DESC, pending_actions.rowid DESC",
            (repo, ActionStatus.PENDING),
        )
        return {PRRef(row["repo"], row["number"]): ActionKind(row["kind"]) for row in rows}

    # -- Your inline comments, until they're posted ----------------------------------

    async def drafted(self, repo: str) -> set[PRRef]:
        """The repo's PRs with comments of yours not posted yet."""
        rows = await self._fetchall(
            "SELECT DISTINCT prs.repo, prs.number FROM draft_comments"
            " JOIN prs ON prs.id = draft_comments.pr_id WHERE prs.repo = ?",
            (repo,),
        )
        return {PRRef(row["repo"], row["number"]) for row in rows}

    async def save_comment(
        self,
        ref: PRRef,
        *,
        path: str,
        line: int,
        side: str,
        body: str,
        head_sha: str,
        start_line: int | None = None,
        start_side: str | None = None,
    ) -> DraftComment:
        """A new inline comment of yours, on ``line`` of ``path`` at ``head_sha`` (or from
        ``start_line`` to it)."""
        pr_id = await self._pr_id(ref)
        row = await self._fetchone(
            "INSERT INTO draft_comments"
            " (pr_id, path, line, side, body, head_sha, created_at, start_line, start_side)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING *",
            (pr_id, path, line, side, body, head_sha, _now(), start_line, start_side),
        )
        await self._db.commit()
        return _draft(row)

    async def edit_comment(self, comment_id: int, body: str) -> None:
        sql = "UPDATE draft_comments SET body = ? WHERE id = ?"
        await self._db.execute(sql, (body, comment_id))
        await self._db.commit()

    async def remove_comments(self, ids: Iterable[int]) -> None:
        await self._db.executemany("DELETE FROM draft_comments WHERE id = ?", [(i,) for i in ids])
        await self._db.commit()

    async def draft_comments(self, ref: PRRef) -> list[DraftComment]:
        """Your inline comments on the PR not posted yet, in the order you wrote them."""
        rows = await self._fetchall(
            """
            SELECT draft_comments.* FROM draft_comments JOIN prs ON prs.id = draft_comments.pr_id
            WHERE prs.repo = ? AND prs.number = ? ORDER BY draft_comments.id
            """,
            (ref.repo, ref.number),
        )
        return [_draft(row) for row in rows]

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

    async def approved_by_you(self, ref: PRRef) -> bool:
        """Whether the last thing Purr posted on the PR for you was an approval."""
        row = await self._fetchone(
            f"{self._ACTION_SELECT} WHERE prs.repo = ? AND prs.number = ?"
            " AND pending_actions.status = ? ORDER BY pending_actions.resolved_at DESC LIMIT 1",
            (ref.repo, ref.number, ActionStatus.EXECUTED),
        )
        return bool(row) and _action(row).kind == ActionKind.APPROVE

    async def approved_head(self, ref: PRRef) -> str | None:
        """The commit you approved the PR at, if approving it is your latest verdict there
        (given on GitHub or through Purr)."""
        pr = await self.get_pr(ref)
        verdict = pr.verdict if pr else None
        return verdict.head if verdict and verdict.kind == ActionKind.APPROVE else None

    async def get_pending_action(self, action_id: str) -> PendingAction | None:
        row = await self._fetchone(
            f"{self._ACTION_SELECT} WHERE pending_actions.id = ?", (action_id,)
        )
        return _action(row) if row else None

    async def set_action_payload(self, action_id: str, payload: dict) -> None:
        """What a pending action would post, changed (you rewrote one of its comments)."""
        sql = "UPDATE pending_actions SET payload = ? WHERE id = ? AND status = ?"
        await self._db.execute(sql, (json.dumps(payload), action_id, ActionStatus.PENDING))
        await self._db.commit()

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
        self,
        action_id: str,
        *,
        expected: ActionStatus,
        new: ActionStatus,
        posted_head: str | None = None,
    ) -> PendingAction | None:
        """Move an action from ``expected`` to ``new`` status, atomically.

        Returns the updated action, or None if it wasn't in ``expected`` status.
        This is what stops one action being approved twice (say, from the TUI
        and by voice at the same time). ``posted_head``: the PR's head it was posted at.
        A verdict posted is your latest verdict on the PR.
        """
        cursor = await self._db.execute(
            "UPDATE pending_actions SET status = ?, resolved_at = ?,"
            " posted_head = COALESCE(?, posted_head) WHERE id = ? AND status = ?",
            (new, _now(), posted_head, action_id, expected),
        )
        await self._db.commit()
        if cursor.rowcount == 0:
            return None
        action = await self.get_pending_action(action_id)
        if action and new == ActionStatus.EXECUTED and action.kind.verdict and posted_head:
            await self.set_verdict(action.pr, Verdict(action.kind, posted_head))
        return action
