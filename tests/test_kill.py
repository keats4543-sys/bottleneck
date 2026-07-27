"""Killing a head without the dashboard going away for four seconds.

Stopping a head is a signal and then a wait on /proc. The wait used to happen on
the loop that draws the list and reads your keys, so the whole dashboard sat
frozen through it - up to four and a half seconds if the head was slow to go,
for a key you pressed to make things move faster.

These tests hold the split to its promise: the signal still goes from the thread
that pressed the key, so a kill that cannot even start says so at once; the
waiting goes somewhere else; and `bottleneck kill`, which has nothing else to do
with the time, still waits and still reports the truth.

No process is signalled and no pane is closed - /proc and tmux are both faked.
"""
import time

from harness import bn as m

FAILED = []
KILLED = []             # every tmux() and os.kill() we would have made
ALIVE = set()           # pids the fake /proc still has


def check(label, got, want):
    ok = got == want
    print(("  ok   " if ok else "  FAIL ") + label.ljust(52)
          + ("" if ok else f"got {got!r}, wanted {want!r}"))
    if not ok:
        FAILED.append(label)


def head(name="kestrel", pid=4242, pane="%3"):
    return {"name": name, "pid": pid, "pane_id": pane, "session_id": "sid-" + name}


m.tmux = lambda *a, **kw: KILLED.append(a)
m.os.kill = lambda pid, sig: KILLED.append(("kill", pid, sig))
m.clear_attention = lambda sid: True
_isdir = m.os.path.isdir
m.os.path.isdir = lambda p: (int(p.rsplit("/", 1)[-1]) in ALIVE
                             if p.startswith("/proc/") else _isdir(p))


def settle(seconds=4.0):
    """Wait for the reaper to have something to say, or give up."""
    until = time.time() + seconds
    while time.time() < until:
        said = m.reaped()
        if said:
            return said
        time.sleep(0.02)
    return []


print("\nthe waiting comes off the loop that draws the list")
KILLED.clear()
ALIVE.clear()
ALIVE.add(4242)
h = head()
began = time.time()
note = m.kill_head(h, wait=False)
took = time.time() - began
check("the key returns at once", took < 0.1, True)
check("and says what it is doing", note, "killing kestrel…")
check("the pane was closed from this thread, not the other one",
      KILLED, [("kill-pane", "-t", "%3")])
check("nothing to report yet", m.reaped(), [])

# The head goes while the reaper is watching.
ALIVE.discard(4242)
check("the outcome turns up once it has died", settle(), ["killed kestrel"])
check("and is only handed over once", m.reaped(), [])

print("\na kill that cannot even start says so on the spot")
KILLED.clear()
bg = {"name": "wren", "pid": 77, "pane_id": None, "session_id": "sid-wren"}


def gone(pid, sig):
    raise ProcessLookupError


m.os.kill = gone
check("a head already gone is not waited on",
      m.kill_head(bg, wait=False), "wren was already gone")
check("and nothing is left running to report it later", settle(0.3), [])


def refused(pid, sig):
    raise PermissionError


m.os.kill = refused
check("nor is one we are not allowed to touch",
      m.kill_head(bg, wait=False), "not allowed to kill wren (pid 77)")
m.os.kill = lambda pid, sig: KILLED.append(("kill", pid, sig))

print("\na background head has no pane, so it takes the signal")
KILLED.clear()
ALIVE.clear()
ALIVE.add(77)
m.kill_head(bg, wait=False)
check("SIGTERM, not kill-pane", KILLED[0][:2], ("kill", 77))
ALIVE.discard(77)
check("and it reports like any other", settle(), ["killed wren"])

print("\nthe one-shot still waits, because nothing else will")
KILLED.clear()
ALIVE.clear()                       # already gone: the first check sees it
check("it returns the outcome itself, not a promise",
      m.kill_head(head("otter", 900)), "killed otter")
check("and leaves nothing in the box for a dashboard that is not there",
      m.reaped(), [])

print("\na head that will not die is said so, not waited on for ever")
# The real thing waits four seconds. Shorten the wait rather than sit through
# it: what is under test is what it says at the end, not how patiently it says
# it.
ALIVE.clear()
ALIVE.add(555)
_sleep = m.time.sleep
m.time.sleep = lambda s: _sleep(0.001)
try:
    check("SIGKILL, then the truth",
          m.kill_head(head("flint", 555)),
          "flint (pid 555) will not die - check it by hand")
finally:
    m.time.sleep = _sleep

print()
print("all pass" if not FAILED else f"{len(FAILED)} FAILED")
raise SystemExit(1 if FAILED else 0)
