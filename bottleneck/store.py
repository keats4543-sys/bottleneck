"""The files bottleneck writes: flags, groups, holds, numbers.

All of it lives under ~/.bottleneck and none of it is precious - every reader
treats a corrupt or missing file as an empty one, because losing the queue
should never be worse than losing the answer to "which head wants me".
"""
import fcntl
import json
import os
import time

from . import config
from .config import ACKS, ATTN, AUTO_FLAG, BINDS, BURIED, CLAIMS, CYCLE
from .config import QUEUE
from .config import SLOTS


def read_json(path, fallback):
    """Read a small state file. A corrupt one reads as absent, never as a crash."""
    try:
        with open(path) as fh:
            got = json.load(fh)
    except (OSError, ValueError):
        return fallback
    return got if type(got) is type(fallback) else fallback


def write_json(path, value):
    # The scratch file is named after this process. One name shared by every
    # writer means two of them open, truncate and write the same file before
    # either renames it, and what lands is whatever the interleaving made -
    # possibly neither of the things being written.
    try:
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w") as fh:
            json.dump(value, fh)
        os.replace(tmp, path)
    except OSError:
        pass


def update_json(path, change, fallback=None):
    """Read, change and write it back, with nobody else in between.

    read_json followed by write_json is two steps, and two processes doing them
    at once lose one of the changes: both read the same book, both add
    themselves to their own copy, and whichever writes second puts back a book
    that never heard of the first.

    That is not a theoretical race. It is how a running dashboard went missing
    from dash.json - after which its pane's mark read as stale, got cleared,
    and the movement keys stood down for everyone because the count of
    dashboards no longer matched the number of dashboards.

    The lock is a file beside the one being changed, so it costs nothing to
    anyone only reading. If it cannot be taken the change still happens: a
    dashboard that cannot lock is better off registering unsafely than not
    registering at all, which would take its pane's mark down with it.
    """
    if fallback is None:
        fallback = {}
    lock = None
    try:
        lock = open(path + ".lock", "a+")
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
    except OSError:
        if lock:
            lock.close()
        lock = None
    try:
        value = change(read_json(path, fallback))
        write_json(path, value)
        return value
    finally:
        if lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            lock.close()


def safe_sid(sid):
    """A session id we are willing to turn into a filename.

    Ids come from Claude's own files and are guids, but `bottleneck clear` takes
    one straight from the command line - and a session id is only ever a name in
    ATTN or ACKS, never a path. The hook applies the same rule.

    A colon is not a path problem; it is how the dashboard names something that
    has no session id at all. A pane we have opened and are still waiting on
    rides the list under `starting:%12` (see pending() in panes.py), and every
    flag file it might otherwise leave behind would be a file about a head that
    never existed. Rejecting the shape here is what makes that structurally
    impossible rather than a rule each caller has to remember.
    """
    sid = sid or ""
    if not sid or "/" in sid or "\\" in sid or ":" in sid or sid.startswith("."):
        return ""
    return sid


# ------------------------------------------------------------------- attention

def read_attention(sid):
    try:
        with open(os.path.join(ATTN, safe_sid(sid) + ".json")) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def ack_ts(sid):
    try:
        return os.path.getmtime(os.path.join(ACKS, safe_sid(sid)))
    except OSError:
        return 0.0


def mark_seen(sid):
    sid = safe_sid(sid)
    if not sid:
        return
    try:
        os.makedirs(ACKS, exist_ok=True)
        open(os.path.join(ACKS, sid), "w").close()
    except OSError:
        pass


def clear_attention(sid):
    sid = safe_sid(sid)
    if not sid:
        return False
    removed = False
    try:
        os.remove(os.path.join(ATTN, sid + ".json"))
        removed = True
    except OSError:
        pass
    mark_seen(sid)
    return removed


# ----------------------------------------------------------------- the queue
#
# Two ways to say "this one matters more", both meant to be one keystroke.
#
# A group is a bucket of heads with a rank. Buckets are keyed by digit, so
# assigning is G then a number, and the ranking is a separate list, so you can
# reprioritise a group without renumbering the keys people have learned. The
# list sorts by group before it sorts by need, which is what makes the go-on key
# finish one group before it offers you the next: the second group's waiting
# heads simply come later in the list.
#
# A hold is the other direction. A finished head outranks a working one, which
# is right until you have read it and decided it can sit - after which it keeps
# jumping the queue for no reason. Holding it drops it below the working heads
# without hiding it. Holds clear themselves when the head does anything new.

