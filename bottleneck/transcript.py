"""Reading a head's transcript: what it is doing, and what it wants."""
import json
import os
import re

from . import config
from .config import PROJECTS, SUMMARY_CHARS, TASK_BUDGET, TASK_CHARS


# ------------------------------------------------------------- transcript tail

def tail_lines(path, count=40, budget=262144):
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            fh.seek(max(0, size - budget))
            chunk = fh.read()
    except OSError:
        return []
    lines = chunk.split(b"\n")
    if size > budget and lines:
        lines = lines[1:]
    return [l for l in lines[-count:] if l.strip()]


def summarise_tool(name, inp):
    inp = inp if isinstance(inp, dict) else {}

    def short(v, n=52):
        v = " ".join(str(v).split())
        return v if len(v) <= n else v[: n - 1] + "…"

    if name == "Bash":
        return f"Bash {short(inp.get('command', ''))}"
    for key in ("file_path", "notebook_path", "path"):
        if inp.get(key):
            return f"{name} {os.path.basename(str(inp[key]))}"
    if name in ("Grep", "Glob"):
        return f"{name} {short(inp.get('pattern', ''), 34)}"
    if name in ("Agent", "Task"):
        return f"Agent {short(inp.get('description') or inp.get('subagent_type', ''), 34)}"
    if name == "Skill":
        return f"/{inp.get('skill', '?')}"
    if name in ("WebFetch", "WebSearch"):
        return f"{name} {short(inp.get('url') or inp.get('query', ''), 34)}"
    if name == "Workflow":
        return f"Workflow {short(inp.get('name', ''), 28)}"
    return name


# Parsing a tail costs a few milliseconds per head and the dashboard does it
# for every head on every redraw, so the answer is kept against the file it was
# read from. A transcript only ever grows, so mtime and size together say all
# there is to say about whether the last answer still holds.
_steps = {}


def read_step(path):
    """(step, when, ask, task, kids) for a head, cached against its transcript."""
    try:
        st = os.stat(path)
    except OSError:
        return "", 0.0, "", "", 0
    stamp = (st.st_mtime_ns, st.st_size)
    hit = _steps.get(path)
    if hit is not None and hit[0] == stamp:
        return hit[1]
    val = parse_step(path)
    if len(_steps) > 128:
        _steps.clear()          # heads come and go; do not grow without bound
    _steps[path] = (stamp, val)
    return val


# ------------------------------------------------------- work sent out and back
#
# A head that starts async agents does not sit there waiting for them. The tool
# returns at once with an id, the head finishes its turn, and the harness quite
# correctly writes down "idle" - the head is not thinking, it is waiting to be
# told. Read only that, and a head with six agents out reads the same as one
# with nothing left to do, which is the opposite of the truth.
#
# Both halves are in the head's own transcript, so counting them costs nothing
# beyond the tail already being read: the launch comes back as a tool result
# naming an agentId, and each finish arrives as a task-notification carrying
# the same id back. What was sent and has not returned is what it is waiting on.

LAUNCHED = b"Async agent launched successfully"
NOTIFIED = b"<task-notification>"
_AGENT_ID = re.compile(r"agentId:\s*([0-9a-zA-Z]+)")
_TASK_ID = re.compile(r"<task-id>\s*([0-9a-zA-Z]+)\s*</task-id>")


def block_texts(entry):
    """(kind, text) for each block in an entry, kept apart, not run together.

    Apart matters: what marks a launch or a notification is the block *opening*
    with it, and joining the blocks first would put that opening in the middle
    of a sentence where it can no longer be recognised. The kind matters for the
    same reason - a launch is something the harness hands back, so it only ever
    arrives as a tool result.
    """
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        return [("text", content)]
    out = []
    for blk in content or []:
        if not isinstance(blk, dict):
            continue
        kind = blk.get("type")
        if kind == "text":
            out.append(("text", blk.get("text") or ""))
        elif kind == "tool_result":
            inner = blk.get("content")
            if isinstance(inner, str):
                out.append(("tool_result", inner))
            else:
                out.extend(("tool_result", b.get("text") or "")
                           for b in inner or [] if isinstance(b, dict))
    return out


