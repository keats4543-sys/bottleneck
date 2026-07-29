"""What the heads in a group tell each other, and when they are told it.

The dashboard solves your half of the problem: which head wants you. This is
the heads' half. Six heads in one repository cannot see each other at all - two
of them rewrite the same file, a third reads a file a fourth has just deleted,
and none of them ever finds out, because the only thing they share is a
filesystem and neither one is watching it.

So each group keeps a small board. A head writes a line to it when it does
something a sibling would want to know - it took a goal, it wrote a file, it
deleted one - and reads what its siblings have written at the start of its own
turn. The board is per group because a group is already the thing that says
"these heads are on the same work"; heads outside a group share nothing, which
is what keeps this quiet by default.

Three rules make it liveable in a context window:

  - **N messages, and no more.** The board holds the last N per group and drops
    the oldest. A head that has been busy for an hour gets the recent past, not
    an hour of it, and the injection has a ceiling you can reason about.
  - **Each head has its own place in the queue.** The board is not flushed when
    it is read; the reader's cursor moves. Two siblings reading at different
    moments both get everything they have not seen, once.
  - **Flushed on an event, not on a timer.** The queue builds while a head
    works and is handed over whole at the next flushable moment - the head's
    next prompt, its next start, or a file clash that has to be said now.

Nothing here imports the rest of the package. It runs inside the hook, on every
tool call of every head, and the import cost of config.py - a config file read
and, on WSL, a glob over a Windows mount - is not a thing to pay per Edit. It
reads bottleneck's own queue.json and catalog.json directly instead, as files.
"""
import fcntl
import json
import os
import time


STATE = os.path.expanduser(os.environ.get("BOTTLENECK_STATE") or "~/.bottleneck")

SHARE = os.path.join(STATE, "share")

# One file, one lock. Every writer here is a hook holding up a tool call, so
# the work has to be small and the failure has to be nothing: a board that
# cannot be written is a fact nobody hears, never an error a head sees.
BOARD = os.path.join(SHARE, "board.json")

# Bottleneck's own books, read-only from here. The group is not ours to decide -
# it is the one you assigned in the dashboard - and the name is the one the
# catalogue already knows, because `brisk-finch wrote cli.py` is a sentence and
# `a3f9e1c2 wrote cli.py` is a lookup.
QUEUE = os.path.join(STATE, "queue.json")

CATALOG = os.path.join(STATE, "catalog.json")


def _flag(name, default):
    return os.environ.get(name, default).strip().lower() \
        not in ("0", "no", "false", "off")


# The whole thing off, for a machine where you want the dashboard and not this.
ENABLED = _flag("BOTTLENECK_SHARE", "1")


# Who notices that a file changed, and it is a trade rather than a preference.
#
# `hook` is a PostToolUse hook: it is told, by the head itself, the instant it
# happens, whether or not anything else is running. It costs a python start on
# every Read, Write, Edit and Bash that head makes - about 40ms, nearly all of
# it startup, because the work itself is a lock and a small file.
#
# `dashboard` reads the same facts out of the transcripts, in the process that
# is already standing and already reading them every couple of seconds. The
# per-tool-call cost goes to nothing. What you give up is that the board is only
# as current as the last pass, and only kept at all while a dashboard is up.
#
# The guard still has to be a hook either way: a warning that arrives before you
# edit a file is a reply to a question, and only a hook is asked.
SOURCE = (os.environ.get("BOTTLENECK_SHARE_SOURCE", "hook").strip().lower()
          or "hook")
if SOURCE not in ("hook", "dashboard"):
    SOURCE = "hook"


# How many messages a group's board holds. This is the ceiling on what one
# injection can cost you: N lines, each about as long as a path.
def _int(name, default):
    try:
        return max(1, int(os.environ.get(name, "") or default))
    except ValueError:
        return default


MAX = _int("BOTTLENECK_SHARE_MAX", 12)


# What happens when you are about to edit a file a sibling has changed under
# you. `warn` says so in the tool's own context and gets out of the way; `ask`
# turns it into a permission prompt, which stops the head dead until you answer
# - right for a fleet racing over one file, wrong for most days. `off` keeps the
# board and drops the guard.
GUARD = (os.environ.get("BOTTLENECK_SHARE_GUARD", "warn").strip().lower()
         or "warn")


