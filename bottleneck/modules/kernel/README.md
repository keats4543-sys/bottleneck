# kernel — heads run through the system-prompt proxy

Claude Code hooks can add context. They cannot change what the session *is* —
by the time any hook runs, the request has been made and the stock identity
block is already in it. Replacing that block only happens at the HTTP boundary.

[cc-kernel-proxy](../../../../cc-kernel-proxy) already does the hard half: it
sits between `claude` and `api.anthropic.com`, rewrites the system prompt,
strips boilerplate without breaking prompt caching, and meters every request.
This module is the other half — making sure the heads bottleneck opens are
pointed at it.

```
bottleneck  ──spawns──▶  head (ANTHROPIC_BASE_URL=127.0.0.1:8790)
                              │
                              └──▶ cc-kernel-proxy ──▶ api.anthropic.com
```

It answers one question the core asks before it opens a pane — what should this
head be launched with — and the answer is the proxy's address, with the proxy
started if it was not already up. `claude` honours `ANTHROPIC_BASE_URL`
natively, so nothing is patched and nothing is wrapped.

## What it plugs into

| point | what it does |
|---|---|
| `env` | `ANTHROPIC_BASE_URL`, and the proxy started if it was down |
| `status` | `kernel` on the tmux status line while it is up |
| `commands` | `bottleneck kernel` — what is running, and what it has cost |

## Commands

```
bottleneck kernel        up or down, what it injects, what it has metered
bottleneck kernel start  bring it up now rather than at the next head
```

## Settings

| | |
|---|---|
| `BOTTLENECK_KERNEL_ROOT` | the proxy checkout (default `~/cc-kernel-proxy`) |
| `BOTTLENECK_KERNEL_AUTOSTART` | `0` to never start it, only use it if it is up |
| `BOTTLENECK_KERNEL_WAIT` | seconds to wait for a starting proxy (default 3) |

## What it will not do

**It never costs you a head.** A proxy that is missing, down, or slow to start
means the head launches straight to the API without the rewrite. Opening a head
is the thing you asked for; the rewrite is worth having and never worth failing
that for. Measured at 529ms to open a head that had to start the proxy, and
1ms once it is up.

**It starts the proxy at the head, not at the dashboard.** That is the moment
it is actually needed, it is the same check the proxy's own launcher makes, and
a dashboard left up all day should not be holding a proxy open for heads you
are not opening.

**The proxy stays its own checkout.** Its kernels, its passes and its config
are not this module's business — see its own README.
