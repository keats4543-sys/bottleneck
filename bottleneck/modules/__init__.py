"""Modules: whole features that plug in, so that carrying one costs no edits.

The problem this exists for is branches. A feature like the group boards is
1,600 lines in files of its own and about 200 lines scattered across six files
that were already there - a line of dispatch in cli.py, a call in the dashboard
loop, hook wiring in install.sh, a name in the test harness. Those 200 lines are
nothing to write and everything to merge: two branches each carrying a feature
touch the same six files in the same places, and neither one can be taken
without taking the argument.

So the core learns about extension points instead of about features. Each point
is one generic line that asks every enabled module the same question, and a
module is a directory under here that answers some of them:

    bottleneck/modules/<name>/__init__.py   with a MODULE dict

    MODULE = {
      "summary":  one line, for `bottleneck modules`

      # what it adds to the program
      "commands": {"name": fn(cmd, rest) -> exit code}   new CLI commands
      "usage":    text appended to `bottleneck --help`
      "state":    directories under ~/.bottleneck it wants created

      # what it puts on the screen - content out
      "mark":     fn(head) -> str    a badge on that head's row
      "lines":    fn(head, width) -> [str]   lines under that head
      "group":    fn(gid, label, heads) -> [str]   lines under a group heading
      "status":   fn(heads) -> str   a segment of the tmux status line

      # what it hears - messages in
      "pass":     fn(heads) called on every dashboard refresh
      "notice":   fn(kind, info) told when a head or a group changes
      "keys":     {"z": fn(head, heads) -> note}   keys in the dashboard
      "key_help": text appended to the `?` line
      "ctl":      {"verb": fn(arg, heads) -> (note, problem)}   the control fifo

      # what claude tells it directly
      "hook":     a file in the module directory to install as a claude hook
      "events":   [(event, matcher)] it wants that hook wired to, or a
                  function returning them - a module decides its own wiring
    }

Every key is optional. Adding a module means adding a directory; it means no
edit to any file outside it, which is the whole point. Two branches carrying two
modules merge as two directories.

The four groups are the four ways a feature can reach out of its directory: it
can add to the program, it can put content on the screen, it can be told what
the core is seeing and what you pressed, and it can be handed what claude says.
A module that wants none of them is a directory nobody notices.

The rule in the other direction is the one that keeps this honest: **a module
may import the core, and the core may never import a module** - only this
registry, which knows them by name and no more. Nothing here imports a module
at import time either: a module that will not load is a module that says so on
one line of `bottleneck modules`, never a dashboard that will not start.
"""
import os


HERE = os.path.dirname(os.path.abspath(__file__))


_found = None


def discovered():
    """Every module directory in this checkout, whether or not it is wanted.

    Answered once and remembered. Some of the questions below are asked per
    head per frame, and a directory listing behind each of those would be a
    syscall storm for an answer that changes when you edit the checkout - which
    is what `R` and forget() are for.
    """
    global _found
    if _found is not None:
        return _found
    try:
        names = sorted(os.listdir(HERE))
    except OSError:
        return []
    _found = [n for n in names
              if not n.startswith(("_", "."))
              and os.path.isfile(os.path.join(HERE, n, "__init__.py"))]
    return _found


def wanted():
    """Which modules to load. Everything in the checkout unless you say else.

    A branch that carries a module is a branch that wants it - that is what
    having checked it out means - so the default is on and the setting is for
    turning one off. `BOTTLENECK_MODULES=none` runs the core alone, which is
    the first thing to try when something is behaving oddly.
    """
    said = os.environ.get("BOTTLENECK_MODULES", "").strip()
    if not said:
        return None                       # everything found
    if said.lower() in ("none", "off", "0", "-"):
        return set()
    return {n.strip() for n in said.split(",") if n.strip()}


_loaded = {}

_broken = {}