def outstanding(lines):
    """How many agents this head has sent out that have not reported back.

    The byte test comes first and throws out nearly every line without decoding
    it - the window is a megabyte and only a handful of lines can possibly be
    about an agent.

    What survives has to be the harness saying it, not the head repeating it,
    and two things together make that stick. The entry has to be a user turn,
    because that is the only side the harness writes on - the head's own prose
    is an assistant turn, however exactly it quotes the words. And the marker
    has to open its block, because the real ones do and a quotation - a
    transcript read into a tool result, a paragraph about this very feature -
    always carries it somewhere in the middle of something else.

    Either test alone lets one case through, which is how both got here.
    """
    sent, back = agent_ids(lines)
    return len(sent - back)


def agent_ids(lines):
    """(sent, back) - agent ids launched, and agent ids that have reported.

    Split out from the count because the count is not the only caller any more:
    the incremental scan keeps these two sets across reads, so it can say what
    is outstanding without re-reading the megabyte they were first seen in.
    """
    sent, back = set(), set()
    for raw in lines:
        launch = LAUNCHED in raw
        if not launch and NOTIFIED not in raw:
            continue
        try:
            entry = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if entry.get("type") != "user":
            continue
        for kind, text in block_texts(entry):
            head = (text or "").lstrip()
            if launch and kind == "tool_result" \
                    and head.startswith(LAUNCHED.decode()):
                sent.update(_AGENT_ID.findall(text))
            elif head.startswith(NOTIFIED.decode()):
                back.update(_TASK_ID.findall(text))
    return sent, back


def subagent_seen(path):
    """When any of this head's agents last wrote something, or 0.0.

    Their transcripts sit in a directory named after the head's own, so this is
    one listing and a stat apiece. It is what keeps a head whose agents have all
    died from reading as busy for ever: no child has written for a while is the
    same quiet that STALLED already means, so it is fed in as the head's own
    last activity and the existing rule does the rest.
    """
    kids = os.path.splitext(path)[0] + "/subagents"
    newest = 0.0
    try:
        with os.scandir(kids) as it:
            for entry in it:
                if not entry.name.endswith(".jsonl"):
                    continue
                try:
                    newest = max(newest, entry.stat().st_mtime)
                except OSError:
                    pass
    except OSError:
        return 0.0
    return newest


# ------------------------------------------------------ reading only what is new
#
# Two of the three answers below need a wide window - your prompt sits at the
# far end of a turn that can be a hundred tool calls long, and an agent's launch
# further still. Re-reading that window every time the file changes cost a
# megabyte and about 7ms per head per pass, and a head that is working changes
# its file constantly: fifteen busy heads came to 101ms and 12.8MB a pass, all
# of it re-reading bytes that had not moved.
#
# A transcript only ever grows. So the window is read once and then only the
# bytes appended since are read, with what they said kept here between passes.
# The answers are the same ones; what goes away is reading the same megabyte to
# arrive at them again.

_scan = {}


# How many of the newest lines to hold, and how much of them. The parse below
# wants the last few entries; the byte cap is what stops one head that printed a
# hundred-kilobyte tool result from being remembered at that size for ever.
TAIL_LINES = 40
TAIL_BYTES = 262144


