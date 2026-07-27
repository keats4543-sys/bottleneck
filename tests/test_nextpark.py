"""Decision-table test for bottleneck next_or_park. No tmux is touched.

Covers the go-on key: take the head that wants you, else park what is up and
show the queue, else walk the heads still working - and never report a quiet
queue as a problem, because a problem is what tmux turns into a popup window.
"""
import json
import os
import tempfile

from harness import bn as m

MOVED = []
PARKED = []
OCCUPIED = [False]

m.focus = lambda head, heads, select=True, ack=True: (
    MOVED.append((head["name"], ack)) or True)
m.park = lambda heads: PARKED.append(True) or True
m.dash_pane = lambda: "%0"
m.main_pane_occupied = lambda dash: OCCUPIED[0]

m.CYCLE = os.path.join(tempfile.mkdtemp(), "cycle.json")


def head(name, state, in_main=False, pane="%1", pid=1, sid=None):
    return {"name": name, "state": state, "pid": pid, "session_id": sid or name,
            "pane_id": pane, "in_main": in_main, "tty": "/dev/pts/1",
            "attention": state in m.NEEDS_ATTENTION,
            "priority": m.STATES[state][0], "idle_for": 10}


FAILED = []


def run(label, heads, occupied=False, state=None,
        expect_moved=None, expect_parked=False, expect_problem=False,
        expect_state=None):
    MOVED.clear()
    PARKED.clear()
    OCCUPIED[0] = occupied
    if state is None:
        try:
            os.remove(m.CYCLE)
        except OSError:
            pass
    else:
        with open(m.CYCLE, "w") as fh:
            json.dump(state, fh)

    note, problem = m.next_or_park(heads)
    got_moved = MOVED[0][0] if MOVED else None
    got_state = m.cycle_state() if os.path.exists(m.CYCLE) else None

    bad = []
    if got_moved != expect_moved:
        bad.append(f"moved {got_moved!r} want {expect_moved!r}")
    if bool(PARKED) != expect_parked:
        bad.append(f"parked {bool(PARKED)} want {expect_parked}")
    if problem != expect_problem:
        bad.append(f"problem {problem} want {expect_problem}")
    if expect_state and got_state != expect_state:
        bad.append(f"state {got_state} want {expect_state}")
    mark = "ok  " if not bad else "FAIL"
    if bad:
        FAILED.append(label)
    print(f"  {mark} {label:<46} {note}")
    for b in bad:
        print(f"       -> {b}")


A = head("waiting-a", "WAITING", pid=1, pane="%1")
B = head("done-b", "DONE", pid=2, pane="%2")
W1 = head("busy-1", "WORKING", pid=3, pane="%3")
W2 = head("busy-2", "WORKING", pid=4, pane="%4")
W1_MAIN = dict(W1, in_main=True)
W2_MAIN = dict(W2, in_main=True)
NOPANE = head("bare-tty", "WAITING", pid=5, pane=None)

print("\nqueue has something waiting")
run("takes the waiting head", [A, W1, W2], occupied=True, expect_moved="waiting-a")
run("waiting beats working even mid-cycle", [A, W1_MAIN],
    occupied=True, state={"sid": "busy-1", "mode": "cycle"}, expect_moved="waiting-a")
run("skips the one already up", [dict(A, in_main=True), B],
    occupied=True, expect_moved="done-b")
run("untouchable head is a problem, not a park", [NOPANE],
    occupied=False, expect_moved=None, expect_problem=True)

print("\nqueue empty - first press parks")
run("parks the head you just answered", [W1_MAIN, W2],
    occupied=True, expect_parked=True,
    expect_state={"sid": "busy-1", "mode": "park"})
run("parks after a queue raise too", [W1_MAIN, W2], occupied=True,
    state={"sid": "busy-1", "mode": "queue"}, expect_parked=True)

print("\nqueue empty - later presses cycle")
run("cycles from the parked head", [W1, W2], occupied=False,
    state={"sid": "busy-1", "mode": "park"}, expect_moved="busy-2",
    expect_state={"sid": "busy-2", "mode": "cycle"})
run("keeps cycling once cycling", [W1, W2_MAIN], occupied=True,
    state={"sid": "busy-2", "mode": "cycle"}, expect_moved="busy-1")
run("wraps round the ring", [W1_MAIN, W2], occupied=True,
    state={"sid": "busy-1", "mode": "cycle"}, expect_moved="busy-2")
run("nothing to cycle to - just parks", [W1_MAIN], occupied=True,
    state={"sid": "busy-1", "mode": "cycle"}, expect_parked=True)
run("no heads at all is quiet, not an error", [], occupied=False,
    expect_parked=True, expect_problem=False)
run("dead heads are not in the ring", [W1, dict(W2, state="DEAD")],
    occupied=False, state={"sid": "busy-1", "mode": "park"}, expect_moved="busy-1")

print()
if FAILED:
    print(f"{len(FAILED)} failed: {', '.join(FAILED)}")
    raise SystemExit(1)
print("all pass")
