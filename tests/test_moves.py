"""One pane move is one exec, and the frame is drawn without blanking it.

Both are about the same half second: the one where you press a key and watch
the layout be assembled. A head is broken out, which leaves the dashboard alone
in the window and so full width; another tmux call puts the new one in. tmux
redraws the client at every call it is given, so between those two the list is
on screen stretched across the whole window - and if a refresh lands in there,
it is laid out that way too and stays wrong until the next one.

So the moves go in one command list, and the redraw stops erasing the screen
ahead of writing to it. No tmux runs here: every call is recorded.
"""
from harness import bn as m

EXECS = []              # one entry per tmux invocation, whatever it carried


class Ran:
    returncode = 0
    stdout = "%new\n"


def fake_tmux(*args, check=False):
    EXECS.append(list(args))
    return Ran()


def fake_many(*cmds):
    EXECS.append([bit for cmd in cmds for bit in cmd])
    return Ran()


m.tmux = fake_tmux
m.tmux_many = fake_many
m.tmux_out = lambda *args: ""
m.tmux_say = lambda text: None
m.invalidate = lambda: None
m.dash_pane = lambda: "%dash"
m.pane_window = lambda pane: "s:0"
m.panes_in_window = lambda win, besides="": ["%old"]
m.panes_by_id = lambda: {"%dash": (1, "s:0.0", "s:0"),
                         "%old": (2, "s:0.1", "s:0")}
m.pane_session = lambda pane: "s"
m.clear_attention = lambda sid: False

FAILED = []


def check(label, got, want):
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {label:<54}"
          f"{'' if ok else f' {got!r} != {want!r}'}")
    if not ok:
        FAILED.append(label)


def head(name, pane, in_main=False):
    return {"name": name, "session_id": f"sid-{name}", "pid": 7, "pane_id": pane,
            "in_main": in_main, "kind": "interactive", "attention": False,
            "state": "WAITING", "priority": 1, "idle_for": 1, "elsewhere": False}


NEW, OLD = head("wren", "%new"), head("otter", "%old", in_main=True)

print("bringing a head in")
EXECS.clear()
m.focus(NEW, [NEW, OLD])
check("takes one exec, not one per move", len(EXECS), 1)
did = EXECS[0] if EXECS else []
check("which breaks the sitting head out", "break-pane" in did, True)
check("and puts the new one in, in the same breath",
      did.index("break-pane") < did.index("join-pane"), True)
check("and lands the cursor on it", did[-2:], ["-t", "%new"])

print("\nparking what is up")
EXECS.clear()
m.park([OLD])
check("also one exec", len(EXECS), 1)
check("with the eviction in it", "break-pane" in EXECS[0], True)
check("and the dashboard selected after", "select-window" in EXECS[0], True)

print("\nopening a pane for a head that does not exist yet")
EXECS.clear()
m._starting = []
m.spawn("claude --name wren", "/w", "wren", [OLD])
check("the eviction and the split are one exec", len(EXECS), 2)
check("the first of which carries both",
      ("break-pane" in EXECS[0], "split-window" in EXECS[0]), (True, True))
check("the second only names the pane it was handed back",
      EXECS[1][:2], ["select-pane", "-t"])
check("which it read off the split", EXECS[1][2], "%new")

print("\ndrawing a frame")


class Screen:
    def __init__(self):
        self.wrote = []

    def write(self, text):
        self.wrote.append(text)

    def flush(self):
        pass


import sys

real_stdout = sys.stdout
sys.stdout = screen = Screen()
try:
    m.paint("one\ntwo")
finally:
    sys.stdout = real_stdout
out = "".join(screen.wrote)
check("the screen is never blanked first", "\033[2J" in out, False)
check("it is one write, so nothing can be seen half done",
      len(screen.wrote), 1)
check("home first", out.startswith("\033[H"), True)
check("every line clears the rest of its own row",
      out.count("\033[K"), 2)
check("and the rows a longer frame left behind go at the end",
      out.endswith("\033[J"), True)

print()
print("all pass" if not FAILED else "FAILURES")
raise SystemExit(0 if not FAILED else 1)
