"""Heads that belong to another machine - the WSL case.

Under WSL the claude you launch through an alias is often the one installed on
the Windows side. It writes its session files and transcripts to the Windows
home, which is readable from here at /mnt/c/Users/<you>/.claude - and writes
nothing at all to the Linux home. Read only the Linux home and the dashboard is
empty while heads are plainly running.

So every home is read. What comes back from over there is not the same kind of
thing, and the whole of this file is about that difference: the pid on a foreign
record is a Windows pid, and this side's /proc will happily answer about a
completely unrelated local process wearing the same number. Reading that as
liveness is a wrong row. Signalling it is somebody else's process killed.

Nothing here touches a real ~/.claude - both homes are temp directories.
"""
import json
import os
import shutil
import tempfile
import time

from harness import bn as m

FAILED = []
TMP = tempfile.mkdtemp(prefix="bottleneck-foreign-")
MINE = os.path.join(TMP, "linux", ".claude")
THEIRS = os.path.join(TMP, "windows", ".claude")


def check(label, got, want):
    ok = got == want
    print(("  ok   " if ok else "  FAIL ") + label.ljust(52)
          + ("" if ok else f"got {got!r}, wanted {want!r}"))
    if not ok:
        FAILED.append(label)


def put(home, pid, sid, name, cwd, status="idle", ago=0.0):
    d = os.path.join(home, "sessions")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, f"{pid}.json"), "w") as fh:
        json.dump({"pid": pid, "sessionId": sid, "name": name, "cwd": cwd,
                   "kind": "interactive", "status": status,
                   "statusUpdatedAt": (time.time() - ago) * 1000}, fh)


def transcript(home, project, sid, lines):
    d = os.path.join(home, "projects", project)
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, sid + ".jsonl")
    with open(p, "w") as fh:
        for l in lines:
            fh.write(json.dumps(l) + "\n")
    return p


# The pid a foreign head carries is the one that matters. Ours is a live local
# process - this one - so /proc has plenty to say about it, all of it about the
# wrong process. That is exactly the confusion under test.
COLLIDING = os.getpid()

put(MINE, 4242, "sid-local", "local-head", "/home/me/proj")
put(THEIRS, COLLIDING, "sid-win", "windows-head", "C:\\Users\\me\\proj")

# The Windows side names its project directories its own way; the point of the
# fallback walk is that we do not have to guess how.
WINTX = transcript(
    THEIRS, "C--Users-me-proj", "sid-win",
    [{"type": "user", "message": {"content": "look at the carry spread"}},
     {"type": "assistant", "timestamp": "2026-07-27T12:00:00Z",
      "message": {"content": [{"type": "text",
                               "text": "Found three of them."}]}}])

m.SESSION_DIRS = [os.path.join(MINE, "sessions"), os.path.join(THEIRS, "sessions")]
m.PROJECT_DIRS = [os.path.join(MINE, "projects"), os.path.join(THEIRS, "projects")]
m.config.SESSION_DIRS = m.SESSION_DIRS
m.config.PROJECT_DIRS = m.PROJECT_DIRS

print("\nboth homes are read")
recs = {r.get("sessionId"): r for r in m.session_records()}
check("the local head is found", "sid-local" in recs, True)
check("and so is the one on the other side", "sid-win" in recs, True)
check("ours is not marked foreign", recs["sid-local"].get(m.FOREIGN), None)
check("theirs is, and says where it came from",
      recs["sid-win"].get(m.FOREIGN), os.path.join(THEIRS, "sessions"))

print("\nits transcript is found without knowing how Windows spells a path")
check("the walk finds it under its session id",
      m.transcript_for("sid-win", "C:\\Users\\me\\proj"),
      os.path.join(THEIRS, "projects", "C--Users-me-proj", "sid-win.jsonl"))

print("\nthe pid on a foreign record is never read as one of ours")
# Everything below runs with no tmux and no panes.
m.panes_by_pid = lambda: ({}, set())
m.dash_pane = lambda: ""
m.pane_window = lambda pane: ""
m.proc_start = lambda pid: "alive"
m.tty_of = lambda pid: "pts/9"

heads = {h["session_id"]: h for h in m.collect()}
win = heads.get("sid-win")
check("the foreign head is on the list", win is not None, True)
check("it is flagged as foreign", win["foreign"], True)
# /proc knows COLLIDING perfectly well - it is this test - and it must not have
# been asked.
check("it has no pane, because its tree is not ours", win["pane_id"], None)
check("and no tty either", win["tty"], None)
check("so it is something to watch, not something to answer",
      win["elsewhere"], True)
