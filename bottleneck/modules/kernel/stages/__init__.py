"""stages - the parts of a rewrite, each replaceable, none of them privileged.

A stage is one thing done to a request on its way past: the stock identity
excised, memory recalled into the newest turn, a tool result truncated, a long
prompt compacted by a smaller model. This registry knows them by name and no
more, in the same shape and for the same reasons as the module registry a level
up - lazy import, so a stage that will not load is a line of text rather than a
dead proxy, and `_put_down`, so a stage that misbehaves is dropped instead of
taking the request with it.

The identity rewrite is *not* built in. It is `stages/identity.py`, listed in
stages.json like anything else, and deleting that line turns it off. That is the
test of whether this is an interface or a decoration.

  The two classes, and why the split is not cosmetic

Prompt caching is a byte-prefix match, so what a stage may write decides what it
must promise. The split is enforced by what each kind of stage is handed - a
prefix stage never receives the messages, a tail stage never receives the system
field - so a stage cannot exceed its class by accident or by trying.

  prefix   apply(system, ctx). The cached part: the system blocks. Must be pure
           and shape-preserving, because every request in a session resends it
           and any variation turns a cache read into a cache write. Checked:
           run twice on its first real body, and the result compared block for
           block, cache_control for cache_control.

  tail     apply(message, ctx). The newest message, which sits after the last
           cache breakpoint and is therefore nobody's prefix. Free to be
           impure, slow and fallible - this is where a model call belongs - and
           held only to finishing and to leaving the rest of the turn alone.

A stage answers with a report: None when it did nothing, or a dict, and a
`"gap"` key in that dict means "I expected to do something and did not", which
is what puts the `!` on the status line. See identity.py for why that matters
more than it sounds.

    STAGE = {
        "summary": "one line, for `bottleneck kernel stages`",
        "writes":  "prefix" or "tail",
        "apply":   fn(what, ctx) -> report or None,
    }
"""
import copy
import importlib.util
import json
import os
import sys


HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.environ.get(
    "BOTTLENECK_KERNEL_STAGES_FILE",
    os.path.join(os.path.dirname(HERE), "stages.json"))

WRITES = ("prefix", "tail")

_found = None
_loaded = {}
_broken = {}
_checked = set()


def dirs():
    """Where stages are looked for, in order. Yours first, ours last.

    A stage is loaded from its path rather than imported as part of this
    package, which is the difference between an interface and a plugin
    directory you are allowed to add to: a stage of your own lives wherever you
    keep it, needs nothing of ours, and one named the same as a stage we ship
    replaces it rather than fighting it.
    """
    said = os.environ.get("BOTTLENECK_KERNEL_STAGES_DIR", "").strip()
    mine = [os.path.expanduser(p) for p in said.split(os.pathsep) if p.strip()]
    return mine + [HERE]


def discovered():
    """{name: path} for every stage file findable, wanted or not."""
    global _found
    if _found is not None:
        return _found
    found = {}
    for where in dirs():
        try:
            names = sorted(os.listdir(where))
        except OSError:
            continue
        for name in names:
            if not name.endswith(".py") or name.startswith(("_", ".")):
                continue
            found.setdefault(name[:-3], os.path.join(where, name))
    _found = found
    return _found


