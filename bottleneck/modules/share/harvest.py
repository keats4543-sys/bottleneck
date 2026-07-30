"""Reading what the heads did to the working tree out of their transcripts.

The other way of noticing that a head wrote a file is a PostToolUse hook, and
it is the honest one: the head tells us itself, the moment it happens, whether
or not anything else is running. What it costs is a process. A hook is a command
claude spawns per event, and the work here - a lock and a small file - is about
a millisecond of the forty that spawning python takes. Every Read, every Edit,
every Bash of every head pays it.

But nothing about those facts is secret. They are already written down, by
claude, in the transcript the dashboard is already reading every couple of
seconds to say what each head is doing. So this reads them out of there
instead, in the process that is already standing, and the per-tool-call cost
goes to nothing at all.

Two things are given up for that, and both are the same thing said twice: the
board is only as current as the last pass, and only kept while a dashboard is
up. For the queue that is nothing - it is read at the start of a turn, and a
turn is not two seconds long. For the guard it is a two-second window in which
a sibling's write is not yet known, against races that take minutes to matter.
For a fleet run with no dashboard at all it is everything, which is why this is
a setting and not a replacement: see SOURCE in board.py.

Only new bytes are ever read, and a head first seen is read from its end - the
board is news from now on, not a history of what happened before anyone was
watching.
"""
import json
import os

# Reaching out to the core is what a module does. The other direction does
# not happen: nothing in bottleneck/ imports anything under modules/.
from bottleneck.transcript import read_from, transcript_for

from . import board


# How far behind we are willing to catch up. A dashboard that has been away for
# a megabyte of transcript is not a witness to what happened while it was gone,
# and replaying it would post an afternoon of file changes as though they had
# just happened. Past this, we skip to the end and say nothing.
CATCHUP_MAX = 1 << 20


# The most any one head's new bytes are parsed in one pass. A head that has just
# printed a hundred kilobytes of test output should not hold up the redraw.
PASS_MAX = 1 << 19


# Lines that cannot contain a tool call are not parsed at all. A transcript is
# mostly tool results - some of them enormous - and json.loads on a half
# megabyte of somebody's build log to discover it is not a tool call is the
# whole cost of doing this in the loop.
_WANTED = (b'"tool_use"',)


def tool_calls(lines):
    """Every (name, input) a batch of transcript lines called for.

    Errors are not filtered out. A tool call that failed is one that did not
    change the file, and the record here would be a warning that a sibling
    changed something they did not - but the alternative is holding every call
    until its result turns up in a later pass, which is a second kind of state
    to keep and lose. Instead the filesystem settles it, in board.touches: what
    matters is whether the file is there and what it is now.
    """
    out = []
    for line in lines:
        if not any(w in line for w in _WANTED):
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        msg = entry.get("message") if isinstance(entry, dict) else None
        blocks = (msg or {}).get("content") if isinstance(msg, dict) else None
        for block in blocks if isinstance(blocks, list) else []:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            name = block.get("name") or ""
            if name in board.WRITERS or name in board.READERS or name == "Bash":
                out.append((name, block.get("input")))
    return out


def harvest(heads, now=None):
    """Put what every grouped head has done since the last pass on its board.

    One read per head of the bytes it has added, and one write for the lot of
    them. Returns how many changes were recorded, which is only of interest to
    a test - the dashboard calls this for its effect and carries on drawing.
    """
    if not board.ENABLED or board.SOURCE != "dashboard":
        return 0
    book = board.board_load()
    events, scans = [], {}
    for head in heads:
        sid = board.safe_sid(head.get("session_id") or "")
        gid = head.get("group") or ""
        cwd = head.get("cwd") or ""
        if not sid or not gid:
            continue
        path = transcript_for(sid, cwd)
        if not path:
            continue
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        at = board.scan_at(book, sid)
        # First sight, a transcript that has been replaced by a shorter one, or
        # a gap too wide to be a witness to: start at the end and say nothing
        # about what is behind us.
        if at is None or at > size or size - at > CATCHUP_MAX:
            scans[sid] = size
            continue
        if at == size:
            continue
        lines, at = read_from(path, at, min(size, at + PASS_MAX))
        scans[sid] = at
        for name, inp in tool_calls(lines):
            for kind, full in board.touches(name, inp, cwd):
                events.append({"sid": sid, "gid": gid, "cwd": cwd,
                               "kind": kind, "path": full})
    if not events and not scans:
        return 0
    return board.record(events, scans, now)
