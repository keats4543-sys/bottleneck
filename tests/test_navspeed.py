"""An arrow key moves the cursor without going back to disk for it.

Every key used to end the frame, which sent the loop round the top of its cycle:
session files read again, a tail parsed for every head, a tmux listing once the
cache had gone cold, and only then a redraw with the cursor one row further on.
On an idle box that is milliseconds. On a loaded one it is the pause people
report - and it multiplies, because keys are only read inside the wait loop, so
a held-down arrow sits in the terminal buffer and pays the whole refresh once
per row.

Nothing that refresh reads can change where the cursor lands. These tests drive
the real loop against a stand-in terminal and hold it to that: arrows repaint,
arrows do not collect, and a key that does act on a head still does.
"""
import io
import os
import sys

from harness import bn as m

FAILED = []


def check(label, got, want):
    ok = got == want
    print(("  ok   " if ok else "  FAIL ") + label.ljust(54)
          + ("" if ok else f"\n         got  {got!r}\n         want {want!r}"))
    if not ok:
        FAILED.append(label)


def head(name, slot, group=""):
    return {"name": name, "session_id": f"sid-{name}", "pid": 7,
            "pane_id": f"%{name}", "pane": "s:0.1", "in_main": False,
            "kind": "interactive", "attention": False, "state": "WAITING",
            "reason": "", "step": "", "task": "", "idle_for": 1, "last_ts": 1,
            "priority": 1, "elsewhere": False, "foreign": False, "active": False,
            "group": group, "group_label": group, "group_rank": 0, "held": False,
            "slot": slot, "cwd": "/tmp", "tty": None, "name_source": ""}


HEADS = [head("one", 1, "g1"), head("two", 2, "g1"), head("three", 3, "g2")]

COLLECTS = [0]          # times the loop went back for the head list
PAINTS = []             # the selection each frame was drawn with
FOCUSED = []            # heads the loop opened


class Terminal:
    """Keystrokes the way a terminal sends them: whole writes, not bytes."""

    def __init__(self, *writes):
        self.writes = list(writes)

    def isatty(self):
        return True

    def fileno(self):
        return 0


class FakeSelect:
    """The terminal is always ready. There is nothing else here to wait for,
    and a test that waited on a clock would be testing the clock."""

    @staticmethod
    def select(read, write, err, timeout=None):
        return ([sys.stdin] if sys.stdin in read else []), [], []


class FakeTermios:
    """Raw mode, without a terminal to put into it.

    The loop only reads keys when it believes it has a real terminal, so this
    has to answer - and the saved settings it hands back come straight to
    tcsetattr on the way out, where the real one would raise on a pipe.
    """
    TCSADRAIN = 1

    @staticmethod
    def tcgetattr(fh):
        return ["saved"]

    @staticmethod
    def tcsetattr(fh, when, attrs):
        return None


class FakeTty:
    @staticmethod
    def setcbreak(fd):
        return None


def counted_collect():
    COLLECTS[0] += 1
    return list(HEADS)


def run(*writes):
    """Drive watch() over one script of keystrokes and report what it did."""
    COLLECTS[0] = 0
    PAINTS.clear()
    FOCUSED.clear()
    term = Terminal(*writes)
    was = {name: sys.modules.get(name) for name in ("select", "termios", "tty")}
    sys.modules["select"] = FakeSelect
    sys.modules["termios"] = FakeTermios
    sys.modules["tty"] = FakeTty
    real_stdin, real_stdout, real_read = sys.stdin, sys.stdout, os.read
    sys.stdin = term
    sys.stdout = io.StringIO()
    # The loop reads whole writes off the terminal. When the script runs out it
    # gets Ctrl-C, the one key that leaves without asking anything first.
    os.read = lambda fd, n: (term.writes.pop(0).encode() if term.writes
                             else b"\x03")
    try:
        m.watch()
    finally:
        sys.stdin, sys.stdout, os.read = real_stdin, real_stdout, real_read
        for name, mod in was.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod


