"""Two session files, one session id.

A session file is named after the pid that wrote it, and a session id outlives
a process: resume a session and Claude writes a second file, while the first
one sits there until someone runs `bottleneck reap`. Under WSL you can get the
same thing across the mount - the session resumed through the Windows install,
its old file still in the Linux home.

So the same head is described twice, by files that disagree, and something has
to pick. Reading order picks by nothing: `os.listdir` hands them over in
whatever order the directory happens to be in, so a leftover file wins half the
time - and it names a pid that has gone, which reads as a dead head and, worse,
is the pid a kill would be aimed at.

Nothing here touches a real ~/.claude - both homes are temp directories.
"""
import io
import json
import os
import tempfile
import time

from harness import bn as m
from bottleneck import procs

REAL_OPEN = open

FAILED = []
TMP = tempfile.mkdtemp(prefix="bottleneck-dupes-")
MINE = os.path.join(TMP, "linux", ".claude")
THEIRS = os.path.join(TMP, "windows", ".claude")

# Pids we say are running, and the start time /proc reports for each. Anything
# else has gone: proc_start answers None, which is what a leftover file's pid
# looks like from here.
RUNNING = {}


def check(label, got, want):
    ok = got == want
    print(("  ok   " if ok else "  FAIL ") + label.ljust(52)
          + ("" if ok else f"got {got!r}, wanted {want!r}"))
    if not ok:
        FAILED.append(label)


def put(home, pid, sid, name, started=0.0, procstart=None):
    d = os.path.join(home, "sessions")
    os.makedirs(d, exist_ok=True)
    rec = {"pid": pid, "sessionId": sid, "name": name, "cwd": "/w",
           "kind": "interactive", "status": "idle",
           "startedAt": (started or time.time()) * 1000,
           "statusUpdatedAt": time.time() * 1000}
    if procstart is not None:
        rec["procStart"] = procstart
    with open(os.path.join(d, f"{pid}.json"), "w") as fh:
        json.dump(rec, fh)


def clear():
    for home in (MINE, THEIRS):
        d = os.path.join(home, "sessions")
        for f in os.listdir(d) if os.path.isdir(d) else []:
            os.remove(os.path.join(d, f))


for home in (MINE, THEIRS):
    os.makedirs(os.path.join(home, "sessions"), exist_ok=True)

m.SESSION_DIRS = [os.path.join(MINE, "sessions"), os.path.join(THEIRS, "sessions")]
m.PROJECT_DIRS = [os.path.join(MINE, "projects"), os.path.join(THEIRS, "projects")]
m.config.SESSION_DIRS = m.SESSION_DIRS
m.config.PROJECT_DIRS = m.PROJECT_DIRS
m.proc_start = lambda pid: RUNNING.get(pid)


def records():
    return {r.get("sessionId"): r for r in m.session_records()}


print("\ntwo files for one session, in the same home")
RUNNING.clear()
RUNNING[200] = "tick-200"
clear()
put(MINE, 100, "sid-a", "the-leftover")
put(MINE, 200, "sid-a", "the-live-one", procstart="tick-200")
check("one record comes back", len(m.session_records()), 1)
check("and it is the one whose process is running",
      records()["sid-a"]["pid"], 200)

print("\nthe same, with the files written the other way round")
# The order two files land in a directory is not something to rely on, so the
# answer has to be the same either way.
clear()
put(MINE, 200, "sid-a", "the-live-one", procstart="tick-200")
put(MINE, 100, "sid-a", "the-leftover")
check("still the running one", records()["sid-a"]["pid"], 200)

print("\na leftover here loses to a live record over there")
# The WSL case: the session was resumed through the Windows install, so the
# current record is the foreign one. Ours is a file about a pid that has gone,
# and preferring it because it is ours is how a running head reads as dead.
RUNNING.clear()
clear()
put(MINE, 100, "sid-a", "the-leftover")
put(THEIRS, 4242, "sid-a", "resumed-over-there")
got = records()["sid-a"]
check("the foreign record wins", got["name"], "resumed-over-there")
check("and is marked foreign", bool(got.get(m.FOREIGN)), True)