# How long a file change stays worth warning about. A sibling's write from this
# morning is history, not a clash - by now either you have read the file since
# or you are working somewhere else entirely.
GUARD_TTL = _int("BOTTLENECK_SHARE_GUARD_TTL", 6 * 3600)


# A head that writes the same file eight times in a turn is doing one thing, not
# eight. Repeats inside this window bump the message that is already there.
COALESCE = 120


# Sessions that have not been heard from in this long stop being listed as
# siblings. They are not necessarily dead - but a goal from four days ago is not
# what the head beside you is doing now.
PEER_TTL = _int("BOTTLENECK_SHARE_PEER_TTL", 6 * 3600)


# Bounds on the parts that would otherwise grow forever.
FILES_MAX = 400

READS_MAX = 200

GOAL_CHARS = 140

TEXT_CHARS = 200


KINDS = ("goal", "joined", "left", "wrote", "edited", "deleted", "note")


# ------------------------------------------------- what a tool call means
#
# Which tools count as reading a file and which as changing one, and how to get
# the file out of one. This lives here rather than in the hook because the hook
# is no longer the only reader: a dashboard deriving the same facts from a
# transcript is looking at the same tool names and the same inputs, and two
# copies of these rules would be two things to keep in step.

READERS = ("Read", "NotebookRead")

WRITERS = {"Write": "wrote", "Edit": "edited", "MultiEdit": "edited",
           "NotebookEdit": "edited", "Update": "edited"}


def paths_of(tool, inp):
    """Every path a tool call is about. Empty for tools that are not about one."""
    inp = inp if isinstance(inp, dict) else {}
    out = []
    for key in ("file_path", "notebook_path", "path"):
        val = inp.get(key)
        if isinstance(val, str) and val.strip():
            out.append(val.strip())
    edits = inp.get("edits")
    if isinstance(edits, list):
        for e in edits[:20]:
            if isinstance(e, dict) and isinstance(e.get("file_path"), str):
                out.append(e["file_path"])
    return out


def shell_targets(command):
    """What a shell command removed or moved, as well as this can be told.

    Best effort on purpose. A head that deletes a file with `rm` is doing the
    thing this whole feature exists to catch, and refusing to look at Bash
    because the general case is undecidable would miss the common case
    entirely. Only `rm` and `mv` are read, only their plain arguments, and the
    caller checks the filesystem afterwards - so the worst a misparse can do is
    fail to notice, never invent a change that did not happen.

    shlex is imported here rather than at the top of the file because it costs
    about as much to import as the whole hook - it pulls in `re` - and one tool
    call in five is a Bash. Every Read and every Edit was paying for it.
    """
    import shlex
    try:
        words = shlex.split(command or "", comments=True)
    except ValueError:
        return []
    out, taking = [], False
    for word in words[:200]:
        base = os.path.basename(word)
        if base in ("rm", "unlink", "mv"):
            taking = True
            continue
        if word in ("&&", "||", ";", "|"):
            taking = False
            continue
        if not taking or word.startswith("-"):
            continue
        out.append(word)
    return out[:10]


def touches(tool, inp, cwd):
    """What one tool call did to the working tree, as (kind, path) pairs.

    The filesystem settles what the tool name only suggests: a Write to a path
    that is not there afterwards is a deletion whatever it was called, and the
    `rm` in a shell command is only believed if the file has actually gone.
    """
    out = []
    for p in paths_of(tool, inp):
        full = p if os.path.isabs(p) else os.path.join(cwd or "", p)
        if tool in WRITERS:
            out.append((WRITERS[tool] if os.path.exists(full) else "deleted",
                        full))
        elif tool in READERS:
            out.append(("read", full))
    if tool == "Bash":
        for p in shell_targets(str((inp or {}).get("command") or "")):
            full = p if os.path.isabs(p) else os.path.join(cwd or "", p)
            if not os.path.exists(full):
                out.append(("deleted", full))
    return out


# --------------------------------------------------------------- the files

def read_json(path, fallback):
    """Read a state file. A corrupt one reads as absent, never as a crash."""
    try:
        with open(path) as fh:
            got = json.load(fh)
    except (OSError, ValueError):
        return fallback
    return got if type(got) is type(fallback) else fallback