def queue_load():
    book = read_json(QUEUE, {})
    return {
        "order": [str(x) for x in book.get("order") or [] if str(x)],
        "names": {str(k): str(v) for k, v in (book.get("names") or {}).items()},
        "of": {str(k): str(v) for k, v in (book.get("of") or {}).items()},
        "hold": {str(k): v for k, v in (book.get("hold") or {}).items()},
    }


def queue_save(book):
    write_json(QUEUE, book)


def group_ids(book):
    """Every group that exists, best first. Assignment can outrun `order`."""
    order = list(book["order"])
    for gid in sorted(set(book["of"].values())):
        if gid not in order:
            order.append(gid)
    return order


def group_label(book, gid):
    return book["names"].get(gid) or f"group {gid}"


def group_rank(book, gid, order=None):
    order = group_ids(book) if order is None else order
    return order.index(gid) if gid in order else len(order)


def set_group(sid, gid):
    """Put a head in a group, or in none when gid is falsey."""
    book = queue_load()
    if gid:
        gid = str(gid)
        book["of"][sid] = gid
        if gid not in book["order"]:
            book["order"].append(gid)
    else:
        book["of"].pop(sid, None)
    queue_save(book)
    return gid


def name_group(gid, label):
    """Give a group a label, or hand it back its number with a blank one.

    Naming is not what makes a group exist - assigning a head is - so a group
    named before anyone is in it still has to appear in the ranking, or it
    would be a label on nothing that quietly sorts last.
    """
    book = queue_load()
    gid = str(gid)
    label = " ".join((label or "").split())
    if label:
        book["names"][gid] = label
        if gid not in book["order"]:
            book["order"].append(gid)
    else:
        book["names"].pop(gid, None)
    queue_save(book)
    return label


def disband_group(gid):
    """Take a group out of the book. Its heads come back unassigned.

    Membership is what keeps a group alive - group_ids() reads the assignments
    as well as the ranking - so dropping the label and the rank is not enough.
    Every entry pointing at it has to go, including the ones for sessions that
    ended weeks ago, or the next listing quietly brings the group back. The
    claims go the same way: a claim is a group waiting for a head that has not
    started, and a group that no longer exists should not be waiting for
    anything.

    Returns how many assignments it let go, or None if there was no such group.
    """
    gid = str(gid or "")
    book = queue_load()
    if not gid or gid not in group_ids(book):
        return None
    freed = [sid for sid, g in book["of"].items() if g == gid]
    book["of"] = {sid: g for sid, g in book["of"].items() if g != gid}
    book["order"] = [g for g in book["order"] if g != gid]
    book["names"].pop(gid, None)
    queue_save(book)
    claims = claims_load()
    kept = {n: v for n, v in claims.items() if str(v.get("group") or "") != gid}
    if len(kept) != len(claims):
        write_json(CLAIMS, kept)
    return len(freed)


def move_group(gid, delta):
    """Shift a group up or down the ranking. Returns the new position, 1-based."""
    book = queue_load()
    order = group_ids(book)
    if gid not in order:
        return 0
    at = order.index(gid)
    to = max(0, min(len(order) - 1, at + delta))
    order.insert(to, order.pop(at))
    book["order"] = order
    queue_save(book)
    return to + 1


# --------------------------------------------------------------- claims
#
# A group is keyed by session id, and a head you have only just launched has no
# session id yet - claude picks one seconds later, in another process. So
# `bottleneck new -g 2` writes down the name it launched under, and the first
# refresh that sees a head wearing that name puts it in the group and tears the
# note up. Claims expire on their own: a head that never starts must not sit
# there waiting to catch the next head that happens to share its name.

CLAIM_TTL = 900


def claims_load():
    return {str(k): v for k, v in read_json(CLAIMS, {}).items()
            if isinstance(v, dict)}


def claim_group(name, gid, now=None):
    """Note that the next head to appear as `name` belongs in group `gid`."""
    name = (name or "").strip()
    if not name:
        return ""
    book = claims_load()
    gid = str(gid or "")
    if gid:
        book[name] = {"group": gid,
                      "at": time.time() if now is None else now}
    else:
        book.pop(name, None)
    write_json(CLAIMS, book)
    return gid


