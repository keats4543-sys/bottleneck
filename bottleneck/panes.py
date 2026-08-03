"""Moving heads in and out of the pane beside the dashboard."""
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time

from . import config
from .config import (DASH_OPT, HEAD_PCT, HOME, NEEDS_ATTENTION, ROLE,
                     SESSION, SPINUP_SECS, STATES)
from .heads import fmt_age
from .procs import kill_pid
from .store import (bind_pane, bury, claims_load, clear_attention, cycle_state,
                    group_ids, group_label, group_rank, mark_seen, queue_load,
                    set_cycle_state, unbind)
from .tmuxio import (dash_pane, invalidate, pane_is_active, pane_session,
                     pane_text, pane_window, panes_by_id, panes_by_pid,
                     panes_in_window, tmux, tmux_many, tmux_out, tmux_say,
                     write_keys_conf)
from .transcript import clip
# Outside the ladder - see bottleneck/modules/__init__.py.
from . import modules


# -------------------------------------------------------------- pane placement

def evict_moves(dash, heads):
    """The commands that send anything sharing the main window back out.

    Handed back rather than run, because every caller has a move of its own to
    make straight afterwards and the two have to happen in one exec. A head
    broken out and nothing put back yet leaves the dashboard alone in the
    window - which is to say full width, for as long as it takes to get another
    tmux call away. tmux redraws the client at each of them, so that flash is
    on screen: a list laid out for a third of the screen, stretched across all
    of it, and back. Both moves in one command list and the client redraws once,
    with the layout it is going to keep.
    """
    win = pane_window(dash)
    if not win:
        return []
    moves = []
    for pane_id in panes_in_window(win, besides=dash):
        label = "head"
        for h in heads:
            if h["pane_id"] == pane_id:
                label = h["name"]
                break
        moves.append(["break-pane", "-d", "-n",
                      label[:32].replace(".", "-").replace(":", "-"),
                      "-s", pane_id])
    return moves


def evict_heads(dash, heads):
    """Move any head sharing the main window back out to its own window."""
    moves = evict_moves(dash, heads)
    if moves:
        tmux_many(*moves)
        invalidate()


def focus(head, heads, select=True, ack=True):
    """Put this head in the pane next to the dashboard.

    select=False leaves the cursor where it is - for a raise you did not ask
    for, so an unattended swap cannot move your typing somewhere else.
    ack=False keeps the attention flag, so the head still reads as wanting you
    until you actually go into it.
    """
    dash = dash_pane()
    if not dash:
        tmux_say("bottleneck: no dashboard pane - run `bottleneck start`")
        return False
    if not head.get("pane_id"):
        if head["kind"] in ("bg", "background"):
            # Deliberately do NOT open Claude's agents view here: it spawns yet
            # another background session, which is more mess, not less. Point at
            # x, which signals the pid and actually ends it.
            tmux_say(f"bottleneck: {head['name']} is backgrounded - no pane to "
                     f"show. Press x to kill it (pid {head['pid']}).")
            return False
        if head.get("foreign"):
            tmux_say(f"bottleneck: {head['name']} runs on the other side of "
                     f"this machine - it can be watched here, not moved")
            return False
        tmux_say(f"bottleneck: {head['name']} runs outside tmux "
                 f"({head.get('tty') or '?'}) - cannot move it")
        return False
    if head.get("in_main"):
        if select:
            tmux("select-pane", "-t", head["pane_id"])
        return True

    # -d leaves the joined pane unselected. Without it tmux moves the cursor
    # into it, which would hijack your typing on an unattended raise.
    args = ["join-pane", "-h", "-l", f"{HEAD_PCT}%"]
    if not select:
        args.append("-d")
    args += ["-s", head["pane_id"], "-t", dash]
    # The eviction and the select-pane ride along on the same exec: two
    # round-trips for one move is most of what makes the go-on key feel slow,
    # and three of them is a layout you watch being assembled.
    moves = evict_moves(dash, heads) + [args]
    if select:
        moves.append(["select-pane", "-t", head["pane_id"]])
    r = tmux_many(*moves)
    invalidate()
    if not r or r.returncode != 0:
        tmux_say(f"bottleneck: could not move {head['name']} in")
        return False
    # A pane that is not a head yet has no flags to clear and no turn to mark
    # seen - what it wants is answered in the pane, not here.
    if ack and not head.get("pending"):
        clear_attention(head["session_id"])
    return True


# ------------------------------------------------------------- the go-on key
#
# One key means "I am done here, take me to whatever is next". What is next is
# usually a head that wants you. When nothing does, the useful thing is not an
# error - it is to put the head you just answered back in its own window and
# show the queue, so you are not sat watching it think. Press again and you
# walk the heads that are still working, one per press.

