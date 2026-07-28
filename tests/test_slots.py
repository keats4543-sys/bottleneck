"""Head numbers and the you-are-here mark. No tmux is touched.

The queue reorders itself while you read it, so the number on a row cannot be
the row. These tests hold a slot still across a reorder, hand a freed number to
the next head, and check that the row you are sitting in is the one lit up.
"""
import os
import re
import tempfile

from harness import bn as m

m.SLOTS = os.path.join(tempfile.mkdtemp(), "slots.json")


def head(name, state="WORKING", in_main=False, active=False, sid=None):
    return {"name": name, "state": state, "pid": 1, "session_id": sid or name,
            "pane_id": "%1", "in_main": in_main, "active": active, "tty": None,
            "kind": "head", "reason": "", "step": "",
            "attention": state in m.NEEDS_ATTENTION,
            "priority": m.STATES[state][0], "idle_for": 10}


FAILED = []


def check(label, got, want):
    ok = got == want
    print(("  ok   " if ok else "  FAIL ") + label.ljust(46)
          + ("" if ok else f"got {got!r}, wanted {want!r}"))
    if not ok:
        FAILED.append(label)


def fresh():
    try:
        os.remove(m.SLOTS)
    except OSError:
        pass


def slots(heads):
    return {h["name"]: h["slot"] for h in m.assign_slots(heads)}


def strip(text):
    """The drawing without the colour, for reading words out of."""
    return re.sub(r"\033\[[0-9;]*m", "", text)


print("\na number belongs to a head, not to a row")
fresh()
a, b, c = head("ann"), head("bo"), head("cy")
check("handed out in list order", slots([a, b, c]), {"ann": 1, "bo": 2, "cy": 3})
check("survives a reorder", slots([c, a, b]), {"ann": 1, "bo": 2, "cy": 3})
check("a newcomer takes the next free one",
      slots([c, head("dee"), a, b]), {"ann": 1, "bo": 2, "cy": 3, "dee": 4})

print("\nnumbers freed by a dead head come back")
fresh()
slots([a, b, c])
check("lowest free number is reused", slots([a, c, head("eli")]),
      {"ann": 1, "cy": 3, "eli": 2})
check("and the reuse sticks", slots([head("eli"), a, c]),
      {"ann": 1, "cy": 3, "eli": 2})

print("\nthe number is how you pick a head")
fresh()
picked = m.assign_slots([a, b, c])
check("by_slot finds it", m.by_slot(picked, "2")["name"], "bo")
check("digit as int works too", m.by_slot(picked, 3)["name"], "cy")
check("a number nobody wears is None", m.by_slot(picked, "9"), None)
check("junk is None, not a crash", m.by_slot(picked, "x"), None)
check("a head with no session id is unpickable",
      m.by_slot(m.assign_slots([dict(head("ghost"), session_id="")]), "0"), None)


def rendered(heads, selected=""):
    m.assign_slots(heads)
    return m.render(heads, width=100, selected=selected)


BAR = "\033[1;97;44m"          # the head open beside you: the whole row
PICK = "\033[7m"               # the row your arrow keys are on: the name


def barred(text):
    """The name on the full-width bar, if there is one."""
    for line in text.split("\n")[1:]:      # skip the title bar
        if line.startswith(BAR):
            hit = re.search(r"\d+ \w+ +([a-z]+)", line)
            return hit.group(1) if hit else None
    return None


def picked(text):
    hit = re.search(re.escape(PICK) + r" *([a-z]+)", text)
    return hit.group(1) if hit else None


print("\nthe head open beside you gets the whole row")
fresh()
check("cursor in a head bars that head",
      barred(rendered([head("ann"), head("bo", active=True), head("cy")])), "bo")
check("cursor on the dashboard falls back to the main pane",
      barred(rendered([head("ann"), head("bo", in_main=True), head("cy")])), "bo")
check("cursor beats the main pane",
      barred(rendered([head("ann", active=True), head("bo", in_main=True)])), "ann")
check("nothing open, no bar",
      barred(rendered([head("ann"), head("bo")])), None)
check("exactly one row is ever barred",
      rendered([head("ann", active=True), head("bo", in_main=True)])
      .split("\n", 1)[1].count(BAR), 1)

print("\nthe row you have pointed at gets its name lit")
fresh()
rows = [head("ann"), head("bo", active=True), head("cy")]
check("the selected name is lit", picked(rendered(rows, selected="cy")), "cy")
check("selecting is not opening", barred(rendered(rows, selected="cy")), "bo")
check("the cursor mark sits on that row",
      [l for l in rendered(rows, selected="cy").split("\n") if "▸" in l][0].count("▸"), 1)
