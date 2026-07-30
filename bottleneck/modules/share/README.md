# share - what the heads in a group tell each other

The dashboard solves your half of the problem: which head wants you. This is the
heads' half. Six heads in one repository cannot see each other at all — two of
them rewrite the same file, a third reads a file a fourth has just deleted, and
none of them ever finds out, because the only thing they share is a filesystem
and nothing is watching it.

So each **group** keeps a small board. A head writes a line to it when it does
something a sibling would want to know, and reads what its siblings have written
at the start of its own turn:

```
<bottleneck-group name="payments">
1 other head shares this group:
  brisk-finch  in this same directory  - port the webhook retries
3 things happened since you were last told:
  4m ago    brisk-finch is here to: port the webhook retries
  2m ago    brisk-finch edited svc/webhook.py
  1m ago    brisk-finch deleted tests/test_retry.py
Re-read any of those files before you edit them - what you have in context is
from before the change.
</bottleneck-group>
```

The group is the boundary, and that is what keeps this quiet. A head in no group
writes nothing and reads nothing, so a lone session pays a dictionary lookup per
tool call and nothing else. Grouping heads is already how you say "these are on
the same work"; this makes them act like it.

**A head says what it is for, once.** The first thing you type at a head becomes
its goal, because nothing else in the session says it as plainly and asking you
to say it twice is how a feature stops being used. `bottleneck goal <n> <what
it is for>` overrides it. Siblings are told, and every one that joins later is
told too.

**A turn only carries what is new.** The block above is an *introduction*, and
a head gets one when it starts or comes back from a compaction — who is here,
what they are for, and how this works. Every turn after that carries only what
has happened since the last one, and a turn where nothing happened injects
nothing at all. Leaving the standing half in was costing 328 identical
characters a turn for a group of three, about 66k over a 200-turn session, and
buying nothing: joining, leaving and saying what you are for are all news, so a
head that was introduced once has been kept current ever since.

**The queue is bounded and per reader.** A group's board keeps the last
`BOTTLENECK_SHARE_MAX` messages — twelve by default — and drops the oldest, so
the injection has a ceiling you can reason about. Reading it is not emptying it:
each head has its own cursor, so two siblings reading at different moments both
get everything they have not seen, exactly once. When something did fall off the
end while a head was busy, the head is told how many rather than quietly handed
a shorter list.

**It is handed over on an event, not on a timer.** The queue builds while a head
works and is delivered whole at the next moment the head can take delivery of
anything: its next prompt, its next start, or a file clash that cannot wait.

**The clash is the point of the file lines.** Before a head edits a file, the
board is asked who last changed it. It says something only when all three hold —
another head did it, recently enough to still be a race, and this head has not
read the file since:

```
brisk-finch (another head in your group) edited svc/webhook.py 2m ago, after you
last read it - re-read it before you write, or you will overwrite that change
```

That last condition is what keeps it from crying wolf: a head that has just read
the file is working from what is actually there, and hears nothing.
`BOTTLENECK_SHARE_GUARD=ask` turns the warning into a permission prompt instead
— right for a fleet racing over one file, wrong for most days. `off` keeps the
board and drops the guard.

| | |
|---|---|
| `bottleneck share` | every board: goals, queues, who changed what |
| `bottleneck goal` | what each head says it is for |
| `bottleneck goal <n> <s>` | say what a head is for |
| `bottleneck note <s>` | put a line on this head's board — run inside its pane |
| `bottleneck share clear` | forget every board; groups and heads are untouched |

## Who notices that a file changed

Two answers, and it is a trade rather than a preference.
`BOTTLENECK_SHARE_SOURCE` picks one; re-run `install.sh` after changing it,
because it decides a line of wiring.

**`hook`** (the default) is a `PostToolUse` hook. The head tells us itself, the
instant it happens, whether or not anything else is running. It costs a process
per Read, Write, Edit and Bash — about 40ms, nearly all of it Python starting,
because the work itself is a lock and a small file.

**`dashboard`** reads the same facts out of the transcripts, in the process that
is already standing and already reading them every couple of seconds to say what
each head is doing. Nothing about a tool call is secret: `claude` has already
written it down. So the per-tool-call cost goes to nothing, and the work moves
into a pass that was happening anyway:

| | `hook` | `dashboard` |
|---|---|---|
| six heads, six tool calls each, per 2s pass | 36 spawns, ~40ms each | one pass, **7ms** |
| the same heads idle | nothing | **0.2ms** |
| board is current to | the tool call | the last pass (~2s) |
| with no dashboard running | still works | **notices nothing** |

The guard stays a hook either way: a warning that arrives *before* you edit a
file is a reply to a question, and only a hook is asked. So `dashboard` mode
leaves `PreToolUse` on edits wired (far rarer than reads) and drops
`PostToolUse` entirely.

Two things are given up, and they are the same thing said twice: the board is
only as current as the last pass, and only kept while a dashboard is up. For the
queue that is nothing — it is read at the start of a turn, and a turn is not two
seconds long. For the guard it is a two-second window against races that take
minutes to matter. For a fleet run with no dashboard it is everything, which is
why this is a setting and not a replacement. `bottleneck share` says which is in
force, and says so loudly when it is `dashboard` and no dashboard is running.

A head first seen is read from the *end* of its transcript, so switching this on
does not post an afternoon of history as news; and a dashboard that has been away
for a megabyte of transcript skips to the end rather than replaying it, because
by then it is not a witness to anything.

## The wiring

Writing is `hooks/share.py`, wired by `install.sh` to `SessionStart`,
`UserPromptSubmit`, `SessionEnd` and to tool calls that touch the working tree —
matched by tool name, so a `Grep` does not start a python. What it does cost is
around 40ms on the tool calls it is matched to, and more than that on a box
whose cores the heads are already fighting over. Almost none of that is the
work — one lock and a small file is about a millisecond — and almost all of it
is python starting. So the three things that make it cheaper are all about
startup: the hooks run under `python3 -S`, the hook loads `share.py` by path
rather than through the package (which would import `config.py`), and `shlex`
is imported inside the one branch that parses a shell command instead of on
every Read. A head in no group is answered by one small file read and nothing
else. Deletions through the
shell are read out of `rm` and `mv` as a best effort: the hook checks whether the
file is actually gone afterwards, so the worst a misparse can do is fail to
notice, never invent a change that did not happen.

---

This is a **module**: everything above lives in `bottleneck/modules/share/`, and
nothing outside that directory knows it is here. `bottleneck modules` says
whether it is loaded; `BOTTLENECK_MODULES` in `~/.bottleneck/config` turns it
off without removing it. See `bottleneck/modules/__init__.py` for what a module
can plug into.
