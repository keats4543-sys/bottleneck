"""Decision-table test for bottleneck auto_raise. No tmux is touched."""
import os

from harness import bn as m

MOVED = []
ACTIVE = set()          # pane ids the cursor is in


def fake_focus(head, heads, select=True, ack=True):
    MOVED.append((head["name"], select, ack))
    return True


m.focus = fake_focus
m.pane_is_active = lambda pane: pane in ACTIVE


def head(name, state, in_main=False, pane="%1", pid=None):
    return {"name": name, "state": state, "pid": pid or abs(hash(name)) % 9999,
            "pane_id": pane, "in_main": in_main,
            "attention": state in m.NEEDS_ATTENTION,
            "priority": m.STATES[state][0], "idle_for": 10}


def run(label, heads, held, active=(), expect_move=None, expect_held="?",
        expect_moved=None, dash=""):
    MOVED.clear()
    ACTIVE.clear()
    ACTIVE.update(active)
    raised, new_held = m.auto_raise(heads, held, dash)
    got = raised["name"] if raised else None
    ok = got == expect_move
    if expect_held != "?":
        ok = ok and new_held == expect_held
    if expect_moved is not None:
        ok = ok and MOVED == expect_moved
    print(f"{'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        print(f"        raised={got!r} want={expect_move!r} "
              f"held={new_held!r} want={expect_held!r} moved={MOVED!r}")
    return ok


A = head("alpha", "WAITING", pane="%1", pid=101)
B = head("bravo", "BLOCKED", pane="%2", pid=102)
W = head("work", "WORKING", in_main=True, pane="%3", pid=103)
I = head("idle", "IDLE", in_main=True, pane="%4", pid=104)

results = [
    # nothing in the main pane at all -> raise, without stealing the cursor
    run("empty main pane, one waiting -> raises it",
        [A], None, expect_move="alpha", expect_held=101),

    run("raise does not select the pane and does not ack",
        [A], None, expect_move="alpha", expect_moved=[("alpha", False, False)]),

    # priority order: BLOCKED (0) sorts before WAITING (1)
    run("picks worst state first",
        sorted([A, B], key=lambda h: h["priority"]), None, expect_move="bravo"),

    # occupied by a head that wants nothing, cursor elsewhere -> free
    run("idle head in main, cursor on dash -> replaced",
        [A, I], None, expect_move="alpha", expect_held=101),

    # you are typing in it -> untouchable
    run("cursor in the main head -> left alone",
        [A, W], None, active={"%3"}, expect_move=None, expect_held=None),

    # main head itself wants you -> do not yank it away
    run("main head needs you -> not displaced",
        [B, head("mine", "WAITING", in_main=True, pane="%5", pid=105)],
        None, expect_move=None),

    # unread hand-off holds the pane against a later arrival
    run("held head not displaced by newcomer",
        [B, head("held", "IDLE", in_main=True, pane="%6", pid=106)],
        106, expect_move=None, expect_held=106),

    # entering it drops the hold, but the cursor rule then protects it
    run("entering the held pane drops the hold, cursor still guards it",
        [B, head("held", "IDLE", in_main=True, pane="%6", pid=106)],
        106, active={"%6"}, expect_move=None, expect_held=None),

    # ...and once you step back to the dashboard the queue moves again
    run("after you look and leave, the queue advances",
        [B, head("held", "IDLE", in_main=True, pane="%6", pid=106)],
        None, expect_move="bravo", expect_held=102),

    # stale hold: that head is no longer in main -> forget it
    run("hold forgotten when head left the main pane",
        [A, I], 106, expect_move="alpha", expect_held=101),

    # nothing to do
    run("nothing waiting -> no move",
        [W], None, expect_move=None, expect_held=None),

    # already up: do not re-raise the same head
    run("top waiting head is already in main -> no move",
        [head("alpha", "WAITING", in_main=True, pane="%1", pid=101)],
        None, expect_move=None),

    # a waiting head with no pane (bg / bare tty) cannot be raised
    run("bg head waiting -> skipped, nothing moves",
        [dict(head("bg", "WAITING", pid=107), pane_id=None)],
        None, expect_move=None),

    run("bg head waiting, movable one behind it -> raises the movable one",
        [dict(head("bg", "BLOCKED", pid=107), pane_id=None), A],
        None, expect_move="alpha"),

    # --- the raise that takes the cursor with it -------------------------
    #
    # Nothing is up and you are sat on the list: there is nothing you could be
    # typing into and nothing to displace, so the head arrives ready to answer.
    run("empty main, cursor on the dashboard -> raised and selected",
        [A], None, active={"%dash"}, dash="%dash",
        expect_move="alpha", expect_held=None,
        expect_moved=[("alpha", True, True)]),

    # Same, but you are off in some other window. Taking the cursor from there
    # would be exactly the yank the select=False rule exists to prevent.
    run("empty main, cursor elsewhere -> raised but not selected",
        [A], None, active={"%9"}, dash="%dash",
        expect_move="alpha", expect_held=101,
        expect_moved=[("alpha", False, False)]),

    # A raise into an occupied pane has to evict first, and carrying the cursor
    # along with a displacement is the hijack, wherever you happen to be sat.
    run("occupied main, cursor on the dashboard -> raised, cursor stays",
        [A, I], None, active={"%dash"}, dash="%dash",
        expect_move="alpha", expect_held=101,
        expect_moved=[("alpha", False, False)]),

    # Without being told which pane is the dashboard it cannot know, so it does
    # the careful thing - which is what every caller but the watch loop gets.
    run("no dashboard pane given -> never takes the cursor",
        [A], None, active={"%dash"},
        expect_move="alpha", expect_held=101,
        expect_moved=[("alpha", False, False)]),
]

print()
print(f"{sum(1 for r in results if r)}/{len(results)} passed")
raise SystemExit(0 if all(results) else 1)
