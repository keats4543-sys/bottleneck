"""The registry: what a module can plug into, and what it cannot break.

The point of modules is that a branch carrying a feature edits no file it
shares with another branch. That only holds if the core asks generic questions,
so these are the questions - commands, help, the refresh pass, hook wiring -
and the answers a module gives to them.

The other half is containment. A module that will not import, or that throws in
the dashboard loop, must cost you that module and nothing else: the loop it
throws in runs every couple of seconds, and a feature taking the whole dashboard
down on a timer would be worse than not having the feature.

The modules here are written into a temp directory, so this tests the registry
and not whatever happens to be in the checkout.
"""
import os
import sys
import tempfile

from harness import bn as m

from bottleneck import modules as reg

TMP = tempfile.mkdtemp(prefix="bottleneck-modules-")
reg.HERE = TMP
reg.__path__.append(TMP)

FAILED = []


def check(label, got, want):
    ok = got == want
    print(("  ok   " if ok else "  FAIL ") + label.ljust(52)
          + ("" if ok else f"\n         got  {got!r}\n         want {want!r}"))
    if not ok:
        FAILED.append(label)


def write(name, body):
    os.makedirs(os.path.join(TMP, name), exist_ok=True)
    with open(os.path.join(TMP, name, "__init__.py"), "w") as fh:
        fh.write(body)
    for stale in [k for k in sys.modules if k.startswith(f"bottleneck.modules.{name}")]:
        del sys.modules[stale]
    reg.forget()


def only(names):
    os.environ["BOTTLENECK_MODULES"] = names
    reg.forget()


write("good", '''
SEEN = []
def run(cmd, rest):
    SEEN.append((cmd, rest))
    return 7
def on_pass(heads):
    SEEN.append(("pass", len(heads)))
MODULE = {"summary": "a module that works",
          "commands": {"widget": run},
          "usage": "  bottleneck widget        do the thing",
          "pass": on_pass,
          "hook": "hook.py",
          "events": lambda: [("PreToolUse", "Write"), ("SessionStart", None)],
          "state": ["widgets"]}
''')
open(os.path.join(TMP, "good", "hook.py"), "w").close()

write("rotten", "raise RuntimeError('no')\n")

write("thrower", '''
def on_pass(heads):
    raise ValueError("in the loop")
MODULE = {"summary": "throws on every refresh", "pass": on_pass}
''')


print("\nwhat a module plugs into")
only("good")
check("it is found", [n for n, _ in reg.enabled()], ["good"])
check("its command is offered", sorted(reg.commands()), ["widget"])
check("its help is collected",
      reg.usage(), ["  bottleneck widget        do the thing"])
check("the dispatch runs it, and its exit code is the command's",
      m.main(["widget", "now"]), 7)
check("with the arguments it was given",
      sys.modules["bottleneck.modules.good"].SEEN[-1], ("widget", ["now"]))
reg.on_pass([{"session_id": "a"}, {"session_id": "b"}])
check("a refresh reaches it",
      sys.modules["bottleneck.modules.good"].SEEN[-1], ("pass", 2))
check("its state directories are asked for", reg.state_dirs(), ["widgets"])


print("\nthe wiring it asks for, and nothing about what it is for")
wiring = reg.wiring()
check("one entry, named after the module",
      [w["name"] for w in wiring], ["good"])
check("with the events it named, matchers and all",
      wiring[0]["events"], [["PreToolUse", "Write"], ["SessionStart", None]])
check("and its own hook file", os.path.basename(wiring[0]["hook"]), "hook.py")
write("hookless", 'MODULE = {"summary": "no hook", "events": [("Stop", None)]}')
only("hookless")
check("a module with no hook file is wired to nothing", reg.wiring(), [])


print("\na module that will not load costs you that module")
only("rotten,good")
check("the good one still loads", [n for n, _ in reg.enabled()], ["good"])
check("and the bad one is named, with why",
      "RuntimeError" in reg.broken().get("rotten", ""), True)
check("the listing says which is which",
      [(r["name"], r["on"]) for r in reg.listing()
       if r["name"] in ("good", "rotten")],
      [("good", True), ("rotten", False)])


print("\na module that throws in the dashboard loop is put down, not passed on")
only("thrower,good")
before = len(sys.modules["bottleneck.modules.good"].SEEN)
reg.on_pass([{"session_id": "a"}])
check("the pass does not raise", True, True)
check("the module beside it still ran",
      len(sys.modules["bottleneck.modules.good"].SEEN), before + 1)
check("the thrower is disabled", "thrower" in reg.broken(), True)
reg.on_pass([{"session_id": "a"}])
check("and is not tried again on the next pass",
      [n for n, _ in reg.enabled()], ["good"])
reg.forget()
check("a reload gives it another chance",
      sorted(n for n, _ in reg.enabled()), ["good", "thrower"])


print("\nturning them off")
only("none")
check("nothing is enabled", reg.enabled(), [])
check("nothing is wired", reg.wiring(), [])
check("the command is not there any more", m.main(["widget"]), 2)
only("good")
check("naming one leaves the others out",
      [n for n, _ in reg.enabled()], ["good"])
os.environ.pop("BOTTLENECK_MODULES", None)
reg.forget()
check("saying nothing wants everything that is there",
      (reg.wanted(), sorted(reg.discovered())),
      (None, ["good", "hookless", "rotten", "thrower"]))


print("\ncontent out: what a module puts on the screen")
write("shows", '''
def mark(head):
    return "*" + head["name"]
def lines(head, width):
    return ["under " + head["name"], "and again", "third", "fourth - too many"]
def group(gid, label, heads):
    return f"{label}: {len(heads)} here"
def status(heads):
    return "%d watched" % len(heads)
MODULE = {"summary": "draws things", "mark": mark, "lines": lines,
          "group": group, "status": status}
''')
only("shows")
HEAD = {"name": "ada", "session_id": "s1", "group": "1", "group_label": "web"}
check("a badge for a row", reg.mark(HEAD), "*ada")
check("lines under a head, capped so one module cannot own the list",
      reg.lines(HEAD, 60), ["under ada", "and again", "third"])