def enabled():
    """(name, MODULE) for every module that is wanted and imports cleanly.

    Imported on first use rather than at import of this file, so that the cost
    of a module is paid by the process that asks for it - and so that a module
    that raises on import is a line of text rather than a dead dashboard.
    """
    want = wanted()
    out = []
    for name in discovered():
        if want is not None and name not in want:
            continue
        if name in _broken:
            continue
        mod = _loaded.get(name)
        if mod is None:
            try:
                pkg = __import__(f"bottleneck.modules.{name}", fromlist=["MODULE"])
                mod = getattr(pkg, "MODULE", None)
                if not isinstance(mod, dict):
                    raise TypeError("no MODULE dict")
            except Exception as exc:                  # noqa: BLE001 - see above
                _broken[name] = f"{type(exc).__name__}: {exc}"
                continue
            _loaded[name] = mod
        out.append((name, mod))
    return out


def forget():
    """Drop what has been loaded, so the next question asks again.

    For tests, and for a dashboard restarting itself after `R`: a module that
    was disabled for throwing gets one more chance when you reload, which is
    what you were reloading for.
    """
    global _found
    _found = None
    _loaded.clear()
    _broken.clear()
    _points.clear()
    _last.clear()


# What each module answers at each extension point, worked out once. The
# alternative is walking every module's dict on every head of every frame.
_points = {}


def providers(point):
    """[(name, value)] for every enabled module that answers to `point`."""
    got = _points.get(point)
    if got is None:
        got = [(name, mod[point]) for name, mod in enabled() if mod.get(point)]
        _points[point] = got
    return got


def _put_down(name, exc, where):
    """Disable a module that raised, and remember why. Never raises itself.

    The same answer everywhere, because the reason is the same everywhere: all
    of this runs inside the dashboard's redraw, and a feature is never worth
    the list it is drawn on.
    """
    _broken[name] = f"disabled after {where} raised: {exc}"
    _loaded.pop(name, None)
    _points.clear()


def broken():
    """Modules that were wanted and would not load, with why. Never raises."""
    enabled()
    return dict(_broken)


def commands():
    """Every CLI command the modules add, as {name: fn(cmd, rest)}.

    First module wins a clash, and the core wins over all of them - cli.py
    looks here only after it has failed to recognise the command itself, so a
    module can never take `kill` away from you.
    """
    out = {}
    for _, mod in enabled():
        for name, fn in (mod.get("commands") or {}).items():
            out.setdefault(name, fn)
    return out


def usage():
    """The help text the modules contribute, in module order."""
    return [str(mod["usage"]).rstrip() for _, mod in enabled() if mod.get("usage")]


# ------------------------------------------------------- content, on the screen
#
# Everything below is called while a frame is being built, which sets both
# rules. It is bounded: a module hands back text, never escapes and never a
# layout, and what it hands back is cut to a size the list can carry - a
# feature is not allowed to take the columns the list is made of. And it is
# contained: a module that raises is put down between one frame and the next,
# because the list has to be drawn either way.

MARK_MAX = 12          # a badge is a glance, not a sentence
LINES_MAX = 3          # per module, per head
STATUS_MAX = 32        # per module, in the tmux status line


def _clean(text, cap):
    """One line of plain text, cut to size. No escapes, no newlines, ever."""
    out = "".join(ch for ch in str(text) if ch >= " " or ch == "\t")
    out = out.replace("\t", " ").strip()
    return out[:cap]


def mark(head):
    """The badges the modules want on this head's row, joined. "" for none."""
    out = []
    for name, fn in providers("mark"):
        try:
            got = _clean(fn(head) or "", MARK_MAX)
        except Exception as exc:                      # noqa: BLE001
            _put_down(name, exc, "a row mark")
            continue
        if got:
            out.append(got)
    return " ".join(out)


