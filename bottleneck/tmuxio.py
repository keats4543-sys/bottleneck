"""Talking to tmux, and working out which pane is the dashboard."""
import os
import shutil
import subprocess
import sys
import time

from . import config
from .config import DASH_OPT, DASHES, REFRESH, ROLE, SESSION
from .procs import claude_procs, proc_start
from .store import read_json, update_json, write_json
from .procs import ancestors


# ------------------------------------------------------------------ tmux basics

def tmux(*args, check=False):
    try:
        r = subprocess.run(["tmux", *args], capture_output=True, text=True, timeout=8)
    except (OSError, subprocess.SubprocessError):
        return None
    if check and r.returncode != 0:
        return None
    return r


def tmux_many(*cmds):
    """Several tmux commands down one exec.

    tmux reads a bare ';' as a command separator, so the join-pane and the
    select-pane that follows it cost one round-trip between them instead of
    two. Only the last command's output comes back, which is why this is for
    doing rather than asking.
    """
    args = []
    for at, cmd in enumerate(cmds):
        if at:
            args.append(";")
        args.extend(cmd)
    return tmux(*args)


def tmux_out(*args):
    r = tmux(*args)
    return r.stdout.strip() if r and r.returncode == 0 else ""


# --------------------------------------------------------------- remembering
#
# A tmux call is a fork, an exec and a socket round-trip: about 11ms here, and
# one press of the go-on key makes a dozen of them. Most are the same three
# questions asked over - which pane is the dashboard, what window is it in,
# what else is in that window - because collect(), next_or_park() and focus()
# each work it out for themselves rather than passing the answer along.
#
# So the answer is remembered, briefly. The window is short on purpose and
# anything that moves a pane drops it by hand: a stale answer here puts a head
# in the wrong place, which is a good deal worse than a slow one.

# One refresh of the dashboard. The loop drops the cache at the top of every
# cycle and everything that moves a pane drops it too, so this is only a
# backstop for the one-shot commands - but matching it to the refresh means a
# key pressed late in a cycle reuses that cycle's answers instead of asking
# again for what is already on the screen in front of you.
CACHE_TTL = max(1.0, REFRESH)

_cache = {}


def cached(key, make, ttl=CACHE_TTL):
    hit = _cache.get(key)
    now = time.monotonic()
    if hit is not None and now - hit[0] < ttl:
        return hit[1]
    val = make()
    _cache[key] = (now, val)
    return val


def invalidate():
    """Forget every remembered answer. Call after anything moves a pane."""
    _cache.clear()


def tmux_say(text):
    tmux("display-message", text)


def panes_by_pid():
    """{pane_pid: (pane_id, 'sess:win.pane', window_index)}

    Also returns the set of pane ids that hold the cursor - pane and window both
    active - so the dashboard can mark the head you are actually sitting in
    without one tmux call per head.
    """
    p = _panes()
    return p["by_pid"], p["cursor"]


def panes_by_id():
    """{pane_id: (pane_pid, 'sess:win.pane', window_index)} for every pane.

    Every pane, which is the difference between this and panes_by_pid: that one
    is keyed by the pid and drops a pane whose pid tmux did not give us, which
    is right for finding the pane a process is in and wrong for anything asking
    what is in a window. A pane we cannot name the process in is still a pane,
    and still has to be moved out of the way.
    """
    return _panes()["by_id"]


def _panes():
    """One listing, read every way we need it.

    The listing is what costs - a fork, an exec and a round-trip, about 21ms
    here - and the number of fields on it costs nothing at all. So everything
    tmux can say about a pane comes back on this one call: where it is, whether
    the cursor is in it, whether it is in copy-mode, and what we marked it as.
    Each of those used to be a call of its own, and a quiet refresh made half a
    dozen of them - most asking about the same panes this listing had already
    described.
    """
    return cached("panes", _read_panes)


# Eight fields, one line per pane. Order matters to _read_panes and nothing
# else; adding to the end is free.
_PANE_FIELDS = ("#{pane_pid}", "#{pane_id}",
                "#{session_name}:#{window_index}.#{pane_index}",
                "#{session_name}:#{window_index}",
                "#{pane_active}#{window_active}",
                "#{" + ROLE + "}", "#{session_name}", "#{pane_in_mode}",
                "#{pane_width}")


