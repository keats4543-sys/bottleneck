"""The group board: what heads tell each other, and when they are told it.

Four claims are being tested. A group is the boundary - a head in none writes
nothing and reads nothing. The queue is per reader, so two siblings both get
every message once, whenever each of them next looks. The board is bounded, and
says so when something fell off the end rather than quietly shortening. And the
guard only speaks when there is a race: another head changed a file, and this
one has not read it since.

State goes to a temp file; no tmux, no /proc, no claude.
"""
import json
import os
import subprocess
import sys
import tempfile

from harness import bn as m                  # the core: groups, the queue
from bottleneck.modules.share import board as b
from bottleneck.modules.share import harvest as hv

TMP = tempfile.mkdtemp(prefix="bottleneck-share-")
m.QUEUE = b.QUEUE = os.path.join(TMP, "queue.json")
b.BOARD = os.path.join(TMP, "board.json")
m.CATALOG = b.CATALOG = os.path.join(TMP, "catalog.json")

MODULE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAILED = []


def check(label, got, want):
    ok = got == want
    print(("  ok   " if ok else "  FAIL ") + label.ljust(52)
          + ("" if ok else f"\n         got  {got!r}\n         want {want!r}"))
    if not ok:
        FAILED.append(label)


def has(label, text, want):
    ok = want in (text or "")
    print(("  ok   " if ok else "  FAIL ") + label.ljust(52)
          + ("" if ok else f"\n         wanted {want!r} in:\n{text}"))
    if not ok:
        FAILED.append(label)


def hasnt(label, text, unwanted):
    ok = unwanted not in (text or "")
    print(("  ok   " if ok else "  FAIL ") + label.ljust(52)
          + ("" if ok else f"\n         did not want {unwanted!r} in:\n{text}"))
    if not ok:
        FAILED.append(label)


def fresh(*groups):
    for path in (m.QUEUE, b.BOARD):
        try:
            os.remove(path)
        except OSError:
            pass
    for sid, gid in groups:
        m.set_group(sid, gid)


CWD = "/tmp/work"
T = 1_000_000.0                      # every time in here is explicit


print("\na group is the sharing boundary")
fresh(("alpha", "1"), ("beta", "1"), ("loner", ""))
b.register("alpha", CWD, now=T)
b.register("loner", CWD, now=T)
b.set_goal("loner", "nobody's business", now=T)
b.note_touch("loner", CWD + "/solo.py", "wrote", CWD, now=T)
check("a head in no group posts nothing",
      [x for gid in b.board_load()["msgs"].values() for x in gid
       if x["sid"] == "loner"], [])
check("and is handed nothing", b.brief("loner", CWD, now=T), "")
check("a group of one with a quiet board costs nothing either",
      b.brief("alpha", CWD, now=T), "")


print("\nwhat one head is for, its siblings are told")
fresh(("alpha", "1"), ("beta", "1"))
b.register("alpha", CWD, now=T)
b.register("beta", CWD, now=T)
b.set_goal("alpha", "port the webhook retries", CWD, now=T)
brief = b.brief("beta", CWD, now=T + 60)
has("the goal reaches the sibling", brief, "port the webhook retries")
has("and the sibling is listed with it", brief,
    "alpha  in this same directory")
has("one sibling is one sibling", brief, "1 other head shares this group")
hasnt("a head is never listed as its own sibling",
      b.brief("alpha", CWD, now=T + 60), "alpha  in ")
check("a goal said twice is not news twice",
      b.set_goal("alpha", "port the webhook retries", CWD, now=T + 5)
      and len([x for x in b.board_load()["msgs"]["1"]
               if x["kind"] == "goal"]), 1)


