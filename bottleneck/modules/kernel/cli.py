"""`bottleneck kernel` - which backend is up, what it runs, what it cost.

Nothing in here knows what a stage does. It asks the registry which are running
and prints what each one reports and explains about itself, so a stage you write
is as visible as the ones shipped - which is the difference between an interface
and a list of special cases.
"""
import copy
import json
import os

from . import proxy
from . import stages
from . import wrap


def usage_rows(path, limit=200):
    try:
        with open(path) as fh:
            lines = fh.readlines()[-limit:]
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


# Claude Code's real opening, one release of it, padded to the length of the
# real thing - a stage that only judges prompts big enough to be real (and they
# should) has nothing to say about a three-line sample.
def sample():
    bulk = ("\nIMPORTANT: ...the rest of claude's own prompt continues here, "
            "untouched, for about this much more...\n" * 90)
    return {
        "system": [
            {"type": "text",
             "text": "You are Claude Code, Anthropic's official CLI for "
                     "Claude.\ncc-billing-header: cc_version=x; "
                     "cc_entrypoint=cli;"},
            {"type": "text",
             "text": "You are an interactive agent that helps users with "
                     "software engineering tasks.",
             "cache_control": {"type": "ephemeral", "ttl": "1h"}},
            {"type": "text", "text": bulk,
             "cache_control": {"type": "ephemeral", "ttl": "1h"}},
        ],
        "messages": [{"role": "user", "content": "what does this repo do?"}],
    }


def show_prompt():
    """What a head would actually be sent, run through the stages configured.

    The claim this command exists to check, and it is checkable rather than
    asserted: every stage runs here exactly as it runs on a live request, so
    what comes back is what a head gets.
    """
    was, now = sample(), sample()
    reports = stages.run(now, {"path": "/v1/messages", "why": "show"})
    running = stages.enabled()
    if not running:
        print("  no stages configured - a head would be sent claude's own "
              f"prompt, unchanged\n  ({stages.CONFIG})")
        return 0
    print(f"  stages: {', '.join(n for n, _ in running)}")

    print(f"\n  system: {len(was['system'])} blocks in, {len(now['system'])} "
          f"out (the shape is never ours to change)")
    for i, (before, after) in enumerate(zip(was["system"], now["system"])):
        same = before.get("cache_control") == after.get("cache_control")
        print(f"\n  --- block {i}: {len(before['text'])} -> "
              f"{len(after['text'])} chars, cache_control "
              f"{'unchanged' if same else 'MOVED - this is a bug'}")
        for line in (after["text"].splitlines() or [""])[:6]:
            print(f"      {line[:96]}")
        extra = len(after["text"].splitlines()) - 6
        if extra > 0:
            print(f"      ... {extra} more lines")

    tail_was = json.dumps(was["messages"][-1])
    tail_now = json.dumps(now["messages"][-1])
    if tail_was != tail_now:
        print(f"\n  --- newest turn: {len(tail_was)} -> {len(tail_now)} chars "
              f"(after the last breakpoint, so it is nobody's prefix)")
        for line in json.loads(tail_now)["content"].splitlines()[:8]:
            print(f"      {line[:96]}")

    print("\n  what each stage said of it")
    for name, stage in running:
        report = reports.get(name) or {}
        told = ", ".join(f"{k}={v}" for k, v in sorted(report.items())
                         if k not in ("gap", "judged")) or "nothing to report"
        print(f"    {name:<10} {stage['writes']:<7} {told}")
        for line in (stage.get("explain") or (lambda: []))():
            print(f"      {line}")

    holes = live_gap()
    for line in stages.gaps(reports):
        if line not in holes:
            holes.append(line)
    for line in holes:
        print(f"\n  FAILING: {line}")
    if any("config.toml" in line for line in holes):
        print(proxy.stanza())
    return 1 if holes else 0


def show_stages():
    """Every stage on disk: what runs, in what order, and what does not."""
    print(f"  {stages.CONFIG}")
    print(f"  BOTTLENECK_KERNEL_STAGES overrides it for one process "
          f"('none' runs the wrapper as a plain hop)\n")
    for name, writes, summary, trouble in stages.listing():
        flag = "  " if trouble is None else "!!"
        print(f"  {flag} {name:<10} {writes:<7} {summary}")
        if trouble:
            print(f"       {trouble}")
    return 0


def live_gap():
    """What the running backend is failing on, as opposed to the sample."""
    from . import chosen
    try:
        return list(chosen().gap())
    except Exception:                   # noqa: BLE001
        return []


def kernel_cmd(cmd, rest):
    from . import backend, chosen
    what = (rest[0] if rest else "").strip().lower()
    which, who = backend(), chosen()

    if what == "show":
        return show_prompt()

    if what in ("stages", "stage"):
        return show_stages()

    if what == "start":
        if who.up():
            print(f"already up on {who.base_url()} ({which})")
            return 0
        print(f"starting the {which} backend ...")
        if who.start(wait=10):
            print(f"up on {who.base_url()}")
            return 0
        print("did not come up")
        if which == "proxy":
            print(f"  see {proxy.ROOT}/log/proxy.log")
        else:
            print(f"  see {os.path.dirname(wrap.LOG)}/wrap.log")
        return 1

    if what and what != "status":
        print(f"unknown: bottleneck kernel {what}")
        print("  status | start | show | stages")
        return 2

    live = who.up()
    print(f"{'up  ' if live else 'down'} {who.base_url()}   backend: {which}")
    if which == "proxy":
        print(f"     {proxy.ROOT}")
        chars = proxy.kernel_chars()
        log = os.path.join(proxy.ROOT, "log", "usage.jsonl")
        if chars:
            print(f"     identity kernel: {chars} chars, replacing the stock "
                  f"block")
    else:
        print(f"     {os.path.dirname(os.path.abspath(wrap.__file__))} "
              f"(in this module, standard library only)")
        running = ", ".join(n for n, _ in stages.enabled()) or "none"
        print(f"     stages: {running}")
        log = wrap.LOG
    holes = live_gap()
    if holes:
        print()
        for line in holes:
            print(f"  !! {line}")
        print("  !! a stage that is configured is not doing what it was "
              "configured to do")
        print("  !! `bottleneck kernel show` runs them and says which")
        print()

    if not live:
        print("     heads opened now would go straight to the API")
        print("     `bottleneck kernel start` brings it up")
        return 1 if holes else 0

    rows = usage_rows(log)
    good = [r for r in rows if r.get("status") == 200]
    if not rows:
        print("     nothing metered yet")
        return 1 if holes else 0
    sent = sum(r.get("input_tokens") or 0 for r in rows)
    back = sum(r.get("output_tokens") or 0 for r in rows)
    made = sum(r.get("cache_creation_input_tokens") or 0 for r in rows)
    read = sum(r.get("cache_read_input_tokens") or 0 for r in rows)
    print(f"     last {len(rows)} requests ({len(good)} ok): {sent:,} in, "
          f"{back:,} out")
    print(f"     cache: {made:,} written, {read:,} read")
    bad = [r for r in rows if r.get("status") and r["status"] >= 400]
    if bad:
        last = bad[-1]
        print(f"     {len(bad)} failed, most recently {last.get('status')} at "
              f"{str(last.get('ts'))[11:19]}")
    return 1 if holes else 0