def _read_panes():
    out = tmux_out("list-panes", "-a", "-F", "\t".join(_PANE_FIELDS))
    p = {"by_pid": {}, "cursor": set(), "by_id": {}, "roles": {},
         "active": set(), "in_mode": set(), "width": {}}
    for line in out.splitlines():
        bits = line.split("\t")
        if len(bits) < 4 or not bits[1]:
            continue
        pane = bits[1]

        def field(at):
            return bits[at] if len(bits) > at else ""

        if bits[0].isdigit():
            p["by_pid"][int(bits[0])] = (pane, bits[2], bits[3])
        p["by_id"][pane] = (int(bits[0]) if bits[0].isdigit() else 0,
                            bits[2], bits[3])
        flags = field(4)
        if flags == "11":
            p["cursor"].add(pane)
        # The cursor is in a pane and in a window; being the active pane of a
        # window nobody is looking at is a different question, and the one
        # auto_raise asks before it displaces anything.
        if flags[:1] == "1":
            p["active"].add(pane)
        p["roles"][pane] = (field(5), field(6))
        if field(7) == "1":
            p["in_mode"].add(pane)
        if field(8).isdigit():
            p["width"][pane] = int(field(8))
    return p


def pane_width(pane_id):
    """How wide tmux says a pane is, or 0.

    What the dashboard lays its rows out to, in place of asking the terminal.
    The two normally agree and disagree at exactly the wrong moment: a head
    exiting or landing beside us changes the layout, and the frame we are about
    to draw is the first thing to be wrong about it. tmux applies the layout
    before it answers, so asking tmux after a move gets the width the pane has
    now; the pty is told separately, and a redraw that gets in first lays the
    whole list out for a width that has already gone - which is then on screen
    until something else provokes a repaint.

    Off the same cached listing as everything else, so in a refresh where
    nothing moved it costs nothing at all.
    """
    return _panes()["width"].get(pane_id, 0) if pane_id else 0


def _pane_flag(pane_id, key, fmt):
    """A yes-or-no about one pane, out of the listing where the listing has it.

    A pane the listing has never heard of is asked about directly, the way
    pane_window does: it is either brand new or gone, and answering "no" for the
    first would let a raise displace a pane you were typing in.
    """
    if not pane_id:
        return False
    p = _panes()
    if pane_id in p["by_id"]:
        return pane_id in p[key]
    return cached((key, pane_id), lambda: tmux_out(
        "display-message", "-p", "-t", pane_id, fmt) == "1")


def locate(pid, panes):
    if pid in panes:
        return panes[pid]
    for anc in ancestors(pid):
        if anc in panes:
            return panes[anc]
    return (None, None, None)


def pane_window(pane_id):
    """Which window a pane is in, out of the listing we already have.

    Asking tmux directly was a second process for something the listing had
    already said - and the listing is cached, so on any path that has looked at
    the panes at all this now costs nothing. A pane the listing does not know
    is worth asking about: it is either brand new or gone, and answering "no
    window" for the first would leave a head sitting in the wrong pane.
    """
    if not pane_id:
        return ""
    known = panes_by_id().get(pane_id)
    if known:
        return known[2]
    return cached(("pane_window", pane_id), lambda: tmux_out(
        "display-message", "-p", "-t", pane_id,
        "#{session_name}:#{window_index}"))


def panes_in_window(win, besides=""):
    """Every pane id in `win`, except one - what else is sharing this window."""
    return [pane for pane, (_, _, where) in panes_by_id().items()
            if where == win and pane != besides]


def pane_is_active(pane_id):
    """True when the cursor sits in this pane - i.e. you are working in it."""
    return _pane_flag(pane_id, "active", "#{pane_active}")


def pane_in_mode(pane_id):
    """True when this pane is in copy-mode, so tmux is eating its keys."""
    return _pane_flag(pane_id, "in_mode", "#{pane_in_mode}")


def pane_text(pane_id, lines=40):
    """What is on the screen in a pane right now, or "".

    The one thing about a pane that cannot be had from the listing, and the
    only way to know anything at all about a pane whose process is not a head
    yet - a claude asking whether it may read this folder has written no
    session file, so its own screen is the whole of the evidence.

    Not cached, and deliberately not called on every pane: it is a tmux
    round-trip each, and the panes worth asking about are the handful we have
    opened and are still waiting on.
    """
    if not pane_id:
        return ""
    return tmux_out("capture-pane", "-p", "-t", pane_id, "-S", f"-{int(lines)}")


def pane_session(pane_id):
    """Which session a pane belongs to, out of the listing we already have."""
    return pane_roles().get(pane_id, ("", ""))[1]