def claims_apply(named, now=None):
    """Hand out the groups claimed for heads that have now turned up.

    `named` is (name, session_id) pairs. A claim is spent by the first head to
    wear its name whether or not the group sticks - one already grouped by hand
    keeps the group you gave it, because a claim is a default, not an override.
    """
    book = claims_load()
    if not book:
        return False
    now = time.time() if now is None else now
    left = {n: rec for n, rec in book.items()
            if now - float(rec.get("at") or 0) < CLAIM_TTL}
    queue = queue_load()
    changed = False
    for name, sid in named:
        rec = left.pop(name, None) if sid else None
        gid = str((rec or {}).get("group") or "")
        if not gid or queue["of"].get(sid):
            continue
        queue["of"][sid] = gid
        if gid not in queue["order"]:
            queue["order"].append(gid)
        changed = True
    if changed:
        queue_save(queue)
    if left != book:
        write_json(CLAIMS, left)
    return changed


# ------------------------------------------------------------------- binds
#
# Which pane a head is sitting in is normally not remembered at all - it is
# worked out, by taking the head's pid and walking up the process tree until a
# pane's own pid turns up. That answer is always current, and it costs nothing
# to keep right, which is why it is the way round it is.
#
# It needs the pid to be one of ours. A head in a Windows .claude, reached from
# WSL through an alias, has a Windows pid: our /proc either knows nothing about
# it or - worse - knows about a completely unrelated local process wearing the
# same number. The walk cannot be done, so the pane cannot be found, so the head
# is visible on the dashboard and cannot be moved into the pane beside it. Which
# is most of what the dashboard is for.
#
# But we opened that pane. Nothing had to be worked out: bottleneck ran the
# split-window itself and was handed the pane id back. So it writes that down,
# against the name it launched under - and the first refresh that sees a head
# wearing that name hands the pane over to its session id and tears the note up.
#
# That is the same two-step the group claims above make, for the same reason:
# the name is all we have at launch, because claude picks a session id seconds
# later in another process.

BIND_TTL = 900


def binds_load():
    book = read_json(BINDS, {})
    return {
        "of": {str(k): str(v) for k, v in (book.get("of") or {}).items()},
        "named": {str(k): v for k, v in (book.get("named") or {}).items()
                  if isinstance(v, dict)},
    }


def binds_save(book):
    write_json(BINDS, book)


def bind_pane(name, pane, now=None):
    """Note that the head about to appear as `name` is sitting in `pane`."""
    name = (name or "").strip()
    if not name or not pane:
        return ""
    book = binds_load()
    book["named"][name] = {"pane": pane,
                           "at": time.time() if now is None else now}
    binds_save(book)
    return pane


def binds_apply(named, now=None):
    """Hand over the panes noted for heads that have now turned up.

    `named` is (name, session_id) pairs, the same list the group claims are
    settled from. A note is spent by the first head to wear its name, and one
    that never turns up expires rather than waiting to catch a later head that
    happens to be called the same thing.
    """
    book = binds_load()
    if not book["named"] and not book["of"]:
        return book
    now = time.time() if now is None else now
    left = {n: rec for n, rec in book["named"].items()
            if now - float(rec.get("at") or 0) < BIND_TTL}
    changed = left != book["named"]
    for name, sid in named:
        rec = left.pop(name, None) if sid else None
        pane = str((rec or {}).get("pane") or "")
        if pane and book["of"].get(sid) != pane:
            book["of"][sid] = pane
            changed = True
    book["named"] = left
    if changed:
        binds_save(book)
    return book


def bound_pane(book, sid):
    """The pane we opened this head into, or "" if we did not open it."""
    return book["of"].get(sid, "") if sid else ""


def unbind(sid):
    """Forget a head's pane - it has gone, or the pane has."""
    book = binds_load()
    if book["of"].pop(sid, None) is not None:
        binds_save(book)
        return True
    return False


# ------------------------------------------------------------------- graves
#
# A head on the other side of a WSL mount cannot be killed, only closed: its pid
# belongs to another machine's numbering and signalling it here would hit
# whatever local process is wearing that number. So x closes its pane, which
# takes the terminal out from under it wherever it is actually running.
#
# What it does not do is take away the session file, which is over in the
# Windows home and written by a process we cannot ask about. So the head came
# straight back on the next refresh - no pane now, so sorted under "elsewhere",
# reading `idle`, for ever. You killed it, watched the pane go, and the
# dashboard went on listing it as a head that was fine.
#
# There is no /proc to settle it with, but there is evidence: we closed its
# terminal at a known moment, and it has written nothing since. That is what is
# written down here. It is not a claim that the process is gone - it is the
# reason the row says so, and it lapses the instant the head writes anything,
# because a head that is still working is still a head whatever we did to its
# pane.