def scan_new(path, size):
    """(tail, task, kids) for a file, from its new bytes and what we knew before.

    Rewound or replaced - a size smaller than where we stopped - and it is not
    the file we were reading, so the window is read again from scratch.
    """
    prev = _scan.get(path)
    if prev is None or prev["at"] > size:
        start = max(0, size - TASK_BUDGET)
        state = {"at": start, "task": "", "sent": {}, "back": set(),
                 "tail": [], "clip": start > 0}
    else:
        state = prev

    lines, at = read_from(path, state["at"], size)
    if state.pop("clip", False) and lines:
        lines = lines[1:]           # the first line of a window starts mid-line
    state["at"] = at

    said = last_prompt(lines)
    if said:
        state["task"] = said

    sent, back = agent_ids(lines)
    # Where each launch was seen, so the set can be aged out the way the window
    # aged it out before: an agent whose launch has scrolled a megabyte into the
    # past is not something this row should still be counting.
    for aid in sent:
        state["sent"][aid] = at
    state["back"].update(back)
    floor = size - TASK_BUDGET
    state["sent"] = {a: seen for a, seen in state["sent"].items() if seen >= floor}
    state["back"] &= set(state["sent"])

    # The newest lines, kept rather than read again. They have just been read to
    # get here, and re-reading the end of the file for them was the whole of
    # what the tail read used to cost: a quarter megabyte a head a pass, for
    # bytes that had not moved.
    tail = (state["tail"] + lines)[-TAIL_LINES:]
    while len(tail) > 1 and sum(len(l) for l in tail) > TAIL_BYTES:
        tail.pop(0)
    state["tail"] = tail

    if len(_scan) > 128:
        _scan.clear()               # heads come and go; do not grow without bound
    _scan[path] = state
    return tail, state["task"], len(set(state["sent"]) - state["back"])


def read_from(path, start, size):
    """The complete lines between `start` and `size`, and where they stopped.

    Stopping on the last newline is what makes the next read safe: a transcript
    is written a line at a time and we may well arrive mid-line, so the tail of
    the read is left for next time rather than parsed as though it were whole.
    """
    if start >= size:
        return [], start
    try:
        with open(path, "rb") as fh:
            fh.seek(start)
            chunk = fh.read(size - start)
    except OSError:
        return [], start
    cut = chunk.rfind(b"\n")
    if cut == -1:
        return [], start
    return [l for l in chunk[:cut].split(b"\n") if l.strip()], start + cut + 1


