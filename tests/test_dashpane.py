"""Finding the dashboard pane. tmux is faked - no server is touched.

A pane outlives the process in it, so the tmux mark saying "dashboard" can go on
pointing at a pane whose dashboard died - and if a head is started there, every
later join-pane aims at a head. These tests hold the mark to its promise: it
counts only while a live dashboard claims the pane.
"""
import json
import os
import tempfile

from harness import bn as m

m.DASHES = os.path.join(tempfile.mkdtemp(), "dash.json")

PANES = []          # (pane_id, role, session)
CALLS = []          # every tmux() we made
ALIVE = set()       # pids the fake /proc knows about


def fake_tmux_out(*args):
    if args[:2] == ("list-panes", "-a"):
        # The one listing everything reads: pid, pane, target, window, the
        # active flags, our mark, the session, copy-mode. Written out in full
        # rather than shortened, because the parsing is the thing under test -
        # a fake that agrees with a format nothing produces tests nothing.
        return "\n".join(f"0\t{p}\t{s}:0.0\t{s}:0\t00\t{r}\t{s}\t0"
                         for p, r, s in PANES)
    if args[0] == "display-message":
        return "bottleneck"
    return ""


def fake_tmux(*args, **kw):
    CALLS.append(args)
    return None


m.tmux_out = fake_tmux_out
m.tmux = fake_tmux
m.proc_start = lambda pid: f"start-{pid}" if pid in ALIVE else None
_isdir = os.path.isdir
m.os.path.isdir = lambda p: (int(p.rsplit("/", 1)[-1]) in ALIVE
                             if p.startswith("/proc/") else _isdir(p))

FAILED = []


def check(label, got, want):
    ok = got == want
    print(("  ok   " if ok else "  FAIL ") + label.ljust(48)
          + ("" if ok else f"got {got!r}, wanted {want!r}"))
    if not ok:
        FAILED.append(label)


def setup(panes, claims, alive):
    PANES[:] = panes
    CALLS.clear()
    # dash_pane remembers its answer for a moment, so a keypress does not ask
    # tmux the same question four times. A new scenario is a new world: drop it,
    # the way anything that moves a pane does.
    m.invalidate()
    ALIVE.clear()
    ALIVE.update(alive)
    with open(m.DASHES, "w") as fh:
        json.dump({p: {"pid": pid, "start": f"start-{pid}"}
                   for p, pid in claims.items()}, fh)


def cleared():
    return [a[4] for a in CALLS if a[:2] == ("set", "-p") and "-u" in a]


print("\na mark backed by a running dashboard")
setup([("%1", "dash", "bottleneck")], {"%1": 100}, {100})
check("is the dashboard", m.dash_pane(), "%1")
check("and is left alone", cleared(), [])

print("\na mark whose dashboard has gone")
setup([("%1", "dash", "bottleneck")], {"%1": 100}, set())
check("is not the dashboard", m.dash_pane(), "")
check("and the mark is cleared", cleared(), ["%1"])

print("\nthe pane a head took over")
setup([("%1", "dash", "bottleneck"), ("%2", "dash", "bottleneck")],
      {"%2": 200}, {200})
check("loses to the pane that is really running one", m.dash_pane(), "%2")
check("and is unmarked so nothing aims at it again", cleared(), ["%1"])

print("\na recycled pid cannot inherit a claim")
setup([("%1", "dash", "bottleneck")], {"%1": 100}, {100})
with open(m.DASHES, "w") as fh:                       # same pid, booted later
    json.dump({"%1": {"pid": 100, "start": "start-999"}}, fh)
check("start time has to match too", m.dash_pane(), "")

print("\nnobody has claimed anything")
setup([("%1", "dash", "bottleneck")], {}, set())
check("the mark is trusted rather than the layout called broken",
      m.dash_pane(), "%1")
check("and is not cleared", cleared(), [])

print("\nan unclaimed mark on a pane with a head in it")
setup([("%1", "dash", "bottleneck"), ("%2", "dash", "bottleneck")], {}, set())
m.panes_with_heads = lambda: {"%1"}
check("loses to the empty pane", m.dash_pane(), "%2")
check("and is cleared", cleared(), ["%1"])
setup([("%1", "dash", "bottleneck")], {}, set())
check("on its own it leaves us with no dashboard", m.dash_pane(), "")
m.panes_with_heads = lambda: set()

print("\nclaims come and go")
# The pane carries the mark, because a dashboard sets it on itself the moment
# it starts - and a claim without it does not count as one. See below.
setup([("%7", "dash", "bottleneck")], {}, {os.getpid()})
m.proc_start = lambda pid: f"start-{pid}"
m.dash_register("%7")
check("registering records this process",
      m.dash_claimed()["%7"]["pid"], os.getpid())
m.dash_release("%7")
check("releasing drops it", m.dash_claimed(), {})
check("and clears the mark", ("set", "-p", "-u", "-t", "%7", m.ROLE) in CALLS, True)

print("\na dead dashboard's claim is swept on the next register")
PANES[:] = [("%8", "dash", "bottleneck"), ("%9", "dash", "bottleneck")]
m.invalidate()          # a new listing, so the remembered one is not the answer
with open(m.DASHES, "w") as fh:
    json.dump({"%9": {"pid": 4242, "start": "boot"}}, fh)
ALIVE.clear()
ALIVE.add(os.getpid())
m.proc_start = lambda pid: f"start-{pid}" if pid in ALIVE else None
m.dash_register("%8")
check("only the live one is left", sorted(m.dash_claimed()), ["%8"])

print("\na claim is only a dashboard while its pane says so")
# One `bottleneck watch` running in a pane nothing points at used to count
# here, and the count is what decides whether the movement keys can name a
# pane - so an unmarked claim stood the fast keys down for the real dashboard.
setup([("%1", "dash", "bottleneck"), ("%2", "", "bottleneck")],
      {"%1": os.getpid(), "%2": os.getpid()}, {os.getpid()})
check("the marked pane counts", sorted(m.dash_claimed()), ["%1"])
check("and the unmarked one does not", "%2" in m.dash_claimed(), False)
setup([("%1", "dash", "bottleneck"), ("%2", "dash", "bottleneck")],
      {"%1": os.getpid(), "%2": os.getpid()}, {os.getpid()})
check("two real dashboards both count", sorted(m.dash_claimed()), ["%1", "%2"])
setup([("%1", "dash", "bottleneck")],
      {"%1": os.getpid(), "%3": os.getpid()}, {os.getpid()})
check("a claim whose pane has gone does not count",
      sorted(m.dash_claimed()), ["%1"])

print("\ncorrupt state is not a crash")
with open(m.DASHES, "w") as fh:
    fh.write("{not json")
check("unreadable claims read as none", m.dash_claimed(), {})

print("\nall pass" if not FAILED else f"\n{len(FAILED)} FAILED")
raise SystemExit(1 if FAILED else 0)