check("only one row is marked",
      rendered(rows, selected="cy").count("▸"), 1)
check("selecting nothing lights nothing", picked(rendered(rows)), None)
check("a selection that has gone lights nothing",
      picked(rendered(rows, selected="ghost")), None)
check("you can point at the head that is already open",
      picked(rendered(rows, selected="bo")), None)   # the bar owns that row

print("\narrows walk the list; groups are their own jump")
fresh()


def g(name, gid, rank):
    h = head(name)
    h["group"], h["group_rank"], h["group_label"] = gid, rank, f"group {gid}"
    return h


rows = [g("aa", "1", 0), g("bb", "1", 0), g("cc", "2", 1), head("dd")]
m.assign_slots(rows)
check("down moves to the next row", m.move_selection(rows, "aa", 1), "bb")
check("and keeps going into the next group - no mode to leave first",
      m.move_selection(rows, "bb", 1), "cc")
check("up comes back", m.move_selection(rows, "cc", -1), "bb")
check("the top does not wrap", m.move_selection(rows, "aa", -1), "aa")
check("nor the bottom", m.move_selection(rows, "dd", 1), "dd")
check("an empty list is not a crash", m.move_selection([], "aa", 1), "aa")
check("a selection that has gone starts from the top",
      m.move_selection(rows, "ghost", 1), "bb")

print("\nleft and right jump a whole group")
check("right lands on the first head of the next group",
      m.jump_group(rows, "aa", 1), "cc")
check("from mid-group too", m.jump_group(rows, "bb", 1), "cc")
check("and again into the unassigned pile", m.jump_group(rows, "cc", 1), "dd")
check("left goes back to the group before",
      m.jump_group(rows, "cc", -1), "aa")
check("the first group does not wrap", m.jump_group(rows, "aa", -1), "aa")
check("nor the last", m.jump_group(rows, "dd", 1), "dd")
check("an empty list is not a crash", m.jump_group([], "aa", 1), "aa")

print("\nthe heading of the group you are in brightens")
marked = m.render(rows, width=90, selected="bb")
check("exactly one cursor, and it is on a row", marked.count("▸"), 1)
check("the name is still lit", picked(marked), "bb")
check("the heading of that group is bright",
      "\033[1;97m group 1" in marked, True)
check("the other heading is not",
      "\033[1;97m group 2" in marked, False)

print("\na group with nobody in it keeps its heading and its place")
# A group is a slot you made, not a side effect of who happens to be running.
# If it disappeared with its last head you could not tell a group you had lost
# from a head you had lost, and the ranking would come back in an order you
# never chose. Only `disband` takes one away.
book = [("1", "group 1"), ("2", "group 2"), ("3", "spare")]
drawn = m.render(rows, width=90, groups=book)
heads_of = [l for l in strip(drawn).split("\n") if "─" in l]
check("the empty group is drawn", any("spare  [3]  empty" in l for l in heads_of),
      True)
check("in the place its rank puts it - after 2, before the loose heads",
      [l.split()[0] for l in heads_of],
      ["group", "group", "spare", "unassigned"])
check("and the groups with heads read as they did",
      sum("empty" in l for l in heads_of), 1)

empty_only = m.render([], width=90, groups=[("4", "later")])
check("a group with no heads at all is still a heading",
      "later  [4]  empty" in strip(empty_only), True)
check("someone who uses no groups still pays no line for them",
      "─" in strip(m.render([head("solo")], width=90, groups=[])), False)

print("\nthe printed number is the slot, not the row")
fresh()
m.assign_slots([a, b, c])
out = m.render(m.assign_slots([c, b]), width=100)   # ann gone, cy first
row = [l for l in out.splitlines() if "cy" in l][0]
check("cy still wears 3", re.search(r"(\d+) ", row).group(1), "3")

print("\na session id is a name, never a path")
check("a guid passes", m.safe_sid("3a3a41f7-713e-4128"), "3a3a41f7-713e-4128")
check("climbing out is refused", m.safe_sid("../../etc/passwd"), "")
check("so is a backslash", m.safe_sid(r"..\\windows"), "")
check("and a leading dot", m.safe_sid(".ssh"), "")
check("empty stays empty", m.safe_sid(None), "")
check("clearing a made-up id removes nothing",
      m.clear_attention("../../nonsense"), False)

print("\nreload signal cannot be mistaken for an exit code")
check("R returns a sentinel, not a number", isinstance(m.RESTART, str), True)
check("and main only re-execs on that", m.RESTART == 0, False)
check("the key is on the help line", " R reload" in m.HELP, True)

print("\nall pass" if not FAILED else f"\n{len(FAILED)} FAILED")
raise SystemExit(1 if FAILED else 0)