check("the local head is untouched by any of this",
      (heads["sid-local"]["foreign"], heads["sid-local"]["pane_id"] is None),
      (False, True))

print("\nan old stamp is not death")
# The session file's stamp moves when the status changes and at no other time,
# so its age is the time since the last transition rather than the time since
# the head last did anything. Two hours of it is a head parked waiting on you,
# or one still inside a single long turn - alive, sat in tmux, and burying it
# takes it out of the queue that exists to hold exactly those.
check("a fresh record is alive", win["state"] != "DEAD", True)
put(THEIRS, COLLIDING, "sid-win", "windows-head", "C:\\Users\\me\\proj",
    status="busy", ago=7200)
old = {h["session_id"]: h for h in m.collect()}["sid-win"]
check("and so is one whose status last changed two hours ago",
      old["state"] != "DEAD", True)
check("its transcript is still moving, so it is not quiet either",
      old["state"], "WORKING")

# Quiet is a thing the transcript says, not the stamp - and still never /proc,
# which would answer about the test runner and call it alive for the wrong
# reason. Age the transcript and the row says how long it has been silent.
os.utime(WINTX, (time.time() - 7200, time.time() - 7200))
quiet = {h["session_id"]: h for h in m.collect()}["sid-win"]
check("a head nothing has written for two hours reads as stalled",
      quiet["state"], "STALLED")
check("and says how long", "quiet for 2h" in quiet["reason"], True)
check("that pid is very much alive here, and was never asked",
      os.path.isdir(f"/proc/{COLLIDING}"), True)

print("\nand it is never signalled")
# The one that matters. With no pane there is nothing to close, and the pid is
# not ours - so the kill has to refuse rather than SIGTERM whatever local
# process is wearing that number. Which, here, is the test runner.
killed = []
m.os.kill = lambda pid, sig: killed.append((pid, sig))
said = m.kill_head(dict(win, pane_id=None), wait=False)
check("nothing was signalled", killed, [])
check("and it says why", "not a pid here" in said, True)
check("naming the head", said.startswith("windows-head "), True)

print("\nnor moved into a pane")
told = []
m.tmux_say = lambda text: told.append(text)
m.dash_pane = lambda: "%0"
check("focus declines", m.focus(dict(win, pane_id=None), []), False)
check("and says it can be watched, not moved",
      "watched here, not moved" in (told[-1] if told else ""), True)


print("\nbut a head we opened ourselves knows its own pane")
# The pane is not worked out for these - it cannot be, the process tree is
# somebody else's. It is remembered: bottleneck ran the split-window and was
# handed the pane id back, so it wrote that down against the name it launched
# under, and the first refresh that sees both hands it over to the session id.
m.BINDS = os.path.join(TMP, "binds.json")
m.config.BINDS = m.BINDS

put(THEIRS, COLLIDING, "sid-win", "windows-head", "C:\\Users\\me\\proj")
m.bind_pane("windows-head", "%77")
book = m.binds_apply([("windows-head", "sid-win")])
check("the note is handed to the session id", m.bound_pane(book, "sid-win"), "%77")
check("and the name it was hung on is spent", "windows-head" in book["named"], False)

# The pane has to still exist, or the note names one somebody else now has.
m.panes_by_id = lambda: {"%77": (0, "bottleneck:0.1", "bottleneck:0")}
win = {h["session_id"]: h for h in m.collect()}["sid-win"]
check("the head now has the pane we opened it into", win["pane_id"], "%77")
check("with the window it is in", win["pane"], "bottleneck:0.1")
check("still foreign, still not a local pid", win["foreign"], True)
# The point of the binding: it is foreign, and it is still yours to work.
check("but no longer merely watched - it is back in the queue",
      win["elsewhere"], False)

print("\na note whose pane has gone is let go, not followed")
m.panes_by_id = lambda: {}
gone = {h["session_id"]: h for h in m.collect()}["sid-win"]
check("no pane is claimed", gone["pane_id"], None)
check("and the note is torn up", m.bound_pane(m.binds_load(), "sid-win"), "")