# ------------------------------------------------------------ which pane is us
#
# The dashboard marks its pane with a tmux option. A pane outlives whatever runs
# in it, so that mark can go on pointing at a pane long after the dashboard in
# it died - and if you then start a head there, every later join-pane and
# break-pane aims at a head, which is how you end up with a claude on the left
# and another on the right and no dashboard anywhere.
#
# So the mark is a hint, never proof. Each dashboard writes down its pane and
# its pid; a mark counts only when that pid is still a live dashboard.

def dash_register(pane):
    """Record this process as the dashboard in `pane`."""
    if not pane:
        return

    def add(book):
        book = {p: v for p, v in book.items() if dash_alive(v)}
        book[pane] = {"pid": os.getpid(), "start": proc_start(os.getpid())}
        return book

    # Under a lock, because the other dashboard is doing this too. Read, add,
    # write from two processes at once and one of them is not in the book it
    # thinks it just joined - and a dashboard missing from the book has its
    # pane mark cleared as stale by the other one.
    update_json(DASHES, add)


def dash_release(pane):
    """Drop our claim on `pane` and clear the mark, when the dashboard stops."""
    if not pane:
        return
    # Same lock as registering: dropping our own claim must not put back a book
    # that has forgotten someone who registered while we were reading it.
    update_json(DASHES, lambda book: {p: v for p, v in book.items()
                                      if p != pane})
    # Put the keys back before the mark, so one pressed during teardown finds
    # the dashboard the slow way rather than typing into whatever takes this
    # pane over.
    if dash_hint() == pane:
        dash_unpoint()
    tmux("set", "-p", "-u", "-t", pane, ROLE)


def dash_hint():
    """The pane the tmux bindings currently aim at, as tmux has it."""
    return tmux_out("show", "-svq", DASH_OPT)


# The keys that move you around used to start a program to work out where the
# dashboard was. That program cost more than everything it then did. But a
# running dashboard already knows its own pane, and tmux can be told a pane at
# bind time - so it rebinds these keys to plain tmux commands against a literal
# pane id, and they cost nothing at all to press.
#
# -t will not expand a format, so the pane cannot come from an option and has
# to be written into the binding. That turns out to be the safer way round:
# tmux never reuses a pane id, so a binding left behind by a dashboard that died
# without tidying up aims at a pane that no longer exists and does nothing,
# rather than sending a keystroke to whoever inherited the place.
#
# (key, table, what it does with the dashboard pane) - and the command to put
# back when there is no dashboard to aim at.
#
# Which keys those are comes from config.KEYS, so all three of these - the fast
# bindings, the slow ones, and the file tmux reads before any dashboard is up -
# are one list of keys wearing three sets of commands. They used to be three
# lists that had to be kept in step by hand.

def dash_keys(pane):
    to_dash = f"select-window -t {pane} ; select-pane -t {pane}"
    # One key both ways, and now tmux decides which way on its own.
    swap = (f"if -F '#{{==:#{{pane_id}},{pane}}}' "
            f"'select-pane -t :.+' '{to_dash}'")
    # The go-on key: type it into the dashboard's own stdin and let the loop
    # that is already running handle it, exactly as if you were sat there and
    # pressed j.
    doing = {"next": f"send-keys -t {pane} j", "dash": to_dash, "swap": swap}
    return [(key, table, doing[act])
            for act, keys in config.KEYS.items() if act in doing
            for key, table in keys]


def our_path():
    """Where this program is, absolutely - for a binding that cannot ask PATH.

    A binding that starts `bottleneck` does so in a shell tmux forks, and that
    shell's PATH is the tmux server's: whatever the environment happened to be
    when the server started, which can easily be from before this was installed.
    run-shell -b then throws the "command not found" away, so the key does
    nothing, silently, for ever. That looks exactly like a broken keybinding and
    is not one - and it is the same silent-PATH failure that had `claude agents
    --json` returning nothing on a machine where claude works fine.

    A running dashboard does not have to guess: it is this program, and it knows
    where it was started from. Only a path that still names one of ours counts,
    because sys.argv[0] under a test runner or an odd entry point is some other
    file entirely, and a binding pointing at that would be worse than the guess.
    """
    me = os.path.realpath(sys.argv[0]) if sys.argv and sys.argv[0] else ""
    if (me and os.path.basename(me).startswith("bottleneck")
            and os.access(me, os.X_OK)):
        return me
    return shutil.which("bottleneck") or os.path.expanduser(
        "~/.local/bin/bottleneck")


