"""share - what the heads in a group tell each other.

The first module, and the shape of one. Everything the feature is lives in this
directory: the boards themselves (board.py), the claude hook that writes to them
and reads from them (hook.py), the commands it adds (cli.py), the dashboard's
way of noticing file changes without a hook (harvest.py), its own tests, and its
own README. Nothing outside this directory knows it exists.

What it plugs into, and where each one is answered:

    commands   `bottleneck share`, `goal`, `note`            cli.py
    usage      the lines they get in `bottleneck --help`     below
    pass       reading transcripts on each dashboard refresh harvest.py
    mark       what is queued for a head, on its row         screen.py
    lines      its goal, under its name                      screen.py
    group      what the group last touched, under its head   screen.py
    status     one count for the tmux bar                    screen.py
    hook       a claude hook, wired to the events it names   hook.py
    state      ~/.bottleneck/share, made at install
"""
import os

from . import board
from . import harvest
from . import screen
from .cli import share_cmd


USAGE = """Heads in a group tell each other what they are doing (`bottleneck share`):
  bottleneck share         the group boards: goals, queues, who changed what
  bottleneck goal [<n>] <s>  say what a head is for - its siblings are told
  bottleneck note <s>      put a line on this head's group board
  bottleneck share clear   forget every board (the heads keep their groups)"""


# Which claude events the hook wants, worked out rather than listed - the
# module decides its own wiring, and this one's depends on how it is set up.
#
# The last entry is the expensive one: a spawn per tool call rather than per
# turn. It is asked for only when the hook is the thing watching the working
# tree. With the dashboard doing that instead, the hook is left off it
# entirely, which is the difference the setting is there to make.
def events():
    want = [("SessionStart", None),
            ("UserPromptSubmit", None),
            ("SessionEnd", None),
            ("PreToolUse", "Write|Edit|MultiEdit|NotebookEdit|Update")]
    if board.SOURCE == "hook":
        want.append(("PostToolUse", "Write|Edit|MultiEdit|NotebookEdit|Update|"
                                    "Read|NotebookRead|Bash"))
    return want


MODULE = {
    "summary": "group boards: goals, file changes and a queue between siblings",
    "commands": {"share": share_cmd, "goal": share_cmd, "note": share_cmd},
    "usage": USAGE,
    "pass": harvest.harvest,
    "mark": screen.mark,
    "lines": screen.lines,
    "group": screen.group,
    "status": screen.status,
    "hook": "hook.py",
    "events": events,
    "state": ["share"],
}


# Where the hook is, for anything that wants to run it directly.
HOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hook.py")