print("\nkilling a bound foreign head closes the pane and signals nothing")
# The hazard the binding reopens: with a pane_id the kill takes the kill-pane
# branch, and the reaper used to poll /proc for the pid and SIGKILL whatever had
# not gone. For a foreign head that is a local process wearing the same number.
m.bind_pane("windows-head", "%77")
m.binds_apply([("windows-head", "sid-win")])
killed, panes_now = [], {"%77": (0, "bottleneck:0.1", "bottleneck:0")}
m.os.kill = lambda pid, sig: killed.append((pid, sig))
m.panes_by_id = lambda: panes_now
m.tmux = lambda *a, **kw: (panes_now.clear() if a[:1] == ("kill-pane",) else None)
m.invalidate = lambda: None
bound = dict(win, pane_id="%77", foreign=True, pid=COLLIDING)
said = m.kill_head(bound)
check("the pane going is what counts as killed", said, "killed windows-head")
check("and nothing was signalled", killed, [])
check("even though that pid is alive here", os.path.isdir(f"/proc/{COLLIDING}"), True)
check("the note is torn up with it", m.bound_pane(m.binds_load(), "sid-win"), "")

print("\na pane that will not close says so rather than escalating")
panes_now["%77"] = (0, "bottleneck:0.1", "bottleneck:0")
m.tmux = lambda *a, **kw: None          # kill-pane does nothing
_sleep = m.time.sleep
m.time.sleep = lambda s: _sleep(0.001)
try:
    said = m.kill_head(dict(bound, session_id="sid-win"))
finally:
    m.time.sleep = _sleep
check("it reports rather than SIGKILLing", "will not close" in said, True)
check("and still signalled nothing", killed, [])


print("\nwhat cannot be answered is kept out of the queue")
# Watching them is useful. Being offered them is not: every key that acts on a
# head declines for these, so the go-on key would send you somewhere it cannot
# then show you, and the header would promise work you cannot do.


def row(name, state, pane, away=False):
    return {"name": name, "state": state, "pane_id": pane, "pane": pane,
            "session_id": "sid-" + name, "pid": abs(hash(name)) % 9999,
            "in_main": False, "attention": state in m.NEEDS_ATTENTION,
            "priority": m.STATES[state][0], "idle_for": 9, "slot": 1,
            "kind": "interactive", "reason": "", "step": "", "task": "",
            "group": "", "group_label": "", "group_rank": 0, "held": False,
            "active": False, "foreign": away, "elsewhere": away, "tty": None}


moved, parked = [], []
m.focus = lambda h, heads, select=True, ack=True: moved.append(h["name"]) or True
m.park = lambda heads: parked.append(True) or True
m.dash_pane = lambda: "%0"
m.main_pane_occupied = lambda dash: False
m.cycle_state = lambda: {}
m.set_cycle_state = lambda sid, mode: None

here, away = row("here-one", "WORKING", "%1"), row("win-one", "BLOCKED", None, True)

# What this used to do: the far-side head is the only one flagged, so it was the
# whole of `want`, and nothing in `want` had a pane - so the key reported a
# problem and did nothing. One unreachable head jammed it for everything else.
was = m.next_or_park([here, dict(away, elsewhere=False)])
check("that used to stop the key dead", was[1], True)
check("with a complaint about a head it could not move",
      "cannot move it" in was[0], True)

moved.clear()
note, problem = m.next_or_park([here, away])
check("now it is not a problem", problem, False)
check("the far-side head is never raised", "win-one" in moved, False)
check("and the key walks on to one it can show you", moved, ["here-one"])

# ...and one you can answer is still offered, with the other one sat there.
moved.clear()
mine = row("here-two", "BLOCKED", "%2")
note, problem = m.next_or_park([away, mine])
check("a head you can answer is still found", moved, ["here-two"])

print("\nand out of the counts that describe the queue")
check("the status line counts only what you can act on",
      m.bar_text([away, mine]).count("1 blocked"), 1)
check("and says nothing at all when only the far side wants you",
      m.bar_text([away, here]), "")
drawn = m.render([mine, away], width=84)
check("the header counts the answerable ones", " 1 heads  1 need you" in drawn, True)
check("and mentions the rest separately", "+1 elsewhere" in drawn, True)
check("under a heading of their own",
      "elsewhere - watched, not answerable" in drawn, True)

print("\nnor raised at you")
raised, _ = m.auto_raise([away], None, "%0")
check("auto-raise leaves them where they are", raised, None)

print("\nthey sort below the queue, whatever state they are in")
m.assign_slots = lambda heads: heads
order = [h["name"] for h in sorted(
    [row("win-blocked", "BLOCKED", None, True), row("local-idle", "IDLE", "%9")],
    key=lambda h: (h["elsewhere"], h["group_rank"], h["priority"], -h["idle_for"]))]
check("an idle head here outranks a blocked one over there",
      order, ["local-idle", "win-blocked"])

shutil.rmtree(TMP, ignore_errors=True)
print()
print("all pass" if not FAILED else f"{len(FAILED)} FAILED")
raise SystemExit(1 if FAILED else 0)
