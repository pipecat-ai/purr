"""Scrolling with keys: a page, or the top or the end, glides there line by line, the way
the mouse wheel goes; a line or two moves at once. The next key goes on from where the
last one was going, not from where its glide has got to, so a held key never falls
behind."""

from textual.widget import Widget

GLIDE_SECONDS = 0.15  # however far: long enough to see it go, too short to wait for
GLIDE_EASING = "out_cubic"  # off at once, slowing down to stop
GLIDE_PAST = 2  # lines: closer than this, it's there at once


def going_to(widget: Widget) -> float:
    """Where the view is going: where an unfinished glide ends, else where it is."""
    if widget.app.animator.is_being_animated(widget, "scroll_y"):
        return widget.scroll_target_y
    return widget.scroll_y


def glide_to(widget: Widget, y: float) -> None:
    """Scroll to ``y``, gliding if it's further than a line or two. Now, not after the
    next refresh: a key right behind it goes on from where this one's going."""
    y = min(max(y, 0), widget.max_scroll_y)
    far = abs(y - going_to(widget)) > GLIDE_PAST
    widget.scroll_to(y=y, animate=far, duration=GLIDE_SECONDS, easing=GLIDE_EASING, immediate=True)


def glide_by(widget: Widget, lines: float) -> None:
    glide_to(widget, going_to(widget) + lines)


def page_lines(widget: Widget) -> int:
    """A page: the view's height, less a line, kept in view for context."""
    return max(widget.scrollable_content_region.height - 1, 1)