def write_json(path, value):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w") as fh:
            json.dump(value, fh)
        os.replace(tmp, path)
    except OSError:
        pass


def board_load():
    """The board, with every part it is supposed to have.

    Written defensively on purpose: this is read by a hook in the middle of
    somebody's tool call, and a board half-written by an older version of this
    file must degrade to an empty one rather than take the head down with it.
    """
    got = read_json(BOARD, {})
    out = {
        "seq": 0,
        "msgs": {},     # group id -> the last MAX messages, oldest first
        "who": {},      # session id -> goal, group, cwd, name, last seen
        "cursor": {},   # session id -> the last seq it has been handed
        "files": {},    # path -> who last changed it, how, when
        "reads": {},    # session id -> path -> when it last read or wrote it
        "scan": {},     # session id -> how far into its transcript we have read
    }
    if not isinstance(got, dict):
        return out
    try:
        out["seq"] = int(got.get("seq") or 0)
    except (TypeError, ValueError):
        pass
    for key in ("msgs", "who", "cursor", "files", "reads", "scan"):
        val = got.get(key)
        if isinstance(val, dict):
            out[key] = val
    return out


def update(change):
    """Read, change and write the board, with nobody else in between.

    The same lock store.py takes for the queue, for the same reason: two heads
    finishing an Edit in the same instant would otherwise each write back a
    board that never heard of the other, and the message that went missing is
    exactly the one about the file they are both in.
    """
    if not ENABLED:
        return board_load()
    lock = None
    try:
        os.makedirs(SHARE, exist_ok=True)
        lock = open(BOARD + ".lock", "a+")
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
    except OSError:
        if lock:
            lock.close()
        lock = None
    try:
        board = board_load()
        out = change(board)
        write_json(BOARD, board)
        return out
    finally:
        if lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            lock.close()


def safe_sid(sid):
    """The same rule the attention hook applies: an id we would name a file."""
    sid = sid or ""
    if not sid or "/" in sid or "\\" in sid or ":" in sid or sid.startswith("."):
        return ""
    return sid


def group_of(sid):
    """Which group this head is in, according to the dashboard. "" is none."""
    of = read_json(QUEUE, {}).get("of")
    if not isinstance(of, dict):
        return ""
    return str(of.get(sid) or "")


def group_name(gid):
    names = read_json(QUEUE, {}).get("names")
    if isinstance(names, dict) and names.get(gid):
        return str(names[gid])
    return f"group {gid}"


def head_name(sid):
    """What the catalogue calls this head, else the front of its id."""
    rec = read_json(CATALOG, {}).get(sid)
    if isinstance(rec, dict) and rec.get("name"):
        return str(rec["name"])
    return (sid or "?")[:8]


# ------------------------------------------------------------- writing to it

def _trim(board, now):
    """Keep the board the size it promised to be."""
    for gid, msgs in list(board["msgs"].items()):
        if not isinstance(msgs, list) or not msgs:
            board["msgs"].pop(gid, None)
            continue
        board["msgs"][gid] = msgs[-MAX:]

    files = {p: r for p, r in board["files"].items() if isinstance(r, dict)}
    if len(files) > FILES_MAX:
        keep = sorted(files.items(), key=lambda kv: float(kv[1].get("at") or 0))
        files = dict(keep[-FILES_MAX:])
    board["files"] = files

    for sid, seen in list(board["reads"].items()):
        if not isinstance(seen, dict):
            board["reads"].pop(sid, None)
            continue
        if len(seen) > READS_MAX:
            keep = sorted(seen.items(), key=lambda kv: float(kv[1] or 0))
            board["reads"][sid] = dict(keep[-READS_MAX:])

    # A head nobody has heard from in a week is not a sibling, and its cursor is
    # not a place in any queue.
    stale = [sid for sid, rec in board["who"].items()
             if not isinstance(rec, dict)
             or now - float(rec.get("seen") or 0) > 7 * 86400]
    for sid in stale:
        board["who"].pop(sid, None)
        board["reads"].pop(sid, None)
    for sid in list(board["cursor"]):
        if sid not in board["who"]:
            board["cursor"].pop(sid, None)


