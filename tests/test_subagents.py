"""A head waiting on agents it started. Nothing is run - transcripts are built.

An async agent returns its tool call at once and finishes later, so the head
that started it ends its turn and the harness writes down "idle". It is not
idle: it has work out and is waiting to be told about it. Reading only the
harness makes a head with six agents running look exactly like a head with
nothing left to do.

The two halves are both in the head's own transcript - the launch comes back
naming an agentId, the finish arrives as a task-notification carrying the same
id - so what these tests pin is the arithmetic between them, and the anchoring
that keeps a head *writing about* agents from counting as a head running them.
"""
import json
import os
import tempfile
import time

from harness import bn as m

TMP = tempfile.mkdtemp()
FAILED = []


def check(label, got, want):
    ok = got == want
    print(("  ok   " if ok else "  FAIL ") + label.ljust(52)
          + ("" if ok else f"got {got!r}, wanted {want!r}"))
    if not ok:
        FAILED.append(label)


def lines(*entries):
    return [json.dumps(e).encode() for e in entries]


def launch(*ids, lead=""):
    """The tool result the Agent tool hands back, as the harness writes it."""
    body = "".join(
        f"{lead}Async agent launched successfully. (internal metadata)\n"
        f"agentId: {i} (internal ID - do not mention to user.)\n" for i in ids)
    return {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "toolu_x", "content": body}]}}


def notify(tid, lead=""):
    body = (f"{lead}<task-notification>\n<task-id>{tid}</task-id>\n"
            f"<status>completed</status>\n<summary>Agent finished</summary>\n"
            f"</task-notification>")
    return {"type": "user", "message": {"content": [
        {"type": "text", "text": body}]}}


def said(text):
    """The head's own words - what it wrote, not what the harness told it."""
    return {"type": "assistant", "message": {"content": [
        {"type": "text", "text": text}]}}


print("\ncounting what went out against what came back")
check("nothing sent, nothing out", m.outstanding(lines(said("hello"))), 0)
check("one sent and still out", m.outstanding(lines(launch("a1"))), 1)
check("one sent, one back", m.outstanding(lines(launch("a1"), notify("a1"))), 0)
check("three sent, one back",
      m.outstanding(lines(launch("a1", "a2", "a3"), notify("a2"))), 2)
check("a notification for something never sent counts for nothing",
      m.outstanding(lines(notify("zz"))), 0)
check("the same one finishing twice is still just finished",
      m.outstanding(lines(launch("a1"), notify("a1"), notify("a1"))), 0)
check("several launches over several turns add up",
      m.outstanding(lines(launch("a1"), said("on it"), launch("a2"),
                          said("and another"), notify("a1"))), 1)

print("\na head writing about agents is not a head running them")
# This test exists because the session that wrote this feature quoted both
# markers into its own transcript, and an unanchored match counted them.
check("a launch quoted mid-sentence is not a launch",
      m.outstanding(lines(launch("a1", lead="the tool says: "))), 0)
check("a notification quoted mid-sentence does not close anything",
      m.outstanding(lines(launch("a1"), notify("a1", lead="it replies: "))), 1)
check("the head's own prose never counts, whatever it quotes",
      m.outstanding(lines(said("Async agent launched successfully\nagentId: a1"))), 0)
check("nor does a transcript it printed into a tool result",
      m.outstanding(lines({"type": "user", "message": {"content": [
          {"type": "tool_result", "tool_use_id": "t",
           "content": "$ cat log\nAsync agent launched successfully\n"
                      "agentId: a1\n"}]}})), 0)

print("\nthe agents' own writing counts as the head's activity")
kids = os.path.join(TMP, "sess", "subagents")
os.makedirs(kids, exist_ok=True)
check("no agent transcripts, nothing to report",
      m.subagent_seen(os.path.join(TMP, "empty.jsonl")), 0.0)
path = os.path.join(TMP, "sess.jsonl")
open(path, "w").close()
check("a directory with nothing in it is the same as none",
      m.subagent_seen(path), 0.0)
with open(os.path.join(kids, "agent-a1.jsonl"), "w") as fh:
    fh.write("{}\n")
os.utime(os.path.join(kids, "agent-a1.jsonl"), (1000, 1000))
check("one agent writing is when it wrote", m.subagent_seen(path), 1000.0)
with open(os.path.join(kids, "agent-a2.jsonl"), "w") as fh:
    fh.write("{}\n")