def main_pane_occupied(dash):
    """True when something other than the dashboard shares the main window."""
    win = pane_window(dash)
    if not win:
        return False
    return bool(panes_in_window(win, besides=dash))


def park(heads):
    """Send whatever is in the main window back to its own window, show the dash."""
    dash = dash_pane()
    if not dash:
        return False
    # Before the eviction, not after: a pane does not change session by having
    # its neighbour moved out, and asking now costs nothing while the listing is
    # still good. Asking after would mean paying for a fresh one.
    sess = pane_session(dash)
    moves = evict_moves(dash, heads) + [["select-window", "-t", dash],
                                        ["select-pane", "-t", dash]]
    if sess:
        moves.append(["switch-client", "-t", sess])
    tmux_many(*moves)
    invalidate()
    return True


def cycle_next(heads, after=""):
    """The next live head with a pane, walking a fixed ring from `after`.

    Ordered by pid, not by state, so the ring does not reshuffle underneath you
    as heads flip between working and idle.
    """
    ring = sorted((h for h in heads if h["pane_id"] and h["state"] != "DEAD"
                   and not h.get("elsewhere")),
                  key=lambda h: h["pid"])
    if not ring:
        return None
    start = next((i for i, h in enumerate(ring)
                  if h["session_id"] and h["session_id"] == after), -1)
    for step in range(1, len(ring) + 1):
        cand = ring[(start + step) % len(ring)]
        if not cand["in_main"]:
            return cand
    return None


def next_or_park(heads):
    """One press of the go-on key. Returns (note, problem), never raises.

    Order: a head that wants you; else park what is up and show the queue;
    else walk the heads that are still working. `problem` is True only when the
    press could not do anything and you need telling why - a normal quiet queue
    is not a problem and must not put anything on screen.
    """
    # Heads on the other side of the machine are watched, not worked: the key
    # cannot show them to you, so it walks past them to something it can.
    here = [h for h in heads if not h.get("elsewhere")]
    want = [h for h in here if h["attention"]]
    want = [h for h in want if not h["in_main"]] or want
    movable = next((h for h in want if h["pane_id"]), None)

    if movable:
        set_cycle_state(movable["session_id"], "queue")
        focus(movable, heads)
        return f"{movable['name']} - {movable['state'].lower()}", False
    if want:
        h = want[0]
        return (f"{h['name']} needs you on {h['tty'] or '?'} - outside tmux, "
                f"cannot move it"), True

    dash = dash_pane()
    if not dash:
        return "no dashboard pane - run `bottleneck start`", True

    cur = next((h for h in heads if h["in_main"]), None)
    st = cycle_state()
    cycling = bool(cur and st.get("mode") == "cycle"
                   and st.get("sid") == cur["session_id"])

    if main_pane_occupied(dash) and not cycling:
        # You just finished with this one. Put it away rather than watch it run.
        set_cycle_state(cur["session_id"] if cur else "", "park")
        park(heads)
        return "nothing waiting - parked, queue is clear", False

    nxt = cycle_next(heads, after=st.get("sid", ""))
    if not nxt:
        park(heads)
        return "nothing waiting", False
    set_cycle_state(nxt["session_id"], "cycle")
    focus(nxt, heads, ack=False)
    return f"{nxt['name']} - {nxt['state'].lower()} (cycling)", False


def send_go(heads, self_pane):
    """Press Enter at the head in `self_pane`, then go on to whatever is next.

    Shared, because two callers want it and they must not drift: the tmux key
    writes it down the control fifo for the dashboard to run, and `bottleneck
    send-go` still runs it in its own process when no dashboard is listening.

    Returns (note, problem) the way next_or_park does.
    """
    me = next((h for h in heads
               if h["pane_id"] and h["pane_id"] == self_pane), None)
    if me:
        # Only ever into a pane we know is a head - a stray Enter elsewhere is
        # not recoverable.
        tmux("send-keys", "-t", me["pane_id"], "Enter")
        # The flags from its last turn are answered now; leaving them set would
        # make the jump land straight back on this same head.
        clear_attention(me["session_id"])
        mark_seen(me["session_id"])
        answered(me)
    return next_or_park(heads)


