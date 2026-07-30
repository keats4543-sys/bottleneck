# kernel — heads run through a rewriting proxy

Claude Code hooks can add context. They cannot change what the session *is* —
by the time any hook runs, the request has been made and the stock identity
block is already in it. Changing the request only happens at the HTTP boundary,
which means a process between claude and the API.

Two parts, and they are worth keeping apart: a **backend** is the hop, and a
**stage** is something done to the request while it is there. The backend knows
nothing about identities or memory; the stages know nothing about HTTP.

There are two backends here, and which is right depends on how far you have
taken it.

**`builtin`** — [`wrap.py`](wrap.py), in this directory. Standard library only,
nothing to install. It forwards the request, runs whatever stages are configured,
streams the answer back and meters the tokens. This is what makes the module a
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
| `status` | `kernel` on the tmux status line while it is up, `kernel!` when a stage says it could not do its job |
| `commands` | `bottleneck kernel` — what is running, and what it has cost |

## Stages — what actually happens to a request

Neither backend decides what a rewrite *is*. A **stage** is one thing done to a
request on its way past, and [`stages.json`](stages.json) says which run and in
what order. The identity rewrite is `stages/identity.py`, listed there like
anything else — **delete the line and it stops happening.** That is the whole
test of whether this is an interface or a decoration.

```
bottleneck kernel stages
     identity   prefix  claude's stock opening sentences out, kernel.md in their place
  !! memory     tail    recall a memory file into the newest user turn
       not run
```

A stage of your own needs nothing of ours — point
`BOTTLENECK_KERNEL_STAGES_DIR` at a directory and it is found first, so a file
named `identity.py` there replaces ours rather than fighting it:

```python
STAGE = {
    "summary": "one line, for `bottleneck kernel stages`",
    "writes":  "prefix",              # or "tail"
    "apply":   lambda system, ctx: {"judged": True},
}
```

### Two classes, and why the split is not cosmetic

| | is handed | must be | for |
|---|---|---|---|
| `prefix` | the system blocks | pure, shape-preserving | identity, stripping, deterministic truncation |
| `tail` | the newest message | nothing but finished | memory recall, a compaction model, anything that calls out |

The split is enforced **by what each kind of stage is handed** — a prefix stage
never receives the messages, a tail stage never receives the system field — so a
stage cannot exceed its class by accident or by trying.

It exists because caching is a byte-prefix match. A prefix stage's output is
resent on every request of the session, so any variation turns a cache read into
a cache write; a tail stage writes after the last breakpoint, where it is
nobody's prefix, and is therefore free to be slow, impure and fallible. That is
where a model call belongs.

### What a stage cannot cost you

A stage that raises, changes the shape of the system field, or turns out not to
be a pure function of its input is **put down and undone** — the body is
restored to what it was, the request goes on without that stage, and the reason
shows up in `bottleneck kernel` and on the status line. It was a thing you added
to a session and is never a reason to lose one.

Purity is checked rather than trusted: the first real body a prefix stage sees,
it runs twice, and the two results are compared byte for byte.

## The three rules a prefix stage is held to

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
Rules 1 and 2 are now enforced by the registry rather than remembered by whoever
writes a stage, which is the point of having a registry.

## The fourth rule, about being wrong

The sentences excised are pinned to exact wording of one Claude Code release.
A later one that says the same thing differently matches nothing, the stock
identity survives, and the kernel goes in *beside* the identity it was meant to
replace — two identities in the prompt, nothing raised, and behaviour you would
struggle to attribute weeks later.

So the sentences live in one file, [`identity.json`](identity.json), with the
version each was last confirmed against, and **a miss is made loud three ways**
— by the same mechanism any stage uses to say it could not do its job:

| where | what you see |
|---|---|
| status line | `kernel!` instead of `kernel` |
| `bottleneck kernel` | the excision named, and a non-zero exit |
| usage log | `stage_gaps` on the row for that request |

A miss is only counted against a system prompt big enough to be a real one —
Claude Code's startup quota probe carries almost no system text, and calling
that a failed excision would cry wolf before every session began. A stage that
looked says so (`judged`); one that had nothing to judge leaves the mark alone
rather than reporting all-clear, so a probe cannot wipe a real failure off the
status line.

The external proxy is a separate program with its own config, so its copy
cannot be deleted — it is *checked* instead. `proxy.gap()` compares
`config.toml`'s patterns against `identity.json` and reports drift in either
direction, and reads the proxy's own debug dump of the body it last sent: a
stock sentence still in there after a rewrite is a live failure whatever the
config claims. `bottleneck kernel show` prints the stanza to paste when they
have parted company.

## Commands

```
bottleneck kernel        up or down, what it injects, what it has metered
bottleneck kernel start  bring it up now rather than at the next head
bottleneck kernel show   what a head would be sent, run through every stage
bottleneck kernel stages what can run, what does run, and in what order
```

## Settings

| | |
|---|---|
| `BOTTLENECK_KERNEL_BACKEND` | `builtin` or `proxy`; default prefers the checkout |
| `BOTTLENECK_KERNEL_ROOT` | the proxy checkout (default `~/cc-kernel-proxy`) |
| `BOTTLENECK_KERNEL_PORT` | the builtin wrapper's port (default 8791) |
| `BOTTLENECK_KERNEL_FILE` | the identity text it injects (default `kernel.md`) |
| `BOTTLENECK_KERNEL_IDENTITY` | the sentences it excises (default `identity.json`) |
| `BOTTLENECK_KERNEL_STAGES` | which stages run, in order; `none` for a plain hop |
| `BOTTLENECK_KERNEL_STAGES_DIR` | where to look for stages before looking here |
| `BOTTLENECK_KERNEL_MEMORY` | the file the memory stage recalls |
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