# Everything the cycle reaches for, stubbed to something that cannot touch the
# machine this runs on. render and paint stand in for the real pair so a frame
# can be counted; the selection each was drawn with is the whole of what matters.
m.collect = counted_collect
m.render = lambda heads, **kw: f"frame:{kw.get('selected', '')}"
m.paint = lambda text: PAINTS.append(text.split(":", 1)[1].split("\n")[0])
m.focus = lambda h, heads: FOCUSED.append(h["name"])
m.catalog_note = lambda heads: None
m.pending = lambda heads, now=None: []
m.publish_bar = lambda heads: None
m.retract_bar = lambda: None
m.auto_enabled = lambda: False
m.reaped = lambda: []
m.pane_width = lambda pane: 100
m.leave_copy_mode = lambda pane: False
m.dash_point = lambda pane: None
m.dash_register = lambda pane: None
m.write_keys_conf = lambda: None
m.warm_claude = lambda: None
m.ctl_open = lambda: None
m.ctl_close = lambda fd: None
m.invalidate = lambda: None
m.clear_attention = lambda sid: False
m.tmux = lambda *args, **kw: None
m.tmux_out = lambda *args: ""
m.tmux_say = lambda text: None

DOWN, UP, RIGHT = "\x1b[B", "\x1b[A", "\x1b[C"

print("\none pass of the cycle, however many arrows are pressed")
run(DOWN)
check("the list is read once, not once per key", COLLECTS[0], 1)
check("the arrow still redrew", len(PAINTS) >= 2, True)
check("with the cursor one row on", PAINTS[-1], "sid-two")

run(DOWN, DOWN)
check("two arrows, still one read", COLLECTS[0], 1)
check("both moves landed", PAINTS[-1], "sid-three")

run(DOWN + DOWN)          # a held-down key: several arrows in one write
check("a held-down arrow does not read the list per row", COLLECTS[0], 1)
check("and every row it passed was drawn",
      PAINTS[-3:], ["sid-one", "sid-two", "sid-three"])

run(DOWN, UP)
check("back up again", PAINTS[-1], "sid-one")

print("\ngroup jumps are cursor moves too")
run(RIGHT)
check("left and right do not read the list either", COLLECTS[0], 1)
check("the cursor is on the next group", PAINTS[-1], "sid-three")

print("\nkeys that act on a head still end the frame")
run(DOWN, "\r")
check("Enter opens what the arrows landed on", FOCUSED, ["two"])
check("and the list is read again after it", COLLECTS[0], 2)

run("2")
check("a slot key opens by number", FOCUSED, ["two"])
check("and reads the list again", COLLECTS[0], 2)

print("\na key that acts is answered before the refresh, not after it")
# Same complaint as the arrows, one step along: the key was acted on at once,
# and the note saying so waited for the top of the next cycle - a full refresh
# behind it, which is the whole delay between pressing a key and being told
# anything happened. Nothing that refresh reads changes what the note says.
ORDER = []
m.collect = lambda: (ORDER.append("collect"), counted_collect())[1]
m.paint = lambda text: (ORDER.append("paint"),
                        PAINTS.append(text.split(":", 1)[1].split("\n")[0]))[0]
NOTES = []
m.render = lambda heads, **kw: (NOTES.append(kw.get("note", "")),
                                f"frame:{kw.get('selected', '')}")[1]

ORDER.clear()
run("c")
check("the answer is painted before the list is read again",
      ORDER[:4], ["collect", "paint", "paint", "collect"])
check("and it is the note the key produced",
      NOTES[1], "cleared all flags")
check("which is still there on the frame after the refresh",
      NOTES[2], "cleared all flags")

ORDER.clear()
run(DOWN)
check("an arrow still costs one paint and no read",
      ORDER[:3], ["collect", "paint", "paint"])

print("\nall pass" if not FAILED else f"\n{len(FAILED)} FAILED")
raise SystemExit(1 if FAILED else 0)
