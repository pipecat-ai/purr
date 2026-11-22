"""What's news, and how it's said: the same for alerts, the activity log and recaps."""

from purr.models import Activity, PRChange
from purr.news import _CHANGES, NEWS_KINDS, is_notable, news, recap_text


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


def test_recaps():
    def activity(kind: str, **data) -> Activity:
        return Activity(id=1, at="t", kind=kind, pr=None, data=data)

    replied = activity("pr.changed", change="replied", by="alice")
    assert recap_text(replied) == "alice replied to the user" and is_notable(replied)
    unpooled = activity("pr.changed", change="unpooled")
    assert not is_notable(unpooled)
    unanswered = activity("session.permission_resolved", answered=False, description="run tests")
    assert is_notable(unanswered)
    assert recap_text(unanswered) == "nobody answered about: run tests; agent carried on"
    assert not is_notable(activity("session.permission_resolved", answered=True))
    assert not is_notable(activity("review.progress"))
