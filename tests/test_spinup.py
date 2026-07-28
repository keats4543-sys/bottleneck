"""A pane we have opened is on the list before it is a head.

The window this is about: spawn() has the pane, and the head will not exist on
disk for another second or two - or, if claude is asking whether it may read
this folder, for as long as you take to answer. collect() cannot see any of
that, so nothing in the list is in_main, the main pane reads as free, and a
free main pane is exactly what auto_raise is looking for.

So the pane gets a row of its own until it becomes a head or goes away. No tmux
is touched: every listing here is a fake.
"""
import time

from harness import bn as m

MOVED = []
PANES = {}              # what tmux would say is there
SCREEN = {}             # what tmux would say is on their screens


def fake_focus(head, heads, select=True, ack=True):
    MOVED.append((head["name"], select, ack))
    return True


m.focus = fake_focus
m.pane_is_active = lambda pane: False
m.panes_by_id = lambda: PANES
m.panes_by_pid = lambda: ({pid: v for v, (pid, _, _) in ()}, set())
m.pane_window = lambda pane: PANES.get(pane, (0, "", ""))[2]
m.dash_pane = lambda: "%1"
m.pane_text = lambda pane, lines=40: SCREEN.get(pane, "")


def head(name, state, in_main=False, pane="%1", pid=None):
    return {"name": name, "state": state, "pid": pid or abs(hash(name)) % 9999,
            "session_id": f"sid-{name}", "pane_id": pane, "in_main": in_main,
            "attention": state in m.NEEDS_ATTENTION,
            "priority": m.STATES[state][0], "idle_for": 10, "elsewhere": False}


def opening(pane="%new", label="delta", left=None, screen=""):
    """Stand where spawn() leaves things: pane open, head not yet on disk.

    `left` is seconds still on the backstop, so a test can say "just started"
    without pretending to know what time it is.
    """
    PANES.clear()
    PANES.update({"%1": (1, "s:0.0", "s:0"), pane: (2, "s:0.1", "s:0")})
    SCREEN.clear()
    SCREEN[pane] = screen
    MOVED.clear()
    left = m.SPINUP_SECS if left is None else left
    now = time.time()
    m._starting = [{"pane": pane, "label": label, "cwd": "/tmp",
                    "at": now - (m.SPINUP_SECS - left), "until": now + left}]


def listed(heads):
    """The list as the dashboard builds it: panes first, then heads."""
    return m.pending(heads) + heads


def check(label, got, want):
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {label:<52}"
          f"{'' if ok else f' {got!r} != {want!r}'}")
    return ok


TRUST = """
╭──────────────────────────────────────────────╮
│ Do you trust the files in this folder?        │
│                                              │
│ /home/you/some-repo                           │
│                                              │
│ Claude Code may read files in this folder.    │
│                                              │
│ ❯ 1. Yes, proceed                             │
│   2. No, exit                                 │
╰──────────────────────────────────────────────╯
"""

# Captured off a real pane. The wording has changed at least once - it used to
# be a boxed "Do you trust the files in this folder?" - and the question mark is
# in the middle of a wrapped paragraph rather than at the end of a line, which
# is the case a "line ending in ?" reader gets wrong.
REAL_TRUST = """
 Accessing workspace:

 /tmp/some-folder

 Quick safety check: Is this a project you created or one you trust? (Like your
 own code, a well-known open source project, or work from your team). If not,
 take a moment to review what's in this folder first.

 Claude Code'll be able to read, edit, and execute files here.

 Security guide

 ❯ 1. Yes, I trust this folder
   2. No, exit

 Enter to confirm · Esc to cancel
"""

A = head("alpha", "WAITING", pane="%1", pid=101)
D = head("delta", "IDLE", in_main=True, pane="%new", pid=105)
results = []

# ------------------------------------------------------------------ the row
print("a head is coming up in the main pane")
opening()
rows = m.pending([A])
results += [
    check("the pane we opened is on the list", [r["name"] for r in rows],
          ["delta"]),
    check("as something starting rather than as a head",
          rows[0]["state"], "STARTING"),
    check("it says so, and spins while it does",
          (rows[0]["reason"], rows[0]["spin"]),
          ("starting up - waiting for claude", True)),
    check("it is not asking for you yet", rows[0]["attention"], False),
    check("and it is the pane beside the dashboard", rows[0]["in_main"], True),
    check("with the pane's own pid, so x has something to wait on",
          rows[0]["pid"], 2),
]