def parse_step(path):
    """(step, when, ask, task, kids) for a head, read from the tail of its
    transcript.

    `step` is what it is doing now. `ask` is what it wants from you, and exists
    because the alternative is a row that says "turn finished, unread" - true,
    and no help at all when five rows say it. The head has just told you what it
    did or what it wants to do; that sentence is worth more than any label we
    could invent for it.

    `task` is the last thing you asked it for. The other two are both read off
    the head's own last message, which is why a row could say "committed and
    pushed" while the head was three prompts further on: what it just said and
    what it is for are different questions, and only one of them was on screen.

    The two halves want different windows, so they take different reads. What
    the head is doing is in the last few entries and nothing else, so that read
    is a short tail. Your prompt and an agent's launch can be a megabyte back,
    but they are also already behind us: scan_new carries them forward from the
    bytes they were first read in, and only new bytes are ever read twice.
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        return "", 0.0, "", "", 0
    lines, task, kids = scan_new(path, size)
    entries = []
    for raw in lines:
        try:
            entries.append(json.loads(raw))
        except (ValueError, TypeError):
            continue
    if not entries:
        return "", 0.0, "", task, kids

    from datetime import datetime

    def stamp(e):
        try:
            return datetime.fromisoformat(
                (e.get("timestamp") or "").replace("Z", "+00:00")).timestamp()
        except (ValueError, AttributeError):
            return 0.0

    last_ts = max((stamp(e) for e in entries), default=0.0)

    done = set()
    for e in entries:
        for blk in (e.get("message") or {}).get("content") or []:
            if isinstance(blk, dict) and blk.get("type") == "tool_result":
                done.add(blk.get("tool_use_id"))

    ask = read_ask(entries, done)
    finished = ""
    for e in reversed(entries):
        if e.get("type") != "assistant":
            continue
        pending, text = [], ""
        for blk in (e.get("message") or {}).get("content") or []:
            if not isinstance(blk, dict):
                continue
            if blk.get("type") == "tool_use":
                label = summarise_tool(blk.get("name", "?"), blk.get("input"))
                if blk.get("id") not in done:
                    pending.append(label)
                elif not finished:
                    finished = label
            elif blk.get("type") == "text" and blk.get("text", "").strip():
                # Kept with its newlines: first_sentence works line by line, and
                # flattening here would hide the headings it means to skip.
                text = blk["text"]
        if pending:
            head = pending[0]
            if len(pending) > 1:
                head += f" (+{len(pending) - 1})"
            return "⚙ " + head, last_ts, ask, task, kids
        if text:
            return "» " + first_sentence(text, 150), last_ts, ask, task, kids
    if finished:
        return "✓ " + finished, last_ts, ask, task, kids
    return "", last_ts, ask, task, kids


def clip(text, n):
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def first_sentence(text, n=SUMMARY_CHARS):
    """The opening claim of a message, without the paragraphs after it.

    A finished turn usually starts by saying what happened - that first sentence
    is the whole point of the row. Headings, bullets and code fences are dropped
    on the way past; they read as noise at one line high.
    """
    text = (text or "").strip()
    keep = []
    for line in text.split("\n"):
        line = line.strip()
        # A table row, a heading, a fence or a quote is furniture. Skip it while
        # looking for the sentence; stop once we have one, because what follows
        # belongs to a different thought.
        if not line or line.startswith(("#", "|", "```", ">")):
            if keep:
                break
            continue
        line = line.lstrip("-*• ").strip()
        # Bold and italic markers survive every other kind of trimming and then
        # show up mid-row as "done** -", so take them out wherever they sit.
        for mark in ("**", "__"):
            line = line.replace(mark, "")
        keep.append(line)
        if len(" ".join(keep)) > n:
            break
    said = " ".join(keep)
    if not said:
        # All furniture - a message that is only a heading and a table. Noisy
        # beats blank: a row saying nothing tells you nothing.
        said = text
        for mark in ("**", "__", "#", "`", "|"):
            said = said.replace(mark, " ")
    # A full stop ends a thought and the rest belongs to the next one. A dash
    # does not - it is nearly always the same sentence carrying on, and cutting
    # at it threw away the half that said something: "Committed and pushed as
    # 985803cf6" fits in a fifth of the budget and tells you nothing that the
    # clause after the dash - which eight files, and whose - would have. So a
    # dash only ends the line once the line is long enough to have earned it.
    for stop, floor in ((". ", 24), ("? ", 24), ("! ", 24),
                        (" - ", n // 2), (" — ", n // 2)):
        cut = said.find(stop)
        if floor <= cut <= n:
            said = said[: cut + (1 if stop[0] in ".?!" else 0)]
            break
    return clip(said, n)


def closing_question(text, n=SUMMARY_CHARS):
    """The question a message ends on, or "" if it does not end on one.

    A finished head puts the decision last: the paragraphs report what happened,
    and the final line asks which way to go. That line is the one worth reading
    off a dashboard, because it is the only part you can answer - "shall I take
    the second one first?" is a row you can act on, where the opening claim only
    tells you the head is done, which the state column already said.

    Only the closing lines count. A question in the middle of a report has
    usually been answered by the report itself.
    """
    lines = []
    for line in reversed((text or "").strip().split("\n")):
        line = line.strip()
        if not line or line.startswith(("#", "|", "```", ">")):
            continue
        line = line.lstrip("-*• ").strip()
        for mark in ("**", "__"):
            line = line.replace(mark, "")
        lines.append(line)
        # Three deep, so a question followed by the options it offers still
        # counts as the closing line.
        if len(lines) == 3:
            break
    for line in lines:
        if not line.endswith("?"):
            continue
        # A long question keeps its end, not its start: the shape of the choice
        # is in the last clause, and the run-up is usually restating the report.
        for stop in (". ", "! "):
            head = line.rfind(stop)
            if head != -1 and len(line) - head <= n:
                line = line[head + len(stop):]
        return clip(line, n)
    return ""


# Words that only say carry on. A prompt made of nothing else steered the head
# without describing it, and standing for a turn that runs for ten minutes it
# would be the whole of what the row said the head was for.
FILLER = {
    "yes", "y", "yeah", "yep", "ya", "ok", "okay", "k", "sure", "fine",
    "right", "good", "great", "perfect", "nice", "no", "nope", "stop",
    "go", "ahead", "continue", "proceed", "carry", "on", "next", "now",
    "do", "it", "this", "that", "them", "all", "both", "ship", "send",
    "please", "thanks", "thank", "you", "and", "then", "sounds", "lets",
    "let", "us", "keep", "going",
}


def bare(said):
    """Is this prompt only telling the head to carry on?

    Word by word, because the shape varies - "yes", "go ahead", "do it", "ok
    ship it" - and a length cutoff alone would throw away short prompts that do
    say something. Anything with a word of its own in it is not bare: "run V14"
    survives on "run" and "v14", which is the whole point.
    """
    words = re.findall(r"[a-z0-9]+", said.lower())
    return bool(words) and len(words) <= 4 and all(w in FILLER for w in words)


def last_prompt(lines):
    """The last thing you typed at this head, out of unparsed transcript lines.

    Parsing a megabyte of JSON per head per refresh to find one string is not
    worth it, so the assistant turns - nearly all of the file, and the big ones
    - are skipped on the raw bytes before anything is decoded.

    What counts is a prompt you wrote. A slash command expands into its own
    instructions and arrives looking exactly like one (isMeta), a subagent's
    brief arrives on the sidechain, and hook output and notifications arrive
    wrapped in tags - none of those are you asking for something.

    And a prompt has to say something. "yes" steered the head without
    describing it, so the search keeps walking back for one that does, and only
    falls back to the confirmation if the window holds nothing else.
    """
    fallback = ""
    for raw in reversed(lines):
        if b'"tool_result"' in raw or b'"type":"assistant"' in raw:
            continue
        try:
            e = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if (e.get("type") != "user" or e.get("isMeta")
                or e.get("isSidechain") or e.get("isCompactSummary")):
            continue
        content = (e.get("message") or {}).get("content")
        if isinstance(content, str):
            said = content
        else:
            said = " ".join(b.get("text", "") for b in (content or [])
                            if isinstance(b, dict) and b.get("type") == "text")
        said = said.strip()
        if not said or said.startswith("<"):
            continue
        if bare(said):
            fallback = fallback or clip(said, TASK_CHARS)
            continue
        return clip(said, TASK_CHARS)
    return fallback


def read_ask(entries, done):
    """One line saying what this head wants from you, or "" if it is not asking.

    Three shapes, in the order they matter: a tool it is holding out for, a
    question it put to you, and failing those, whatever it said last.
    """
    for e in reversed(entries):
        if e.get("type") != "assistant":
            continue
        text = ""
        for blk in (e.get("message") or {}).get("content") or []:
            if not isinstance(blk, dict):
                continue
            if blk.get("type") == "tool_use" and blk.get("id") not in done:
                name, inp = blk.get("name", "?"), blk.get("input")
                if name == "AskUserQuestion" and isinstance(inp, dict):
                    qs = inp.get("questions") or []
                    if qs and isinstance(qs[0], dict):
                        return "asks: " + clip(qs[0].get("question", ""), 160)
                return "wants to " + clip(summarise_tool(name, inp), 160)
            if blk.get("type") == "text" and blk.get("text", "").strip():
                text = blk["text"]
        if text:
            # The question it signs off with, if it signed off with one - a head
            # that ends "want me to do both?" is asking, whatever its state
            # column says, and the same prefix the AskUserQuestion tool gets
            # says so. Otherwise the opening claim, which is the report.
            closing = closing_question(text)
            return "asks: " + closing if closing else first_sentence(text)
    return ""


# ----------------------------------------------------------------- fleet build

def transcript_for(sid, cwd):
    """Where a session's transcript is, across every home we read.

    The guess first - claude names a project directory after the working
    directory - and a walk of the directories if it misses. The walk is what
    carries a head whose cwd we cannot spell: a Windows head's cwd is a Windows
    path, and no amount of replacing slashes turns C:\\Users\\me\\proj into the
    name the Windows side gave that folder. Its transcript is still sat in that
    home's projects directory under its own session id, which is the thing we
    are actually looking for.
    """
    for projects in config.PROJECT_DIRS:
        direct = os.path.join(projects, cwd.replace("/", "-"), sid + ".jsonl")
        if os.path.exists(direct):
            return direct
    for projects in config.PROJECT_DIRS:
        try:
            for proj in os.listdir(projects):
                cand = os.path.join(projects, proj, sid + ".jsonl")
                if os.path.exists(cand):
                    return cand
        except OSError:
            continue
    return None
