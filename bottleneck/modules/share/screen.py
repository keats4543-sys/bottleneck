"""What the boards look like from the dashboard.

Everything the module knew was written for one reader - the head itself, told
at the top of a turn what its siblings had been doing. None of it reached the
person watching the list, who is the one deciding which head to open next and
had no way to see that two of them were about to edit the same file.

So this is the same boards, answered as rows: a badge where the count belongs,
the head's goal under its name, and what the group has been touching under the
group's heading. It adds nothing to what is recorded - every question here is
asked of a board that was written anyway.
"""
import time

from . import board


# The board is one file, and it is read once a frame rather than once a head.
# mark() is called for every row, and a JSON load per row is a load per head
# per two seconds for a fact that is the same across the whole frame.
_at, _book = 0.0, None


def book(now=None):
    global _at, _book
    now = time.time() if now is None else now
    if _book is None or now - _at > 0.5 or now < _at:
        try:
            _book = board.board_load()
        except Exception:                             # noqa: BLE001
            _book = {"msgs": {}, "who": {}, "cursor": {}, "files": {}}
        _at = now
    return _book


def mark(head):
    """How much is waiting for this head that it has not been handed yet.

    The queue is flushed into the head's own context at the top of its next
    turn, so this is the one number you cannot otherwise see: what its siblings
    have said that it has not caught up on.
    """
    if not board.ENABLED:
        return ""
    sid = board.safe_sid(head.get("session_id") or "")
    if not sid:
        return ""
    waiting, missed = board.unread(book(), sid)
    if not waiting and not missed:
        return ""
    return f"✉{len(waiting)}" + ("+" if missed else "")


def lines(head, width):
    """What this head said it was for, under its name."""
    if not board.ENABLED:
        return []
    sid = board.safe_sid(head.get("session_id") or "")
    rec = book()["who"].get(sid) if sid else None
    goal = (rec or {}).get("goal") or "" if isinstance(rec, dict) else ""
    return [f"→ {goal}"] if goal else []


def group(gid, label, heads):
    """The last thing anybody in this group touched, under its heading.

    A file is the thing two heads collide over, so the file is what goes here.
    It is one line for the whole group because that is the scale the question
    is asked at - not "what has this head done" but "is this group standing on
    its own feet".
    """
    if not board.ENABLED or not gid:
        return []
    now = time.time()
    latest, when = None, 0.0
    for path, rec in (book().get("files") or {}).items():
        if not isinstance(rec, dict) or (rec.get("group") or "") != gid:
            continue
        at = float(rec.get("at") or 0)
        if at > when:
            latest, when = (path, rec), at
    if not latest or now - when > board.PEER_TTL:
        return []
    path, rec = latest
    who = board.head_name(rec.get("sid") or "") or "someone"
    return [f"{board.short(path)} {rec.get('action') or 'changed'} by {who} "
            f"{board.ago(now - when)}"]


def status(heads):
    """One number for the tmux bar: how much is queued across every group."""
    if not board.ENABLED:
        return ""
    got = book()
    total = 0
    for head in heads:
        sid = board.safe_sid(head.get("session_id") or "")
        if sid:
            total += len(board.unread(got, sid)[0])
    return f"✉{total}" if total else ""
