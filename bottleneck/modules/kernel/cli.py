"""`bottleneck kernel` - which backend is up, what it injects, what it cost."""
import json
import os

from . import proxy
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


def show_prompt():
    """The system prompt a head would actually receive, rewritten here.

    The claim this command exists to check: that the kernel replaces the stock
    identity rather than arguing with it. Run against the real opening of
    claude's own prompt, so what comes back is what a head would get.
    """
    stock = [
        {"type": "text",
         "text": "You are Claude Code, Anthropic's official CLI for Claude.\n"
                 "cc-billing-header: cc_version=x; cc_entrypoint=cli;"},
        {"type": "text",
         "text": "You are an interactive agent that helps users with software "
                 "engineering tasks.",
         "cache_control": {"type": "ephemeral", "ttl": "1h"}},
        {"type": "text",
         "text": "\nIMPORTANT: ...the rest of claude's own prompt continues "
                 "here, untouched...",
         "cache_control": {"type": "ephemeral", "ttl": "1h"}},
    ]
    got = wrap.rewrite_system(json.loads(json.dumps(stock)), wrap.kernel_text())
    print(f"  {len(stock)} system blocks in, {len(got)} out "
          f"(the shape is never changed - see wrap.py)")
    for i, (was, now) in enumerate(zip(stock, got)):
        same = was.get("cache_control") == now.get("cache_control")
        print(f"\n  --- block {i}: {len(was['text'])} -> {len(now['text'])} "
              f"chars, cache_control "
              f"{'unchanged' if same else 'MOVED - this is a bug'}")
        for line in (now["text"].splitlines() or [""])[:6]:
            print(f"      {line[:96]}")
        if len(now["text"].splitlines()) > 6:
            print(f"      ... {len(now['text'].splitlines()) - 6} more lines")
    return 0


def kernel_cmd(cmd, rest):
    from . import backend, chosen
    what = (rest[0] if rest else "").strip().lower()
    which, who = backend(), chosen()

    if what == "show":
        return show_prompt()

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
        return 2

    live = who.up()
    print(f"{'up  ' if live else 'down'} {who.base_url()}   backend: {which}")
    if which == "proxy":
        print(f"     {proxy.ROOT}")
        chars = proxy.kernel_chars()
        log = os.path.join(proxy.ROOT, "log", "usage.jsonl")
    else:
        print(f"     {os.path.dirname(os.path.abspath(wrap.__file__))} "
              f"(in this module, standard library only)")
        chars = len(wrap.kernel_text())
        log = wrap.LOG
    if chars:
        print(f"     identity kernel: {chars} chars, replacing the stock block")
    if not live:
        print("     heads opened now would go straight to the API")
        print("     `bottleneck kernel start` brings it up")
        return 0

    rows = usage_rows(log)
    good = [r for r in rows if r.get("status") == 200]
    if not rows:
        print("     nothing metered yet")
        return 0
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
    return 0
