"""Turning a GitHub snapshot into changes to Purr's PR list.

Pure functions, so the rules are easy to test without GitHub or a database:

- The pool is a watched repo's open, non-draft, unassigned PRs by other people.
- PRs assigned to you are tracked from any repo.
- A PR that drops out of both is removed if Purr never reviewed it. If Purr did,
  it stays, and its GitHub state is looked up to tell closed/merged from
  "assigned to someone else".
- Between full syncs, a repo is synced incrementally: only PRs updated since
  the last sync are fetched, and each is classified as in or out of the pool.
  PRs that weren't updated are left as they are.
- Nothing is concluded from a search that failed or came back incomplete.
- A repo's first sync doesn't report every PR in its pool as new.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace

from purr.github import PRStatus, Snapshot
from purr.models import PR, GHState, PRChange, PRRef, PRState


@dataclass
class Change:
    pr: PRRef
    change: PRChange
    title: str


@dataclass
class SyncPlan:
    upserts: list[PR] = field(default_factory=list)
    deletes: list[PRRef] = field(default_factory=list)
    gh_states: dict[PRRef, GHState] = field(default_factory=dict)
    changes: list[Change] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.upserts or self.deletes or self.gh_states)


def _updated(snapshot: Snapshot) -> tuple[list, set[PRRef]]:
    """Split incremental updates into PRs now in the pool and refs known to be out of it."""
    if not snapshot.updates:
        return [], set()
    assert snapshot.viewer, "incremental updates need the viewer's login"
    pooled, out = [], set()
    for prs in snapshot.updates.values():
        for pr in prs:
            if pr.in_pool(snapshot.viewer):
                pooled.append(pr)
            else:
                out.add(pr.ref)
    return pooled, out


def _covered(ref: PRRef, snapshot: Snapshot, watched: set[str]) -> bool:
    """Whether the snapshot says for sure that this PR is in none of the searches."""
    if snapshot.assigned is None:
        return False
    if ref.repo not in watched or ref.repo in snapshot.pool:
        return True
    return ref.repo in snapshot.updates and ref in _updated(snapshot)[1]


def _seen(snapshot: Snapshot) -> dict[PRRef, PR]:
    pooled, _ = _updated(snapshot)
    seen = {pr.ref: pr.to_pr(assigned_to_me=False) for prs in snapshot.pool.values() for pr in prs}
    seen.update((pr.ref, pr.to_pr(assigned_to_me=False)) for pr in pooled)
    for pr in snapshot.assigned or []:
        seen[pr.ref] = pr.to_pr(assigned_to_me=True)
    return seen


def _same_metadata(a: PR, b: PR) -> bool:
    return (a.title, a.author, a.url, a.head_sha, a.updated_at, a.assigned_to_me, a.gh_state) == (
        b.title,
        b.author,
        b.url,
        b.head_sha,
        b.updated_at,
        b.assigned_to_me,
        b.gh_state,
    )


def missing_reviewed(
    current: Iterable[PR], snapshot: Snapshot, watched: Iterable[str]
) -> list[PRRef]:
    """Open PRs Purr has reviewed that dropped out of every search.

    Their status has to be looked up before planning: they may have been closed
    or merged, or just assigned to someone else.
    """
    watched = set(watched)
    seen = _seen(snapshot)
    return [
        pr.ref
        for pr in current
        if pr.ref not in seen
        and pr.state != PRState.NOT_REVIEWED
        and _covered(pr.ref, snapshot, watched)
    ]


def plan_sync(
    current: Iterable[PR],
    snapshot: Snapshot,
    *,
    watched: Iterable[str],
    first_sync: Iterable[str] = (),
    statuses: Mapping[PRRef, PRStatus] | None = None,
) -> SyncPlan:
    """Work out how the store should change after a poll.

    Args:
        current: The open PRs in the store.
        snapshot: What GitHub returned.
        watched: Watched repo names.
        first_sync: Watched repos never synced before; their pool isn't reported as new.
        statuses: GitHub status of the PRs ``missing_reviewed()`` returned.
    """
    watched, first_sync, statuses = set(watched), set(first_sync), statuses or {}
    plan = SyncPlan()
    current_by_ref = {pr.ref: pr for pr in current}

    for ref, new in _seen(snapshot).items():
        old = current_by_ref.get(ref)
        if old is None:
            plan.upserts.append(new)
            if new.assigned_to_me:
                plan.changes.append(Change(ref, PRChange.ASSIGNED, new.title))
            elif ref.repo not in first_sync:
                plan.changes.append(Change(ref, PRChange.POOLED, new.title))
            continue
        if not _same_metadata(old, new):
            plan.upserts.append(new)
        if new.assigned_to_me and not old.assigned_to_me:
            plan.changes.append(Change(ref, PRChange.ASSIGNED, new.title))
        elif old.assigned_to_me and not new.assigned_to_me:
            plan.changes.append(Change(ref, PRChange.UNASSIGNED, new.title))
        if new.head_sha != old.head_sha and old.state != PRState.NOT_REVIEWED:
            plan.changes.append(Change(ref, PRChange.NEW_COMMITS, new.title))

    seen = _seen(snapshot)
    for ref, old in current_by_ref.items():
        if ref in seen or not _covered(ref, snapshot, watched):
            continue
        if old.state == PRState.NOT_REVIEWED:
            plan.deletes.append(ref)
            change = PRChange.UNASSIGNED if old.assigned_to_me else PRChange.UNPOOLED
            plan.changes.append(Change(ref, change, old.title))
            continue
        status = statuses.get(ref)
        if status is None:
            continue  # couldn't look it up; try again next poll
        if status.state != GHState.OPEN:
            plan.gh_states[ref] = status.state
            change = PRChange.MERGED if status.state == GHState.MERGED else PRChange.CLOSED
            plan.changes.append(Change(ref, change, old.title))
        elif old.assigned_to_me:
            plan.upserts.append(replace(old, assigned_to_me=False))
            plan.changes.append(Change(ref, PRChange.UNASSIGNED, old.title))
    return plan