print("\nfile changes travel, paths as the reader would type them")
fresh(("alpha", "1"), ("beta", "1"))
b.register("alpha", CWD, now=T)
b.register("beta", CWD, now=T)
b.note_touch("alpha", CWD + "/svc/webhook.py", "wrote", CWD, now=T)
b.note_touch("alpha", CWD + "/tests/test_retry.py", "deleted", CWD, now=T + 10)
brief = b.brief("beta", CWD, now=T + 70)
has("the write arrives", brief, "alpha wrote svc/webhook.py")
has("so does the deletion", brief, "alpha deleted tests/test_retry.py")
has("with how long ago, not a timestamp", brief, "1m ago")
has("and what to do about it", brief, "Re-read any of those files")
check("a path outside the reader's directory keeps its own shape",
      b.short("/etc/hosts", CWD), "/etc/hosts")


print("\neight edits to one file are one thing happening")
fresh(("alpha", "1"), ("beta", "1"))
b.register("alpha", CWD, now=T)
b.register("beta", CWD, now=T)
def kinds(gid="1", kind="edited"):
    return [x for x in b.board_load()["msgs"].get(gid) or []
            if x["kind"] == kind]


for i in range(8):
    b.note_touch("alpha", CWD + "/cli.py", "edited", CWD, now=T + i)
check("coalesced into one message", len(kinds()), 1)
check("which carries the time of the last of them", kinds()[0]["at"], T + 7)
check("but not across the window",
      (b.note_touch("alpha", CWD + "/cli.py", "edited", CWD, now=T + 999)
       and len(kinds())), 2)


print("\nthe queue is per reader, and reading it is not emptying it")
fresh(("alpha", "1"), ("beta", "1"), ("gamma", "1"))
for sid in ("alpha", "beta", "gamma"):
    b.register(sid, CWD, now=T)
b.note_touch("alpha", CWD + "/one.py", "wrote", CWD, now=T)


def written(msgs):
    return [x["path"] for x in msgs if x["kind"] == "wrote"]


got, _ = b.flush("beta", now=T + 1)
check("beta is handed it", written(got), [CWD + "/one.py"])
check("and not again", b.flush("beta", now=T + 2)[0], [])
check("gamma has not read yet, so gamma still gets it",
      written(b.flush("gamma", now=T + 3)[0]), [CWD + "/one.py"])
check("nobody is handed their own message",
      [x for x in b.flush("alpha", now=T + 4)[0] if x["sid"] == "alpha"], [])


print("\nthe board is bounded, and says when something fell off it")
fresh(("alpha", "1"), ("beta", "1"))
b.register("alpha", CWD, now=T)
b.register("beta", CWD, now=T)
b.flush("beta", now=T)                       # beta is up to date, then goes away
for i in range(b.MAX + 5):
    b.note_touch("alpha", f"{CWD}/f{i}.py", "wrote", CWD, now=T + i * 1000)
check("no more than MAX are kept", len(b.board_load()["msgs"]["1"]), b.MAX)
got, missed = b.flush("beta", now=T + 99_000)
check("beta gets the ones that are left", len(got), b.MAX)
check("and is told how many it missed", missed, 5)
check("a head that has never read gets the board, not a warning",
      b.flush("delta", now=T)[1], 0)


print("\nthe guard speaks when there is a race, and not otherwise")
fresh(("alpha", "1"), ("beta", "1"))
b.register("alpha", CWD, now=T)
b.register("beta", CWD, now=T)
b.note_read("beta", CWD + "/shared.py", now=T)
check("nothing has happened yet", b.clash("beta", CWD + "/shared.py", now=T), "")
b.note_touch("alpha", CWD + "/shared.py", "wrote", CWD, now=T + 60)
said = b.clash("beta", CWD + "/shared.py", now=T + 120)
has("a sibling wrote it after beta read it", said, "alpha")
has("said as a race, not as history", said, "after you last read it")
check("alpha is not warned about alpha's own write",
      b.clash("alpha", CWD + "/shared.py", now=T + 120), "")
b.note_read("beta", CWD + "/shared.py", now=T + 100)
check("re-reading it settles the matter",
      b.clash("beta", CWD + "/shared.py", now=T + 120), "")
b.note_touch("alpha", CWD + "/gone.py", "deleted", CWD, now=T + 60)
has("a deletion is worded as one",
    b.clash("beta", CWD + "/gone.py", now=T + 61), "may no longer be there")
