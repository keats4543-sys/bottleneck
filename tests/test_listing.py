"""One listing, and everything that used to be a tmux call of its own.

A tmux call is a fork, an exec and a socket round-trip - about 21ms - and the
number of fields on it costs nothing. A quiet refresh was making half a dozen,
most of them asking about panes the listing had already described: a second
`list-panes` for the marks, one `display-message` to ask whether the cursor was
in a pane, another for copy-mode, another for the session a pane is in.

So these tests count. Not the answers alone - those were right before - but how
many times tmux was asked for them.
"""
from harness import bn as m

FAILED = []
ASKED = []              # every tmux_out() the code made


def check(label, got, want):
    ok = got == want
    print(("  ok   " if ok else "  FAIL ") + label.ljust(52)
          + ("" if ok else f"got {got!r}, wanted {want!r}"))
    if not ok:
        FAILED.append(label)


# %1 is the dashboard, sat in the window you are looking at. %2 is a head beside
# it. %3 is the active pane of a window nobody is watching, and is in copy-mode.
PANES = [
    ("111", "%1", "bottleneck:0.0", "bottleneck:0", "11", "dash", "bottleneck", "0"),
    ("222", "%2", "bottleneck:0.1", "bottleneck:0", "01", "", "bottleneck", "0"),
    ("333", "%3", "other:2.0", "other:2", "10", "", "other", "1"),
]


def fake_tmux_out(*args):
    ASKED.append(args)
    if args[:2] == ("list-panes", "-a"):
        return "\n".join("\t".join(p) for p in PANES)
    return ""


m.tmux_out = fake_tmux_out


def listings():
    return [a for a in ASKED if a[:2] == ("list-panes", "-a")]


print("\none refresh's worth of questions is one listing")
m.invalidate()
ASKED.clear()
by_pid, cursor = m.panes_by_pid()
m.panes_by_id()
m.pane_roles()
m.pane_is_active("%1")
m.pane_is_active("%3")
m.pane_in_mode("%1")
m.pane_session("%2")
m.pane_window("%2")
check("everything came off one listing", len(listings()), 1)
check("and nothing else was asked", len(ASKED), 1)

print("\nand it still says all the things it used to")
check("panes are keyed by the pid in them", by_pid[222][0], "%2")
check("the cursor is where both flags are set", cursor, {"%1"})
check("a pane's window comes off it", m.pane_window("%2"), "bottleneck:0")
check("so does what we marked it", m.pane_roles()["%1"], ("dash", "bottleneck"))
check("and the session it sits in", m.pane_session("%3"), "other")

print("\nactive and cursor are different questions")
# auto_raise asks "are you typing in this pane", which is true of the active
# pane of an unwatched window; the row highlight asks "are you sat in it", which
# is not. One field answered both for a while, and the wrong one displaced a
# pane someone was working in.
check("the pane you are sat in is active", m.pane_is_active("%1"), True)
check("so is the active pane of a window you are not watching",
      m.pane_is_active("%3"), True)
check("but only one of them holds the cursor", "%3" in cursor, False)
check("a pane doing neither is not active", m.pane_is_active("%2"), False)
check("and nothing was asked to find that out", len(listings()), 1)

print("\ncopy-mode comes off the same line")
check("the pane in it is known", m.pane_in_mode("%3"), True)
check("and the ones that are not", m.pane_in_mode("%1"), False)
check("still one listing", len(listings()), 1)

print("\na pane the listing has never heard of is asked about")
# Brand new or gone. Guessing "no" for the first would let a raise displace a
# pane you were typing in, which is the whole thing pane_is_active guards.
ASKED.clear()
m.pane_is_active("%9")
check("the fallback is a direct question",
      [a[0] for a in ASKED], ["display-message"])
check("and it names the pane it is about", "%9" in ASKED[0], True)
ASKED.clear()
m.pane_in_mode("%9")
check("copy-mode falls back the same way",
      [a[0] for a in ASKED], ["display-message"])
check("an empty pane id asks nobody anything",
      (m.pane_is_active(""), m.pane_in_mode(""), len(ASKED)), (False, False, 1))

print("\nthe listing is remembered until something moves a pane")
ASKED.clear()
m.panes_by_pid()
m.pane_roles()
check("a second pass asks nothing", ASKED, [])
m.invalidate()
m.panes_by_pid()
check("moving a pane means asking again", len(listings()), 1)

print("\na short or empty line is skipped, not a crash")
PANES.append(("444",))
PANES.append(("", "", "", ""))
m.invalidate()
check("a line with too little on it is dropped", "%4" in m.panes_by_id(), False)
check("and the rest still parse", sorted(m.panes_by_id()), ["%1", "%2", "%3"])
PANES[:] = PANES[:3]

# The fields at the end are the ones a tmux too old to know them would leave
# off. Losing copy-mode should cost copy-mode and nothing else.
PANES[:] = [p[:5] for p in PANES]
m.invalidate()
check("missing trailing fields do not lose the pane",
      sorted(m.panes_by_id()), ["%1", "%2", "%3"])
check("what is left still answers", m.pane_is_active("%1"), True)
check("and what is missing reads as no", m.pane_in_mode("%3"), False)

print()
print("all pass" if not FAILED else f"{len(FAILED)} FAILED")
raise SystemExit(1 if FAILED else 0)
