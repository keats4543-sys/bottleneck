"""Reading a transcript by what has been added to it, not from the top again.

Your prompt and an agent's launch can be a megabyte back in a file the head is
still writing to. Re-reading that window every time the file changed cost about
7ms and a megabyte per head per pass - fifteen busy heads came to 101ms and
12.8MB, nearly all of it bytes that had not moved.

A transcript only grows, so the window is read once and after that only what is
new. These tests hold that to giving the same answers: what carries forward,
what a new line changes, and what happens when the file is not the one we were
reading any more.
"""
import json
import os
import tempfile

from harness import bn as m

TMP = tempfile.mkdtemp()
FAILED = []
COUNT = [0]


def check(label, got, want):
    ok = got == want
    print(("  ok   " if ok else "  FAIL ") + label.ljust(48)
          + ("" if ok else f"\n         got  {got!r}\n         want {want!r}"))
    if not ok:
        FAILED.append(label)


def prompt(s):
    return {"type": "user", "timestamp": "2026-07-27T09:00:00Z",
            "message": {"content": s}}


def said(s):
    return {"type": "assistant", "timestamp": "2026-07-27T09:00:01Z",
            "message": {"content": [{"type": "text", "text": s}]}}


def bulk(n):
    """An assistant turn big enough to push things out of a window."""
    return said("x" * n)


def new_file(*entries):
    COUNT[0] += 1
    path = os.path.join(TMP, f"t{COUNT[0]}.jsonl")
    with open(path, "w") as fh:
        for e in entries:
            fh.write(json.dumps(e) + "\n")
    return path


def append(path, *entries):
    with open(path, "a") as fh:
        for e in entries:
            fh.write(json.dumps(e) + "\n")
    return path


def read(path):
    """read_step with its own cache dropped, so the scan is what is tested."""
    m._steps.clear()
    return m.read_step(path)


def task_of(path):
    return read(path)[3]


def step_of(path):
    return read(path)[0]


print("\nthe same answers as reading it whole")
p = new_file(prompt("reconcile V16 against S0"), said("Working on it."))
check("the prompt", task_of(p), "reconcile V16 against S0")
check("and the step", step_of(p), "» Working on it.")

print("\ncarried forward, not read again")
check("a prompt read once stays after more is written",
      task_of(append(p, said("Still going."))), "reconcile V16 against S0")
check("even after the file has grown past the window",
      task_of(append(p, bulk(m.TASK_BUDGET + 5000))),
      "reconcile V16 against S0")
check("and the step still comes from the newest line",
      step_of(append(p, said("Still going."))), "» Still going.")
check("a new prompt replaces it",
      task_of(append(p, prompt("now do V17"))), "now do V17")

print("\nonly the bytes that are new")
p2 = new_file(prompt("first ask"), said("ok"))
read(p2)
before = m._scan[p2]["at"]
read(p2)
check("nothing appended, nothing read", m._scan[p2]["at"], before)
append(p2, said("more"))
read(p2)
check("appending moves us on", m._scan[p2]["at"] > before, True)

print("\na line that is still being written")
p3 = new_file(prompt("the ask"), said("done"))
with open(p3, "a") as fh:
    fh.write('{"type": "user", "message": {"content": "half a li')
check("half a line is not an answer yet", task_of(p3), "the ask")
with open(p3, "a") as fh:
    fh.write('ne"}}\n')
check("and is read once it is whole", task_of(p3), "half a line")

print("\na file that is not the one we were reading")
p4 = new_file(prompt("the old ask"), said("ok"))
read(p4)
with open(p4, "w") as fh:                       # same name, new file
    fh.write(json.dumps(prompt("the new ask")) + "\n")
check("a shorter file is read from scratch", task_of(p4), "the new ask")

print("\nthe tail it keeps")
p5 = new_file(*[said(f"line {i}") for i in range(60)])
read(p5)
check("no more lines than the parse wants",
      len(m._scan[p5]["tail"]), m.TAIL_LINES)
check("and it is the newest of them",
      json.loads(m._scan[p5]["tail"][-1])["message"]["content"][0]["text"],
      "line 59")
p6 = new_file(said("small"))
read(p6)
append(p6, bulk(m.TAIL_BYTES + 1000))
read(p6)
held = sum(len(l) for l in m._scan[p6]["tail"])
check("one huge entry does not make us hold two",
      len(m._scan[p6]["tail"]), 1)
check("and what is held is that one entry", held > m.TAIL_BYTES, True)

print("\nagents still count across reads")
launch = {"type": "user", "timestamp": "2026-07-27T09:00:02Z",
          "message": {"content": [{"type": "tool_result", "content":
                                   "Async agent launched successfully\nagentId: abc123"}]}}
back = {"type": "user", "timestamp": "2026-07-27T09:00:03Z",
        "message": {"content": [{"type": "text", "content": None, "text":
                                 "<task-notification><task-id>abc123</task-id>"}]}}
p7 = new_file(prompt("go"), launch)
check("one out", read(p7)[4], 1)
check("still out after an unrelated line", read(append(p7, said("thinking")))[4], 1)
check("still out once the launch is off the tail",
      read(append(p7, *[said(f"n{i}") for i in range(50)]))[4], 1)
check("and back in when it reports", read(append(p7, back))[4], 0)

print("\nnothing there at all")
check("a file that does not exist", m.read_step(os.path.join(TMP, "nope.jsonl")),
      ("", 0.0, "", "", 0))
p8 = new_file()
check("an empty file", read(p8), ("", 0.0, "", "", 0))

print("\nall pass" if not FAILED else f"\n{len(FAILED)} FAILED")
raise SystemExit(1 if FAILED else 0)