check("an ancient change is history, not a clash",
      b.clash("beta", CWD + "/gone.py", now=T + b.GUARD_TTL + 61), "")


print("\nleaving is said once, and stops the head being a sibling")
fresh(("alpha", "1"), ("beta", "1"))
b.register("alpha", CWD, now=T)
b.register("beta", CWD, now=T)
b.set_goal("alpha", "the thing", CWD, now=T)
b.leave("alpha", now=T + 10)
brief = b.brief("beta", CWD, now=T + 20)
has("the sibling is told it ended", brief, "alpha ended")
has("and stops being listed", brief, "no other head is in this group")


print("\ncorrupt state is not a crash")
with open(b.BOARD, "w") as fh:
    fh.write("{ not json")
check("an unreadable board reads as empty", b.board_load()["msgs"], {})
check("and writing to it still works",
      bool(b.register("alpha", CWD, now=T)), True)


# --------------------------------------------------------------- the hook
#
# Everything above is the library. This is the wire: the hook is what claude
# actually runs, and a board that works while nothing reaches it is not a
# feature. Run as a subprocess, the way claude runs it, with its own state.

print("\nthe hook, end to end")
HOOK_STATE = tempfile.mkdtemp(prefix="bottleneck-hook-")
os.makedirs(HOOK_STATE, exist_ok=True)
with open(os.path.join(HOOK_STATE, "queue.json"), "w") as fh:
    json.dump({"order": ["1"], "names": {"1": "payments"},
               "of": {"one": "1", "two": "1"}, "hold": {}}, fh)
WORK = tempfile.mkdtemp(prefix="bottleneck-work-")


def hook(event, **kw):
    ev = {"session_id": kw.pop("sid", "one"), "hook_event_name": event,
          "cwd": WORK, **kw}
    env = dict(os.environ, BOTTLENECK_STATE=HOOK_STATE)
    out = subprocess.run([sys.executable, os.path.join(MODULE_DIR, "hook.py")],
                         input=json.dumps(ev), capture_output=True, text=True,
                         env=env)
    check(f"{event} exits clean", (out.returncode, out.stderr), (0, ""))
    try:
        return json.loads(out.stdout or "{}").get("hookSpecificOutput") or {}
    except ValueError:
        return {"stdout": out.stdout}


target = os.path.join(WORK, "app.py")
open(target, "w").close()
hook("UserPromptSubmit", sid="one", prompt="rewrite the retry path")
hook("PostToolUse", sid="one", tool_name="Write",
     tool_input={"file_path": target}, tool_response={})
said = hook("UserPromptSubmit", sid="two", prompt="check the tests")
has("the sibling's goal arrives through the hook",
    said.get("additionalContext"), "rewrite the retry path")
has("and so does the file it wrote",
    said.get("additionalContext"), "wrote app.py")
check("the head's own first prompt became its goal",
      json.load(open(os.path.join(HOOK_STATE, "share", "board.json")))
      ["who"]["one"]["goal"], "rewrite the retry path")

said = hook("PreToolUse", sid="two", tool_name="Edit",
            tool_input={"file_path": target})
has("editing behind a sibling is warned about",
    said.get("additionalContext"), "before you write")
hook("PostToolUse", sid="two", tool_name="Read", tool_input={"file_path": target})
said = hook("PreToolUse", sid="two", tool_name="Edit",
            tool_input={"file_path": target})
check("having read it, the warning stops", said.get("additionalContext"), None)

os.remove(target)
hook("PostToolUse", sid="two", tool_name="Bash",
     tool_input={"command": f"rm -f {target}"}, tool_response={})
said = hook("UserPromptSubmit", sid="one", prompt="what now")
has("a deletion from the shell is noticed too",
    said.get("additionalContext"), "deleted app.py")

hook("SessionEnd", sid="two")
check("an ended head is off the board",
      "two" in json.load(open(os.path.join(HOOK_STATE, "share", "board.json")))["who"],
      False)

print("\nall pass" if not FAILED else f"\n{len(FAILED)} FAILED")
raise SystemExit(1 if FAILED else 0)
