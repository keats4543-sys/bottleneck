"""Aiming the movement keys at the dashboard. tmux is faked - no server runs.

Alt+j used to start a program whose whole job was to find the dashboard. A
running dashboard already knows where it is, so it writes its own pane into the
binding and the key never leaves tmux.

The catch these tests exist for: key tables belong to the tmux server, not to a
session. A binding that names a pane names it for every session at once, so the
moment there are two dashboards the fast keys have to stand down - the slow path
is the only one that can tell whose Alt+j it was.
"""
import os

from harness import bn as m
from bottleneck import tmuxio

CALLS = []          # every tmux() and tmux_many() we made
HINT = [""]         # what tmux currently has in the pointer option
CLAIMED = [{}]      # live dashboards, as dash_claimed() sees them


def fake_tmux(*args, **kw):
    CALLS.append(args)
    if args[:2] == ("set", "-s") and len(args) > 3:
        HINT[0] = args[-1]
    if args[:3] == ("set", "-s", "-u"):
        HINT[0] = ""
    return None


def fake_many(*cmds):
    # A prefixed key has no -n, so the key is not at a fixed index - but it is
    # always the argument the action follows.
    CALLS.append(("MANY", tuple(c[-2] for c in cmds)))
    return None


# Kept before the stub below takes its place: the last section tests the real
# one, and the harness sets a name on every module that has it.
REAL_DASH_HINT = tmuxio.dash_hint

m.tmux = fake_tmux
m.tmux_many = fake_many
m.dash_hint = lambda: HINT[0]
m.dash_claimed = lambda: CLAIMED[0]

FAILED = []


def check(label, got, want):
    ok = got == want
    print(("  ok   " if ok else "  FAIL ") + label.ljust(54)
          + ("" if ok else f"got {got!r}, wanted {want!r}"))
    if not ok:
        FAILED.append(label)


def bound_keys():
    """The keys the last bind_all touched, in order."""
    for c in reversed(CALLS):
        if c[0] == "MANY":
            return list(c[1])
    return []


def setup(hint, claimed):
    CALLS.clear()
    HINT[0] = hint
    CLAIMED[0] = claimed


print("\nthe keys a dashboard writes for itself")
keys = m.dash_keys("%4")
actions = {k: a for k, _, a in keys}
check("the go-on key types j into the dashboard's own stdin",
      actions["M-j"], "send-keys -t %4 j")
check("its older name does the same", actions["M-a"], actions["M-j"])
check("and so does the prefixed one", actions["a"], actions["M-j"])
check("back-to-the-dash needs no program either",
      actions["M-d"], "select-window -t %4 ; select-pane -t %4")
check("the swap key asks tmux which side you are on",
      actions["M-o"].startswith("if -F '#{==:#{pane_id},%4}'"), True)
check("every key names a real pane, never a format",
      all("#{@" not in a for a in actions.values()), True)
check("the fallbacks cover exactly the same keys",
      sorted(k for k, _, _ in m.FALLBACK_KEYS), sorted(actions))
check("and every fallback goes back through the program",
      all("bottleneck" in a for _, _, a in m.FALLBACK_KEYS), True)

# By path, not by name. tmux forks these from the server, whose PATH is whatever
# it was when the server started - and run-shell -b throws "command not found"
# away, so a name it cannot resolve is a key that silently does nothing.
check("by a path, never by a bare name",
      all(f"'{m.our_path()} " in a for _, _, a in m.FALLBACK_KEYS), True)
check("and that path is absolute", m.our_path().startswith("/"), True)

# The static bindings, which are what is in play before a dashboard has rebound
# anything - the very moment a stale PATH is most likely.
import re
conf = open(os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "tmux.conf")).read()
bare = [l.strip() for l in conf.splitlines()
        if not l.strip().startswith("#")
        and re.search(r'run-shell[^"\']*["\']\s*bottleneck\b', l)]
check("the shipped bindings name no bare command either", bare, [])
check("they use the install path", conf.count("~/.local/bin/bottleneck") >= 6, True)

print("\none dashboard: the keys aim straight at it")
setup(hint="", claimed={"%4": {}})
m.dash_point("%4")
check("the keys are rebound", bound_keys(), [k for k, _, _ in keys])
check("and the pointer records which pane they name", HINT[0], "%4")

setup(hint="%4", claimed={"%4": {}})
m.dash_point("%4")
check("already aimed here -> nothing is rebound", CALLS, [])

print("\ntwo dashboards: nobody may claim a server-wide key")
setup(hint="%4", claimed={"%4": {}, "%9": {}})
m.dash_point("%4")
check("the fast keys stand down",
      bound_keys(), [k for k, _, _ in m.FALLBACK_KEYS])
check("and the pointer is cleared with them", HINT[0], "")

setup(hint="", claimed={"%4": {}, "%9": {}})
m.dash_point("%4")
check("a second dashboard starting never aims them", CALLS, [])

print("\nand back again once it is alone")
setup(hint="", claimed={"%4": {}})
m.dash_point("%4")
check("the fast keys come back", bound_keys(), [k for k, _, _ in keys])
check("aimed at the one left running", HINT[0], "%4")

print("\nletting go")
setup(hint="%4", claimed={})
m.dash_unpoint()
check("quitting puts the slow keys back",
      bound_keys(), [k for k, _, _ in m.FALLBACK_KEYS])
check("and leaves nothing pointing anywhere", HINT[0], "")

setup(hint="", claimed={"%4": {}})
m.dash_point("")
check("a dashboard outside tmux aims nothing", CALLS, [])

print()
print("and asking tmux where the keys point is not a thing to do every refresh")
# The check above is idempotent and cheap to make and was costing a fork, an
# exec and a socket round-trip - about 7ms - every two seconds, to be told what
# tmux said last time. What it watches for is a second dashboard appearing,
# which a few seconds late is still settling either way.
ASKED = [0]


def counted(*args):
    ASKED[0] += 1
    return "%9"


tmuxio.tmux_out = counted
tmuxio._steady.clear()

check("the first refresh asks", (REAL_DASH_HINT(), ASKED[0]), ("%9", 1))
check("the next several do not",
      ([REAL_DASH_HINT() for _ in range(20)][-1], ASKED[0]), ("%9", 1))
tmuxio.invalidate()
check("and a pane moving does not make it a question again - "
      "this is not about panes", (REAL_DASH_HINT(), ASKED[0]), ("%9", 1))
tmuxio.steady_set("dash_hint", "%3")
check("what we write, we know without asking",
      (REAL_DASH_HINT(), ASKED[0]), ("%3", 1))
tmuxio._steady["dash_hint"] = (-1e9, "%3")         # older than the window
check("and it is asked again once the window is up",
      (REAL_DASH_HINT(), ASKED[0]), ("%9", 2))

print()
print("all pass" if not FAILED else f"{len(FAILED)} FAILED")
raise SystemExit(1 if FAILED else 0)
