"""Turning a GitHub snapshot into changes to Purr's PR list.

Pure functions, so the rules are easy to test without GitHub or a database:

- A watched repo lists its open, non-draft PRs by other people, whoever is
  assigned. The unassigned ones are the pool.
- PRs assigned to you, or asking for your review by name, are tracked from any
  repo. A request is news, but only being assigned starts a review.
- So are PRs by others you've commented on or reviewed on GitHub ("involved",
  which sticks), for replies to you; quietly: they're news only when someone
  replies (see the GitHub worker).
- Never your own PRs: you work on them elsewhere (in Claude Code, say).
- The pool is a watched repo's: PRs from other repos never join or leave it.
- A PR that drops out of both is removed if Purr never reviewed it. If Purr did,
  it stays, and its GitHub state is looked up to tell closed/merged from
  "drafted" or "unassigned from you elsewhere".
- Between full syncs, a repo is synced incrementally: only PRs updated since
  the last sync are fetched, and each is classified as listed or not. PRs that
  weren't updated are left as they are.
- Nothing is concluded from a search that failed or came back incomplete.
- A reviewed PR's new head is read from the PR itself before it's believed: the
  searches lag behind a push (Purr's own, say) and can report the head from
  before it, which would look like new commits. Likewise, a merged or closed PR
  the searches still list is open again only if the PR itself says so.
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

    def report(self, ref: PRRef, change: PRChange | None, title: str) -> None:
        """Tell about ``change``, if there is one."""
        if change:
            self.changes.append(Change(ref, change, title))


def _updated(snapshot: Snapshot) -> tuple[list, set[PRRef]]:
    """Split incremental updates into PRs now listed and refs known not to be."""
    if not snapshot.updates:
        return [], set()
    assert snapshot.viewer, "incremental updates need the viewer's login"
    me = snapshot.viewer
    updated = [pr for prs in snapshot.updates.values() for pr in prs]
    listed = [pr for pr in updated if pr.listed(me)]
    out = {pr.ref for pr in updated if not pr.listed(me)}
    return listed, out


def _covered(ref: PRRef, snapshot: Snapshot, watched: set[str]) -> bool:
    """Whether the snapshot says for sure that this PR is in none of the searches."""
    searches = (snapshot.assigned, snapshot.requested, snapshot.involved)
    if any(found is None for found in searches):
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
    for pr in snapshot.involved or []:
        seen.setdefault(pr.ref, pr.to_pr(me))
    for ref in {pr.ref for pr in snapshot.involved or []}:
        seen[ref] = replace(seen[ref], involved=True)
    for pr in snapshot.assigned or []:
        seen[pr.ref] = replace(pr.to_pr(me), assigned_to_me=True)
    return seen


def _same_metadata(old: PR, new: PR) -> bool:
    """Whether GitHub reports nothing new; Purr's own fields don't count."""
    own = {"id": old.id, "state": old.state, "model": old.model, "state_at": old.state_at}
    return replace(new, **own, replies=old.replies) == old


def _assignment_change(old: PR, new: PR, watched: bool) -> PRChange | None:
    """Assigned to or unassigned from you; otherwise, joined or left a watched repo's pool."""
    if new.assigned_to_me != old.assigned_to_me:
        return PRChange.ASSIGNED if new.assigned_to_me else PRChange.UNASSIGNED
    if new.in_pool != old.in_pool and watched:
        return PRChange.POOLED if new.in_pool else PRChange.UNPOOLED
    return None


def _new_pr_change(pr: PR, first_sync: bool, watched: bool) -> PRChange | None:
    if pr.assigned_to_me:
        return PRChange.ASSIGNED
    if pr.requested_from_me:
        return PRChange.REQUESTED
    if pr.in_pool and watched and not first_sync:
        return PRChange.POOLED
    return None  # someone else's, not from a watched repo, or part of a repo's first sync


def _gone_change(pr: PR, watched: bool) -> PRChange | None:
    """An unreviewed PR that's no longer listed (closed, drafted, unassigned elsewhere)."""
    if pr.assigned_to_me:
        return PRChange.UNASSIGNED
    return PRChange.UNPOOLED if pr.in_pool and watched else None


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


def moved_reviewed(current: Iterable[PR], snapshot: Snapshot) -> list[PRRef]:
    """PRs Purr has reviewed whose head the searches say moved.

    Their status has to be looked up before planning: the PR itself has its head
    right, where the searches may still have the one from before a push.
    """
    seen = _seen(snapshot)
    return [
        pr.ref
        for pr in current
        if pr.ref in seen
        and seen[pr.ref].head_sha != pr.head_sha
        and pr.state != PRState.NOT_REVIEWED
    ]