def _post(board, sid, gid, kind, text="", path="", now=None):
    """Put one message on a group's board, or bump the one already there."""
    now = time.time() if now is None else now
    if not gid or kind not in KINDS:
        return None
    msgs = board["msgs"].setdefault(gid, [])
    if not isinstance(msgs, list):
        msgs = board["msgs"][gid] = []
    for old in reversed(msgs[-4:]):
        # Eight edits to one file in one turn is one thing happening. The
        # message keeps its place in the queue and takes the newer time, so a
        # sibling reads "wrote cli.py, a minute ago" and not eight of them.
        if (isinstance(old, dict) and old.get("sid") == sid
                and old.get("kind") == kind and old.get("path") == path
                and now - float(old.get("at") or 0) < COALESCE):
            old["at"] = now
            old["text"] = text or old.get("text") or ""
            return old
    board["seq"] = int(board["seq"]) + 1
    msg = {"seq": board["seq"], "at": now, "sid": sid, "kind": kind,
           "text": (text or "")[:TEXT_CHARS], "path": path}
    msgs.append(msg)
    return msg


def _seen(board, sid, gid, cwd, now):
    """This head exists, is in this group, is here, and was heard from now.

    Every write goes through here, so a head that is grouped from the dashboard
    halfway through a turn is in its group by its next tool call rather than by
    its next prompt - which is the difference between a sibling being warned
    about the file it is in and being warned about it afterwards. Returns the
    group it used to be in, which is how joining is noticed.
    """
    rec = board["who"].get(sid)
    rec = rec if isinstance(rec, dict) else {}
    was = rec.get("group") or ""
    rec.update({"group": gid, "seen": now,
                "cwd": cwd or rec.get("cwd") or "",
                "name": rec.get("name") or head_name(sid)})
    rec.setdefault("goal", "")
    rec.setdefault("at", now)
    board["who"][sid] = rec
    return was


def register(sid, cwd="", now=None):
    """Note that this head exists, where it is, and which group it is in.

    Called when a head starts and at every prompt, because all three of those
    can change under us: a head is grouped from the dashboard long after it
    started, and `cd` moves the second half of a session somewhere else.
    """
    sid = safe_sid(sid)
    if not sid or not ENABLED:
        return {}
    now = time.time() if now is None else now
    gid = group_of(sid)

    def change(board):
        was = _seen(board, sid, gid, cwd, now)
        rec = board["who"][sid]
        if gid and gid != was:
            # Joining is worth a line - it is how a sibling learns there is
            # someone else in here at all - and it carries the goal, if one has
            # been said, so the news and the context arrive together.
            _post(board, sid, gid, "joined", rec.get("goal") or "", now=now)
        _trim(board, now)
        return rec

    return update(change)


def set_goal(sid, text, cwd="", now=None):
    """Say what this head is for. Siblings are told; a repeat is not news."""
    sid = safe_sid(sid)
    text = " ".join((text or "").split())[:GOAL_CHARS]
    if not sid or not text or not ENABLED:
        return ""
    now = time.time() if now is None else now
    gid = group_of(sid)

    def change(board):
        said = (board["who"].get(sid) or {}).get("goal") \
            if isinstance(board["who"].get(sid), dict) else ""
        _seen(board, sid, gid, cwd, now)
        if said == text:
            return text
        board["who"][sid]["goal"] = text
        _post(board, sid, gid, "goal", text, now=now)
        _trim(board, now)
        return text

    return update(change)


def goal_of(sid):
    rec = board_load()["who"].get(safe_sid(sid))
    return (rec or {}).get("goal") or "" if isinstance(rec, dict) else ""


def note_touch(sid, path, action, cwd="", now=None):
    """A head changed a file. Tell the group, and remember who did it.

    Two records, not one. The message is news, and ages out of the queue like
    any other news. The ledger entry is the standing fact - "cli.py was last
    changed by brisk-finch, at 14:02" - and it is what the guard reads when
    somebody else is about to open that file.
    """
    sid = safe_sid(sid)
    path = os.path.abspath(os.path.expanduser(path or ""))
    if not sid or not path or action not in ("wrote", "edited", "deleted"):
        return None
    if not ENABLED:
        return None
    now = time.time() if now is None else now
    gid = group_of(sid)
    if not gid:
        return None

    def change(board):
        board["files"][path] = {"sid": sid, "action": action, "at": now,
                                "group": gid}
        # We just changed it, so we have seen it: this is what stops a head
        # being warned about its own writes on the next pass.
        board["reads"].setdefault(sid, {})[path] = now
        msg = _post(board, sid, gid, action, path=path, now=now)
        _seen(board, sid, gid, cwd, now)
        _trim(board, now)
        return msg

    return update(change)


