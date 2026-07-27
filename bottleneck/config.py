"""Where everything lives, and what the words mean.

Paths, tunables and the state table. Nothing here reaches out to the machine -
import it from anywhere without a cycle.
"""
import os


VERSION = "0.1.0"


HOME = os.path.expanduser("~")


# Claude Code's own files. Read-only as far as we are concerned.
SESSIONS = os.path.join(HOME, ".claude", "sessions")


PROJECTS = os.path.join(HOME, ".claude", "projects")


# ...and everywhere else one might be.
#
# Under WSL, claude is often the one installed on the Windows side, reached
# through an alias. It writes its files to the Windows home - readable from
# here, at /mnt/c/Users/<you>/.claude - and writes nothing at all to the Linux
# home we would otherwise be the only place we look. The heads run, and the
# dashboard shows an empty list.
#
# So the search is a list. The Linux home first, because it is ours and it is
# fast; anything else after it. Set BOTTLENECK_CLAUDE_HOMES to colon-separated
# directories to say exactly where, or leave it and the Windows homes are found
# by looking - only when there is a /mnt/c to look in, so this costs nothing on
# a machine that is not WSL.
#
# What comes back from over there is not the same kind of thing. A head in the
# Windows home has a Windows pid, and this side's /proc knows nothing about it -
# see FOREIGN in procs.py, which is what stops us reading that number as one of
# ours and signalling whatever local process happens to be wearing it.

def _windows_homes():
    """Every Windows-side ~/.claude a WSL box can see, or nothing."""
    import glob
    if not os.path.isdir("/mnt/c/Users"):
        return []
    return sorted(d for d in glob.glob("/mnt/c/Users/*/.claude")
                  if os.path.isdir(os.path.join(d, "sessions")))


def claude_homes():
    """Every .claude directory to read heads out of, ours first.

    Worked out once, at import: a drive that is mounted does not come and go
    under a running dashboard, and globbing /mnt/c on every refresh would put a
    Windows filesystem round-trip in the middle of the loop.
    """
    said = os.environ.get("BOTTLENECK_CLAUDE_HOMES", "").strip()
    if said:
        homes = [os.path.expanduser(p) for p in said.split(":") if p.strip()]
    else:
        homes = [os.path.join(HOME, ".claude")] + _windows_homes()
    seen, out = set(), []
    for h in homes:
        h = os.path.realpath(h)
        if h not in seen:
            seen.add(h)
            out.append(h)
    return out


CLAUDE_HOMES = claude_homes()


SESSION_DIRS = [os.path.join(h, "sessions") for h in CLAUDE_HOMES]


PROJECT_DIRS = [os.path.join(h, "projects") for h in CLAUDE_HOMES]


# Ours. Everything we write lives here and nowhere else.
STATE = os.path.expanduser(os.environ.get("BOTTLENECK_STATE")
                           or os.path.join(HOME, ".bottleneck"))


ATTN = os.path.join(STATE, "attention")


ACKS = os.path.join(STATE, "acks")


AUTO_FLAG = os.path.join(STATE, "autoraise")


CATALOG = os.path.join(STATE, "catalog.json")


CYCLE = os.path.join(STATE, "cycle.json")


SLOTS = os.path.join(STATE, "slots.json")


DASHES = os.path.join(STATE, "dash.json")


QUEUE = os.path.join(STATE, "queue.json")


CLAIMS = os.path.join(STATE, "claims.json")


# Which pane we opened a head into, for heads whose pane cannot be found
# by walking the process tree - see BINDS in store.py.
BINDS = os.path.join(STATE, "binds.json")


CONFIG = os.path.join(STATE, "config")


def load_config():
    """KEY=VALUE lines in ~/.bottleneck/config, as defaults for the knobs.

    A real environment variable always wins, so a one-off `BOTTLENECK_X=1 ...`
    still does what you expect. Kept deliberately dumb: no quoting, no shell.
    """
    try:
        with open(CONFIG) as fh:
            lines = fh.readlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        # Quotes come off only in pairs. Stripping them one end at a time ate
        # the last character of any value that ended in one - and the key for
        # "sent, next" on a terminal that has taken Alt+Enter is very likely to
        # be M-', which arrived as M- and made tmux say "unknown key".
        for q in ('"', "'"):
            if len(val) > 1 and val[0] == q and val[-1] == q:
                val = val[1:-1]
                break
        if key.startswith("BOTTLENECK_"):
            os.environ.setdefault(key, val)


