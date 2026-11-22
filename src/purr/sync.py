"""Turning a GitHub snapshot into changes to Purr's PR list.

Pure functions, so the rules are easy to test without GitHub or a database:

- A watched repo lists its open, non-draft PRs by other people, whoever is
  assigned. The unassigned ones are the pool.
- PRs assigned to you, or asking for your review by name, are tracked from any
  repo. A request is news, but only being assigned starts a review.
- A PR that drops out of both is removed if Purr never reviewed it. If Purr did,
  it stays, and its GitHub state is looked up to tell closed/merged from
  "drafted" or "unassigned from you elsewhere".
- Between full syncs, a repo is synced incrementally: only PRs updated since
  the last sync are fetched, and each is classified as listed or not. PRs that
  weren't updated are left as they are.
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
    by: str = ""  # who, when it was someone (who replied)


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
    """Split incremental updates into PRs now listed and refs known not to be."""
    if not snapshot.updates:
        return [], set()
    assert snapshot.viewer, "incremental updates need the viewer's login"
    listed, out = [], set()
    for prs in snapshot.updates.values():
        for pr in prs:
            if pr.listed(snapshot.viewer):
                listed.append(pr)
            else:
                out.add(pr.ref)
    return listed, out


def _covered(ref: PRRef, snapshot: Snapshot, watched: set[str]) -> bool:
    """Whether the snapshot says for sure that this PR is in none of the searches."""
    if snapshot.assigned is None or snapshot.requested is None:
        return False
    if ref.repo not in watched or ref.repo in snapshot.open:
        return True
    return ref.repo in snapshot.updates and ref in _updated(snapshot)[1]


def _seen(snapshot: Snapshot) -> dict[PRRef, PR]:
    me = snapshot.viewer or ""
    listed, _ = _updated(snapshot)
    seen = {pr.ref: pr.to_pr(me) for prs in snapshot.open.values() for pr in prs}
    seen.update((pr.ref, pr.to_pr(me)) for pr in listed)
    for pr in snapshot.requested or []:
        seen.setdefault(pr.ref, replace(pr.to_pr(me), requested_from_me=True))
    for pr in snapshot.assigned or []:
        seen[pr.ref] = replace(pr.to_pr(me), assigned_to_me=True)
    return seen


def _same_metadata(old: PR, new: PR) -> bool:
    """Whether GitHub reports nothing new; Purr's own fields don't count."""
    own = {"id": old.id, "state": old.state, "model": old.model, "state_at": old.state_at}
    return replace(new, **own, replies=old.replies) == old


def _assignment_change(old: PR, new: PR) -> PRChange | None:
    """Assigned to or unassigned from you; otherwise, joined or left the pool."""
    if new.assigned_to_me != old.assigned_to_me:
        return PRChange.ASSIGNED if new.assigned_to_me else PRChange.UNASSIGNED
    if new.in_pool != old.in_pool:
        return PRChange.POOLED if new.in_pool else PRChange.UNPOOLED
    return None


def _new_pr_change(pr: PR, first_sync: bool) -> PRChange | None:
    if pr.assigned_to_me:
        return PRChange.ASSIGNED
    if pr.requested_from_me:
        return PRChange.REQUESTED
    if pr.in_pool and not first_sync:
        return PRChange.POOLED
    return None  # someone else's, or part of a repo's first sync


def _gone_change(pr: PR) -> PRChange | None:
    """An unreviewed PR that's no longer listed (closed, drafted, unassigned elsewhere)."""
    if pr.assigned_to_me:
        return PRChange.UNASSIGNED
    return PRChange.UNPOOLED if pr.in_pool else None


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

    def report(ref: PRRef, change: PRChange | None, title: str) -> None:
        if change:
            plan.changes.append(Change(ref, change, title))

    seen = _seen(snapshot)
    for ref, new in seen.items():
        old = current_by_ref.get(ref)
        if old is None:
            plan.upserts.append(new)
            report(ref, _new_pr_change(new, ref.repo in first_sync), new.title)
            continue
        if not _same_metadata(old, new):
            plan.upserts.append(new)
        report(ref, _assignment_change(old, new), new.title)
        if new.requested_from_me and not old.requested_from_me and not new.assigned_to_me:
            report(ref, PRChange.REQUESTED, new.title)
        if new.head_sha != old.head_sha and old.state != PRState.NOT_REVIEWED:
            report(ref, PRChange.NEW_COMMITS, new.title)

    for ref, old in current_by_ref.items():
        if ref in seen or not _covered(ref, snapshot, watched):
            continue
        if old.state == PRState.NOT_REVIEWED:
            plan.deletes.append(ref)
            report(ref, _gone_change(old), old.title)
            continue
        status = statuses.get(ref)
        if status is None:
            continue  # couldn't look it up; try again next poll
        if status.state != GHState.OPEN:
            plan.gh_states[ref] = status.state
            change = PRChange.MERGED if status.state == GHState.MERGED else PRChange.CLOSED
            plan.changes.append(Change(ref, change, old.title))
        elif old.assigned_to_me:
            me = (snapshot.viewer or "").lower()
            others = tuple(a for a in old.assignees if a.lower() != me)
            plan.upserts.append(replace(old, assigned_to_me=False, assignees=others))
            report(ref, PRChange.UNASSIGNED, old.title)
    return plan