# ---------------------------------------------------------------- the freeze
opening()
results += [
    check("the queue names what it is waiting for",
          m.starting(listed([A])), "delta"),
    check("and nothing is raised into the pane it is in",
          m.auto_raise(listed([A]), None, "%dash"), (None, None)),
    check("so the new head is not evicted to make room", MOVED, []),
]

# The other half of the same rule: asking is still asking. A queue that ignored
# your keypress because something else was starting would be a worse bargain
# than the one this fixes.
opening()
results.append(
    check("but a raise you asked for still happens",
          bool(m.focus(A, [A])) and MOVED[-1][0], "alpha"))

# ------------------------------------------------ the question nobody could see
print("\na pane asking whether it may read the folder")
opening(screen=TRUST)
rows = m.pending([A])
results += [
    check("reads as waiting on you", rows[0]["state"], "WAITING"),
    check("and says what it is asking", rows[0]["reason"],
          "asks: Do you trust the files in this folder?"),
    check("so it counts as needing you", rows[0]["attention"], True),
    check("with no spinner - it is not loading, it is waiting",
          rows[0]["spin"], False),
]

# Worst case, and the one that used to lose the question altogether: something
# else was raised, the starting pane was broken out to a window of its own, and
# the trust prompt was left in a window you were not looking at.
opening(screen=TRUST)
PANES["%new"] = (2, "s:1.0", "s:1")
results.append(
    check("and the go-on key takes you to it, wherever it went",
          m.next_or_park(listed([A]))[0], "delta - waiting"))

# A question is not a reason to stop the rest of the fleet reaching you: it is
# answered in the pane, and the pane is where you already are.
opening(screen=TRUST)
results.append(
    check("it does not hold the queue", m.starting(listed([A])), ""))

# -------------------------------------------------- and the ways the wait ends
print("\nthe wait ends when it should")
opening()
results += [
    check("the head turns up in the list -> no row", m.pending([A, D]), []),
    check("and the queue moves again",
          m.auto_raise(listed([A, D]), None, "%dash")[0]["name"], "alpha"),
]

opening()
PANES.pop("%new")
results += [
    check("the pane went away -> no row", m.pending([A]), []),
    check("and the queue moves again",
          m.auto_raise(listed([A]), None, "%dash")[0]["name"], "alpha"),
]

# A launch that never becomes a head - `claude: command not found` sitting in a
# pane that will not go away by itself - must not hold the queue for ever. It
# must also not vanish off the list: the pane is still there, and what it says
# is the only thing anyone can act on.
opening(left=5)
results.append(
    check("still waiting a moment before the backstop",
          m.starting(listed([A])), "delta"))

opening(left=-1, screen="bash: claude: command not found\n")
rows = m.pending([A])
results += [
    check("a head that never arrives stops holding the queue",
          m.starting(listed([A])), ""),
    check("and the queue moves again",
          m.auto_raise(listed([A]), None, "%dash")[0]["name"], "alpha"),
    check("but the pane stays on the list", [r["name"] for r in rows],
          ["delta"]),
    check("saying what the pane says", "command not found" in rows[0]["reason"],
          True),
    check("and stops spinning, because nothing is coming", rows[0]["spin"],
          False),
]

# --------------------------------------------------------------- nothing open
print("\nwith nothing starting it is the queue it always was")
m._starting = []
MOVED.clear()
results += [
    check("no rows", m.pending([A]), []),
    check("no wait to report", m.starting([A]), ""),
    check("and the raise happens as before",
          m.auto_raise([A], None, "%dash")[0]["name"], "alpha"),
]

# ------------------------------------------------------- reading a pane's mind
print("\nwhat counts as a question")
results += [
    check("the trust prompt as it actually looks", m.pane_asks(REAL_TRUST),
          "Quick safety check: Is this a project you created or one you trust?"),
    check("and as it used to look, boxed", m.pane_asks(TRUST),
          "Do you trust the files in this folder?"),
    check("options with no question is still a question",
          m.pane_asks("Choose a theme:\n  1. Dark\n  2. Light\n"),
          "waiting on an answer in the pane"),
    check("a question with no options is output, not a prompt",
          m.pane_asks("Did that work? I think so.\nrunning tests\n"), ""),
    check("ordinary output is not a question",
          m.pane_asks("bash: claude: command not found\n"), ""),
    check("and an empty pane is not either", m.pane_asks(""), ""),
]

print()
print("all pass" if all(results) else "FAILURES")
raise SystemExit(0 if all(results) else 1)