def buried_load():
    return {str(k): v for k, v in read_json(BURIED, {}).items()
            if isinstance(v, dict)}


def bury(sid, stamp, now=None):
    """Note that we closed this head's terminal, and what it had written by then."""
    sid = safe_sid(sid)
    if not sid:
        return False
    book = buried_load()
    book[sid] = {"at": time.time() if now is None else now,
                 "stamp": float(stamp or 0)}
    write_json(BURIED, book)
    return True


def unbury(sid):
    """Forget a burial - the head has written since, or its record has gone."""
    book = buried_load()
    if book.pop(safe_sid(sid), None) is None:
        return False
    write_json(BURIED, book)
    return True


def grave_stale(grave, last_ts):
    """Has this head done anything since we closed its terminal?

    A second of slack, the same as a hold: the stamp we buried it with is the
    one being compared against, and equal is not "since".
    """
    return float(last_ts or 0) > float((grave or {}).get("stamp") or 0) + 1


def set_hold(sid, when):
    """Hold a head at the moment it reached this state, or let it go."""
    book = queue_load()
    if when:
        book["hold"][sid] = when
    else:
        book["hold"].pop(sid, None)
    queue_save(book)
    return bool(when)


def held_at(book, sid):
    try:
        return float(book["hold"].get(sid) or 0)
    except (TypeError, ValueError):
        return 0.0


def hold_stale(state, last_ts, hold):
    """Has this hold outlived what it was holding?

    A hold is against one finished state. The head doing anything since - a new
    turn, another tool call - means you are holding something that no longer
    exists, so it lets go rather than making you remember to.
    """
    if not hold:
        return False
    return last_ts > hold + 1 or state in ("WORKING", "DEAD")


# ------------------------------------------------------------- head numbers
#
# The list sorts by who needs you most and how long they have waited, so rows
# move on their own while you read them - and `2` would mean a different head by
# the time you pressed it. So the number on a row is not the row: it is a slot,
# handed to a head when it first appears and held until it is gone. Freed slots
# get reused, which keeps the numbers short enough to type.

def slots_load():
    try:
        with open(SLOTS) as fh:
            book = json.load(fh)
        return {k: int(v) for k, v in book.items() if str(v).isdigit()}
    except (OSError, ValueError, AttributeError):
        return {}


def slots_save(book):
    try:
        tmp = SLOTS + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(book, fh)
        os.replace(tmp, SLOTS)
    except OSError:
        pass


def assign_slots(heads):
    """Give every head a number that will not change while it lives."""
    old = slots_load()
    listed = {h["session_id"] for h in heads if h["session_id"]}
    book = {sid: n for sid, n in old.items() if sid in listed}
    taken = set(book.values())
    for h in heads:
        sid = h["session_id"]
        if not sid:
            h["slot"] = 0
            continue
        if sid not in book:
            n = 1
            while n in taken:
                n += 1
            book[sid] = n
            taken.add(n)
        h["slot"] = book[sid]
    if book != old:
        slots_save(book)
    return heads


def by_slot(heads, key):
    """The head wearing number `key`, or None. Used by the digit keys."""
    try:
        want = int(key)
    except (TypeError, ValueError):
        return None
    # 0 is what a head with no session id gets: it has no number, so nothing
    # should be able to name it.
    if want < 1:
        return None
    return next((h for h in heads if h.get("slot") == want), None)


def cycle_state():
    try:
        with open(CYCLE) as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def set_cycle_state(sid, mode):
    try:
        with open(CYCLE, "w") as fh:
            json.dump({"sid": sid or "", "mode": mode}, fh)
    except OSError:
        pass


def auto_enabled():
    """Auto-raise is on unless you turned it off. The file outlives a restart."""
    try:
        with open(AUTO_FLAG) as fh:
            return fh.read().strip() not in ("0", "off", "no")
    except OSError:
        return os.environ.get("BOTTLENECK_AUTORAISE", "1").lower() \
            not in ("0", "no", "false", "off")


def set_auto(on):
    with open(AUTO_FLAG, "w") as fh:
        fh.write("1" if on else "0")
    return on
