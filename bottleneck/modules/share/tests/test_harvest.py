"""The other way of noticing a file changed: the dashboard, not a hook.

The claim being tested is equivalence with a caveat. Reading tool calls out of
a transcript must put the same things on the board that the head's own
PostToolUse hook would have put there - the same kinds, the same paths, the
same guard behaviour - and it must not post an afternoon of history the first
time it looks at a head, or the same change twice because it read the same
bytes twice.

No tmux, no /proc, no claude: a temp directory with transcripts written by hand.
"""
import json
import os
import tempfile

from harness import bn as m                  # the core: groups, the queue
from bottleneck.modules.share import board as b
from bottleneck.modules.share import harvest as hv

TMP = tempfile.mkdtemp(prefix="bottleneck-harvest-")
PROJECTS = os.path.join(TMP, "projects")
WORK = os.path.join(TMP, "work")
os.makedirs(os.path.join(PROJECTS, WORK.replace("/", "-")))
os.makedirs(WORK)

m.QUEUE = b.QUEUE = os.path.join(TMP, "queue.json")
b.BOARD = os.path.join(TMP, "board.json")
m.CATALOG = b.CATALOG = os.path.join(TMP, "catalog.json")
m.PROJECT_DIRS = [PROJECTS]
b.SOURCE = "dashboard"

FAILED = []


def check(label, got, want):
    ok = got == want
    print(("  ok   " if ok else "  FAIL ") + label.ljust(52)
          + ("" if ok else f"\n         got  {got!r}\n         want {want!r}"))
    if not ok:
        FAILED.append(label)


def head(sid, group="1"):
    return {"session_id": sid, "group": group, "cwd": WORK}


def transcript(sid):
    return os.path.join(PROJECTS, WORK.replace("/", "-"), sid + ".jsonl")


def append(sid, *calls):
    """Add tool calls to a head's transcript, shaped the way claude writes them."""
    with open(transcript(sid), "a") as fh:
        for name, inp in calls:
            fh.write(json.dumps({
                "type": "assistant", "uuid": "x",
                "message": {"role": "assistant", "content": [
                    {"type": "text", "text": "on it"},
                    {"type": "tool_use", "id": "toolu_1", "name": name,
                     "input": inp},
                ]},
            }) + "\n")
            # A result, usually much larger than the call. The parse has to
            # walk past these without paying to decode them.
            fh.write(json.dumps({
                "type": "user", "uuid": "y",
                "message": {"role": "user", "content": [
                    {"tool_use_id": "toolu_1", "type": "tool_result",
                     "content": "ok " * 4000, "is_error": False},
                ]},
            }) + "\n")


def posted(gid="1", kinds=("wrote", "edited", "deleted")):
    return [(x["kind"], os.path.basename(x["path"]))
            for x in b.board_load()["msgs"].get(gid) or [] if x["kind"] in kinds]


def fresh():
    for path in (m.QUEUE, b.BOARD):
        try:
            os.remove(path)
        except OSError:
            pass
    for sid in ("one", "two"):
        try:
            os.remove(transcript(sid))
        except OSError:
            pass
        open(transcript(sid), "w").close()
    m.set_group("one", "1")
    m.set_group("two", "1")


target = os.path.join(WORK, "app.py")
other = os.path.join(WORK, "notes.md")


print("\na head first seen is read from its end, not from its beginning")
fresh()
open(target, "w").close()
append("one", ("Write", {"file_path": target}))
check("what happened before anyone was watching stays there",
      hv.harvest([head("one")]), 0)
check("and nothing is on the board", posted(), [])


print("\nfrom then on, what it does lands the way the hook would have landed it")
append("one", ("Edit", {"file_path": target}))
check("one change, recorded once", hv.harvest([head("one")]), 1)
check("as an edit of that file", posted(), [("edited", "app.py")])
check("reading the same bytes again posts nothing",
      hv.harvest([head("one")]), 0)
check("and leaves the board alone", posted(), [("edited", "app.py")])


print("\nthe filesystem settles what the tool name only suggests")
append("one", ("Write", {"file_path": other}))          # never created
append("one", ("Bash", {"command": f"rm -f {target}"}))  # and this one is gone
os.remove(target)
hv.harvest([head("one")])
check("a write of a file that is not there is a deletion",
      ("deleted", "notes.md") in posted(), True)
check("so is an rm, once the file has actually gone",
      ("deleted", "app.py") in posted(), True)


print("\nreads are noticed too - it is what keeps the guard quiet")
fresh()
open(target, "w").close()
hv.harvest([head("one"), head("two")])                    # both seen, both at EOF
append("one", ("Write", {"file_path": target}))
hv.harvest([head("one"), head("two")])
check("two is warned about one's write",
      "app.py" in b.clash("two", target), True)
append("two", ("Read", {"file_path": target}))
hv.harvest([head("one"), head("two")])
check("having read it, two is not", b.clash("two", target), "")
check("a read is not news on the board", posted(), [("wrote", "app.py")])


print("\nan ungrouped head is not read at all")
fresh()
m.set_group("two", "")
append("two", ("Write", {"file_path": target}))
hv.harvest([head("two", group="")])
check("nothing of its is on any board", b.board_load()["msgs"], {})
check("and nothing about it was written down", b.board_load()["scan"], {})


print("\na dashboard that has been away is not a witness")
fresh()
hv.harvest([head("one")])
with open(transcript("one"), "a") as fh:
    fh.write(" " * (hv.CATCHUP_MAX + 1000) + "\n")
append("one", ("Write", {"file_path": target}))
check("the gap is skipped rather than replayed",
      hv.harvest([head("one")]), 0)
check("nothing landed", posted(), [])
append("one", ("Edit", {"file_path": target}))
check("and it picks up again from there",
      (hv.harvest([head("one")]), posted()), (1, [("edited", "app.py")]))


print("\nthe source setting is what decides who does this")
fresh()
append("one", ("Write", {"file_path": target}))
b.SOURCE = "hook"
check("with the hook feeding the boards, the dashboard reads nothing",
      hv.harvest([head("one")]), 0)
b.SOURCE = "dashboard"


print("\nthe rules are one set, not two")
# What the hook does on a PostToolUse and what this does from a transcript are
# the same function called with the same arguments. This is the guard on that
# staying true: both sides go through share.touches.
check("a write that exists", b.touches("Write", {"file_path": target}, WORK),
      [("wrote", target)])
os.remove(target)
check("a write that does not", b.touches("Write", {"file_path": target}, WORK),
      [("deleted", target)])
check("a read", b.touches("Read", {"file_path": other}, WORK),
      [("read", other)])
check("a relative path is against the head's own directory",
      b.touches("Read", {"file_path": "sub/x.py"}, WORK),
      [("read", os.path.join(WORK, "sub/x.py"))])
check("a shell command that removed nothing that is gone",
      b.touches("Bash", {"command": "ls -la"}, WORK), [])

print("\nall pass" if not FAILED else f"\n{len(FAILED)} FAILED")
raise SystemExit(1 if FAILED else 0)
