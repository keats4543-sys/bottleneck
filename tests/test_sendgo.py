"""Alt+Enter: press Enter at the head you are in, then go on to the next one.

The press used to be followed by half a second of time.sleep and a full re-read
of every head, on the thread that draws the list and reads your keys. It was
there because the head you had just answered still read as wanting you, so the
jump landed straight back on it.

These tests hold the replacement to the same promise without the pause: the
Enter goes to the right pane, the head that got it stops asking for you, and the
jump lands on the next head that does want you - even when that head's file has
not caught up yet, which is the case no length of pause could be sure of.

No tmux runs and no pane moves.
"""
import time

from harness import bn as m

FAILED = []
SENT = []               # every tmux() we would have made
MOVED = []              # every head focus() was asked to bring in


def check(label, got, want):
    ok = got == want
    print(("  ok   " if ok else "  FAIL ") + label.ljust(52)
          + ("" if ok else f"got {got!r}, wanted {want!r}"))
    if not ok:
        FAILED.append(label)


def head(name, state, pane, in_main=False):
    return {"name": name, "state": state, "pane_id": pane, "pane": pane,
            "session_id": "sid-" + name, "pid": abs(hash(name)) % 9999,
            "in_main": in_main, "attention": state in m.NEEDS_ATTENTION,
            "priority": m.STATES[state][0], "idle_for": 10, "tty": "pts/1",
            "reason": "turn finished, unread"}


m.tmux = lambda *a, **kw: SENT.append(a)
m.focus = lambda h, heads, select=True, ack=True: MOVED.append(h["name"]) or True
m.clear_attention = lambda sid: True
m.mark_seen = lambda sid: None
m.dash_pane = lambda: "%0"
m.park = lambda heads: True
m.main_pane_occupied = lambda dash: True
m.cycle_state = lambda: {}
m.set_cycle_state = lambda sid, mode: None
# collect() is what the pause was waiting to make truthful. Nothing may call it:
# a press that re-reads the fleet is the cost this replaced.
m.collect = lambda: (_ for _ in ()).throw(
    AssertionError("send_go re-read the fleet"))


print("\nthe Enter goes to the pane you pressed it in, and nowhere else")
SENT.clear()
MOVED.clear()
mine = head("kestrel", "BLOCKED", "%1", in_main=True)
other = head("otter", "DONE", "%2")
began = time.time()
note, problem = m.send_go([mine, other], "%1")
took = time.time() - began

check("no pause", took < 0.05, True)
check("one keystroke, to the pane we were in",
      SENT, [("send-keys", "-t", "%1", "Enter")])
check("and the jump goes on to the head that wants you", MOVED, ["otter"])
check("which is what it says", note, "otter - done")
check("a normal press is not a problem", problem, False)

print("\nthe head you answered stops asking for you")
# Two of the three things that made it read as waiting we put right ourselves,
# on disk, before the pause ever started. The third is the harness's own status,
# which still says permission_prompt until the harness changes it - so it is
# said here rather than waited for.
check("it no longer wants you", mine["attention"], False)
check("and is working, not holding out", mine["state"], "WORKING")
check("with the ranking to match", mine["priority"], m.STATES["WORKING"][0])
check("and nothing left to report about the last turn", mine["reason"], "")

print("\nthe press does not land back on the head that got it")
# The case the pause existed for and could not actually promise: this head is
# the only one flagged, and its file still says it wants you.
SENT.clear()
MOVED.clear()
alone = head("wren", "BLOCKED", "%3", in_main=True)
note, problem = m.send_go([alone], "%3")
check("Enter still went in", SENT, [("send-keys", "-t", "%3", "Enter")])
check("but nothing was raised on top of it", MOVED, [])
check("it parks instead of bouncing", note, "nothing waiting - parked, queue is clear")
check("and that is not a problem", problem, False)

print("\na pane that is not a head is never typed into")
SENT.clear()
MOVED.clear()
note, problem = m.send_go([head("otter", "DONE", "%2")], "%9")
check("no stray Enter", SENT, [])
check("the jump still happens", MOVED, ["otter"])

# Nor when the pane belongs to a head we cannot name - send-go is called from a
# key that may be pressed anywhere.
SENT.clear()
m.send_go([], "%1")
check("nor with no heads at all", SENT, [])

print()
print("all pass" if not FAILED else f"{len(FAILED)} FAILED")
raise SystemExit(1 if FAILED else 0)
