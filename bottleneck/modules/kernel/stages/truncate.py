"""truncate - an enormous tool result cut down, the same way every time.

A history stage, and the reason that class exists. A conversation grows and
every turn of it is resent; a single `cat` of a large file can sit in the
history for the rest of a session, paid for on every request. Cutting it is
worth real money.

What makes it safe is that the cut is keyed on the content and nothing else.
The same string always cuts to the same string, whatever turn it is in, whatever
else is in the conversation, however many times it has been sent before - so the
cached prefix stays byte-identical and the saving is a saving rather than a
cache miss that costs more than it saved. A truncation keyed on position, or on
"how much are we over budget", would be the version that quietly doubles your
bill.

Head and tail are both kept because that is where the information is: what the
command was doing and how it ended. The middle of a 200,000-character file
listing is the part nobody reads.

Ported from cc-kernel-proxy's optimize pass, which is where the numbers come
from, so a machine without that checkout loses nothing by not having it.
"""

MARK = "\n\n[... {n} chars elided - bottleneck, deterministic truncation ...]\n\n"


def limits():
    from . import settings
    got = settings("truncate")

    def num(key, fallback):
        try:
            return int(got.get(key, fallback))
        except (TypeError, ValueError):
            return fallback

    return num("max_chars", 40000), num("head", 30000), num("tail", 8000)


def cut(text, most, head, tail):
    """`text`, shortened if it is long. A pure function of `text`."""
    if most <= 0 or len(text) <= most:
        return text
    elided = len(text) - head - tail
    if elided <= 0:
        return text
    return text[:head] + MARK.format(n=elided) + text[len(text) - tail:]


def cut_content(content, most, head, tail):
    """tool_result content is a string, or a list of blocks. Both happen."""
    if isinstance(content, str):
        return cut(content, most, head, tail), len(content)
    if isinstance(content, list):
        saved = 0
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                was = block.get("text") or ""
                now = cut(was, most, head, tail)
                if now != was:
                    block["text"] = now
                    saved += len(was) - len(now)
        return content, saved
    return content, 0


def apply(messages, ctx):
    most, head, tail = limits()
    if most <= 0:
        return None
    saved = cuts = 0
    for message in messages:
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            was = block.get("content")
            now, less = cut_content(was, most, head, tail)
            if less:
                block["content"] = now
                saved += less
                cuts += 1
    if not cuts:
        return None                     # nothing big enough is not a failure
    return {"judged": True, "cuts": cuts, "chars_saved": saved}


def explain():
    most, head, tail = limits()
    return [f"tool results over {most:,} chars keep {head:,} head + "
            f"{tail:,} tail"]


STAGE = {
    "summary": "cut oversized tool results, keyed on content so the cache "
               "prefix holds",
    "writes": "history",
    "apply": apply,
    "explain": explain,
}