os.utime(os.path.join(kids, "agent-a2.jsonl"), (2000, 2000))
check("with several it is the busiest of them", m.subagent_seen(path), 2000.0)
with open(os.path.join(kids, "notes.txt"), "w") as fh:
    fh.write("x")
os.utime(os.path.join(kids, "notes.txt"), (9000, 9000))
check("and only their transcripts count", m.subagent_seen(path), 2000.0)

print("\nwhat the dashboard makes of it")
#
# The real collect(), with everything it reads about the machine stubbed out.
# The point is the order of the rules, not the plumbing: this is where "waiting
# on agents" has to beat both the stop flag its finished turn left behind and
# the harness writing "idle", and it can only be shown against the actual chain.
STALL = m.STALL_SECS

m.session_records = lambda: [
    {"pid": 4242, "sessionId": "sid-1", "cwd": "/w", "name": "kestrel",
     "kind": "interactive", "status": STUB["raw"]}]
m.transcript_for = lambda sid, cwd: "/does-not-matter.jsonl"
m.read_step = lambda path: ("» dispatched", STUB["last_ts"], "", "", STUB["kids"])
m.subagent_seen = lambda path: 0.0
m.read_attention = lambda sid: ({"kind": "stop"} if STUB["stop"] else None)
m.ack_ts = lambda sid: STUB["seen"]
m.proc_start = lambda pid: "alive"
m.tty_of = lambda pid: None
m.panes_by_pid = lambda: ({}, set())
m.dash_pane = lambda: ""
m.pane_window = lambda pane: ""

STUB = {"raw": "idle", "kids": 0, "last_ts": 0.0, "stop": False,
        "seen": 0.0}


def state_of(kids, quiet, raw="idle", stop=False, unread=True):
    now = time.time()
    # An unread turn is what makes an idle head read as DONE; acking it is
    # the difference between "finished, go look" and plain nothing-to-do.
    STUB.update(raw=raw, kids=kids, last_ts=now - quiet, stop=stop,
                seen=0.0 if unread else now)
    rows = m.collect()
    return rows[0]["state"], rows[0]["reason"]


check("agents out and someone is writing -> working",
      state_of(2, quiet=5), ("WORKING", "waiting on 2 subagents"))
check("one of them reads as one, not as 1 subagents",
      state_of(1, quiet=5), ("WORKING", "waiting on 1 subagent"))
check("the harness calling it idle does not overrule that",
      state_of(1, quiet=5, raw="idle")[0], "WORKING")
check("nor does the stop flag its finished turn left behind",
      state_of(1, quiet=5, raw="idle", stop=True)[0], "WORKING")
check("and it is not something you have to answer",
      m.collect()[0]["attention"], False)
# An agent that reads and searches for a living writes nothing for minutes at a
# time, and that is it working. The row says how long and stays out of the
# queue; only a silence long enough to stop being explicable asks for you.
check("agents out and quiet a while -> still working, with the age",
      state_of(1, quiet=m.QUIET_SECS + 60),
      ("WORKING", f"waiting on 1 subagent - quiet for {m.fmt_age(m.QUIET_SECS + 60)}"))
check("and a long quiet turn is not something you have to answer",
      m.collect()[0]["attention"], False)
check("agents out but everything silent far too long -> stalled",
      state_of(1, quiet=STALL + 60)[0], "STALLED")
check("which says what it is actually waiting on",
      "1 subagent out" in state_of(1, quiet=STALL + 60)[1], True)
check("a head on its own, quiet a while -> working, and says how long",
      state_of(0, quiet=m.QUIET_SECS + 60, raw="busy"),
      ("WORKING", f"quiet for {m.fmt_age(m.QUIET_SECS + 60)}"))
check("and only past the stall cutoff does it ask for you",
      state_of(0, quiet=STALL + 60, raw="busy")[0], "STALLED")
check("no agents out, turn read -> plain idle",
      state_of(0, quiet=5, unread=False)[0], "IDLE")
check("no agents out, turn unread -> done, as it always was",
      state_of(0, quiet=5)[0], "DONE")
check("no agents out and a stop flag is still done",
      state_of(0, quiet=5, stop=True)[0], "DONE")
check("a head genuinely busy is untouched by any of this",
      state_of(0, quiet=5, raw="busy")[0], "WORKING")

print()
print("all pass" if not FAILED else f"{len(FAILED)} FAILED")
raise SystemExit(1 if FAILED else 0)
