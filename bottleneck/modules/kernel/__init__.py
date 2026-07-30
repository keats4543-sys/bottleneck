"""kernel - launch every head through a rewriting proxy of our own.

The thing a hook cannot reach. Changing what a session *is* means changing the
request, and no hook sees the request: by the time one runs, it has already been
made. It only happens at the HTTP boundary, which means a process between claude
and the API - and the module's whole job is to answer the one question the core
asks before it opens a pane, with the address of that process.

That process is [wrap.py](wrap.py), here, standard library only. It used to be
one of two, the other being an external cc-kernel-proxy checkout, and that
arrangement is retired: heads depended every day on a program that was not in
this repository, not version controlled anywhere, and carrying its own copy of
the rules this module is held to. Its passes are stages here now.

What the wrapper does to a request is not decided by the wrapper. See
[stages](stages/__init__.py) - identity replacement, truncation, memory, or
something of yours from a directory of your own.

It is never allowed to cost you a head: if the wrapper will not come up, the
head launches straight to the API without the rewrite.

    env      ANTHROPIC_BASE_URL, and the wrapper started if it was down
    status   `kernel` while it is up, `kernel!` when a stage cannot do its job
    commands `bottleneck kernel` - what is running and what it has cost
"""
import time

from . import wrap
from .cli import kernel_cmd


USAGE = """Heads run through a rewriting proxy (`bottleneck kernel`):
  bottleneck kernel        up or down, what it runs, what it cost
  bottleneck kernel start  bring it up now rather than at the next head
  bottleneck kernel show   what a head would actually be sent, stage by stage
  bottleneck kernel stages what can run, what does run, and in what order"""


def env(spec):
    """What a head about to be opened should be launched with.

    Started here rather than with the dashboard because this is the moment it is
    needed, and a dashboard left up all day should not hold a proxy open for
    heads nobody is opening.
    """
    if not wrap.start():
        return {}
    return {"ANTHROPIC_BASE_URL": wrap.base_url()}


# Two caches, two lifetimes, both for the same reason: every one of these is
# asked on every redraw of the dashboard, and none of the answers changes at
# anything like that rate. Probing a port costs 3.7ms of round trip per frame
# uncached - small, but it is the core's loop being spent on a module's
# question, which is exactly what a module is not entitled to do.
_UP = [0.0, False]
_GAP = [0.0, []]


def forget():
    """Drop both caches. For tests, and for a dashboard reloading itself."""
    _UP[0] = _GAP[0] = 0.0


def up():
    """Is the wrapper answering. Cached for a couple of seconds.

    Short, because this is what decides whether a head about to open gets the
    rewrite, and being two seconds out of date there costs one head its stages.
    """
    now = time.time()
    if now >= _UP[0]:
        _UP[1] = wrap.up()
        _UP[0] = now + 2
    return _UP[1]


def gap():
    """What the stages are failing to do. [] when clean.

    Cached, because this reads files and the status line asks every couple of
    seconds. Thirty seconds late is soon enough for a fault that only changes
    when Claude Code is upgraded or a stage is edited.
    """
    now = time.time()
    if now < _GAP[0]:
        return _GAP[1]
    try:
        _GAP[1] = wrap.gap()
    except Exception:                   # noqa: BLE001 - never cost a redraw
        _GAP[1] = []
    _GAP[0] = now + 30
    return _GAP[1]


def status(heads):
    if not up():
        return ""
    # The whole point of the mark: a rewrite that stopped matching looks exactly
    # like one that is working, right up until you read the prompt.
    return "kernel!" if gap() else "kernel"


MODULE = {
    "summary": "launch heads through a rewriting proxy, with what it does to a "
               "request configured as stages",
    "commands": {"kernel": kernel_cmd},
    "usage": USAGE,
    "env": env,
    "status": status,
}
