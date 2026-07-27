"""How a head gets launched when `claude` is not a word tmux knows.

tmux runs a pane's command through a shell that is neither interactive nor a
login shell. An alias does not exist in one, a shell function does not exist in
one, and on Debian and WSL a PATH set in ~/.bashrc does not exist in one either
- the file returns on its first line when the shell is not interactive. So the
program you just typed by hand is "command not found" in the pane, and these
tests hold the launch line to something that works anyway.
"""
import os
import shlex
import subprocess

from harness import bn as m

FAILED = []


def check(label, got, want):
    ok = got == want
    print(("  ok   " if ok else "  FAIL ") + label.ljust(46)
          + ("" if ok else f"\n         got  {got!r}\n         want {want!r}"))
    if not ok:
        FAILED.append(label)


class Shell:
    """A stand-in for asking your shell, and a note of whether we bothered."""

    def __init__(self, answer=""):
        self.answer, self.asked = answer, 0

    def __call__(self, script, timeout=8):
        self.asked += 1
        return self.answer


def resolve(on_path=None, shell_says="", knob="", args=None):
    """find_claude() with the machine stubbed out from under it.

    Returns what it resolved, the stub shell, and - because the stubs have to
    still be standing when it is asked - what claude_cmd() would launch.
    """
    m.config.CLAUDE = knob
    m._LAUNCH = None
    real_which, real_ask = m.shutil.which, m.ask_shell
    shell = Shell(shell_says)
    m.shutil.which = lambda name: on_path
    m.ask_shell = shell
    try:
        found = m.find_claude()
        # What one resolution cost. claude_cmd() below resolves again, having
        # its own cache to fill, and that second ask is not the thing under
        # test - "resolved once, not once per head" is, further down.
        asked = shell.asked
        launch = m.claude_cmd(args or "--name x")
        shell.asked = asked
        return found, shell, launch
    finally:
        m.shutil.which, m.ask_shell = real_which, real_ask
        m.config.CLAUDE = ""
        m._LAUNCH = None
        m._finder = None


print("\nthe ordinary case costs nothing")
got, shell, _ = resolve(on_path="/usr/local/bin/claude")
check("a claude on our own PATH is used as it stands", got,
      "/usr/local/bin/claude")
check("and no shell is started to confirm it", shell.asked, 0)

print("\nwhen only your shell knows")
got, shell, _ = resolve(on_path=None, shell_says="/home/me/.local/bin/claude")
check("the path your shell names", got, "/home/me/.local/bin/claude")
check("asked once", shell.asked, 1)

print("\nwhen claude is an alias or a function")
got, _, _ = resolve(on_path=None, shell_says="alias claude='claude --verbose'")
check("the launch goes through your shell, login and interactive",
      got.startswith(f"{shlex.quote(m.config.SHELL)} -lic "), True)
check("and the shell is handed claude, not the alias body",
      got.endswith(" claude"), True)

print("\nwhen nobody knows")
got, _, launch = resolve(on_path=None, shell_says="")
check("no launch line at all", got, "")
check("and the caller gets an empty command", launch, "")

print("\nthe knob wins outright")
got, shell, _ = resolve(on_path="/usr/local/bin/claude", knob="/opt/claude/bin/claude")
check("BOTTLENECK_CLAUDE is used as given", got, "/opt/claude/bin/claude")
check("and nothing is searched or asked", shell.asked, 0)

print("\nthe launch line carries the arguments")
m._LAUNCH = None
m.config.CLAUDE = "/opt/claude"
check("appended to whatever we resolved",
      m.claude_cmd('--name "grains" --dangerously-skip-permissions'),
      '/opt/claude --name "grains" --dangerously-skip-permissions')
check("resolved once, not once per head", m.claude_cmd("--continue"),
      "/opt/claude --continue")
m.config.CLAUDE = ""
m._LAUNCH = None

print("\nreading the answer back out of a noisy shell")
real_run = m.subprocess.run


class Ran:
    def __init__(self, out):
        self.stdout, self.stderr, self.returncode = out, "", 0


def shell_printing(out):
    m.subprocess.run = lambda *a, **k: Ran(out)
    try:
        return m.ask_shell('printf "__bn__%s\\n" "$(command -v claude)"')
    finally:
        m.subprocess.run = real_run


check("a greeting in your rc file is not the answer",
      shell_printing("Welcome back!\nyou have mail\n__bn__/usr/bin/claude\n"),
      "/usr/bin/claude")
