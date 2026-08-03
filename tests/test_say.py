"""`bottleneck say`: the answer you would have typed, delivered from elsewhere.

This is the half a caller outside tmux was missing. `bottleneck json` already
says which head wants you; without this there is no way to answer one except by
being sat in its pane. A client on another machine can now read the queue over
ssh and reply to it.

What the tests hold to. The text arrives as text, so a word tmux reads as a key
name is not read as one. The Enter follows it, and the head it went to stops
asking for you. A head with nowhere to type is refused rather than guessed at.
And an answer with a newline in it is refused, because half of it would go in
as the answer and the rest as the next one.

No tmux runs.
"""
from harness import bn as m

FAILED = []
SENT = []               # every tmux() we would have made
CLEARED = []            # session ids whose attention flag was dropped


def check(label, got, want):
    ok = got == want
    print(("  ok   " if ok else "  FAIL ") + label.ljust(52)
          + ("" if ok else f"got {got!r}, wanted {want!r}"))
    if not ok:
        FAILED.append(label)


class Ran:
    returncode = 0
    stdout = ""


def head(name, pane="%1", state="BLOCKED", tty="pts/1"):
    return {"name": name, "state": state, "pane_id": pane, "pane": pane,
            "session_id": "sid-" + name, "pid": 1234, "in_main": False,
            "attention": state in m.NEEDS_ATTENTION, "tty": tty,
            "priority": m.STATES[state][0], "idle_for": 10,
            "reason": "turn finished, unread"}


m.tmux = lambda *a, **kw: SENT.append(a) or Ran()
m.clear_attention = lambda sid: CLEARED.append(sid)
m.mark_seen = lambda sid: None


print("\nthe text goes in as text, and the Enter follows it")
SENT.clear()
CLEARED.clear()
h = head("kestrel")
note, problem = m.say(h, "yes, use the second one")
check("literal, and after the options end",
      SENT[0], ("send-keys", "-t", "%1", "-l", "--", "yes, use the second one"))
check("then submitted", SENT[1], ("send-keys", "-t", "%1", "Enter"))
check("two calls, so no ';' of yours is a separator", len(SENT), 2)
check("no problem", problem, False)
check("and it says what it did", note, "kestrel - said 23 chars")

print("\nthe head that got the answer stops asking for you")
check("its flag is dropped", CLEARED, ["sid-kestrel"])
check("it no longer wants you", h["attention"], False)
check("and reads as working", h["state"], "WORKING")

print("\na word tmux would read as a key is still a word")
# The whole reason for -l. Without it "Enter" is a keystroke and the answer
# would be an empty line.
SENT.clear()
m.say(head("otter", "%2"), "Enter C-c q")
check("sent as the text it is",
      SENT[0], ("send-keys", "-t", "%2", "-l", "--", "Enter C-c q"))

print("\nan answer that starts with a dash is an answer")
SENT.clear()
m.say(head("wren", "%3"), "--force it")
check("the -- keeps it out of the options",
      SENT[0], ("send-keys", "-t", "%3", "-l", "--", "--force it"))

print("\n--no-enter types without submitting")
SENT.clear()
CLEARED.clear()
h = head("finch", "%4")
m.say(h, "half an answer", submit=False)
check("one call, no Enter", len(SENT), 1)
check("and nothing is marked answered - it has not been", CLEARED, [])
check("so it still wants you", h["attention"], True)

print("\nwhat is refused, rather than guessed at")
SENT.clear()
note, problem = m.say(head("swift", pane=None, tty="pts/9"), "hello")
check("a head with no pane is a problem", problem, True)
check("and says where it is instead",
      note, "swift is not in a pane (pts/9) - cannot answer it")
check("nothing was typed anywhere", SENT, [])

note, problem = m.say(head("swan", "%5"), "first line\nsecond line")
check("a newline is refused", problem, True)
check("with the reason",
      note, "one line at a time - a newline would submit half of it")

note, problem = m.say(head("teal", "%6"), "   ")
check("so is an empty answer", (note, problem), ("nothing to say", True))
check("still nothing typed", SENT, [])

print("\na tmux that will not take it is reported, not assumed")
SENT.clear()


class Broke:
    returncode = 1
    stdout = ""


m.tmux = lambda *a, **kw: SENT.append(a) or Broke()
note, problem = m.say(head("robin", "%7"), "yes")
check("the failure comes back", problem, True)
check("naming the pane", note, "tmux would not type at %7")
check("and no Enter chased it", len(SENT), 1)

print()
print("all pass" if not FAILED else f"{len(FAILED)} FAILED")
raise SystemExit(1 if FAILED else 0)