def say(head, text, submit=True):
    """Type `text` at a head and, unless told not to, press Enter after it.

    The answer you would have typed, delivered from somewhere else - another
    pane, a script, or a client on your laptop reaching this box over ssh. It
    is the other half of what a caller outside tmux needs: `bottleneck json`
    says which head wants you, and this one answers it.

    The text goes in as text. `send-keys -l` sends every byte literally, so a
    word tmux would otherwise read as a key name ("Enter", "C-c") arrives as
    that word. `--` ends the options, so an answer starting with a dash is an
    answer and not a flag.

    One line only. A newline in the middle would submit half the answer and
    leave the rest to be read as the next one, and there is no way to send the
    second half that is not a guess about the terminal underneath. Refusing is
    the honest answer; the caller can split it and call twice.

    Returns (note, problem), the way the other pane verbs do.
    """
    text = (text or "").replace("\r", "")
    if not text.strip():
        return "nothing to say", True
    if "\n" in text:
        return "one line at a time - a newline would submit half of it", True
    if not head.get("pane_id"):
        # A head outside tmux, or one whose claude lives on the other side of a
        # WSL mount and never got a pane. Watched, not answered - the same rule
        # the raise key follows, and for the same reason: there is nowhere to
        # aim the keys.
        where = head.get("tty") or "no tty"
        return f"{head['name']} is not in a pane ({where}) - cannot answer it", True

    # Two calls, not one tmux_many: that packs commands into one exec by
    # putting a bare ';' between them, and a ';' is a thing somebody says. The
    # extra round-trip is a millisecond and it cannot be made to mean anything.
    r = tmux("send-keys", "-t", head["pane_id"], "-l", "--", text)
    if r is None or r.returncode != 0:
        return f"tmux would not type at {head['pane'] or head['pane_id']}", True

    if submit:
        tmux("send-keys", "-t", head["pane_id"], "Enter")
        # It has its answer. Leaving the flags set would keep it at the top of
        # the queue, waiting on a reply it already has.
        clear_attention(head["session_id"])
        mark_seen(head["session_id"])
        answered(head)
    return f"{head['name']} - said {len(text)} chars", False


def answered(head):
    """Say what pressing Enter did, rather than waiting to be told it.

    This used to be half a second of time.sleep and then a full re-read of every
    head - on the one thread that draws the list and reads your keys, for the
    key you press most. What the pause was for: the head we just answered still
    reading as wanting us, so the jump landed straight back on it.

    Two of the three things that make it read that way we had already put right
    ourselves - the attention flag is deleted and the turn is marked seen, both
    on disk, before the pause ever started. The third is the harness's own
    status, which still says permission_prompt until the harness gets round to
    changing it, and no length of pause is the right one to wait for that: half
    a second is a guess that costs half a second and can still be wrong.

    So it is said here instead. You pressed Enter at this head; whatever its
    file still says, it is not waiting on you, and it has work rather than a
    finished turn. That is a claim about the next second or two and it is
    replaced by fact on the next refresh - the same trade the raise already
    makes when it writes down which head it moved instead of reading the fleet
    again to be told.
    """
    head["attention"] = False
    head["state"] = "WORKING"
    head["priority"] = STATES["WORKING"][0]
    head["reason"] = ""


_starting = []          # panes we have opened that are not heads yet


# What a pane looks like when whatever is in it wants an answer before it will
# go any further. A numbered choice is the shape every one of them has, with or
# without a box around it: "❯ 1. Yes, I trust this folder".
_OPTION = re.compile(r"^[❯>*\s]*\d+[.)]\s+\S")


def _bare(line):
    """One line off a pane with the box it was drawn in taken away."""
    return line.strip().strip("│┃|╭╮╰╯┌┐└┘┏┓┗┛─━ ").strip()


def pane_asks(text):
    """What a pane is asking, or "" if it is not asking anything.

    The one everybody meets is the trust prompt - "Do you trust the files in
    this folder?", which claude puts up in a directory it has not run in
    before. Until it is answered there is no session id, no session file and no
    transcript, so nothing the dashboard normally reads exists yet: the head is
    sat in a pane waiting for a keystroke, and the list it should be at the top
    of cannot see it at all. Its own screen is the only evidence there is.

    Read as a shape rather than as a sentence, because the sentence changes -
    the wording of the trust prompt has changed at least once, and the theme
    picker on a first ever run is a different question entirely. What they have
    in common is a numbered list of answers with the question above it. The
    options are what say this is a prompt and not just output; the last
    question mark above them is what to quote.

    The question mark is not at the end of its line. The prompt is a wrapped
    paragraph - "Quick safety check: Is this a project you created or one you
    trust? (Like your own code...)" - so what is wanted is the sentence ending
    at the last "?", not the line it happens to sit on.
    """
    lines = [b for b in (_bare(l) for l in text.splitlines()[-60:]) if b]
    at = next((i for i, l in enumerate(lines) if _OPTION.match(l)), -1)
    if at < 0:
        return ""
    for line in reversed(lines[:at]):
        if "?" not in line:
            continue
        line = line[:line.rindex("?") + 1]
        # Only the sentence that ends in the question. What comes before it is
        # a preamble, and on a dashboard it is the part nobody reads.
        for stop in (". ", "! ", "? "):
            head = line.rfind(stop, 0, len(line) - 1)
            if head != -1:
                line = line[head + len(stop):]
        return clip(line, 150)
    # A prompt we cannot quote is still a prompt - the options are there.
    return "waiting on an answer in the pane"