def lines(head, width=100):
    """The lines the modules want under this head, in module order."""
    out = []
    for name, fn in providers("lines"):
        try:
            got = fn(head, width) or []
            got = [got] if isinstance(got, str) else list(got)
        except Exception as exc:                      # noqa: BLE001
            _put_down(name, exc, "a head line")
            continue
        for line in got[:LINES_MAX]:
            line = _clean(line, max(8, width - 4))
            if line:
                out.append(line)
    return out


def group_lines(gid, label, heads, width=100):
    """The lines the modules want under a group's heading."""
    out = []
    for name, fn in providers("group"):
        try:
            got = fn(gid, label, heads) or []
            got = [got] if isinstance(got, str) else list(got)
        except Exception as exc:                      # noqa: BLE001
            _put_down(name, exc, "a group line")
            continue
        for line in got[:LINES_MAX]:
            line = _clean(line, max(8, width - 4))
            if line:
                out.append(line)
    return out


def status(heads):
    """Segments for the tmux status line, already safe to hand to tmux.

    tmux reads `#` as the start of a format, so a module's text is escaped
    rather than trusted: a status segment is a fact about your heads, and a
    module should not be able to turn one into a tmux directive by accident.
    """
    out = []
    for name, fn in providers("status"):
        try:
            got = _clean(fn(heads) or "", STATUS_MAX)
        except Exception as exc:                      # noqa: BLE001
            _put_down(name, exc, "the status line")
            continue
        if got:
            out.append(got.replace("#", "##"))
    return out


# --------------------------------------------------------- messages, coming in


def keys():
    """{key: fn(head, heads) -> note} the modules want in the dashboard.

    Consulted only after every key the core knows, so a module can add a key
    and can never take one - the same rule as commands, for the same reason.
    """
    out = {}
    for _, table in providers("keys"):
        for key, fn in (table or {}).items():
            out.setdefault(str(key), fn)
    return out


def key_help():
    """What the modules want said about their keys on the `?` line."""
    return [_clean(mod["key_help"], 60) for _, mod in enabled()
            if mod.get("key_help")]


def press(key, head, heads):
    """Run a module's key. Returns the note to show, or None if nobody owns it.

    None and "" are different answers: a module can own a key and have nothing
    to say about it, and the frame still has to be drawn again - whatever it
    did is on the list, not in a note about the list.
    """
    fn = keys().get(key)
    if not fn:
        return None
    name = next((n for n, table in providers("keys") if key in (table or {})), "")
    try:
        return _clean(fn(head, heads) or "", 200)
    except Exception as exc:                          # noqa: BLE001
        _put_down(name, exc, f"the {key} key")
        return f"{name or 'a module'} failed on {key} - see `bottleneck modules`"


def ctl_verbs():
    """{verb: fn(arg, heads)} the modules want on the control fifo.

    The fifo is how something outside the dashboard - a tmux binding, a script,
    another head - says a word to it. A module can be spoken to the same way.
    """
    out = {}
    for _, table in providers("ctl"):
        for verb, fn in (table or {}).items():
            out.setdefault(str(verb), fn)
    return out


def ctl(verb, arg, heads):
    """Run a module's control verb. Returns (note, problem), or None if none."""
    fn = ctl_verbs().get(verb)
    if not fn:
        return None
    name = next((n for n, table in providers("ctl") if verb in (table or {})), "")
    try:
        got = fn(arg, heads)
    except Exception as exc:                          # noqa: BLE001
        _put_down(name, exc, f"the {verb} verb")
        return f"{name or 'a module'} failed on {verb}", True
    if isinstance(got, tuple):
        note, problem = (list(got) + [False])[:2]
        return _clean(note or "", 200), bool(problem)
    return _clean(got or "", 200), False


def on_pass(heads):
    """Tell every module that the dashboard has just looked at the fleet.

    Called from the redraw loop, so it has to be cheap and it has to be safe: a
    module that throws in here would take the dashboard down on a timer, which
    is the one thing a dashboard must not do. It is disabled instead, and says
    so under `bottleneck modules`.
    """
    for name, fn in providers("pass"):
        try:
            fn(heads)
        except Exception as exc:                      # noqa: BLE001 - see above
            _put_down(name, exc, "a refresh")
    _notice(heads)