def note_read(sid, path, now=None):
    """This head has now seen this file as it currently stands.

    Cheap and worth it: it is the difference between "somebody else changed
    this" - true of half the repo by teatime - and "somebody else changed this
    since you last looked", which is the only version worth interrupting for.
    """
    sid = safe_sid(sid)
    path = os.path.abspath(os.path.expanduser(path or ""))
    if not sid or not path or not ENABLED:
        return False
    now = time.time() if now is None else now
    gid = group_of(sid)

    def change(board):
        board["reads"].setdefault(sid, {})[path] = now
        # A head reading for an hour is a head that is still here. Without
        # this, only prompts and writes kept it on the sibling list, and one
        # deep in a long read stopped being one.
        _seen(board, sid, gid, "", now)
        _trim(board, now)
        return True

    return update(change)


def record(events, scans=None, now=None):
    """A batch of facts, applied in one write. What a dashboard pass uses.

    The hook writes one thing at a time because one is all it ever knows. A
    pass of the dashboard knows everything six heads did in the last two
    seconds, and taking the lock once per fact would be six locks and six
    rewrites to answer one question.

    An event is {sid, gid, cwd, kind, path}, where kind is wrote, edited,
    deleted or read - already decided by whoever read the transcript, because
    what a transcript means is not this file's business. `scans` is how far
    into each head's transcript the reader got, written down here so that a
    dashboard restarting does not have to choose between replaying a day of
    history and forgetting there was any.
    """
    if not ENABLED:
        return 0
    now = time.time() if now is None else now
    events = [e for e in events or [] if safe_sid(e.get("sid")) and e.get("path")]

    def change(board):
        n = 0
        for e in events:
            sid, gid = e["sid"], str(e.get("gid") or "")
            path = os.path.abspath(os.path.expanduser(e["path"]))
            at = float(e.get("at") or now)
            kind = e.get("kind")
            if kind == "read":
                board["reads"].setdefault(sid, {})[path] = at
                _seen(board, sid, gid, e.get("cwd") or "", at)
                continue
            if kind not in ("wrote", "edited", "deleted") or not gid:
                continue
            board["files"][path] = {"sid": sid, "action": kind, "at": at,
                                    "group": gid}
            board["reads"].setdefault(sid, {})[path] = at
            _post(board, sid, gid, kind, path=path, now=at)
            _seen(board, sid, gid, e.get("cwd") or "", at)
            n += 1
        for sid, where in (scans or {}).items():
            if safe_sid(sid):
                board["scan"][sid] = {"at": int(where), "when": now}
        # A place in a transcript nobody has read for a week is a place we
        # would not resume from anyway - see CATCHUP_MAX in harvest.py - so it
        # is a name kept for nothing. It ages out rather than being tied to the
        # who list: a head is written down here before it has said anything at
        # all, which is exactly the pass that has nothing to put in `who`.
        for sid, rec in list(board["scan"].items()):
            if not isinstance(rec, dict) \
                    or now - float(rec.get("when") or 0) > 7 * 86400:
                board["scan"].pop(sid, None)
        _trim(board, now)
        return n

    return update(change)


def scan_at(board, sid):
    """How far into this head's transcript the last reader got, or None."""
    rec = board["scan"].get(safe_sid(sid))
    if not isinstance(rec, dict):
        return None
    try:
        return int(rec.get("at"))
    except (TypeError, ValueError):
        return None


def note(sid, text, cwd="", now=None):
    """A line a head puts on the board by hand. The one free-form kind."""
    sid = safe_sid(sid)
    text = " ".join((text or "").split())
    if not sid or not text or not ENABLED:
        return ""
    now = time.time() if now is None else now
    gid = group_of(sid)
    if not gid:
        return ""
    update(lambda board: _post(board, sid, gid, "note", text, now=now))
    return text