def _last_said(text):
    """The last thing a pane put on the screen, for a launch that went wrong."""
    for line in reversed(text.splitlines()[-40:]):
        said = _bare(line)
        if said:
            return said[:120]
    return ""


def pending(heads, now=None):
    """Rows for the panes we have opened that are not heads yet.

    There are seconds between opening the pane and the head existing: claude
    picks its session id in another process, and until it has written a session
    file there is nothing on disk for collect() to find. For that window the
    pane beside the dashboard holds a head that the list cannot see - which
    reads, to everything that asks, exactly like an empty main pane.

    That is not a cosmetic gap. auto_raise treats an empty main pane as free,
    so the first waiting head in the queue was being raised into the pane the
    new one was still coming up in - and focus() breaks the sitting pane out to
    a window of its own to make room, so the head you had just asked for was
    torn out mid-launch and left somewhere you were not looking.

    The window is normally a second or two. It is not bounded: claude asks
    whether it may read a folder it has not run in before, and it will sit on
    that question for as long as you take to answer it. Held off the list, that
    was the worst case this has - a pane asking you something, on a dashboard
    whose whole job is to tell you which pane is asking you something, showing
    nothing. Worse once the backstop expired: the record was dropped, the pane
    became fair game, and the question you had not answered was broken out to a
    window you were not looking at.

    So the pane gets a row of its own until it is a head or it is gone, and the
    row says what it is doing - coming up, asking you something, or sat there
    long enough that something has clearly gone wrong. Only the first of those
    holds the queue off the main pane, and only for SPINUP_SECS: a pane that is
    asking you something is not a reason to stop the rest of the fleet
    reaching you, and neither is one that has printed "command not found" and
    will sit there until somebody closes it.
    """
    global _starting
    if not _starting:
        return []
    now = time.time() if now is None else now
    known = panes_by_id()
    _, cursor = panes_by_pid()
    main_win = pane_window(dash_pane())
    claims, book = claims_load(), queue_load()
    order = group_ids(book)
    kept, rows = [], []

    for at in _starting:
        pane, label = at["pane"], at["label"]
        # The head has turned up and can speak for itself. Two rows for one
        # pane would be worse than the none we started with.
        if any(h.get("pane_id") == pane for h in heads):
            continue
        where = known.get(pane)
        if where is None:
            continue                    # the pane has gone; so has the wait
        kept.append(at)

        text = pane_text(pane)
        asked = pane_asks(text)
        elapsed = max(0.0, now - at["at"])
        if asked:
            state, reason, holds, spin = "WAITING", f"asks: {asked}", False, False
        elif now < at["until"]:
            state, reason = "STARTING", "starting up - waiting for claude"
            holds, spin = True, True
        else:
            # Long past the point where a head should have appeared. What the
            # pane last printed is the only thing anyone can act on - it is
            # usually "command not found", and it is the answer to the question
            # you would open the pane to ask.
            said = _last_said(text)
            # No spinner. Nothing is coming, and an animation on a pane that
            # has stopped is the dashboard telling you to keep waiting.
            state, holds, spin = "STARTING", False, False
            reason = (f"no head after {fmt_age(elapsed)} - the pane says: {said}"
                      if said else
                      f"no head after {fmt_age(elapsed)} - nothing in the pane")

        gid = str((claims.get(label) or {}).get("group") or "")
        rows.append({
            # Not a session id - it has none, and will not have one until it is
            # a head. This names the pane, which is the thing that exists, and
            # is shaped so that nothing writes a state file about it: see
            # safe_sid in store.py.
            "session_id": f"starting:{pane}",
            # The pane's own pid, so x has something real to wait on. It is the
            # shell the pane was opened with; the claude we are waiting for is
            # under it, and closing the pane ends both.
            "pid": where[0],
            "name": label,
            "name_source": "",
            "cwd": at.get("cwd", ""),
            "kind": "starting",
            "foreign": False,
            "elsewhere": False,
            "state": state,
            "reason": reason,
            "step": "",
            "task": "",
            "idle_for": elapsed,
            "last_ts": at["at"],
            "tty": None,
            "pane_id": pane,
            "pane": where[1],
            "in_main": bool(main_win and where[2] == main_win),
            "active": pane in cursor,
            "priority": STATES[state][0],
            "attention": state in NEEDS_ATTENTION,
            "group": gid,
            "group_label": group_label(book, gid) if gid else "",
            "group_rank": group_rank(book, gid, order) if gid else len(order),
            "held": False,
            # What the rest of the program checks rather than reading the state
            # column: this row is a pane, not a head, so the keys that act on a
            # head's session id have nothing to act on.
            "pending": True,
            "holds": holds,
            "spin": spin,
        })

    _starting = kept
    return rows