# What the fleet looked like on the pass before, for working out what changed.
_last = {}


def _notice(heads):
    """Work out what changed since the last pass, and say so once.

    Every module wanting to know when a head appears or joins a group would
    otherwise keep its own copy of the last fleet and diff it - the same loop
    written as many times as there are modules, each of them subtly different
    about what counts as a change. It is one diff, here, and what comes out of
    it is:

        head.new     a head we had not seen        {head}
        head.gone    one that is not there now     {session_id, head}
        head.state   WAITING -> BLOCKED and so on  {head, was}
        group.join   moved group, or left one      {head, group, was}
        group.name   a group was renamed           {group, label, was}

    The first pass says nothing. Everything is new to a dashboard that has just
    started, and a module woken with twelve head.new for heads that have been
    running since yesterday would be hearing about its own startup, not about
    the fleet.
    """
    seen = {h.get("session_id") or "": {
        "state": h.get("state"), "group": h.get("group", ""),
        "label": h.get("group_label", ""), "head": h} for h in heads
        if h.get("session_id")}
    first, was = not _last, dict(_last)
    _last.clear()
    _last.update({sid: {k: v for k, v in row.items() if k != "head"}
                  for sid, row in seen.items()})
    if first:
        return
    told = []
    labels = {}
    for sid, row in seen.items():
        old = was.get(sid)
        if old is None:
            told.append(("head.new", {"head": row["head"]}))
            continue
        if old["state"] != row["state"]:
            told.append(("head.state", {"head": row["head"], "was": old["state"]}))
        if old["group"] != row["group"]:
            told.append(("group.join", {"head": row["head"],
                                        "group": row["group"], "was": old["group"]}))
        if row["group"] and old["label"] != row["label"]:
            labels[row["group"]] = (row["label"], old["label"])
    for sid, old in was.items():
        if sid not in seen:
            told.append(("head.gone", {"session_id": sid, "head": None}))
    for gid, (label, before) in labels.items():
        told.append(("group.name", {"group": gid, "label": label, "was": before}))
    if not told:
        return
    for name, fn in providers("notice"):
        for kind, info in told:
            try:
                fn(kind, info)
            except Exception as exc:                  # noqa: BLE001
                _put_down(name, exc, f"notice of {kind}")
                break


def wiring():
    """What each module wants installed as a claude hook, for install.sh.

    A list of {name, hook, events}: `hook` is a path in the checkout, `events`
    are (event, matcher) pairs. The installer knows nothing about what any of
    them are for - a module decides its own wiring, including deciding it
    wants none today because of how it is configured.
    """
    out = []
    for name, mod in enabled():
        hook = mod.get("hook")
        if not hook:
            continue
        events = mod.get("events") or []
        try:
            events = events() if callable(events) else events
        except Exception:                             # noqa: BLE001
            continue
        pairs = [[str(ev), (m or None)] for ev, m in events if ev]
        if not pairs:
            continue
        path = hook if os.path.isabs(hook) else os.path.join(HERE, name, hook)
        if os.path.isfile(path):
            out.append({"name": name, "hook": path, "events": pairs})
    return out


def state_dirs():
    """Directories under ~/.bottleneck the modules want made at install."""
    out = []
    for _, mod in enabled():
        for d in mod.get("state") or []:
            out.append(str(d))
    return out


def listing():
    """One line per module, for `bottleneck modules`. Loads nothing new."""
    want = wanted()
    rows = []
    for name in discovered():
        on = want is None or name in want
        mod = dict(enabled()).get(name) if on else None
        why = _broken.get(name)
        rows.append({
            "name": name,
            "on": bool(mod) and not why,
            "why": why or ("" if on else "not in BOTTLENECK_MODULES"),
            "summary": (mod or {}).get("summary", "") if mod else "",
        })
    return rows
