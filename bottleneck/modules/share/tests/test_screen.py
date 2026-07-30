"""The boards, on the dashboard - and the boards drawn into a real frame.

The rest of the module is tested as a thing heads say to each other. This is
the other reader: the person watching the list, who wants to know which head is
behind on its group and which group is standing on a file. So these are the
four screen points the module answers, and then the same answers found in the
output of the core's own render() - because the point of an extension point is
that the core draws it, and a test of the module alone would not show that.

State goes to a temp file; no tmux, no /proc, no claude.
"""
import json
import os
import tempfile

from harness import bn as m                  # the core: render, groups
from bottleneck import modules as reg
from bottleneck.modules.share import board as b
from bottleneck.modules.share import screen as s

TMP = tempfile.mkdtemp(prefix="bottleneck-screen-")
m.QUEUE = b.QUEUE = os.path.join(TMP, "queue.json")
b.BOARD = os.path.join(TMP, "board.json")
m.CATALOG = b.CATALOG = os.path.join(TMP, "catalog.json")

FAILED = []


def check(label, got, want):
    ok = got == want
    print(("  ok   " if ok else "  FAIL ") + label.ljust(52)
          + ("" if ok else f"\n         got  {got!r}\n         want {want!r}"))
    if not ok:
        FAILED.append(label)


def has(label, text, want):
    check(label, want in (text or ""), True)


def fresh(*groups):
    """A clean board, the groups these heads are in, and no stale cache."""
    for path in (b.BOARD, b.QUEUE, b.CATALOG):
        if os.path.exists(path):
            os.remove(path)
    for sid, gid in groups:
        m.set_group(sid, gid)
    s._book, s._at = None, 0.0


def head(sid, name, gid="", label=""):
    return {"session_id": sid, "name": name, "group": gid, "group_label": label,
            "state": "WAITING", "attention": True, "in_main": False,
            "idle_for": 5, "reason": "", "step": "", "kind": "head",
            "slot": 1, "elsewhere": False, "held": 0, "task": ""}


print("\nwhat the boards look like as rows")
fresh(("s1", "1"), ("s2", "1"))
b.register("s1", TMP)
b.register("s2", TMP)
b.set_goal("s1", "the parser")
b.note("s2", "started on the lexer")
# grace has just been handed its queue; ada has not looked yet. That is the
# contrast the badge exists to draw, so the test starts from it rather than
# from two heads that happen to be equally behind.
b.flush("s2")
with open(b.CATALOG, "w") as fh:
    json.dump({"s1": {"name": "ada"}, "s2": {"name": "grace"}}, fh)
s._book = None

check("a head with something queued says how much",
      s.mark(head("s1", "ada")), "✉2")
check("a head that has been handed its queue says nothing",
      s.mark(head("s2", "grace")), "")
check("its goal goes under its name", s.lines(head("s1", "ada"), 80),
      ["→ the parser"])
check("a head that never said what it was for gets no line",
      s.lines(head("s2", "grace"), 80), [])
check("and the bar counts what is waiting across the fleet",
      s.status([head("s1", "ada"), head("s2", "grace")]), "✉2")

b.note_touch("s2", os.path.join(TMP, "lexer.py"), "wrote", TMP)
s._book = None
line = s.group("1", "web", [head("s1", "ada", "1"), head("s2", "grace", "1")])
has("a group heading says what the group last touched", line[0], "lexer.py")
has("and who did it", line[0], "grace")
check("a group nobody has touched gets no line", s.group("2", "api", []), [])


print("\nthe same thing, drawn by the core")
reg.forget()
os.environ["BOTTLENECK_MODULES"] = "share"
s._book = None
frame = m.render([head("s1", "ada", "1", "web"), head("s2", "grace", "1", "web")],
                 width=100, groups=[("1", "web")])
# The count is asked for rather than written down: the file change above
# is itself a message, and a test that hard-codes the number is a test
# that breaks whenever the board learns to say something new.
has("the badge is on the row", frame, s.mark(head("s1", "ada")))
has("the goal is under the name", frame, "→ the parser")
has("the group's line is under its heading", frame, "lexer.py")
check("the group heading still comes first",
      frame.index("web") < frame.index("lexer.py"), True)
has("and the core's own columns are untouched by any of it", frame, "WAITING")

bar = m.bar_text([head("s1", "ada", "1", "web")])
has("the tmux bar carries the count too", bar,
    s.status([head("s1", "ada", "1", "web")]))
has("after the core's own counter", bar, "1 waiting")

print("\nturned off, the list is the list")
os.environ["BOTTLENECK_MODULES"] = "none"
reg.forget()
plain = m.render([head("s1", "ada", "1", "web")], width=100, groups=[("1", "web")])
check("no badge", "✉" in plain, False)
check("no goal", "the parser" in plain, False)
check("and nothing of the module in the bar",
      "✉" in m.bar_text([head("s1", "ada", "1", "web")]), False)
os.environ.pop("BOTTLENECK_MODULES", None)
reg.forget()

print("\nall pass" if not FAILED else f"\n{len(FAILED)} FAILED")
raise SystemExit(1 if FAILED else 0)
