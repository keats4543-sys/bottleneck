"""Priority groups and holds. State goes to a temp file; no tmux, no /proc.

Two claims are being tested. A group outranks need: the go-on key must finish
the first group before it offers you anything from the second, however long the
second has been waiting. And a hold is the opposite of attention: a head you
have read and put aside drops below the heads still working, and picks itself up
again the moment that head does anything new.
"""
import os
import tempfile

from harness import bn as m

m.QUEUE = os.path.join(tempfile.mkdtemp(), "queue.json")
FAILED = []


def check(label, got, want):
    ok = got == want
    print(("  ok   " if ok else "  FAIL ") + label.ljust(48)
          + ("" if ok else f"\n         got  {got!r}\n         want {want!r}"))
    if not ok:
        FAILED.append(label)


def fresh():
    try:
        os.remove(m.QUEUE)
    except OSError:
        pass


def head(name, state="WORKING", age=10, group="", held=False):
    book = m.queue_load()
    return {
        "name": name, "session_id": name, "state": state, "idle_for": age,
        "priority": m.STATES[state][0], "attention": state in m.NEEDS_ATTENTION,
        "group": group, "group_rank": m.group_rank(book, group) if group else 99,
        "held": held, "pane_id": "%1", "in_main": False, "active": False,
        "pid": 1, "kind": "head", "reason": "", "step": "", "tty": None,
        "last_ts": 1000.0, "slot": 1,
    }


def order_of(heads):
    return [h["name"] for h in
            sorted(heads, key=lambda h: (h["group_rank"], h["priority"], -h["idle_for"]))]


print("\na group outranks how long you have waited")
fresh()
m.set_group("urgent", "1")
m.set_group("later", "2")
first = head("urgent", "WAITING", age=5, group="1")
second = head("later", "BLOCKED", age=9000, group="2")
check("the better group wins even against a worse wait",
      order_of([second, first]), ["urgent", "later"])
check("a working head in the first group still loses to a waiting one",
      order_of([head("busy", "WORKING", age=1, group="1"), first]),
      ["urgent", "busy"])
check("an ungrouped head sorts after every group",
      order_of([head("loose", "BLOCKED", age=500), first]), ["urgent", "loose"])

print("\nranking groups against each other")
fresh()
m.set_group("a", "1")
m.set_group("b", "2")
check("assignment order is the starting rank", m.group_ids(m.queue_load()), ["1", "2"])
check("demoting moves it down", m.move_group("1", 1), 2)
check("and the ranking followed", m.group_ids(m.queue_load()), ["2", "1"])
check("promoting moves it back", m.move_group("1", -1), 1)
check("the top cannot be promoted off the end", m.move_group("1", -1), 1)
check("nor the bottom demoted", m.move_group("2", 1), 2)
check("moving a group nobody is in does nothing", m.move_group("9", -1), 0)

print("\nnaming and clearing")
fresh()
m.set_group("a", "3")
check("a group with no name says which it is",
      m.group_label(m.queue_load(), "3"), "group 3")
check("naming it says what it is now", m.name_group("3", "release"), "release")
check("a named one says the name", m.group_label(m.queue_load(), "3"), "release")
check("renaming replaces rather than adds",
      [m.name_group("3", "  the   release  "), m.queue_load()["names"]],
      ["the release", {"3": "the release"}])
check("a blank name hands the number back", m.name_group("3", ""), "")
check("and the label goes with it",
      m.group_label(m.queue_load(), "3"), "group 3")
m.set_group("a", "")
check("clearing takes the head out", m.queue_load()["of"], {})
check("but the group survives for the next head",
      m.group_ids(m.queue_load()), ["3"])

print("\na name is enough to make a group real")
fresh()
check("naming one nobody is in yet ranks it",
      [m.name_group("7", "next week"), m.group_ids(m.queue_load())],
      ["next week", ["7"]])
