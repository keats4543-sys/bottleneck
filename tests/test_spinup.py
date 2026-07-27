"""The queue stands off the main pane while a head we opened is coming up.

The window this is about: spawn() has the pane, and the head will not exist on
disk for another second or two. collect() cannot see it, so nothing in the list
is in_main, so the main pane reads as free - and a free main pane is exactly
what auto_raise is looking for. No tmux is touched.
"""
import time

from harness import bn as m

MOVED = []
PANES = {}              # what tmux would say is there


def fake_focus(head, heads, select=True, ack=True):
    MOVED.append((head["name"], select, ack))
    return True


m.focus = fake_focus
m.pane_is_active = lambda pane: False
m.panes_by_id = lambda: PANES


def head(name, state, in_main=False, pane="%1", pid=None):
    return {"name": name, "state": state, "pid": pid or abs(hash(name)) % 9999,
            "pane_id": pane, "in_main": in_main,
            "attention": state in m.NEEDS_ATTENTION,
            "priority": m.STATES[state][0], "idle_for": 10, "elsewhere": False}


def opening(pane="%new", label="delta", left=None):
    """Stand where spawn() leaves things: pane open, head not yet on disk.

    `left` is seconds still on the backstop, so a test can say "just started"
    without pretending to know what time it is - auto_raise asks the clock
    itself and cannot be handed one.
    """
    PANES.clear()
    PANES.update({"%1": (1, "s:0.0", 0), pane: (2, "s:0.1", 0)})
    MOVED.clear()
    left = m.SPINUP_SECS if left is None else left
    m._starting = (pane, label, time.time() + left)


def check(label, got, want):
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {label:<52}"
          f"{'' if ok else f' {got!r} != {want!r}'}")
    return ok


A = head("alpha", "WAITING", pane="%1", pid=101)
D = head("delta", "IDLE", in_main=True, pane="%new", pid=105)
results = []

# ---------------------------------------------------------------- the freeze
print("a head is coming up in the main pane")
opening()
results += [
    check("the queue names what it is waiting for", m.starting([A]), "delta"),
    check("and nothing is raised into the pane it is in",
          m.auto_raise([A], None, "%dash"), (None, None)),
    check("so the new head is not evicted to make room", MOVED, []),
]

# The other half of the same rule: asking is still asking. A queue that ignored
# your keypress because something else was starting would be a worse bargain
# than the one this fixes.
opening()
results.append(
    check("but a raise you asked for still happens",
          bool(m.focus(A, [A])) and MOVED[-1][0], "alpha"))

# --------------------------------------------- and the three ways the wait ends
print("\nthe wait ends when it should")
opening()
results += [
    check("the head turns up in the list -> over", m.starting([A, D]), ""),
    check("and the queue moves again",
          m.auto_raise([A, D], None, "%dash")[0]["name"], "alpha"),
]

opening()
PANES.pop("%new")
results += [
    check("the pane went away -> over", m.starting([A]), ""),
    check("and the queue moves again",
          m.auto_raise([A], None, "%dash")[0]["name"], "alpha"),
]

# A launch that never becomes a head - `claude: command not found` sitting in a
# pane that will not go away by itself - must not hold the queue for ever.
opening(left=5)
results.append(
    check("still waiting a moment before the backstop", m.starting([A]), "delta"))

opening(left=-1)
results += [
    check("a head that never arrives times out", m.starting([A]), ""),
    check("and the queue moves again",
          m.auto_raise([A], None, "%dash")[0]["name"], "alpha"),
]

# --------------------------------------------------------------- nothing open
print("\nwith nothing starting it is the queue it always was")
m._starting = None
MOVED.clear()
results += [
    check("no wait to report", m.starting([A]), ""),
    check("and the raise happens as before",
          m.auto_raise([A], None, "%dash")[0]["name"], "alpha"),
]

print()
print("all pass" if all(results) else "FAILURES")
raise SystemExit(0 if all(results) else 1)