# Before the knobs below read the environment, because that is the only moment
# it can matter: they are constants, bound once at import, and a config file
# read after them changes nothing.
#
# This call went missing when one file became a package - the function came
# across, the call did not - and every knob in ~/.bottleneck/config has been
# read by the shell half of `bottleneck new` and ignored by everything written
# in Python since. BOTTLENECK_DANGEROUS=1 is the one you notice: heads started
# from the dashboard came up asking permission for every command, while heads
# started from the command line did not.
load_config()


# What to run to start a head. Set this to an absolute path when `claude` is an
# alias, a shell function, or on a PATH that only an interactive shell sets up:
# tmux runs a pane's command through a shell that is neither interactive nor a
# login shell, so none of those three exist by the time the pane starts.
CLAUDE = os.environ.get("BOTTLENECK_CLAUDE", "").strip()


# The shell to ask what `claude` means, when we have to ask. Yours, not sh:
# an alias lives in the shell that defined it.
SHELL = os.environ.get("SHELL") or "/bin/bash"


STALL_SECS = int(os.environ.get("BOTTLENECK_STALL_SECS", "300"))


# How long a head we have just opened has to turn up in the list before the
# queue stops waiting for it. Only ever a backstop - the wait normally ends the
# moment the head appears, a second or two after launch. It exists for the
# launch that never becomes a head at all, so a pane printing "command not
# found" cannot hold the queue for the rest of the day.
SPINUP_SECS = float(os.environ.get("BOTTLENECK_SPINUP_SECS", "30"))


REFRESH = float(os.environ.get("BOTTLENECK_REFRESH", "2"))


SESSION = os.environ.get("BOTTLENECK_TMUX_SESSION", "bottleneck")


HEAD_PCT = os.environ.get("BOTTLENECK_HEAD_PCT", "62")


# ------------------------------------------------------------------- the keys
#
# Which key does what, from ~/.bottleneck/config like everything else.
#
# They were spread across three places that had to agree and could not be told
# otherwise: a static tmux.conf, the fast bindings a running dashboard installs,
# and the slow ones it puts back when it stands down. Changing a key meant
# editing a tracked file and knowing which of the three mattered when.
#
# There is one reason this had to become configurable rather than stay a good
# default, and it is not taste. Alt+Enter - the key for "sent, take me to the
# next one" - never arrives on Windows: the terminal binds it to full screen
# before WSL sees it, and the legacy console host cannot be told otherwise. A
# scheme with no way to say "use this key instead" is a scheme that does not
# work on that machine at all.
#
#   BOTTLENECK_KEY_SENDGO=prefix:Enter,M-'
#
# Comma-separated, in the order you want them bound. A bare key is no-prefix,
# so it works while you are typing at a head. `prefix:` in front means after
# your tmux prefix. An empty value binds nothing at all, which is how you hand
# a key back to whatever else wanted it:
#
#   BOTTLENECK_KEY_SENDGO=
#
# The names are tmux's own, so anything tmux accepts works here: M-j, C-Space,
# F5, prefix:Enter.

def _keys(name, default):
    """Parse one action's keys. (key, table) pairs, in the order given."""
    said = os.environ.get("BOTTLENECK_KEY_" + name)
    said = default if said is None else said
    out = []
    for spec in said.split(","):
        spec = spec.strip()
        if not spec:
            continue
        table, _, key = spec.rpartition(":")
        # Only "prefix:" means the prefix table. Anything else before a colon is
        # part of the key - tmux has no key with a colon in it today, but
        # guessing that it never will is not worth the line it saves.
        if table and table.lower() != "prefix":
            key, table = spec, ""
        out.append((key, "prefix" if table.lower() == "prefix" else "root"))
    return out


# Exactly what was bound before any of this was configurable.
KEY_DEFAULTS = {
    "next": "M-j,M-a,prefix:a",
    "sendgo": "M-Enter,prefix:M-Enter,prefix:Enter",
    "dash": "M-d",
    "swap": "M-o,prefix:o",
    "left": "M-Left",
    "right": "M-Right",
    "start": "prefix:F",
    "reload": "prefix:R",
}

KEYS = {act: _keys(act.upper(), spec) for act, spec in KEY_DEFAULTS.items()}


def default_keys():
    """Every key a default names, whatever the config now says.

    The generated file unbinds these before it binds anything, so that taking a
    key out of the config actually takes it off the keyboard. Without it,
    tmux.conf's own bindings - which are what a tmux that has never run
    bottleneck falls back on - would quietly stay in force, and setting
    BOTTLENECK_KEY_SENDGO= to hand Alt+Enter back would not hand it back.
    """
    seen, out = set(), []
    for spec in KEY_DEFAULTS.values():
        for key, table in _keys("__none__", spec):
            if (key, table) not in seen:
                seen.add((key, table))
                out.append((key, table))
    return out


