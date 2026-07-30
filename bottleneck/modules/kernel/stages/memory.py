"""memory - what was learned before, recalled into the turn being sent.

A tail stage, and the reason the two classes exist. Everything the identity
stage does is pinned to the cached prefix and must be pure to the byte; this
does the opposite thing in the opposite place. Memory changes between turns -
that is what makes it memory - so writing it into the system prompt would
rewrite the cached prefix on every request and turn a session's cache reads
into cache writes. Written into the newest message instead, which sits after the
last breakpoint and is nobody's prefix, it costs nothing.

The registry hands this stage one message and no way to reach any other, so
"only the newest turn" is not a promise it is trusted to keep.

What it recalls is a file, and deliberately so. This is a demonstration that the
interface carries a second class of stage, not a memory service: a real one
would select against the turn, and *that* is the interesting version - a model
call, slow and fallible, which is exactly what a tail stage is allowed to be and
a prefix stage is not. The seam is the point; what plugs into it is yours.
"""
import os


HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT = os.path.join(os.environ.get("BOTTLENECK_STATE")
                       or os.path.expanduser("~/.bottleneck"),
                       "kernel", "memory.md")

HEADER = "\n\n<recalled>\n"
FOOTER = "\n</recalled>"


def source():
    from . import settings
    return os.path.expanduser(
        os.environ.get("BOTTLENECK_KERNEL_MEMORY")
        or settings("memory").get("file") or DEFAULT)


def cap():
    from . import settings
    try:
        return int(settings("memory").get("max_chars") or 4000)
    except (TypeError, ValueError):
        return 4000


def recalled():
    """The text to inject, already capped. "" when there is nothing."""
    try:
        with open(source()) as fh:
            text = fh.read().strip()
    except OSError:
        return ""
    limit = cap()
    if len(text) > limit:
        # The tail, not the head: a memory file is appended to, so the end of
        # it is the part that has not been seen before.
        text = "...\n" + text[-limit:]
    return text


def apply(message, ctx):
    """Recall into the newest turn, if it is a turn a person just took."""
    if message.get("role") != "user":
        return None
    text = recalled()
    if not text:
        return None                     # nothing recorded is not a failure

    block = HEADER + text + FOOTER
    content = message.get("content")
    if isinstance(content, str):
        message["content"] = content + block
    elif isinstance(content, list):
        # Appended to the last text block rather than added as a new one: the
        # shape of a turn is not ours to change any more than the prefix is,
        # and a tool_result the model is mid-way through answering is the wrong
        # place to put anything.
        at = next((i for i in range(len(content) - 1, -1, -1)
                   if isinstance(content[i], dict)
                   and content[i].get("type") == "text"), None)
        if at is None:
            return {"judged": True,
                    "gap": "the newest turn has no text block to recall into"}
        content[at]["text"] = (content[at].get("text") or "") + block
    else:
        return None
    return {"judged": True, "chars": len(text), "from": source()}


STAGE = {
    "summary": "recall a memory file into the newest user turn, after the "
               "cache breakpoint",
    "writes": "tail",
    "apply": apply,
}
