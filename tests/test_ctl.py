"""The control fifo the tmux keys write to. No tmux, no dashboard, no panes.

Alt+Enter used to start a python process to do work the running dashboard was
already holding all the state for - upwards of a tenth of a second before the
first useful instruction. Now it writes one line here instead. These tests hold
that line to its shape: what the fifo accepts, what it ignores, and that a key
pressed with nothing listening cannot wedge the shell that pressed it.
"""
import os

from harness import bn as m

FAILED = []


def check(label, got, want):
    ok = got == want
    print(("  ok   " if ok else "  FAIL ") + label.ljust(52)
          + ("" if ok else f"got {got!r}, wanted {want!r}"))
    if not ok:
        FAILED.append(label)


print("\nthe fifo opens, and is a fifo")
fd = m.ctl_open()
check("it opens", fd is not None, True)
check("and what it made is a pipe", os.path.isfile(m.CTL), False)
check("readable straight away, without blocking", m.ctl_read(fd), [])

print("\na line written to it comes back as a verb and its argument")
with open(m.CTL, "w") as fh:
    fh.write("sendgo %7\n")
check("one line, split at the space", m.ctl_read(fd), [("sendgo", "%7")])

with open(m.CTL, "w") as fh:
    fh.write("next\nsendgo %2\n")
check("several at once keep their order",
      m.ctl_read(fd), [("next", ""), ("sendgo", "%2")])

with open(m.CTL, "w") as fh:
    fh.write("\n  \nnext \n")
check("blank lines are not verbs", m.ctl_read(fd), [("next", "")])

print("\nthe fifo never blocks the key that wrote to it")
# Held open for writing as well as reading, so a read with nothing waiting
# returns empty rather than reporting end-of-file forever after the last writer
# closed - which is what would happen with a plain O_RDONLY open.
with open(m.CTL, "w") as fh:
    fh.write("next\n")
m.ctl_read(fd)
check("still open after a writer came and went", m.ctl_read(fd), [])

print("\nverbs are dispatched, and an unknown one is a problem, not a crash")
CALLED = []
m.send_go = lambda heads, pane: (CALLED.append(("send_go", pane)) or "sent", False)
m.next_or_park = lambda heads: (CALLED.append(("next", None)) or "moved", False)

check("sendgo carries the pane it was pressed in",
      m.do_ctl("sendgo", "%7", []), ("sent", False))
check("and that is the pane it acts on", CALLED[-1], ("send_go", "%7"))
check("next needs no argument", m.do_ctl("next", "", []), ("moved", False))
check("j is the same key by its dashboard name",
      m.do_ctl("j", "", []), ("moved", False))
_, problem = m.do_ctl("wat", "", [])
check("an unknown verb is reported, not run", problem, True)

print("\nclosing takes the fifo away with it")
m.ctl_close(fd)
check("nothing left behind for a stale key to write into",
      os.path.exists(m.CTL), False)
check("closing twice is not an error", m.ctl_close(None), None)

print()
print("all pass" if not FAILED else f"{len(FAILED)} FAILED")
raise SystemExit(1 if FAILED else 0)