check("a line under a group heading",
      reg.group_lines("1", "web", [HEAD, HEAD], 60), ["web: 2 here"])
check("a segment for the status line", reg.status([HEAD]), ["1 watched"])

write("nasty", '''
MODULE = {"summary": "returns rubbish",
          "mark": lambda head: "\\x1b[31mred\\n" + "x" * 40,
          "status": lambda heads: "100%% #[bg=red]"}
''')
only("nasty")
check("escapes and newlines never reach the frame",
      reg.mark(HEAD), ("[31mred" + "x" * 40)[:12])
check("and tmux formats in a status segment are made literal",
      reg.status([HEAD]), ["100%% ##[bg=red]"])


print("\nmessages in: keys and the control fifo")
write("takes", '''
SEEN = []
def z(head, heads):
    SEEN.append(("z", head["name"], len(heads)))
    return "pressed z on " + head["name"]
def quiet(head, heads):
    return ""
def verb(arg, heads):
    SEEN.append(("ctl", arg))
    return "did " + arg, False
MODULE = {"summary": "listens", "keys": {"z": z, "Z": quiet},
          "key_help": "z do the thing", "ctl": {"thing": verb}}
''')
only("takes")
check("a key it asked for reaches it",
      reg.press("z", HEAD, [HEAD]), "pressed z on ada")
check("with the row you were pointing at",
      sys.modules["bottleneck.modules.takes"].SEEN[-1], ("z", "ada", 1))
check("a key nobody owns is None, not an empty note", reg.press("y", HEAD, []), None)
check("owning a key and saying nothing is an empty note, which is not None",
      reg.press("Z", HEAD, []), "")
check("its keys are on the help line", reg.key_help(), ["z do the thing"])
check("a word on the control fifo reaches it",
      reg.ctl("thing", "now", [HEAD]), ("did now", False))
check("a verb nobody owns is None, so the core can say so",
      reg.ctl("nope", "", [HEAD]), None)


print("\nmessages in: what changed since the last pass")
write("hears", '''
SEEN = []
MODULE = {"summary": "watches", "notice": lambda kind, info: SEEN.append((kind, info))}
''')
only("hears")


def fleet(*rows):
    return [{"session_id": s, "state": st, "group": g, "group_label": l,
             "name": s} for s, st, g, l in rows]


reg.on_pass(fleet(("a", "WAITING", "", ""), ("b", "WORKING", "1", "web")))
seen = sys.modules["bottleneck.modules.hears"].SEEN
check("the first pass says nothing - a dashboard starting is not news", seen, [])
reg.on_pass(fleet(("a", "BLOCKED", "", ""), ("b", "WORKING", "1", "web"),
                  ("c", "WORKING", "", "")))
check("a head that changed state, a head that appeared",
      [(k, i.get("was") or (i["head"] or {}).get("session_id")) for k, i in seen],
      [("head.state", "WAITING"), ("head.new", "c")])
seen.clear()
reg.on_pass(fleet(("a", "BLOCKED", "1", "web"), ("b", "WORKING", "1", "hub")))
check("a head that joined a group, one that went, and a group renamed",
      sorted(k for k, _ in seen), ["group.join", "group.name", "head.gone"])
check("the join says where it came from",
      next(i["was"] for k, i in seen if k == "group.join"), "")
check("the rename says both names",
      next((i["was"], i["label"]) for k, i in seen if k == "group.name"),
      ("web", "hub"))
check("the head that went is named",
      next(i["session_id"] for k, i in seen if k == "head.gone"), "c")


print("\nthe same containment on every one of them")
write("rots", '''
def boom(*a, **k):
    raise ValueError("no")
MODULE = {"summary": "throws at every point", "mark": boom, "lines": boom,
          "group": boom, "status": boom, "keys": {"z": boom},
          "notice": boom, "ctl": {"v": boom}}
''')
for point, call in (("mark", lambda: reg.mark(HEAD)),
                    ("lines", lambda: reg.lines(HEAD, 60)),
                    ("group", lambda: reg.group_lines("1", "web", [], 60)),
                    ("status", lambda: reg.status([HEAD]))):
    only("rots,shows")
    got = call()
    check(f"a module throwing at `{point}` is dropped, the frame is still drawn",
          (bool(got), "rots" in reg.broken()), (True, True))
only("rots")
check("a key that throws answers rather than raising",
      "rots" in reg.press("z", HEAD, []), True)
check("and the module is put down", "rots" in reg.broken(), True)
only("rots")
check("a control verb that throws is a problem, not a traceback",
      reg.ctl("v", "", [])[1], True)
only("rots")
reg.on_pass(fleet(("a", "WAITING", "", "")))
reg.on_pass(fleet(("a", "BLOCKED", "", "")))
check("a notice that throws puts the module down", "rots" in reg.broken(), True)


print("\nthe core wins a name clash")
write("greedy", '''
MODULE = {"summary": "wants a core command",
          "commands": {"kill": lambda cmd, rest: 99}}
''')
only("greedy")
# `kill` with no arguments is the core saying how to use it, which is a 2. If
# the module had taken the name it would be a 99, and a module able to take
# `kill` off you is a module able to take anything off you.
check("a module cannot take a command the core already has",
      m.main(["kill"]), 2)

print("\nall pass" if not FAILED else f"\n{len(FAILED)} FAILED")
raise SystemExit(1 if FAILED else 0)
