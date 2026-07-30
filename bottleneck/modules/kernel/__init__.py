"""kernel - launch every head through a system-prompt rewriting proxy.

The thing a hook cannot reach. Replacing claude's stock identity block means
changing the request, and no hook sees the request: by the time one runs, it has
already been made. It only happens at the HTTP boundary, which means a process
between claude and the API - and the module's whole job is to answer the one
question the core asks before it opens a pane, with the address of that process.

Two of them can do the work, and which is right depends on how far you have
taken it:

    builtin   wrap.py, in this directory. Two hundred lines, standard library
              only, nothing to install. Rewrites the system prompt and meters
              the answer, and does no more than that. This is what makes the
              module a demonstration rather than a pointer at somebody else's
              program.

    proxy     an external cc-kernel-proxy checkout, which does the same thing
              properly: token-optimisation passes, configurable replacement
              rules, its own test suite. Used when it is there.

`BOTTLENECK_KERNEL_BACKEND` picks one; the default is whichever is available,
preferring the external checkout, because a machine that has one has it for a
reason. Neither is ever allowed to cost you a head: if the chosen backend will
not come up, the head launches straight to the API without the rewrite.

    env      ANTHROPIC_BASE_URL, and the backend started if it was down
    status   which backend is up, on the tmux status line
    commands `bottleneck kernel` - what is running and what it has cost
"""
import os

from . import proxy
from . import wrap
from .cli import kernel_cmd


USAGE = """Heads run through a system-prompt proxy (`bottleneck kernel`):
  bottleneck kernel        which backend is up, what it injects, what it cost
  bottleneck kernel start  bring it up now rather than at the next head
  bottleneck kernel show   the system prompt a head would actually receive"""


def backend():
    """"builtin" or "proxy" - which one this machine should be using."""
    said = os.environ.get("BOTTLENECK_KERNEL_BACKEND", "").strip().lower()
    if said in ("builtin", "wrap"):
        return "builtin"
    if said in ("proxy", "external", "cc-kernel-proxy"):
        return "proxy"
    return "proxy" if proxy.PRESENT else "builtin"


def chosen():
    """The module behind the current backend. Both answer up/start/base_url."""
    return proxy if backend() == "proxy" else wrap


def env(spec):
    """What a head about to be opened should be launched with.

    Started here rather than with the dashboard because this is the moment it
    is needed, and a dashboard left up all day should not hold a proxy open for
    heads nobody is opening.
    """
    who = chosen()
    if not who.start():
        return {}
    return {"ANTHROPIC_BASE_URL": who.base_url()}


def status(heads):
    # The presence check comes first, and is not decoration: `up` is a probe of
    # a port, and a port can be answered by something that is not the backend
    # we were asked for. Without this, asking for a checkout that is not here
    # reports the other backend's proxy as though it were ours.
    which = backend()
    if which == "proxy" and not proxy.PRESENT:
        return ""
    return ("kernel" if which == "proxy" else "kernel*") if chosen().up() else ""


MODULE = {
    "summary": "launch heads through a system-prompt proxy (built in, or an "
               "external cc-kernel-proxy)",
    "commands": {"kernel": kernel_cmd},
    "usage": USAGE,
    "env": env,
    "status": status,
}
