"""What's news, and how it's said: the same for live alerts and from the activity log."""

from purr.models import PRChange
from purr.news import _CHANGES, NEWS_KINDS, news


def changed(change: str, **data) -> dict:
    return {"change": change, "title": "Fix the bus", **data}


def test_every_kind_of_change_is_told():
    # A new kind of change without its entry would read as its raw name.
    assert set(_CHANGES) == set(PRChange)


def test_news():
    assert news("pr.changed", changed("assigned")) == "assigned to you"
    assert news("pr.changed", changed("requested")) == "asks for your review"
    assert news("pr.changed", changed("replied", by="alice, bob")) == "alice, bob replied"
    assert news("pr.changed", changed("new_commits")) is None  # in the log, not news
    assert news("pr.changed", changed("someday")) is None  # from a newer Purr
    issues = [{"severity": "issue"}, {"severity": "nit"}, {"severity": "issue"}]
    assert news("review.finished", {"findings": issues}) == "review: found 2 issues"
    assert news("review.finished", {"findings": [{"severity": "nit"}]}) == "review: nothing found"
    assert news("review.failed", {"error": "boom"}) == "review failed"
    assert news("session.permission_request", {}) == "needs your OK"
    assert news("action.pending", {}) is None
    assert set(NEWS_KINDS) == {
        "pr.changed",
        "review.finished",
        "review.failed",
        "session.permission_request",
    }
