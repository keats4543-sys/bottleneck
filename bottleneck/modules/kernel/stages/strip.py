"""strip - boilerplate out of the system prompt, by pattern.

The plainest possible prefix stage, and the one you are most likely to want to
edit rather than read. Patterns come from stages.json; there are none by
default, because a stage that deletes text out of claude's prompt on the say-so
of a file nobody has opened is a bad default.

    "settings": {"strip": {"patterns": ["(?m)^IMPORTANT: Assist with.*$"]}}

Keep the list short and static. Every pattern here is a bet on wording somebody
else controls, and the bet is quiet when it is lost: the text simply stays. So
each pattern says whether it is `required`, and a required one that matches
nothing on a real prompt is reported the same way a missed excision is - on the
status line, in the usage log, and by `bottleneck kernel`.
"""
import re


_LOADED = None


def patterns():
    """[(regex, required)] - never raises. A bad pattern is skipped, not fatal."""
    global _LOADED
    from . import settings
    got = settings("strip").get("patterns") or []
    key = repr(got)
    if _LOADED is not None and _LOADED[0] == key:
        return _LOADED[1]
    out = []
    for row in got:
        if isinstance(row, str):
            row = {"pattern": row}
        if not isinstance(row, dict):
            continue
        try:
            out.append((re.compile(row["pattern"]),
                        bool(row.get("required")), row["pattern"]))
        except (KeyError, TypeError, re.error):
            continue
    _LOADED = (key, out)
    return out


def min_chars():
    """Below this, a prompt is not one these patterns were written for.

    The same gate identity.py keeps, and for the same reason: Claude Code makes
    smaller requests with system prompts of their own, and a required pattern
    cannot match a section that prompt never had. Judging one of those would
    report a failure on a strip that is working - which is the whole failure
    mode this reporting exists to avoid.
    """
    from . import settings
    try:
        return int(settings("strip").get("min_chars", 8000))
    except (TypeError, ValueError):
        return 8000


def apply(system, ctx):
    rows = patterns()
    if not rows or not isinstance(system, list):
        return None
    size = sum(len(b.get("text") or "") for b in system
               if isinstance(b, dict) and b.get("type") == "text")
    hit, chars = set(), 0
    for block in system:
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        text = block.get("text") or ""
        for regex, _required, source in rows:
            now, count = regex.subn("", text)
            if count:
                hit.add(source)
                chars += len(text) - len(now)
                text = now
        # Never left empty: an empty text block is a 400 from the API, and a
        # pattern that happens to match a whole block is exactly how you would
        # get one. The identity stage learned this from a live head.
        if text.strip():
            block["text"] = text
    if size < min_chars():
        return {"chars": chars}         # no `judged`: nothing to judge here
    missing = [src for _r, required, src in rows if required and src not in hit]
    report = {"judged": True, "stripped": len(hit), "chars": chars}
    if missing:
        report["gap"] = [f"required pattern matched nothing: {src[:60]}"
                         for src in missing]
    return report


def explain():
    rows = patterns()
    if not rows:
        return ["no patterns configured - see stages.json"]
    return [f"{'required' if req else 'optional'}  {src[:70]}"
            for _r, req, src in rows]


STAGE = {
    "summary": "remove boilerplate from the system prompt by pattern",
    "writes": "prefix",
    "apply": apply,
    "explain": explain,
}