def not_open(current: Iterable[PR], snapshot: Snapshot) -> list[PRRef]:
    """PRs the searches list that aren't among the store's open ones: new ones, or ones the
    store has as merged or closed (see ``plan_sync``'s ``closed``)."""
    open_refs = {pr.ref for pr in current}
    return [ref for ref in _seen(snapshot) if ref not in open_refs]


def plan_sync(
    current: Iterable[PR],
    snapshot: Snapshot,
    *,
    watched: Iterable[str],
    first_sync: Iterable[str] = (),
    statuses: Mapping[PRRef, PRStatus] | None = None,
    closed: Iterable[PRRef] = (),
) -> SyncPlan:
    """Work out how the store should change after a poll.

    Args:
        current: The open PRs in the store.
        snapshot: What GitHub returned.
        watched: Watched repo names.
        first_sync: Watched repos never synced before; their pool isn't reported as new.
        statuses: GitHub status of the PRs ``missing_reviewed()`` and
            ``moved_reviewed()`` returned, and of those in ``closed``.
        closed: PRs the searches list that the store has as merged or closed. GitHub's
            search lags: it can list a PR as open for a while after it's merged (by you,
            a moment ago). Only its status says it's open again (reopened).
    """
    watched, first_sync, statuses = set(watched), set(first_sync), statuses or {}
    closed = set(closed)
    plan = SyncPlan()
    current_by_ref = {pr.ref: pr for pr in current}
    seen = _seen(snapshot)
    for ref, new in seen.items():
        old = current_by_ref.get(ref)
        if ref in closed:
            _plan_reopened(plan, new, statuses.get(ref))
        elif old is None:
            plan.upserts.append(new)
            change = _new_pr_change(new, ref.repo in first_sync, ref.repo in watched)
            plan.report(ref, change, new.title)
        else:
            _plan_found_again(plan, old, new, statuses.get(ref), watched=ref.repo in watched)
    me = snapshot.viewer or ""
    for ref, old in current_by_ref.items():
        if ref not in seen and _covered(ref, snapshot, watched):
            _plan_gone(plan, old, statuses.get(ref), me=me, watched=ref.repo in watched)
    return plan


def _plan_reopened(plan: SyncPlan, new: PR, status: PRStatus | None) -> None:
    """A PR the store has as merged or closed, listed again: open again only if its status
    says so (reopened), its head as the status has it. Not news."""
    if status is not None and status.state == GHState.OPEN:
        plan.upserts.append(replace(new, head_sha=status.head_sha))


def _plan_found_again(
    plan: SyncPlan, old: PR, new: PR, status: PRStatus | None, *, watched: bool
) -> None:
    """A PR in the store that the searches found again: what's new about it, to store and tell.
    Its ``status``, if it was looked up, has its head right."""
    new = replace(new, involved=new.involved or old.involved)  # it sticks
    if status:
        new = replace(new, head_sha=status.head_sha)
    if not _same_metadata(old, new):
        plan.upserts.append(new)
    plan.report(new.ref, _assignment_change(old, new, watched), new.title)
    if new.requested_from_me and not old.requested_from_me and not new.assigned_to_me:
        plan.report(new.ref, PRChange.REQUESTED, new.title)
    if new.head_sha != old.head_sha and old.state != PRState.NOT_REVIEWED:
        plan.report(new.ref, PRChange.NEW_COMMITS, new.title)


def _plan_gone(plan: SyncPlan, old: PR, status: PRStatus | None, *, me: str, watched: bool) -> None:
    """A PR in the store that dropped out of every search, for sure.

    Unreviewed, it goes. Reviewed, it stays: closed or merged if its ``status``
    says so, or no longer yours if it was assigned to you.
    """
    if old.state == PRState.NOT_REVIEWED:
        plan.deletes.append(old.ref)
        plan.report(old.ref, _gone_change(old, watched), old.title)
        return
    if status is None:
        return  # couldn't look it up; try again next poll
    if status.state != GHState.OPEN:
        plan.gh_states[old.ref] = status.state
        change = PRChange.MERGED if status.state == GHState.MERGED else PRChange.CLOSED
        plan.report(old.ref, change, old.title)
    elif old.assigned_to_me:
        others = tuple(a for a in old.assignees if a.lower() != me.lower())
        plan.upserts.append(replace(old, assigned_to_me=False, assignees=others))
        plan.report(old.ref, PRChange.UNASSIGNED, old.title)
