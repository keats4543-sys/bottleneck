# kernel — heads run through the system-prompt proxy

Claude Code hooks can add context. They cannot change what the session *is* —
by the time any hook runs, the request has been made and the stock identity
block is already in it. Replacing that block only happens at the HTTP boundary.

There are two of them here, and which is right depends on how far you have
taken it.

**`builtin`** — [`wrap.py`](wrap.py), in this directory. About two hundred lines,
standard library only, nothing to install. It rewrites the system prompt, streams
the answer back, and meters the tokens. This is what makes the module a
demonstration of the idea rather than a pointer at somebody else's program.

**`proxy`** — an external [cc-kernel-proxy](../../../../cc-kernel-proxy)
checkout, which does the same job properly: configurable replacement rules,
token-optimisation passes, its own test suite.

`BOTTLENECK_KERNEL_BACKEND` picks one. The default is whichever is available,
preferring the external checkout — a machine that has one has it for a reason.

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
| `env` | `ANTHROPIC_BASE_URL`, and the backend started if it was down |
| `status` | `kernel` on the tmux status line while it is up |
| `commands` | `bottleneck kernel` — what is running, and what it has cost |

## The three rules the rewrite will not break

All of them are about prompt caching, which is a byte-prefix match: anything
that alters the prefix differently between two requests in a session turns a
cache read into a cache write and costs money instead of saving it.

1. **The shape of the system field is never touched.** Same blocks, same order,
   each keeping its own `cache_control` exactly where claude put it. Only the
   text inside a block changes.
2. **Every rewrite is a pure function of its input.** No clock, no counter, no
   request id — same bytes in, same bytes out, on the first request and the
   four hundredth.
3. **A block is never left empty.** Claude Code sends parts of its identity as
   blocks of their own, so excising one outright leaves `""`, and the API
   rejects that with `400 system.1: cache_control cannot be set for empty text
   blocks` — every turn of every session failing, with the identity swap
   working perfectly. The kernel goes into the block that was emptied instead,
   which is where the identity it replaces used to be anyway.

Rules 1 and 3 were learned from a live head, in that order: the 400 came first,
and the first fix for it dropped the empty block and moved a caching breakpoint.

## Commands

```
bottleneck kernel        up or down, what it injects, what it has metered
bottleneck kernel start  bring it up now rather than at the next head
```

## Settings

| | |
|---|---|
| `BOTTLENECK_KERNEL_BACKEND` | `builtin` or `proxy`; default prefers the checkout |
| `BOTTLENECK_KERNEL_ROOT` | the proxy checkout (default `~/cc-kernel-proxy`) |
| `BOTTLENECK_KERNEL_PORT` | the builtin wrapper's port (default 8791) |
| `BOTTLENECK_KERNEL_FILE` | the identity text it injects (default `kernel.md`) |
| `BOTTLENECK_KERNEL_AUTOSTART` | `0` to never start it, only use it if it is up |
| `BOTTLENECK_KERNEL_WAIT` | seconds to wait for a starting proxy (default 3) |

## What it will not do

**It never costs you a head.** A proxy that is missing, down, or slow to start
means the head launches straight to the API without the rewrite. Opening a head
is the thing you asked for; the rewrite is worth having and never worth failing
that for. Measured at 529ms to open a head that had to start the proxy, and
1ms once it is up.

**`bottleneck kernel show` prints the prompt a head would actually get**, run
through the real rewrite against claude's real opening blocks — so the claim
that the identity is replaced rather than argued with is checkable rather than
asserted.

**It starts the backend at the head, not at the dashboard.** That is the moment
it is actually needed, it is the same check the proxy's own launcher makes, and
a dashboard left up all day should not be holding a proxy open for heads you
are not opening.

**The proxy stays its own checkout.** Its kernels, its passes and its config
are not this module's business — see its own README.
