"""bottleneck - one tmux view over every local Claude Code head.

The parts, in the order they depend on each other:

    config      paths, tunables, the state table
    store       the files under ~/.bottleneck: flags, groups, holds, numbers
    procs       what is running, from /proc and from Claude's own list
    tmuxio      talking to tmux, and which pane is the dashboard
    transcript  reading a head's transcript: what it does, what it wants
    catalog     names, and the book of sessions we have seen
    heads       one list of every head, with everything the display needs
    panes       moving heads in and out of the pane beside the list
    ui          the list on the screen and the keys that drive it
    cli         every command, and the dispatch that picks one

Nothing above imports anything below it, which is the only rule here.
"""
from .config import VERSION

__all__ = ["VERSION", "run"]


def run(argv=None):
    from .cli import run as _run
    return _run(argv)