def _fallback_keys(run=None):
    run = run or our_path()
    doing = {
        "next": f"run-shell -b '{run} next --jump'",
        "dash": f"run-shell -b '{run} todash'",
        "swap": f"run-shell -b '{run} swap'",
    }
    return [(key, table, doing[act])
            for act, keys in config.KEYS.items() if act in doing
            for key, table in keys]


# Worked out once, at import. Where this program lives does not change under a
# running dashboard, and the answer is wanted at teardown - when the terminal is
# already being handed back and there is nothing left to spend on it.
FALLBACK_KEYS = _fallback_keys()


# --------------------------------------------------- the keys tmux reads first
#
# Before any dashboard is running, the only keys are the ones in tmux's own
# config - and tmux cannot read ~/.bottleneck/config, which is ours. So the keys
# are written out as a tmux config and sourced.
#
# Everything here is the slow path by definition: with no dashboard there is
# nothing to send a keystroke to, so each of these starts the program. They name
# it by path, because a shell tmux forks has the server's PATH and that can
# easily predate the install - and run-shell -b throws "command not found" away,
# so the key would do nothing at all, silently.

def sendgo_cmd(run):
    """Press Enter at this head, then go on.

    The one key that cannot be pure tmux: it has to press Enter, let the harness
    come out of idle, and only then read the queue. A running dashboard holds
    all of that already, so the key writes one line down its fifo rather than
    starting a second copy of the program. `timeout` is the guard - with nobody
    listening the write cannot complete, and after a third of a second it gives
    up and does it the slow way.
    """
    fifo = config.CTL
    return (f"run-shell -b \"timeout 0.3 sh -c '[ -p {fifo} ] && "
            f"echo sendgo #{{pane_id}} > {fifo}' 2>/dev/null || {run} send-go\"")


def quoted(key):
    """A key name tmux will read as one word.

    M-' is a perfectly good key and the obvious one to move "sent, next" to when
    your terminal has taken Alt+Enter - but written bare into a config it opens
    a quoted string, and tmux swallows the rest of the line as part of the key
    name. Quoting the name costs nothing for the keys that did not need it.
    """
    return '"' + key.replace("\\", "\\\\").replace('"', '\\"') + '"'


def keys_conf(run=None):
    """The whole binding set, as a tmux config file."""
    run = run or our_path()
    doing = {
        "next": f"run-shell -b \"{run} next --jump\"",
        "dash": f"run-shell -b \"{run} todash\"",
        "swap": f"run-shell -b \"{run} swap\"",
        "sendgo": sendgo_cmd(run),
        "start": f"run-shell \"{run} start\"",
        "reload": f"run-shell \"{run} reload >/dev/null 2>&1\"",
        "left": "select-pane -L",
        "right": "select-pane -R",
    }
    out = ["# Generated by bottleneck from ~/.bottleneck/config - do not edit.",
           "# Change BOTTLENECK_KEY_* there and reload; this file is rewritten",
           "# every time bottleneck starts, watches or reloads.",
           "",
           "# Every key a default names, cleared first. tmux.conf binds the",
           "# defaults so a tmux that has never run bottleneck still works, and",
           "# without this pass they would survive being taken out of the config",
           "# - so handing Alt+Enter back to your terminal would not hand it",
           "# back. -q because unbinding a key nothing bound is not an error we",
           "# want reported at every reload.",
           ]
    for key, table in config.default_keys():
        out.append(f"unbind -q {'' if table == 'prefix' else '-n '}{quoted(key)}")
    out.append("")
    for act, keys in config.KEYS.items():
        for key, table in keys:
            flag = "" if table == "prefix" else "-n "
            out.append(f"bind {flag}{quoted(key)} {doing[act]}")
    return "\n".join(out) + "\n"


def write_keys_conf(path=None):
    """Put the bindings where tmux can source them. Returns the path, or ""."""
    path = path or config.KEYS_CONF
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w") as fh:
            fh.write(keys_conf())
        os.replace(tmp, path)
        return path
    except OSError:
        return ""


def bind_all(table):
    cmds = []
    for key, where, action in table:
        cmds.append(["bind"] + ([] if where == "prefix" else ["-n"])
                    + [key, action])
    if cmds:
        tmux_many(*cmds)


