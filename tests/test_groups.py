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

print("\ndisbanding takes a group apart")
fresh()
m.set_group("sid-a", "2")
m.set_group("sid-b", "2")
m.set_group("sid-c", "1")
m.name_group("2", "grains")
m.claim_group("not-up-yet", "2")
check("it says how many heads it let go", m.disband_group("2"), 2)
check("the ranking loses it", m.group_ids(m.queue_load()), ["1"])
check("so does the name book", m.queue_load()["names"], {})
check("its heads come back unassigned, and nobody else moves",
      m.queue_load()["of"], {"sid-c": "1"})
check("a claim waiting on it is torn up too", m.claims_load(), {})
check("disbanding it again is not an error", m.disband_group("2"), None)
check("nor is a group that never existed", m.disband_group("9"), None)
check("nor is no group at all", m.disband_group(""), None)

# The whole reason this is a command and not three edits. A group lives in the
# assignments as much as in the ranking, so one entry left behind - from a
# session that ended weeks ago and will never be seen again - is enough to
# bring the group back on the next listing.
fresh()
m.set_group("long-gone", "3")
book = m.queue_load()
book["order"] = []
m.queue_save(book)
check("an assignment on its own keeps a group alive",
      m.group_ids(m.queue_load()), ["3"])
check("disband clears the assignment, not just the rank",
      m.disband_group("3"), 1)
check("so it does not come back", m.group_ids(m.queue_load()), [])

fresh()
m.name_group("4", "later")
check("a group named before anyone joins it exists",
      m.group_ids(m.queue_load()), ["4"])
check("and can be disbanded empty", m.disband_group("4"), 0)
check("leaving nothing", (m.group_ids(m.queue_load()), m.queue_load()["names"]),
      ([], {}))

print("\nG then d disbands from the dashboard")
# The one thing on the group key that is about a group rather than a head, so
# the one thing that has to work with nothing pointed at: a group whose last
# head has exited is exactly the group you want rid of, and there is nothing
# left to select.
TYPED = []


def typing(*keys):
    """Answer the key prompts in order, and remember what was asked."""
    keys = list(keys)
    TYPED.clear()

    def fake(prompt=""):
        TYPED.append(prompt)
        return keys.pop(0) if keys else ""
    m.next_key = fake


fresh()
m.set_group("sid-x", "5")
m.name_group("5", "grains")
rows = [head("ann", group="5")]
typing("d", "5")
check("it says what it did", m.queue_key("G", rows, "ann"),
      "grains disbanded - 1 head unassigned")
check("and the group is gone", m.group_ids(m.queue_load()), [])
check("the prompt offers the key", "d disbands" in TYPED[0], True)
check("and the second prompt asks which", "disband which group" in TYPED[1],
      True)

fresh()
m.name_group("6", "empty one")
typing("d", "6")
check("an empty group says so rather than counting nobody",
      m.queue_key("G", [], ""), "empty one disbanded - it was empty")

fresh()
typing("d")
check("with no groups at all it does not ask which",
      m.queue_key("G", [], ""), "no groups to disband")

fresh()
m.name_group("7", "kept")
typing("d", "\x1b")
check("escape at the second prompt changes nothing",
      m.queue_key("G", [], ""), "cancelled")
check("and the group stands", m.group_ids(m.queue_load()), ["7"])

fresh()
typing("2")
check("grouping still needs a head to point at",
      m.queue_key("G", [], ""), "no head selected - bring one up first")
check("and nothing was written", m.queue_load()["of"], {})

fresh()
rows = [head("ann")]
typing("2")
check("pointing at one still puts it in a group",
      m.queue_key("G", rows, "ann"), "ann joins group 2   N names it")
check("which is what the book says", m.queue_load()["of"], {"ann": "2"})

print("\ncorrupt state is not a crash")
with open(m.QUEUE, "w") as fh:
    fh.write("{ not json")
check("unreadable queue reads as empty", m.queue_load()["of"], {})
check("and as no groups", m.group_ids(m.queue_load()), [])

print("\nall pass" if not FAILED else f"\n{len(FAILED)} FAILED")
raise SystemExit(1 if FAILED else 0)