def starting(heads):
    """The name of the head the queue is standing off for, or "".

    Pure, over a list that already has the pending rows on it - the reading is
    done in pending(), once a refresh, and this is only the question auto_raise
    asks of the answer.

    Only automatic movement waits on it. `j`, Alt+j and the go-on key are you
    asking for a head by name, and a queue that ignored you because something
    else was starting would be a worse bargain than the one this fixes.
    """
    return next((h["name"] for h in heads if h.get("holds")), "")


def auto_raise(heads, held, dash_pane_id=""):
    """Bring the top waiting head into the main pane when that pane is free.

    Free means one of: nothing is in there; or what is in there wants nothing
    from you and does not have your cursor in it. Whatever you are actually
    sitting in stays put - the queue reorders behind it and `j` walks it.

    `held` is the pid of a head this function raised that you have not looked at
    yet. It is not displaced by a later arrival, or two heads finishing close
    together would push the first one out before you ever saw it.

    A raise normally leaves the cursor where it is, because moving it would
    take your typing somewhere you did not ask for. There is one case where
    that reasoning runs out: the main pane is empty and you are sat on the
    dashboard, so there is nothing you could be typing into and nothing to
    displace. Then the raise takes the cursor with it and you can answer the
    head straight away. `dash_pane_id` is what makes that case knowable - pass
    the dashboard's own pane and it checks whether the cursor is in it.

    Returns (head_raised_or_None, new_held).
    """
    # A head we opened is still coming up in there. The pane reads as empty
    # because the head is not on disk yet, and raising into it would evict the
    # one you just asked for.
    if starting(heads):
        return None, held

    want = [h for h in heads
            if h["attention"] and h["pane_id"] and not h.get("elsewhere")]
    cur = next((h for h in heads if h["in_main"]), None)

    # Once you put the cursor in it, it stops being an unread hand-off.
    if held is not None:
        if not cur or cur["pid"] != held or pane_is_active(cur["pane_id"]):
            held = None

    if not want:
        return None, held
    if cur:
        if cur["attention"]:
            return None, held           # already showing something that wants you
        if held is not None:
            return None, held           # an unread hand-off is sitting there
        if pane_is_active(cur["pane_id"]):
            return None, held           # you are typing in it

    top = want[0]
    if cur and top["pid"] == cur["pid"]:
        return None, held

    # Nothing is up and the cursor is on the list: hand the head the cursor as
    # well as the pane. Only ever with the main pane empty - with something
    # already in there the raise has to displace it first, and taking your
    # cursor along with a displacement is the hijack this guards against.
    take = not cur and bool(dash_pane_id) and pane_is_active(dash_pane_id)
    if not focus(top, heads, select=take, ack=take):
        return None, held
    # Handing over the cursor is you having seen it, so there is no unread
    # hand-off left to protect and the next arrival may take the pane.
    return top, (None if take else top["pid"])


_LAUNCH = None


def find_claude():
    """How to say "claude" in a pane, or "" if nobody here knows.

    tmux runs a pane's command through a shell that is neither interactive nor
    a login shell. An alias does not exist in one. A shell function does not
    exist in one. On Debian and on WSL, ~/.bashrc returns on its first line in
    one - so a PATH set there does not exist either. All three work when you
    type `claude` yourself and none of them survive being handed to tmux, which
    is why the pane could say "command not found" about a program you had just
    used.

    So: an absolute path if one can be had, because a path needs no shell to
    agree with it. Failing that, ask your shell the way you use it - login and
    interactive - and take the path it names. Failing even that, hand the whole
    launch to that shell and let it expand its own alias.
    """
    if config.CLAUDE:
        return config.CLAUDE
    found = shutil.which("claude")
    if found:
        return found
    # Our own PATH is the tmux server's, which is whatever the environment
    # happened to be when the server started. Yours is the one that works.
    named = ask_shell('printf "__bn__%s\\n" "$(command -v claude || type -P claude)"')
    if named.startswith("/"):
        return named
    if named:
        # An alias or a function: no path to resolve, so the shell that knows
        # the name has to be the shell that runs it.
        return f"{shlex.quote(config.SHELL)} -lic " + shlex.quote("claude \"$@\"") + " claude"
    return ""