def leave(sid, now=None):
    """The head has ended. Say so once, and stop listing it as a sibling."""
    sid = safe_sid(sid)
    if not sid or not ENABLED:
        return False
    now = time.time() if now is None else now

    def change(board):
        rec = board["who"].get(sid)
        if not isinstance(rec, dict):
            return False
        gid = rec.get("group") or ""
        _post(board, sid, gid, "left", rec.get("goal") or "", now=now)
        board["who"].pop(sid, None)
        board["reads"].pop(sid, None)
        board["cursor"].pop(sid, None)
        _trim(board, now)
        return True

    return update(change)


# ------------------------------------------------------------- reading from it

def peers(board, sid, now=None):
    """The other heads in this head's group, most recently heard from first."""
    now = time.time() if now is None else now
    me = board["who"].get(sid)
    gid = (me or {}).get("group") or ""
    if not gid:
        return []
    out = []
    for other, rec in board["who"].items():
        if other == sid or not isinstance(rec, dict):
            continue
        if (rec.get("group") or "") != gid:
            continue
        if now - float(rec.get("seen") or 0) > PEER_TTL:
            continue
        out.append(dict(rec, session_id=other))
    return sorted(out, key=lambda r: -float(r.get("seen") or 0))


def unread(board, sid):
    """What this head has not been handed yet, and how much it missed.

    `missed` is worked out rather than counted: the board knows the seq of the
    oldest message it still holds, and anything between the reader's cursor and
    that fell off the end while the head was busy. Saying so matters - a queue
    that silently drops is a queue you cannot trust the absence of a line in.
    """
    rec = board["who"].get(sid)
    gid = (rec or {}).get("group") or "" if isinstance(rec, dict) else ""
    if not gid:
        return [], 0
    msgs = [m for m in board["msgs"].get(gid) or [] if isinstance(m, dict)]
    if not msgs:
        return [], 0
    try:
        seen = int(board["cursor"].get(sid) or 0)
    except (TypeError, ValueError):
        seen = 0
    mine = [m for m in msgs
            if int(m.get("seq") or 0) > seen and m.get("sid") != sid]
    oldest = min(int(m.get("seq") or 0) for m in msgs)
    missed = max(0, oldest - seen - 1) if seen else 0
    return mine, missed


def flush(sid, now=None):
    """Hand this head its queue and move its cursor past it.

    The queue is not emptied - each head has its own cursor, so a sibling that
    has not read yet still gets everything. The board itself only ever forgets
    by being full.
    """
    sid = safe_sid(sid)
    if not sid or not ENABLED:
        return [], 0
    now = time.time() if now is None else now
    got = {}

    def change(board):
        msgs, missed = unread(board, sid)
        got["msgs"], got["missed"] = msgs, missed
        top = board["seq"]
        rec = board["who"].get(sid)
        if isinstance(rec, dict):
            rec["seen"] = now
        board["cursor"][sid] = top
        _trim(board, now)
        return None

    update(change)
    return got.get("msgs") or [], got.get("missed") or 0


def clash(sid, path, now=None):
    """Has somebody else changed this file since this head last looked?

    Returns a sentence, or "" when there is nothing to say. Three things have
    to hold: another session did it, it is recent enough to still be a race
    rather than history, and this head has not read the file since. The last
    one is what keeps the guard quiet - a head that has just read the file is
    working from what is actually there.
    """
    sid = safe_sid(sid)
    path = os.path.abspath(os.path.expanduser(path or ""))
    if not sid or not path or not ENABLED or GUARD == "off":
        return ""
    now = time.time() if now is None else now
    board = board_load()
    rec = board["files"].get(path)
    if not isinstance(rec, dict) or rec.get("sid") == sid:
        return ""
    at = float(rec.get("at") or 0)
    if now - at > GUARD_TTL:
        return ""
    mine = board["reads"].get(sid)
    if isinstance(mine, dict) and float(mine.get(path) or 0) >= at:
        return ""
    who = head_name(rec.get("sid") or "")
    action = str(rec.get("action") or "changed")
    if action == "deleted":
        return (f"{who} (another head in your group) deleted {short(path)} "
                f"{ago(now - at)} - it may no longer be there to edit")
    return (f"{who} (another head in your group) {action} {short(path)} "
            f"{ago(now - at)}, after you last read it - re-read it before you "
            f"write, or you will overwrite that change")


