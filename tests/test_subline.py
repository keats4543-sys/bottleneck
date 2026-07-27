"""What a row says a head wants. Reads made-up transcripts off disk.

"Claude needs your permission to use Bash" is the same sentence on every row
that needs you, which makes five waiting heads look identical. These tests hold
the subline to something you could act on: the tool being held out for, the
question being put, or the first thing the head actually said.
"""
import json
import os
import tempfile

from harness import bn as m

TMP = tempfile.mkdtemp()
FAILED = []


def check(label, got, want):
    ok = got == want
    print(("  ok   " if ok else "  FAIL ") + label.ljust(46)
          + ("" if ok else f"\n         got  {got!r}\n         want {want!r}"))
    if not ok:
        FAILED.append(label)


def assistant(*blocks):
    return {"type": "assistant", "timestamp": "2026-07-26T09:00:00Z",
            "message": {"content": list(blocks)}}


def text(s):
    return {"type": "text", "text": s}


def use(name, inp, tid="t1"):
    return {"type": "tool_use", "id": tid, "name": name, "input": inp}


def result(tid="t1"):
    return {"type": "user", "timestamp": "2026-07-26T09:00:01Z",
            "message": {"content": [{"type": "tool_result", "tool_use_id": tid}]}}


def transcript(*entries):
    path = os.path.join(TMP, f"t{len(os.listdir(TMP))}.jsonl")
    with open(path, "w") as fh:
        for e in entries:
            fh.write(json.dumps(e) + "\n")
    return path


def prompt(s, **fields):
    e = {"type": "user", "timestamp": "2026-07-26T08:59:00Z",
         "message": {"content": s}}
    e.update(fields)
    return e


def ask_of(*entries):
    return m.read_step(transcript(*entries))[2]


def step_of(*entries):
    return m.read_step(transcript(*entries))[0]


def task_of(*entries):
    return m.read_step(transcript(*entries))[3]


print("\nwhat it is holding out for")
check("the command it wants to run",
      ask_of(assistant(text("Let me check the tests."),
                       use("Bash", {"command": "pytest -x tests/unit"}))),
      "wants to Bash pytest -x tests/unit")
check("the file it wants to edit",
      ask_of(assistant(use("Edit", {"file_path": "/src/parser.rs"}))),
      "wants to Edit parser.rs")
check("a tool already answered is not a request",
      ask_of(assistant(text("Done - the suite is green."),
                       use("Bash", {"command": "pytest"})), result()),
      "Done - the suite is green.")

print("\na question it put to you")
check("the question itself, not the tool name",
      ask_of(assistant(use("AskUserQuestion",
                           {"questions": [{"question": "Squash these commits?"}]}))),
      "asks: Squash these commits?")
check("a malformed question falls back to the tool",
      ask_of(assistant(use("AskUserQuestion", {"questions": []}))),
      "wants to AskUserQuestion")

print("\nfailing those, what it said")
check("the opening claim, not the whole essay",
      ask_of(assistant(text("Pushed six commits and CI is green. "
                            "Then a second paragraph nobody needs on one line."))),
      "Pushed six commits and CI is green.")
check("markdown bold does not leak into the row",
      ask_of(assistant(text("**W5 does not exist** - it was killed last week."))),
      "W5 does not exist - it was killed last week.")
check("a heading is skipped in favour of the sentence under it",
      ask_of(assistant(text("## Cycle report\nEverything ran clean."))),
      "Everything ran clean.")
check("a bullet keeps its text, loses its bullet",
      ask_of(assistant(text("- fixed the parser bug"))), "fixed the parser bug")
check("all furniture is noisy rather than blank",
      ask_of(assistant(text("## Report\n| a | b |\n| - | - |"))),
      "Report a b - -")
check("nothing to say is empty, not a crash", ask_of(assistant(text("   "))), "")
check("no transcript at all", m.read_step(os.path.join(TMP, "nope.jsonl")),
      ("", 0.0, "", "", 0))

print("\na dash is the sentence carrying on, not the end of it")
check("the clause after the dash is the half that says something",
      ask_of(assistant(text("Committed and pushed as 985803cf6 - exactly the "
                            "eight files I named, nothing swept from the "
                            "parallel session. Then a paragraph."))),
      "Committed and pushed as 985803cf6 - exactly the eight files I named, "
      "nothing swept from the parallel session.")
check("an em dash is no different",
      ask_of(assistant(text("Fixed the parser — the lexer was eating "
                            "newlines. And more."))),
      "Fixed the parser — the lexer was eating newlines.")
check("a dash still ends a line long enough to have earned it",
      len(ask_of(assistant(text("word " * 30 + "- and a trailing clause "
                                + "more " * 40)))) <= m.SUMMARY_CHARS, True)
check("a full stop still ends the opening claim",
      ask_of(assistant(text("Pushed six commits and CI is green. "
                            "Then a second paragraph."))),
      "Pushed six commits and CI is green.")

print("\nthe question it signs off with")
check("the closing question wins over the opening claim",
      ask_of(assistant(text("Fixed both tests and the suite is green.\n\n"
                            "Want me to squash these commits?"))),
      "asks: Want me to squash these commits?")
check("a question in the middle is not a sign-off, so no asks:",
      ask_of(assistant(text("Was the lexer wrong? It was.\n\nAll green now."))),
      "Was the lexer wrong? It was.")