def ask_shell(script, timeout=20):
    """Run one line in your login interactive shell and read the answer back.

    Only lines the script marked count. An interactive shell runs your whole
    ~/.bashrc, and a shell that greets you, prints a fortune or warns about
    something would otherwise be answering the question we asked.

    Twenty seconds, up from eight, because eight was not the generous number it
    looks. A ~/.bashrc that sources conda and nvm took 6.1 seconds here - and a
    timeout is not a delay, it is a wrong answer: find_claude reads it as "your
    shell does not know either" and n then reports that claude cannot be found
    at all, on a machine where typing `claude` works. Waiting longer only costs
    something to whoever presses n in the first seconds of a dashboard, and
    warm_claude means nobody normally waits at all.
    """
    try:
        r = subprocess.run([config.SHELL, "-lic", script],
                           capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return ""
    marked = [line[len("__bn__"):].strip()
              for line in (r.stdout or "").splitlines()
              if line.startswith("__bn__")]
    return marked[-1] if marked else ""


_finder = None


def warm_claude():
    """Start working out how to say "claude" before anyone asks for a head.

    Resolving costs nothing at all when claude is on our own PATH. When it is
    not - the case find_claude exists for - it costs a login interactive shell,
    which is your whole ~/.bashrc: measured at 6.1 seconds on the machine this
    was written on, against a timeout of twenty. That was being spent lazily, at
    the moment you pressed n, with the cursor sat in a prompt that had stopped
    taking keys. The answer cannot change under a running dashboard, so there is
    no reason to wait for the question before going and getting it.

    Called once, when the dashboard starts. Anyone who presses n before it comes
    back waits for the same answer rather than starting a second shell.
    """
    global _finder
    if _LAUNCH is not None or _finder is not None:
        return
    _finder = threading.Thread(target=_resolve, daemon=True,
                               name="bn-find-claude")
    _finder.start()


def _resolve():
    global _LAUNCH
    _LAUNCH = find_claude()


def claude_cmd(args):
    """The launch line for a head, or "" when claude cannot be found at all.

    Resolved once: it costs a shell startup, and it cannot change under a
    running dashboard without you editing the config it reads. Usually resolved
    already - warm_claude started on it when the dashboard did - and this waits
    on that rather than asking again. "" is a resolved answer too: nobody knows
    where claude is, and asking a second shell will not change that.
    """
    global _LAUNCH
    if _LAUNCH is None:
        if _finder is not None:
            _finder.join()
        if _LAUNCH is None:     # nobody went looking, or the thread died trying
            _LAUNCH = find_claude()
    return f"{_LAUNCH} {args}" if _LAUNCH else ""


def env_prefix(spec):
    """`NAME=value ` for each thing the modules want in a head's environment.

    Shell-quoted, because a value is a path or a URL and one day will be
    something with a space in it. Empty when no module asked for anything,
    which is the ordinary case and costs the command line nothing.
    """
    got = modules.environ(spec)
    return "".join(f"{k}={shlex.quote(v)} " for k, v in sorted(got.items()))


def spawn(cmd, cwd, label, heads):
    """Open a brand-new pane beside the dashboard running `cmd`.

    An empty command means claude was never found. Saying so here is the whole
    point: the alternative is a new pane that prints "claude: command not
    found" and then sits waiting for a keypress, which looks like a bottleneck
    bug and is not one.
    """
    if not cmd.strip():
        tmux_say("bottleneck: cannot find claude - set BOTTLENECK_CLAUDE "
                 "in ~/.bottleneck/config to its full path")
        return False
    # Whatever the modules want true before claude starts. In front of the
    # command rather than exported here: tmux runs this through a shell, and a
    # variable set on that line belongs to the head and to nothing else on the
    # box - not to the dashboard, and not to the next head, which may be
    # launched by a checkout carrying different modules.
    cmd = env_prefix({"cwd": cwd, "label": label}) + cmd
    dash = dash_pane()
    if not dash:
        print("no dashboard pane - run `bottleneck start`", file=sys.stderr)
        return False
    # -P -F asks tmux which pane it just made. We used to not ask, and then name
    # the pane with a select-pane aimed at whatever was current - which is the
    # new one, so it worked. Asking is worth the same round-trip: the answer is
    # the only way to know where a head went when its own pid cannot tell us,
    # which is every head whose claude lives on the other side of a WSL mount.
    #
    # In with the eviction, so the window is never briefly a dashboard on its
    # own - see evict_moves. Nothing else in the list prints, so the pane id is
    # the last thing to come back whatever went before it.
    r = tmux_many(*evict_moves(dash, heads), [
        "split-window", "-h", "-l", f"{HEAD_PCT}%", "-t", dash,
        "-c", cwd, "-P", "-F", "#{pane_id}",
        f"{cmd} || {{ printf '\\n[bottleneck] exited %s\\n' \"$?\"; read -r _; }}"])
    said = (r.stdout or "").strip() if r and r.returncode == 0 else ""
    pane = said.splitlines()[-1].strip() if said else ""
    invalidate()
    if not pane:
        tmux_say("bottleneck: could not open the pane")
        return False
    tmux_many(["select-pane", "-t", pane, "-T", label[:32]])
    # Written down before the head exists. It picks its session id seconds from
    # now, in another process; the name is all we have to hang this on until the
    # first refresh that sees the head sees both.
    bind_pane(label, pane)
    # And the pane gets a row of its own until that refresh comes, with the
    # queue standing off it while it does - see pending(). Set last, so a spawn
    # that failed above never freezes anything.
    now = time.time()
    _starting.append({"pane": pane, "label": label, "cwd": cwd, "at": now,
                      "until": now + SPINUP_SECS})
    return True


# ------------------------------------------------------------- killing a head
#
# Stopping a head is one signal and then a wait: close the pane or send SIGTERM,
# then ask /proc every fifth of a second whether it has gone, for four seconds,
# and SIGKILL it if it has not. Nearly always it is over in one of those checks.
#
# On the dashboard that wait was four and a half seconds of a frozen list -
# nothing redrew, and a key pressed during it turned up whenever the reap
# finished. So the waiting happens on a thread of its own and leaves what it has
# to say in a box the loop empties on its next pass, which is at most a refresh
# away. Only the waiting moves: the signal is still sent from the thread you
# pressed the key in, so a kill that cannot even start still says so on the spot.
#
# `bottleneck kill` keeps waiting in its own process. It has nothing else to do
# with the time, and a one-shot that returned before the head was gone would be
# claiming something it had not checked.

_reaped = []
_reaped_lock = threading.Lock()


def reaped():
    """What the background kills have finished saying since you last looked."""
    with _reaped_lock:
        out, _reaped[:] = list(_reaped), []
    return out


def kill_head(head, wait=True):
    """Stop a head, and say what happened.

    With wait=False the answer comes back the moment the signal is away, and
    the outcome turns up in reaped() once the process is actually gone.
    """
    stopped = _stop(head)
    if stopped is not None:
        return stopped                  # never even started - nothing to wait on
    if wait:
        return _reap(head)
    threading.Thread(target=_reap_into_box, args=(head,), daemon=True).start()
    return f"killing {head['name']}…"


def _stop(head):
    """Send the stop. Returns a final answer if there is one, else None.

    Background heads have no pane, so there is nothing to close - signal the
    process directly.
    """
    name, pid = head["name"], head["pid"]
    if head.get("pane_id"):
        tmux("kill-pane", "-t", head["pane_id"])
        return None
    if head.get("foreign"):
        # No pane to close and a pid that is not ours to signal: that number
        # names a process on the machine claude is actually running on, and
        # some local process may well be wearing it here. Sending SIGTERM to
        # whatever that turns out to be is the one outcome worse than not
        # killing anything.
        return (f"{name} runs outside this machine - stop it where it runs "
                f"(pid {pid} is not a pid here)")
    # kind=bg heads run under a bg-pty-host with no tty.
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return f"{name} was already gone"
    except PermissionError:
        return f"not allowed to kill {name} (pid {pid})"
    return None


def _reap(head):
    """Wait for a head that has been sent its stop, and say how it went."""
    name, pid = head["name"], head["pid"]

    if head.get("foreign"):
        # The pane is ours and was just closed; the pid is not ours and must
        # not be polled, let alone signalled. /proc would be answering about
        # whichever local process is wearing that number - and the escalation
        # below, which SIGKILLs whatever has not gone after four seconds, would
        # then kill it. So the pane going is the whole of the evidence, and it
        # is the right evidence: closing it takes the tty out from under the
        # head, wherever the head is actually running.
        for _ in range(20):
            time.sleep(0.2)
            invalidate()
            if head.get("pane_id") not in panes_by_id():
                clear_attention(head["session_id"])
                unbind(head["session_id"])
                # Its session file is in the Windows home and nothing here can
                # take it away, so without this the head is back on the next
                # refresh - no pane, sorted under "elsewhere", reading idle,
                # for ever. Write down when we closed it and what it had
                # written by then; collect() reads the row off that until the
                # head writes something, which is the one thing that would mean
                # we were wrong. See bury() in store.py.
                bury(head["session_id"], head.get("last_ts") or 0)
                return f"killed {name}"
        return f"{name}: its pane will not close - check it where it runs"

    for _ in range(20):
        time.sleep(0.2)
        if not os.path.isdir(f"/proc/{pid}"):
            clear_attention(head["session_id"])
            return f"killed {name}"

    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
    time.sleep(0.5)
    if os.path.isdir(f"/proc/{pid}"):
        return f"{name} (pid {pid}) will not die - check it by hand"
    clear_attention(head["session_id"])
    return f"killed {name} (needed SIGKILL)"


def _reap_into_box(head):
    """_reap on a thread: nobody is waiting on the answer, so leave it out."""
    try:
        said = _reap(head)
    except Exception as exc:            # a thread that dies says nothing at all
        said = f"kill of {head['name']} went wrong: {exc}"
    with _reaped_lock:
        _reaped.append(said)


def launched_from():
    """Where `bottleneck start` was run, which is where a new head should land.

    The dashboard pane used to be opened at $HOME, and the dashboard's own
    directory is what a new head defaults to - so `n` offered you home no
    matter which project you started the fleet in, and the only way to say
    otherwise was a line in the config file naming one directory for good.
    """
    try:
        here = os.getcwd()
    except OSError:
        return HOME
    return here if os.path.isdir(here) else HOME


def start():
    """Build the two-pane layout, or attach to it if it already exists."""
    write_keys_conf()
    if not tmux("has-session", "-t", SESSION, check=True):
        tmux("new-session", "-d", "-s", SESSION, "-n", "main", "-c",
             launched_from(), "exec bottleneck watch")
        time.sleep(0.4)
        first = tmux_out("list-panes", "-t", f"{SESSION}:main", "-F", "#{pane_id}")
        if first:
            tmux("set", "-p", "-t", first.splitlines()[0], ROLE, "dash")
    elif not dash_pane():
        # The session is up but has no live dashboard - the old one quit, or
        # its pane was taken over. Build a new one in its own window and read
        # the pane id back from tmux; asking for "the window called main" could
        # hand us the wrecked one, since names are not unique.
        pane = tmux_out("new-window", "-t", SESSION, "-n", "main", "-c",
                        launched_from(),
                        "-P", "-F", "#{pane_id}", "exec bottleneck watch")
        pane = pane.splitlines()[0].strip() if pane else ""
        if pane:
            tmux("set", "-p", "-t", pane, ROLE, "dash")

    if os.environ.get("TMUX"):
        tmux("switch-client", "-t", SESSION)
    else:
        os.execvp("tmux", ["tmux", "attach-session", "-t", SESSION])
    return 0


def reload_all():
    """Pick up an edited checkout without losing a single head.

    `bottleneck` and the hook are symlinks, so the code is already current -
    every new invocation reads the new file. Two things do hang on to the old
    version: tmux, which holds the key bindings it read at startup, and the
    dashboard itself, a loop that has been running since you opened it. This
    re-reads the config and restarts that one pane. The heads are separate
    processes in their own panes and never notice.
    """
    said = []
    # Before the source-file below, which is what reads it.
    write_keys_conf()
    conf = os.path.join(HOME, ".tmux.conf")
    if os.path.exists(conf):
        tmux("source-file", conf)
        # Re-reading the file put the slow fallback bindings back. Forget which
        # pane the fast ones named, so the dashboard we are about to restart
        # sees them as wrong and installs its own again.
        tmux("set", "-s", "-u", DASH_OPT)
        said.append("bindings")
    invalidate()
    dash = dash_pane()
    if dash:
        tmux("respawn-pane", "-k", "-t", dash, "exec bottleneck watch")
        tmux("set", "-p", "-t", dash, ROLE, "dash")
        said.append("dashboard")
    else:
        said.append("no dashboard pane - `bottleneck start` to build one")
    return "reloaded: " + ", ".join(said)


def restart_here():
    """Replace this dashboard with the current version of the code.

    Called after the watch loop has put the terminal back, so the new process
    starts on a clean one. Nothing is killed and no pane moves: the same pty
    just gets a new occupant, which is why this can be a plain key and not a
    tmux binding. The bindings are re-read first, since they live in tmux and
    not in this process.
    """
    conf = os.path.join(HOME, ".tmux.conf")
    if os.environ.get("TMUX") and os.path.exists(conf):
        tmux("source-file", conf)
    me = os.path.realpath(sys.argv[0]) if sys.argv and sys.argv[0] else ""
    for path in (me, "bottleneck"):
        if not path:
            continue
        try:
            os.execvp(path, [path, "watch"])
        except OSError:
            continue
    print("bottleneck: cannot re-exec - run `bottleneck watch` again",
          file=sys.stderr)
    return 1