check("nothing marked is no answer at all",
      shell_printing("Welcome back!\n"), "")
check("a shell that cannot be started is no answer either",
      (lambda: [m.subprocess.__setattr__("run", boom := (lambda *a, **k: (_ for _ in ()).throw(OSError()))),
                m.ask_shell("x")][1])(), "")
m.subprocess.run = real_run

print("\nthe pane is not opened on an empty command")
said = []
real_say, real_dash = m.tmux_say, m.dash_pane
m.tmux_say = lambda msg: said.append(msg)
m.dash_pane = lambda: (_ for _ in ()).throw(
    AssertionError("looked for a pane with nothing to run in it"))
try:
    check("spawn refuses", m.spawn("", "/tmp", "x", []), False)
    check("and says which knob fixes it",
          "BOTTLENECK_CLAUDE" in " ".join(said), True)
finally:
    m.tmux_say, m.dash_pane = real_say, real_dash

print("\nthe shell script resolves it the same way")
here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
new = os.path.join(here, "bin", "bottleneck-new")
check("bottleneck-new parses",
      subprocess.run(["/bin/bash", "-n", new]).returncode, 0)
# A PATH without claude on it and a shell that answers nothing: the two halves
# of the WSL failure at once. BOTTLENECK_STATE points nowhere so the real
# config file cannot quietly supply the answer.
r = subprocess.run(["/bin/bash", new, "x"], capture_output=True, text=True,
                   env={"PATH": "/usr/bin:/bin", "SHELL": "/bin/false",
                        "BOTTLENECK_STATE": "/nonexistent",
                        "HOME": os.environ["HOME"]})
check("and says so rather than opening a dead window",
      "cannot find claude" in r.stderr, True)
check("naming the knob", "BOTTLENECK_CLAUDE" in r.stderr, True)


print("\nthe shell is asked before you need the answer, not while you wait")
# When claude is not on our own PATH, resolving it takes a login interactive
# shell - your whole ~/.bashrc, 6.1 seconds on the machine this was written on.
# It used to be spent at the moment you pressed n, with the cursor sat in a
# prompt that had stopped taking keys. Now the dashboard starts on it as it
# starts, and n reads the answer off it.
import threading

warm_which, warm_ask = m.shutil.which, m.ask_shell
m._LAUNCH = None
m._finder = None
looking, carry_on, asks = threading.Event(), threading.Event(), []


def slow_shell(script, timeout=20):
    asks.append(script)
    looking.set()
    carry_on.wait(5)
    return "/home/me/.local/bin/claude"


m.shutil.which = lambda name: None
m.ask_shell = slow_shell
try:
    m.warm_claude()
    check("warming goes looking straight away", looking.wait(5), True)
    m.warm_claude()
    check("and warming twice is still one shell", len(asks), 1)
    carry_on.set()
    check("the launch line waits on the answer already coming",
          m.claude_cmd("--name x"), "/home/me/.local/bin/claude --name x")
    check("rather than starting a shell of its own", len(asks), 1)
finally:
    m.shutil.which, m.ask_shell = warm_which, warm_ask
    m._LAUNCH = None
    m._finder = None

print("\nnobody knowing is an answer too, and is not asked twice")
asked = []
m.shutil.which = lambda name: None
m.ask_shell = lambda script, timeout=20: asked.append(script) or ""
try:
    m.warm_claude()
    m._finder.join()
    check("the press gets nothing to launch", m.claude_cmd("--name x"), "")
    check("and does not go asking again", (m.claude_cmd("-c"), len(asked)),
          ("", 1))
finally:
    m.shutil.which, m.ask_shell = warm_which, warm_ask
    m._LAUNCH = None
    m._finder = None

print("\na shell that never answers")
# A timeout is not a delay, it is a wrong answer: it reads as "your shell does
# not know either", and n then reports that claude cannot be found at all on a
# machine where typing claude works. Hence twenty seconds rather than eight - a
# ~/.bashrc that sources conda and nvm took 6.1 of them here.


def timing_out(*a, **k):
    raise m.subprocess.TimeoutExpired(cmd="bash", timeout=20)


m.subprocess.run = timing_out
try:
    check("reads as no answer, not as a crash", m.ask_shell("x"), "")
finally:
    m.subprocess.run = real_run

print("\nall pass" if not FAILED else f"\n{len(FAILED)} FAILED")
raise SystemExit(1 if FAILED else 0)