check("a question above the options it offers still counts",
      ask_of(assistant(text("Which first?\n- the parser\n- the lexer"))),
      "asks: Which first?")
check("a long question keeps its end, not its run-up",
      ask_of(assistant(text("I have read the whole report and checked it "
                            "twice. Shall I take the second one first?"))),
      "asks: Shall I take the second one first?")
check("a statement is not a question",
      ask_of(assistant(text("All green now."))), "All green now.")

print("\nwhat you asked it for")
check("the last thing you typed",
      task_of(prompt("update me on the ml-lab queue"),
              assistant(text("Done.")))
      , "update me on the ml-lab queue")
check("the newest prompt, not the first",
      task_of(prompt("first thing"), assistant(text("ok")),
              prompt("read V16 and reconcile it against S0")),
      "read V16 and reconcile it against S0")
check("a slash command expansion is not something you typed",
      task_of(prompt("yes commit it, and run S0 first"),
              prompt("# commit\nCreate a git commit with attribution.",
                     isMeta=True)),
      "yes commit it, and run S0 first")
check("a subagent brief is not something you typed",
      task_of(prompt("the real ask"),
              prompt("go and search the codebase", isSidechain=True)),
      "the real ask")
check("a notification is not something you typed",
      task_of(prompt("the real ask"), prompt("<task-notification>done</task>")),
      "the real ask")
check("a tool result is not something you typed",
      task_of(prompt("the real ask"), result()), "the real ask")
check("no prompt yet is empty, not a crash",
      task_of(assistant(text("hello"))), "")

print("\na prompt has to say something")
check("yes is not what the head is for",
      task_of(prompt("reconcile V16 against S0"), assistant(text("ok")),
              prompt("yes")),
      "reconcile V16 against S0")
for said in ("y", "go ahead", "do it", "ok ship it", "sure, continue",
             "yes please", "sounds good", "next", "no"):
    check(f"{said!r} is not either",
          task_of(prompt("the real ask"), prompt(said)), "the real ask")
check("a short prompt that says something survives",
      task_of(prompt("the real ask"), prompt("run V14")), "run V14")
check("so does a short one made of ordinary words",
      task_of(prompt("the real ask"), prompt("commit it and push")),
      "commit it and push")
check("nothing but confirmations is better than nothing",
      task_of(prompt("yes")), "yes")
long_ask = "please " + "reconcile the registry " * 20
check("a long prompt is cut to its own budget",
      len(task_of(prompt(long_ask))) <= m.TASK_CHARS, True)

print("\nlong things get cut, and say so")
long_claim = "The refactor touched " + "many files " * 20
check("cut to one line with an ellipsis", ask_of(assistant(text(long_claim)))[-1], "…")
check("and stays within the budget",
      len(ask_of(assistant(text(long_claim)))) <= m.SUMMARY_CHARS, True)

print("\nthe step line gets the same treatment")
check("a heading does not become the step",
      step_of(assistant(text("## Cycle report\nEverything ran clean."))),
      "» Everything ran clean.")
check("a pending tool still wins the step line",
      step_of(assistant(text("checking"), use("Bash", {"command": "ls"}))),
      "⚙ Bash ls")

print("\nthe summary sits under the row, wrapped")
check("short text is one line",
      m.wrap_body("all clean", 60), ["      all clean"])
check("nothing to say takes no line", m.wrap_body("", 60), [])
check("wrapping happens between words",
      [l.strip() for l in m.wrap_body("one two three four five six", 26)],
      ["one two three four", "five six"])
check("every line is indented",
      all(l.startswith(" " * 6) for l in m.wrap_body("a b c d e f g h", 24)), True)
check("no line runs past the pane",
      max(len(l) for l in m.wrap_body("x " * 60, 40)) <= 40, True)
long = " ".join(f"word{i}" for i in range(60))
check("a long body stops at the cap", len(m.wrap_body(long, 40)), m.SUB_LINES)
check("and says it was cut", m.wrap_body(long, 40)[-1].endswith("…"), True)
check("a body that just fits is not marked cut",
      m.wrap_body("one two three four", 30)[-1].endswith("…"), False)
check("an unbreakable word is cut, not overflowed",
      max(len(l) for l in m.wrap_body("/" + "x" * 200, 40)) <= 40, True)

print("\nthe prompt line above it")
attn = {"task": "read V16 and reconcile it against S0", "attention": True}
quiet = {"task": "read V16 and reconcile it against S0", "attention": False}
m.TASKLINE = "attention"
check("a row that wants you shows what it is for",
      m.wrap_task(attn, 70), ["      · read V16 and reconcile it against S0"])
check("a row that does not want you stays one line high",
      m.wrap_task(quiet, 70), [])
check("no prompt takes no line", m.wrap_task({"attention": True}, 70), [])
m.TASKLINE = "all"
check("all means all", len(m.wrap_task(quiet, 70)), 1)
m.TASKLINE = "off"
check("off means off", m.wrap_task(attn, 70), [])
m.TASKLINE = "attention"
check("one line however long the prompt",
      len(m.wrap_task({"task": "reconcile " * 40, "attention": True}, 70)), 1)
check("and it never runs past the pane",
      len(m.wrap_task({"task": "reconcile " * 40, "attention": True}, 70)[0])
      <= 70, True)

print("\nall pass" if not FAILED else f"\n{len(FAILED)} FAILED")
raise SystemExit(1 if FAILED else 0)
