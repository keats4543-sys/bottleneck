"""kernel - launch every head through the system-prompt proxy.

The thing a hook cannot do. cc-kernel-proxy sits between claude and the API and
rewrites the system prompt on the way past - which means replacing the stock
identity block, not adding to it, and that is only reachable at the HTTP
boundary. By the time any hook runs the request has been made.

So this module does not intercept anything. It answers one question the core
asks before it opens a pane - what should this head be launched with - with the
address of a proxy it has made sure is running. claude honours
ANTHROPIC_BASE_URL natively; everything else is the proxy's own business.

    env      ANTHROPIC_BASE_URL, and the proxy started if it was not
    status   whether it is up, on the tmux status line
    commands `bottleneck kernel` - what is running and what it has cost

The proxy is a separate checkout and stays one. If it is not there, this module
is inert and says so.
"""
from . import proxy
from .cli import kernel_cmd


USAGE = """Heads run through the system-prompt proxy (`bottleneck kernel`):
  bottleneck kernel        is it up, what it injects, what it has cost
  bottleneck kernel start  bring it up now rather than at the next head"""


def env(spec):
    """What a head about to be opened should be launched with.

    Starting the proxy here rather than with the dashboard is deliberate: this
    is the moment it is actually needed, it is the same check its own launcher
    makes, and a dashboard you leave running all day should not be holding a
    proxy up for heads you are not opening.

    A proxy that will not come up returns nothing at all, and the head launches
    straight to the API - the rewrite is worth having and never worth failing
    to open a head for.
    """
    if not proxy.PRESENT or not proxy.start():
        return {}
    return {"ANTHROPIC_BASE_URL": proxy.base_url()}


def status(heads):
    if not proxy.PRESENT:
        return ""
    return "kernel" if proxy.up() else ""


MODULE = {
    "summary": "launch heads through the system-prompt proxy (cc-kernel-proxy)",
    "commands": {"kernel": kernel_cmd},
    "usage": USAGE,
    "env": env,
    "status": status,
}