# Where the generated bindings are written. tmux cannot read the config file
# above - it is ours, not its - so the keys are turned into a tmux config here
# and sourced. Regenerated whenever bottleneck starts, watches or reloads, so
# editing the config file and reloading is the whole of the workflow.
KEYS_CONF = os.path.join(STATE, "keys.conf")


ROLE = "@bottleneck_role"


# A server-wide tmux option naming the dashboard's pane. The bindings read it
# so the common keys are pure tmux - `send-keys -t <dash> j` costs a fraction
# of a millisecond, where starting python to work the same thing out costs
# well over a hundred. The dashboard sets it, refreshes it on every redraw and
# unsets it on the way out.
DASH_OPT = "@bottleneck_dash"


# The dashboard's control fifo. Keys that cannot be expressed in tmux alone
# write one line here instead of starting a program; the dashboard is already
# running and already holds fresh state, so it does the work.
CTL = os.path.join(STATE, "ctl")


# The attention counter in the status line, as a server option the dashboard
# writes. It used to be `#(bottleneck bar)`, which tmux re-ran every
# status-interval: a whole python startup and a cold read of every transcript -
# 190 to 350ms, twice a minute per second, on a box whose cores the heads were
# already fighting over. The dashboard recounts anyway on every refresh and has
# the answer in hand, so it puts it where tmux can read it without running
# anything. Unset while no dashboard is up, which is the honest state: a count
# nothing is maintaining should not sit there looking current.
BAR_OPT = "@bottleneck_bar"


def default_dir():
    """Where new heads land unless you say otherwise: here, else home."""
    cand = os.environ.get("BOTTLENECK_DEFAULT_DIR")
    if cand and os.path.isdir(os.path.expanduser(cand)):
        return os.path.expanduser(cand)
    try:
        return os.getcwd()
    except OSError:
        return HOME


DEFAULT_DIR = default_dir()


# Heads launch with permission prompts on, like plain `claude`. Set
# BOTTLENECK_DANGEROUS=1 to launch them with --dangerously-skip-permissions
# instead - handy for a fleet you are not watching, and exactly as risky as it
# sounds.
DANGEROUS = (" --dangerously-skip-permissions"
             if os.environ.get("BOTTLENECK_DANGEROUS", "0").lower()
             in ("1", "yes", "true", "on") else "")


STATES = {
    "BLOCKED": (0, "BLOCKED", "1;91"),
    "WAITING": (1, "WAITING", "1;93"),
    "DONE":    (2, "DONE",    "1;92"),
    "STALLED": (3, "STALLED", "1;95"),
    "WORKING": (6, "working", "96"),
    # A head you have seen and put aside. It is finished, and you decided it can
    # wait - so it sorts below heads that are still working, which is the whole
    # point: "done" should not outrank "busy" once you have read it.
    "HELD":    (7, "held",    "35"),
    "IDLE":    (8, "idle",    "90"),
    "DEAD":    (9, "dead",    "90"),
}


NEEDS_ATTENTION = ("BLOCKED", "WAITING", "DONE", "STALLED")


def c(code, text):
    if os.environ.get("NO_COLOR"):
        return text
    return f"\033[{code}m{text}\033[0m"


AGENTS_TTL = float(os.environ.get("BOTTLENECK_AGENTS_TTL", "20"))


# How much of a summary is worth keeping. It is not the pane width: the summary
# is wrapped onto its own lines under the row, so this is the budget across all
# of them, and wrap_body does the cutting to fit.
SUMMARY_CHARS = 170


# The prompt line is shorter than the summary on purpose. It is there to remind
# you what the head is for, not to be read word for word - one line, no wrap.
TASK_CHARS = 120


# How far back to look for your prompt. A quarter megabyte is plenty to find
# the last thing said and nowhere near enough to find the last thing asked: one
# turn of a head reading files can run to half a megabyte of tool output on its
# own, and the prompt that started it sits behind all of it. A head whose turn
# outruns even this loses the line, which is why the row does not depend on it.
TASK_BUDGET = int(os.environ.get("BOTTLENECK_TASK_BUDGET", 1 << 20))


# Whether a row also shows the prompt it is working on: all, attention (only
# the rows asking for you), or off. It costs a row of height per head, which is
# the thing the list can least spare - and it buys the one fact the rest of the
# row cannot give you at any width, because everything else there is read off
# the head's own last message. `attention` is the cheaper half of it.
TASKLINE = os.environ.get("BOTTLENECK_TASKLINE", "all").strip().lower()


# ---------------------------------------------------------------------- output

SUB_INDENT = 6


SUB_LINES = max(1, int(os.environ.get("BOTTLENECK_SUBLINES", "2")))
