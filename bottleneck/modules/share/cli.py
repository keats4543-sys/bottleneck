"""`share`, `goal` and `note`: the commands this module adds.

All three are thin. The rule that matters lives in board.py - a group is the
sharing boundary, so a head in no group has nothing to read and nobody to tell -
and these only ever say so out loud.

Reaching out to the core is what a module does; being reached into is what it
does not. The imports below are all one way.
"""
import os
import sys
import time

from bottleneck import config
from bottleneck.heads import collect
from bottleneck.store import by_slot, group_ids, group_label, queue_load
from bottleneck.tmuxio import dash_pane

from . import board


def head_here(heads):
    """The head whose pane this command is running in, if any.

    A head running `bottleneck note` is inside its own pane, so it does not
    have to know its session id - which it has no reliable way of learning
    anyway. The pane is the identity, the same way it is for the go-on key.
    """
    pane = os.environ.get("TMUX_PANE", "")
    return next((h for h in heads if h.get("pane_id") == pane), None) if pane \
        else None


def pick(heads, key):
    """A head by number, name or the front of its session id."""
    got = by_slot(heads, key) if key.isdigit() else None
    return got or next((h for h in heads if key in (h["name"], h["session_id"])
                        or h["session_id"].startswith(key)), None)


def share_cmd(cmd, rest):
    if cmd == "share" and rest and rest[0] in ("clear", "reset"):
        for path in (board.BOARD, board.BOARD + ".lock"):
            try:
                os.remove(path)
            except OSError:
                pass
        print("boards forgotten - groups, goals and queues all start again")
        return 0

    heads = collect()
    book = board.board_load()

    if cmd == "note":
        me = head_here(heads)
        text = " ".join(rest)
        if not text:
            print("usage: bottleneck note <what you are doing>", file=sys.stderr)
            return 2
        if not me:
            print("run this inside a head's own pane - it is how the note "
                  "knows whose it is", file=sys.stderr)
            return 1
        if not me["group"]:
            print(f"{me['name']} is in no group, so there is nobody to tell "
                  f"(`bottleneck group {me['slot']} 1` puts it in one)",
                  file=sys.stderr)
            return 1
        board.note(me["session_id"], text, me.get("cwd") or "")
        print(f"posted to {group_label(queue_load(), me['group'])}")
        return 0

    if cmd == "goal":
        target, text = None, ""
        if rest:
            target = pick(heads, rest[0]) if len(rest) > 1 else None
            text = " ".join(rest[1:] if target else rest)
        if rest and not target:
            target = head_here(heads)
            if not target:
                print("no such head, and this is not a head's pane - "
                      "`bottleneck goal <n> <what it is for>`", file=sys.stderr)
                return 1
        if not target:                       # no arguments: report, don't set
            for h in heads:
                rec = book["who"].get(h["session_id"]) or {}
                print(f"{h['slot']:>2}  {h['name']:<16} "
                      f"{rec.get('goal') or '-'}")
            return 0
        board.set_goal(target["session_id"], text, target.get("cwd") or "")
        if not target["group"]:
            print(f"{target['name']}: {text}\n(in no group - nobody is being "
                  f"told yet; `bottleneck group {target['slot']} 1` fixes that)")
        else:
            print(f"{target['name']} -> {text}")
        return 0

    # `share`, with no argument: every board, in the dashboard's own order.
    queue = queue_load()
    if board.SOURCE == "dashboard":
        # Worth saying out loud, because it is the one arrangement where the
        # boards can be silently out of date: no dashboard, no file changes
        # noticed, and nothing else on the screen would tell you.
        print("file changes: read from transcripts by the dashboard"
              + ("" if dash_pane() else "  - NO DASHBOARD IS RUNNING, so "
                                        "nothing is being noticed"))
    order = [g for g in group_ids(queue) if g]
    if not order:
        print("no groups yet - heads share nothing until they are in one")
        return 0
    now = time.time()
    for gid in order:
        members = [h for h in heads if h["group"] == gid]
        print(f"[{gid}] {group_label(queue, gid)}"
              f"  {len(members)} head{'' if len(members) == 1 else 's'}")
        # The board outlives the heads on it by design - a goal is worth
        # reading after the head that said it has gone - so a session it knows
        # about that is not running is listed too, and said to be gone rather
        # than quietly left out of a list of who is here.
        live = {h["session_id"] for h in members}
        gone = [(sid, rec) for sid, rec in book["who"].items()
                if (rec or {}).get("group") == gid and sid not in live]
        for h in members:
            rec = book["who"].get(h["session_id"]) or {}
            waiting, _ = board.unread(book, h["session_id"])
            print(f"    {h['name']:<16} {rec.get('goal') or '(no goal said)'}"
                  + (f"   [{len(waiting)} unread]" if waiting else ""))
        for sid, rec in gone:
            print(f"    {(rec.get('name') or sid[:8]):<16} "
                  f"{rec.get('goal') or '(no goal said)'}   (not running)")
        msgs = [m for m in book["msgs"].get(gid) or [] if isinstance(m, dict)]
        for m in msgs[-8:]:
            print(board.line_of(m, config.DEFAULT_DIR, now))
        if msgs:
            print(f"    ({len(msgs)} of {board.MAX} kept)")
    return 0