check("naming it again does not rank it twice",
      [m.name_group("7", "next week"), m.group_ids(m.queue_load())],
      ["next week", ["7"]])
check("and a head can then join the name rather than the number",
      [m.set_group("x", "7"), m.group_label(m.queue_load(), "7")],
      ["7", "next week"])

print("\na hold drops a finished head below the working ones")
fresh()
check("held sorts after working", m.STATES["HELD"][0] > m.STATES["WORKING"][0], True)
check("and before idle", m.STATES["HELD"][0] < m.STATES["IDLE"][0], True)
check("a held head is not asking for you",
      "HELD" in m.NEEDS_ATTENTION, False)
check("holding writes the moment it was held", m.set_hold("x", 1000.0), True)
check("and it can be read back", m.held_at(m.queue_load(), "x"), 1000.0)
check("letting go removes it", m.set_hold("x", 0), False)
check("a head that was never held reads as zero",
      m.held_at(m.queue_load(), "never"), 0.0)

print("\nthe hold lets go by itself")
WHEN = 1000.0
check("a head sitting exactly where you left it stays held",
      m.hold_stale("DONE", WHEN, WHEN), False)
check("a clock tick is not activity",
      m.hold_stale("DONE", WHEN + 0.5, WHEN), False)
check("a new turn lets it go", m.hold_stale("DONE", WHEN + 200, WHEN), True)
check("so does starting work again",
      m.hold_stale("WORKING", WHEN, WHEN), True)
check("and so does dying", m.hold_stale("DEAD", WHEN, WHEN), True)
check("a head that was never held is not stale",
      m.hold_stale("DONE", WHEN + 999, 0), False)
check("a head still waiting on you stays held",
      m.hold_stale("BLOCKED", WHEN, WHEN), False)

print("\na group claimed before the head has a session id")
fresh()
m.CLAIMS = os.path.join(os.path.dirname(m.QUEUE), "claims.json")
try:
    os.remove(m.CLAIMS)
except OSError:
    pass
m.claim_group("grains", "2")
check("the claim alone groups nobody", m.queue_load()["of"], {})
check("a head with no session id yet cannot take it",
      m.claims_apply([("grains", "")]), False)
check("the head turning up takes it",
      m.claims_apply([("grains", "sid-1")]), True)
check("and it lands in the group it asked for",
      m.queue_load()["of"], {"sid-1": "2"})
check("the group exists for the ranking too",
      m.group_ids(m.queue_load()), ["2"])
check("a spent claim does not catch the next head of that name",
      m.claims_apply([("grains", "sid-2")]), False)
check("which is still ungrouped", m.queue_load()["of"].get("sid-2"), None)

fresh()
m.claim_group("stale", "1", now=0.0)
check("a claim older than the ttl is not honoured",
      m.claims_apply([("stale", "sid-3")]), False)
check("and it is torn up rather than left to catch someone later",
      m.claims_load(), {})

fresh()
m.claim_group("mine", "1")
m.set_group("sid-4", "3")
check("a claim never overrides a group you set by hand",
      m.claims_apply([("mine", "sid-4")]), False)
check("the hand-set group stands", m.queue_load()["of"], {"sid-4": "3"})
check("and the claim is spent, not left waiting", m.claims_load(), {})

fresh()
m.claim_group("gone", "1")
check("clearing a claim drops it", m.claim_group("gone", ""), "")
check("leaving nothing behind", m.claims_load(), {})
check("an unnamed head cannot be claimed", m.claim_group("", "1"), "")
check("nothing claimed is no work", m.claims_apply([("any", "sid-5")]), False)

print("\ncorrupt state is not a crash")
with open(m.QUEUE, "w") as fh:
    fh.write("{ not json")
check("unreadable queue reads as empty", m.queue_load()["of"], {})
check("and as no groups", m.group_ids(m.queue_load()), [])

print("\nall pass" if not FAILED else f"\n{len(FAILED)} FAILED")
raise SystemExit(1 if FAILED else 0)