def _import(name, path):
    """A stage file, as a module. Raises on anything wrong.

    A stage that lives in this directory is imported as part of this package,
    not loaded again from its path. The difference is not stylistic: loading it
    both ways gives two module objects with the same code and separate state,
    and then the copy the registry runs and the copy anything else imported
    disagree about their own settings. That is a bug you find by watching a
    test change a filename and the running stage ignore it.
    """
    if os.path.dirname(os.path.abspath(path)) == HERE:
        return __import__(f"bottleneck.modules.kernel.stages.{name}",
                          fromlist=["STAGE"])
    spec = importlib.util.spec_from_file_location(f"bnkstage_{name}", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod            # so it is one module, not per-load
    spec.loader.exec_module(mod)
    return mod


def config():
    """{"run": [names in order], "settings": {name: {...}}} - never raises."""
    try:
        with open(CONFIG) as fh:
            got = json.load(fh)
        return got if isinstance(got, dict) else {}
    except (OSError, ValueError):
        return {}


def settings(name):
    got = (config().get("settings") or {}).get(name)
    return got if isinstance(got, dict) else {}


def wanted():
    """The stages to run, in order. Order is the config's, not the disk's.

    Unlike modules, this is a list rather than a set and the default is *not*
    everything found: a stage changes what a session is, and one that starts
    running because it was checked out is a change nobody made. Say what runs.
    """
    said = os.environ.get("BOTTLENECK_KERNEL_STAGES", "").strip()
    if said:
        if said.lower() in ("none", "off", "0", "-"):
            return []
        return [n.strip() for n in said.split(",") if n.strip()]
    run = config().get("run")
    return [str(n) for n in run] if isinstance(run, list) else []


def enabled():
    """[(name, STAGE)] for every wanted stage that loads and is well formed."""
    out = []
    for name in wanted():
        if name in _broken:
            continue
        stage = _loaded.get(name)
        if stage is None:
            try:
                path = discovered().get(name)
                if not path:
                    raise FileNotFoundError(
                        f"no {name}.py in {os.pathsep.join(dirs())}")
                stage = getattr(_import(name, path), "STAGE", None)
                if not isinstance(stage, dict):
                    raise TypeError("no STAGE dict")
                if stage.get("writes") not in WRITES:
                    raise TypeError(f"writes must be one of {WRITES}")
                if not callable(stage.get("apply")):
                    raise TypeError("apply must be callable")
            except Exception as exc:            # noqa: BLE001 - see the docstring
                _broken[name] = f"{type(exc).__name__}: {exc}"
                continue
            _loaded[name] = stage
        out.append((name, stage))
    return out


def forget():
    """Drop what has been loaded, so the next request asks again."""
    global _found
    _found = None
    _loaded.clear()
    _broken.clear()
    _checked.clear()


def _put_down(name, why):
    """Disable a stage and remember why. Never raises.

    The request continues without it. A stage is a thing you added to a session
    and never a reason to fail one - the same rule the module registry keeps,
    for the same reason one level down.
    """
    _broken[name] = why
    _loaded.pop(name, None)


def broken():
    """Stages that were wanted and would not run, with why. Never raises."""
    enabled()
    return dict(_broken)


def shape(system):
    """What a prefix stage may not change: the blocks, and the breakpoints."""
    if not isinstance(system, list):
        return ("str", 0)
    return tuple((b.get("type"), json.dumps(b.get("cache_control"), sort_keys=True))
                 if isinstance(b, dict) else ("?", "")
                 for b in system)


def _prefix(name, stage, body, ctx):
    system = body.get("system")
    if system is None:
        return None
    # Kept so a stage that breaks its promise can be undone as well as put
    # down. Detecting the violation and forwarding the damage anyway would be
    # the worst of both: a 400 from the API, or worse a session whose cache is
    # quietly being rewritten, with a line in a log to explain it afterwards.
    keep = copy.deepcopy(system)
    twice = copy.deepcopy(system) if name not in _checked else None
    try:
        report = stage["apply"](system, ctx)
        if shape(system) != shape(keep):
            raise ValueError("changed the shape of the system field - the "
                             "blocks and their cache_control are claude's, "
                             "not ours")
        if twice is not None:
            _checked.add(name)
            stage["apply"](twice, ctx)
            if json.dumps(twice) != json.dumps(system):
                raise ValueError("is not a pure function of its input - the "
                                 "same bytes in gave different bytes out, "
                                 "which turns every cache read in the session "
                                 "into a write")
    except Exception:
        body["system"] = keep
        raise
    return report


def _tail(name, stage, body, ctx):
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return None
    last = messages[-1]
    if not isinstance(last, dict):
        return None
    keep = copy.deepcopy(last)
    try:
        return stage["apply"](last, ctx)
    except Exception:
        messages[-1] = keep
        raise


def run(body, ctx=None):
    """Every wanted stage over `body`, in order. Returns {name: report}.

    `body` is modified in place. Nothing here raises: a stage that throws, or
    that breaks the promise its class makes, is put down and the request goes on
    without it, carrying whatever the stages before it already did.
    """
    ctx = dict(ctx or {})
    reports = {}
    for name, stage in enabled():
        run_it = _prefix if stage["writes"] == "prefix" else _tail
        try:
            report = run_it(name, stage, body, dict(ctx, stage=name))
        except Exception as exc:                # noqa: BLE001 - see _put_down
            _put_down(name, f"disabled after it {exc}"
                            if isinstance(exc, ValueError)
                            else f"disabled after raising {type(exc).__name__}:"
                                 f" {exc}")
            continue
        if report:
            reports[name] = report
    return reports


def gaps(reports):
    """['name: what it expected and did not find'] out of a run's reports."""
    out = []
    for name, report in sorted((reports or {}).items()):
        got = report.get("gap") if isinstance(report, dict) else None
        for line in ([got] if isinstance(got, str) else (got or [])):
            out.append(f"{name}: {line}")
    return out


def verdict(reports):
    """The gaps, or None when no stage was in a position to judge this request.

    The distinction is not pedantry. Claude Code's startup quota check carries
    almost no system prompt, and a stage looking at one has nothing to say about
    whether it is working - so a stage says `judged` when it actually looked.
    Without that, one probe would read as "everything is fine" and wipe a real
    failure off the status line.
    """
    if not any(isinstance(r, dict) and r.get("judged")
               for r in (reports or {}).values()):
        return None
    return gaps(reports)


def describe(name):
    """(writes, summary) for a stage on disk without running it. For the CLI.

    Imports it, which is why nothing on the request path calls this: a stage
    you have chosen not to run should not cost you its import on every turn.
    """
    path = discovered().get(name)
    if not path:
        return "?", "not on disk"
    try:
        stage = getattr(_import(name, path), "STAGE", {}) or {}
        return stage.get("writes", "?"), stage.get("summary", "")
    except Exception as exc:                    # noqa: BLE001
        return "?", f"will not import: {type(exc).__name__}: {exc}"


def listing():
    """[(name, writes, summary, trouble)] for every stage on disk.

    Ordered as configured, then whatever else is checked out - so what is
    available but switched off is as visible as what is running. `trouble` is
    None for a stage that is running fine.
    """
    order = wanted()
    known = dict(enabled())
    bad = broken()
    out = []
    for name in order:
        stage = known.get(name)
        if stage:
            out.append((name, stage["writes"], stage.get("summary", ""), None))
        else:
            out.append((name, "-", "", bad.get(name, "not on disk")))
    for name in sorted(discovered()):
        if name not in order:
            writes, summary = describe(name)
            out.append((name, writes, summary, "not run"))
    return out