def dash_point(pane):
    """Aim the movement keys straight at `pane`, with no program in between.

    Only with one dashboard running. Key tables belong to the tmux server, not
    to a session, so a binding that names a pane names it for everybody - and
    with two dashboards up, Alt+j from either session would land in whichever
    started last. The slow path does not have that problem: it works out the
    dashboard from the pane you pressed the key in, and prefers your own
    session. So when there is more than one, the keys go back to it and pay the
    process to stay correct.

    Called every refresh, not just at startup, so it settles either way as
    dashboards come and go. Idempotent: the option records which pane the
    bindings currently name, and matching that is the whole check.
    """
    if not pane:
        return
    alone = len(dash_claimed()) <= 1
    aimed = dash_hint()
    if alone and aimed != pane:
        bind_all(dash_keys(pane))
        tmux("set", "-s", DASH_OPT, pane)
    elif not alone and aimed:
        dash_unpoint()


def dash_unpoint():
    """Put the keys back to finding the dashboard the slow way."""
    bind_all(FALLBACK_KEYS)
    tmux("set", "-s", "-u", DASH_OPT)


def dash_alive(entry):
    """True when this record still names a running dashboard."""
    try:
        pid = int(entry.get("pid") or 0)
    except (AttributeError, TypeError, ValueError):
        return False
    if pid <= 0 or not os.path.isdir(f"/proc/{pid}"):
        return False
    # Pids get reused. The start time pins it to the process we wrote down.
    want = entry.get("start")
    return not want or proc_start(pid) == want


def pane_roles():
    """{pane_id: (role, session)} for every pane, from one cached listing.

    This had a listing of its own until the fields moved onto the shared one:
    two `list-panes -a` calls a refresh, asking about the same panes a
    millisecond apart.
    """
    return _panes()["roles"]


def dash_claimed():
    """{pane_id: record} for panes that are running a dashboard and are one.

    Two things have to agree, and for a while only one of them was asked. The
    book says which processes are alive. The pane's own mark says which pane is
    the dashboard. A claim without the mark is a `bottleneck watch` running
    somewhere that nothing points at - which dash_pane already declined to
    treat as the dashboard, but which still counted itself here, and the count
    is what decides whether the movement keys can name a pane.

    So one unmarked claim stood the fast keys down for the real dashboard, and
    every M-j after that started a program to work out where to go: a fifth of
    a second, per press, for a dashboard that was not one.
    """
    book = {p: v for p, v in read_json(DASHES, {}).items() if dash_alive(v)}
    if not book:
        return book
    roles = pane_roles()
    return {p: v for p, v in book.items()
            if roles.get(p, ("", ""))[0] == "dash"}


def panes_with_heads():
    """Pane ids with a claude running in them. Costs a /proc walk, so it is
    only used to settle a mark nothing has claimed."""
    panes, _ = panes_by_pid()
    busy = set()
    for row in claude_procs():
        pane, _, _ = locate(row["pid"], panes)
        if pane:
            busy.add(pane)
    return busy


def dash_pane():
    """The dashboard pane to act on.

    Only a pane a live dashboard has claimed counts. A stale mark is cleared on
    the way past, so the next `bottleneck start` builds a real one instead of
    finding this one and deciding there is nothing to do.

    With more than one dashboard running, prefer the one in our own session -
    otherwise a command fired from one dashboard lands in another's window.

    The answer costs two tmux calls and a /proc walk, and three different
    callers want it during one keypress, so it is remembered for a moment.
    """
    return cached("dash_pane", _dash_pane)


def _dash_pane():
    claimed = dash_claimed()
    found, stale = [], []
    for pane, (role, sess) in pane_roles().items():
        if role != "dash":
            continue
        (found if pane in claimed else stale).append((pane, sess))

    if not found and stale and not read_json(DASHES, {}):
        # Nothing has ever been claimed: either the running dashboard predates
        # this check, or state was wiped under it. Trust the marks rather than
        # declare the layout broken - but do not clear them.
        #
        # An empty book is the only reason to do that. A book whose entries are
        # all dead says the opposite: those dashboards are gone, and their marks
        # are exactly what we must not follow.
        # One thing still disqualifies a mark with no book behind it: a pane
        # with a claude running in it is a head's pane, whoever marked it.
        busy = panes_with_heads()
        found = [(p, s) for p, s in stale if p not in busy]
        stale = [(p, s) for p, s in stale if p in busy]
    for pane, _ in stale:
        tmux("set", "-p", "-u", "-t", pane, ROLE)
    if not found:
        return ""

    # Which session are we running in? $TMUX_PANE is set inside a pane, and the
    # listing already said which session every pane belongs to.
    mine = pane_session(os.environ.get("TMUX_PANE", ""))
    for pane, sess in found:
        if mine and sess == mine:
            return pane
    for pane, sess in found:
        if sess == SESSION:
            return pane
    return found[0][0]