print("\nbut a live local record still beats a foreign one")
# Unchanged from before: when our own pid is real, it is the one to have -
# it can be walked to a pane and it is ours to signal.
RUNNING.clear()
RUNNING[100] = "tick-100"
clear()
put(MINE, 100, "sid-a", "ours-and-running", procstart="tick-100")
put(THEIRS, 4242, "sid-a", "over-there")
got = records()["sid-a"]
check("ours is kept", got["name"], "ours-and-running")
check("and is not marked foreign", got.get(m.FOREIGN), None)

print("\nwith nothing running, the newest file wins")
RUNNING.clear()
clear()
put(MINE, 100, "sid-a", "older", started=time.time() - 3600)
put(MINE, 300, "sid-a", "newer", started=time.time() - 60)
check("the newer of two leftovers", records()["sid-a"]["name"], "newer")

print("\na record with no session id is never merged with another")
RUNNING.clear()
clear()
put(MINE, 100, "", "no-id-one")
put(MINE, 300, "", "no-id-two")
check("both are kept", len(m.session_records()), 2)

print("\nand the head that comes out of it is the live one")
m.panes_by_pid = lambda: ({}, set())
m.panes_by_id = lambda: {}
m.dash_pane = lambda: ""
m.pane_window = lambda pane: ""
m.tty_of = lambda pid: None
m.transcript_for = lambda sid, cwd: None
RUNNING.clear()
clear()
put(MINE, 100, "sid-a", "the-leftover")
put(THEIRS, 4242, "sid-a", "resumed-over-there")
head = m.collect()[0]
check("it is not buried as dead", head["state"] != "DEAD", True)
check("it wears the name the current file gives it",
      head["name"], "resumed-over-there")
check("and carries the pid that file names", head["pid"], 4242)

print("\nps asks the same question of a pid it finds running")
# `ps` reads /proc directly and calls a claude process a head when a session
# file names its pid. A leftover file names a pid that has gone - and the
# number gets handed out again, so the next claude to wear it was being dressed
# up as the head that file describes. Same test as everywhere else: the file
# has to be about this process, not just about this number.
RUNNING.clear()
clear()
put(MINE, 100, "sid-a", "the-leftover", procstart="tick-100")
put(MINE, 300, "sid-b", "still-running", procstart="tick-300")
RUNNING[300] = "tick-300"
# 100 is running, but it has been recycled: /proc says it started later than
# the file that names it did.
RUNNING[100] = "tick-100-but-a-different-one"
m.SESSIONS = os.path.join(MINE, "sessions")


class FakeProcOS:
    """The real os, with /proc holding the two pids this test is about."""

    def __init__(self, real, pids):
        self.real, self.pids = real, pids

    def __getattr__(self, name):
        return getattr(self.real, name)

    def listdir(self, path):
        return self.pids if path == "/proc" else self.real.listdir(path)


def fake_open(path, *a, **k):
    if str(path).startswith("/proc/") and str(path).endswith("/cmdline"):
        return io.BytesIO(b"claude \0--resume\0")
    return REAL_OPEN(path, *a, **k)


# Set on the module rather than through the harness: `os` and `open` are not
# bottleneck's names, and only this one function's view of them is being bent.
procs.os = FakeProcOS(os, ["100", "300"])
procs.open = fake_open
try:
    rows = {r["pid"]: r for r in m.claude_procs()}
finally:
    procs.os = os
    del procs.open
check("the running head is tracked", rows[300]["tracked"], True)
check("and reads as a head", rows[300]["role"], "head")
check("the recycled pid is not tracked", rows[100]["tracked"], False)
check("and is flagged rather than dressed up", rows[100]["role"], "orphan?")

print("\nall pass" if not FAILED else f"\n{len(FAILED)} FAILED: {FAILED}")
raise SystemExit(1 if FAILED else 0)
