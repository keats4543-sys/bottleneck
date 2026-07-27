"""Getting back out of a question you did not mean to ask for.

Pressing n asked three questions in a row and gave you no way to leave: Esc did
nothing, because the line was read in cooked mode and the terminal holds a line
until Enter - so the key that means "no, take me back" could only arrive after
you had committed the line it was meant to abandon.

These tests hold Esc to meaning one step back, and one more step back out of
the flow entirely.
"""
import io
import os
import sys

from harness import bn as m

FAILED = []


def check(label, got, want):
    ok = got == want
    print(("  ok   " if ok else "  FAIL ") + label.ljust(46)
          + ("" if ok else f"\n         got  {got!r}\n         want {want!r}"))
    if not ok:
        FAILED.append(label)


class Terminal:
    """Keystrokes the way a terminal sends them: whole writes, not bytes."""

    def __init__(self, *writes):
        self.writes = list(writes)

    def isatty(self):
        return True

    def fileno(self):
        return 0


def typing(*writes):
    """Run ask() against a stand-in terminal and give back what it returned."""
    term = Terminal(*writes)
    real_stdin, real_stdout, real_read = sys.stdin, sys.stdout, os.read
    sys.stdin = term
    sys.stdout = io.StringIO()
    os.read = lambda fd, n: (term.writes.pop(0).encode() if term.writes else b"")
    try:
        return m.ask("prompt: ")
    finally:
        sys.stdin, sys.stdout, os.read = real_stdin, real_stdout, real_read


print("\nan answer is an answer")
check("a line and Enter", typing("grains\r"), "grains")
check("arriving one key at a time", typing("g", "r", "a", "i", "n", "s", "\n"),
      "grains")
check("Enter on its own is an empty answer, not a refusal", typing("\r"), "")
check("surrounding space is not part of it", typing("  grains  \r"), "grains")

print("\nEsc is not an answer")
check("Esc leaves", typing("\x1b") is m.BACK, True)
check("Esc mid-line leaves too", typing("grai", "\x1b") is m.BACK, True)
check("Ctrl-C at a prompt means the same", typing("\x03") is m.BACK, True)
check("a terminal that goes away leaves", typing() is m.BACK, True)

print("\nediting the line")
check("backspace rubs out", typing("grainz\x7f", "s\r"), "grains")
check("backspace on an empty line is harmless", typing("\x7f\x7fok\r"), "ok")
check("Ctrl-U starts again", typing("wrong\x15right\r"), "right")

print("\nkeys that are not text")
check("an arrow is swallowed, not typed",
      typing("gr\x1b[Aains\r"), "grains")
check("an arrow does not leave either", typing("\x1b[B", "ok\r"), "ok")
check("a tab is not part of the name", typing("gr\tains\r"), "grains")

print("\nwalking a flow")


def flow_of(script):
    """Run a three-step flow, each step answering from `script` in turn."""
    seen = []

    def step(n):
        def run(answers):
            seen.append((n, list(answers)))
            return script[n].pop(0)
        return run

    return m.flow(step(0), step(1), step(2)), seen


got, seen = flow_of({0: ["a"], 1: ["b"], 2: ["c"]})
check("straight through", got, ["a", "b", "c"])
check("each step sees the answers before it",
      [a for _, a in seen], [[], ["a"], ["a", "b"]])

got, seen = flow_of({0: ["a", "A"], 1: [m.BACK, "b"], 2: ["c"]})
check("Esc goes back one question", got, ["A", "b", "c"])
check("and the question it went back to is asked again",
      [n for n, _ in seen], [0, 1, 0, 1, 2])

got, _ = flow_of({0: [m.BACK], 1: [], 2: []})
check("Esc on the first question leaves the flow", got, None)

got, seen = flow_of({0: ["a", m.BACK], 1: [m.BACK], 2: []})
check("and backing out through the first one leaves too", got, None)
check("having asked it again on the way", [n for n, _ in seen], [0, 1, 0])

got, seen = flow_of({0: ["a"], 1: [None, "b"], 2: ["c"]})
check("a step that will not take the answer stays put", got, ["a", "b", "c"])
check("without asking anything else",
      [n for n, _ in seen], [0, 1, 1, 2])

got, _ = flow_of({0: ["a"], 1: [False], 2: ["c"]})
check("a false answer is still an answer", got, ["a", False, "c"])

print("\nan answer given, backed over, and given again")
got, seen = flow_of({0: ["first", "second"], 1: [m.BACK, "b"], 2: ["c"]})
check("the stale answer is gone from what the next step sees",
      [a for _, a in seen][-1], ["second", "b"])

print("\nall pass" if not FAILED else f"\n{len(FAILED)} FAILED")
raise SystemExit(1 if FAILED else 0)