# ------------------------------------------------------------------ formatting

def ago(secs):
    secs = max(0, int(secs))
    if secs < 45:
        return "just now"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def short(path, cwd=""):
    """A path as the reader would type it: relative to them, else from ~."""
    path = path or ""
    if cwd:
        try:
            rel = os.path.relpath(path, cwd)
            if not rel.startswith(".."):
                return rel
        except ValueError:
            pass
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path.startswith(home + os.sep) else path


def line_of(msg, cwd="", now=None):
    """One message, as a line. Time first: it is what you read them by."""
    now = time.time() if now is None else now
    kind = msg.get("kind") or "note"
    who = head_name(msg.get("sid") or "")
    when = ago(now - float(msg.get("at") or now))
    path = short(msg.get("path") or "", cwd)
    text = msg.get("text") or ""
    if kind in ("wrote", "edited", "deleted"):
        body = f"{kind} {path}"
    elif kind == "goal":
        body = f"is here to: {text}"
    elif kind == "joined":
        body = "joined the group" + (f" - {text}" if text else "")
    elif kind == "left":
        body = "ended"
    else:
        body = text
    return f"  {when:<9} {who} {body}"


def brief(sid, cwd="", now=None, extra="", standing=True):
    """The whole injection: who else is here, and what they have done.

    One block, tagged, so a head can tell at a glance that this is bottleneck
    talking and not you. Empty when there is nothing to say - a group of one
    with a quiet board costs the context window nothing at all.

    `standing` is the difference between an introduction and an update. An
    introduction says who is here and how this works, and is what a head wants
    when it starts or comes back from a compaction knowing none of it. An
    update is only what has happened since it was last told.

    They were the same thing, and the standing half was going into every turn:
    measured at 328 identical characters a turn for a group of three, ~66k over
    a 200-turn session, re-read by the model every time. The roster is not lost
    by leaving it out either - joining, leaving and saying what you are for are
    all news, so a head that was told once has been kept up to date since.
    """
    sid = safe_sid(sid)
    if not sid or not ENABLED:
        return ""
    now = time.time() if now is None else now
    board = board_load()
    rec = board["who"].get(sid)
    gid = (rec or {}).get("group") or "" if isinstance(rec, dict) else ""
    if not gid:
        return ""
    cwd = cwd or (rec or {}).get("cwd") or ""
    others = peers(board, sid, now)
    msgs, missed = flush(sid, now)
    if not others and not msgs and not extra:
        return ""
    if not standing and not msgs and not extra:
        return ""                       # nothing has happened; say nothing

    out = [f"<bottleneck-group name=\"{group_name(gid)}\">"]
    if extra:
        out.append(extra)
    if standing and others:
        out.append(f"{len(others)} other head shares this group:" if
                   len(others) == 1 else
                   f"{len(others)} other heads share this group:")
        for peer in others:
            goal = peer.get("goal") or "(has not said what it is for)"
            where = short(peer.get("cwd") or "", cwd) or "?"
            out.append(f"  {peer.get('name') or peer['session_id'][:8]}"
                       f"  in {'this same directory' if where == '.' else where}"
                       f"  - {goal}")
    elif standing:
        out.append("no other head is in this group right now.")
    if msgs:
        out.append(f"{len(msgs)} thing{'' if len(msgs) == 1 else 's'} happened "
                   f"since you were last told"
                   + (f" ({missed} older dropped - the board keeps {MAX})"
                      if missed else "") + ":")
        out.extend(line_of(m, cwd, now) for m in msgs)
        if any(m.get("kind") in ("wrote", "edited", "deleted") for m in msgs):
            out.append("Re-read any of those files before you edit them - what "
                       "you have in context is from before the change.")
    if standing:
        out.append("These heads share your working tree. Say what you are "
                   "doing with `bottleneck note`, and keep off files another "
                   "head is in.")
    out.append("</bottleneck-group>")
    return "\n".join(out)
