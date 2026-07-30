"""identity - claude's opening sentences out, a kernel of your own in.

The thing a hook cannot do. By the time any hook runs the request has been made
and the stock identity is already in it; replacing it happens at the HTTP
boundary or not at all.

A prefix stage, so it is held to purity and to shape. Both were learned from a
live head rather than from reasoning: excising a sentence that was a block of
its own left "", which the API rejects with `400 system.1: cache_control cannot
be set for empty text blocks` - every turn of every session failing while the
identity swap worked perfectly - and the first fix for that dropped the empty
block and moved a caching breakpoint. So the kernel goes *into* the block the
excision emptied, which is where the identity it replaces used to be anyway.

The sentences live in identity.json with the Claude Code version each was last
confirmed against, because they are pinned to exact wording and a release that
rewords its opening matches none of them - and then the stock identity survives,
the kernel goes in beside it, and nothing fails. That is what `gap` is for.
"""
import json
import os
import re


HERE = os.path.dirname(os.path.abspath(__file__))


def _kernel_file():
    """Yours if you have one, ours if you do not.

    The kernel is the one part of this that is genuinely personal - it is what
    you want a session to be - so the state directory wins over the checkout,
    and the file in the module stays a demonstration rather than something you
    have to edit around. Nothing writes your kernel into this repository.
    """
    said = os.environ.get("BOTTLENECK_KERNEL_FILE")
    if said:
        return os.path.expanduser(said)
    yours = os.path.join(os.environ.get("BOTTLENECK_STATE")
                         or os.path.expanduser("~/.bottleneck"),
                         "kernel", "kernel.md")
    return yours if os.path.exists(yours) else os.path.join(
        os.path.dirname(HERE), "kernel.md")


KERNEL_FILE = _kernel_file()
IDENTITY_FILE = os.environ.get(
    "BOTTLENECK_KERNEL_IDENTITY",
    os.path.join(os.path.dirname(HERE), "identity.json"))

HEADER = "# Operating Identity\n\n"

_LOADED = None


def identity(reload=False):
    """[{name, pattern, witness, re}] - what this stage expects to excise.

    Never raises. An unreadable source excises nothing, which shows up as every
    excision missing on the first real prompt - loud, rather than a rewrite that
    quietly does half its job.
    """
    global _LOADED
    if _LOADED is not None and not reload and _LOADED[0] == IDENTITY_FILE:
        return _LOADED[1]
    out = []
    try:
        with open(IDENTITY_FILE) as fh:
            for row in (json.load(fh).get("excise") or []):
                out.append(dict(row, re=re.compile(row["pattern"])))
    except (OSError, ValueError, KeyError, TypeError, re.error):
        out = []
    _LOADED = (IDENTITY_FILE, out)
    return out


def min_system_chars():
    """Below this, a system prompt is a probe and a miss means nothing.

    Claude Code opens with tens of thousands of characters. Its startup quota
    check carries almost none, and calling that a failed excision would cry wolf
    before every session began.
    """
    try:
        with open(IDENTITY_FILE) as fh:
            return int(json.load(fh).get("min_system_chars") or 2000)
    except (OSError, ValueError, TypeError):
        return 2000


def kernel_text():
    try:
        with open(KERNEL_FILE) as fh:
            return fh.read().strip()
    except OSError:
        return ""


def rewrite_text(text, hit=None):
    """The stock identity out. A pure function of `text`.

    `hit` collects the name of every excision that matched; what is missing from
    it afterwards is what this build of Claude Code no longer says the way we
    think it does.
    """
    for row in identity():
        found, count = row["re"].subn("", text)
        if count and hit is not None:
            hit.add(row["name"])
        text = found
    return text


def system_chars(system):
    if isinstance(system, str):
        return len(system)
    if not isinstance(system, list):
        return 0
    return sum(len(b.get("text") or "") for b in system
               if isinstance(b, dict) and b.get("type") == "text")


def rewrite_system(system, kernel, hit=None):
    """The system field rewritten in place. Same shape, different words.

    Returns it as well as mutating it, because a plain string system prompt
    cannot be mutated - the stage below handles that case, and callers holding
    a list get the same object back.
    """
    if not kernel:
        return system
    inject = HEADER + kernel + "\n\n"
    if isinstance(system, str):
        return inject + rewrite_text(system, hit)
    if not isinstance(system, list):
        return system

    originals = []
    for i, block in enumerate(system):
        if isinstance(block, dict) and block.get("type") == "text":
            originals.append(block.get("text") or "")
            block["text"] = rewrite_text(block["text"], hit)
        else:
            originals.append(None)

    def is_text(b):
        return isinstance(b, dict) and b.get("type") == "text"

    # The block an excision emptied, if any, else the first.
    at = next((i for i, b in enumerate(system) if is_text(b) and not b["text"]),
              next((i for i, b in enumerate(system) if is_text(b)), None))
    if at is not None:
        system[at]["text"] = inject + system[at]["text"]
    # Anything still empty keeps what it came with: better an identity we failed
    # to excise than a request the API refuses.
    for i, block in enumerate(system):
        if is_text(block) and not block["text"] and originals[i]:
            block["text"] = originals[i]
    return system


def apply(system, ctx):
    """Run over the system field. See the registry for what a stage may do."""
    kernel = kernel_text()
    if not kernel:
        return {"gap": f"no kernel text - {KERNEL_FILE}"}
    hit = set()
    got = rewrite_system(system, kernel, hit)
    if isinstance(system, str):
        # A string system prompt cannot be written back through the list the
        # registry handed us. Nothing has sent one yet; say so rather than
        # pretend it worked.
        return {"gap": "the system prompt is a plain string, which this stage "
                       "cannot write back in place"}
    if system_chars(got) < min_system_chars():
        # A probe, not a real prompt. No `judged`: this stage has nothing to say
        # about whether it is working, and saying "fine" here would wipe a real
        # failure off the status line every time claude checked its quota.
        return {"chars": system_chars(got)}
    missed = [row["name"] for row in identity() if row["name"] not in hit]
    if not identity():
        missed = [f"no excisions loaded - {IDENTITY_FILE}"]
    # Belt and braces, and they catch different things. A pattern that matched
    # says the excision fired; a witness still present afterwards says it did
    # not do the job - which is what a *partially* reworded sentence looks like,
    # where the regex still matches some of it and leaves the rest behind.
    left = "\n".join(b.get("text") or "" for b in got
                     if isinstance(b, dict) and b.get("type") == "text")
    missed += [f"{row['name']} (survived the rewrite)" for row in identity()
               if row.get("witness") and row["witness"] in left
               and row["name"] not in missed]
    report = {"judged": True, "chars": system_chars(got),
              "excised": sorted(hit)}
    if missed:
        report["gap"] = [f"{name} matched nothing, so the stock identity is "
                         f"still in the prompt beside the kernel"
                         for name in missed]
        report["missed"] = missed
    return report


def explain():
    """What this stage would tell you about itself. For `bottleneck kernel`.

    A stage reports what it did to one request; this is the standing state
    behind that - which sentences it is looking for and what they were last
    confirmed against. The CLI prints it without knowing what any of it means.
    """
    rows = identity()
    if not rows:
        return [f"no excisions loaded - {IDENTITY_FILE}"]
    out = [f"{row['name']:<18} pinned to Claude Code "
           f"{row.get('matched', '?')}" for row in rows]
    out.append(f"kernel: {len(kernel_text())} chars from {KERNEL_FILE}")
    return out


STAGE = {
    "summary": "claude's stock opening sentences out, kernel.md in their place",
    "writes": "prefix",
    "apply": apply,
    "explain": explain,
}
