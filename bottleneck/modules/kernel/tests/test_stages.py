"""The interface, held to its promises by stages written to break them.

Testing that identity.py works is testing a stage. This file tests the thing
that makes it replaceable: that a stage of your own, from a directory of your
own, runs; that one which lies about its class cannot reach what it was not
given; and that every way a stage can be wrong costs you the stage and never the
request. Four stages here are deliberately broken, which is the only way to know
the containment is real rather than intended.

Nothing here touches the network. The stages are written to a temp directory and
the registry is pointed at it.
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "..")))

from bottleneck.modules.kernel import stages

TMP = tempfile.mkdtemp(prefix="bottleneck-stages-")
MINE = os.path.join(TMP, "mine")
os.makedirs(MINE)

FAILED = []


def check(label, got, want):
    ok = got == want
    print(("  ok   " if ok else "  FAIL ") + label.ljust(58)
          + ("" if ok else f"\n         got  {got!r}\n         want {want!r}"))
    if not ok:
        FAILED.append(label)


def stage(name, body):
    with open(os.path.join(MINE, name + ".py"), "w") as fh:
        fh.write(body)


def using(run, **env):
    """Point the registry at the temp directory with `run` configured."""
    os.environ["BOTTLENECK_KERNEL_STAGES_DIR"] = MINE
    os.environ["BOTTLENECK_KERNEL_STAGES"] = run
    for key, val in env.items():
        os.environ[key] = val
    stages.forget()


def body(system_text="x" * 200, turn="hello"):
    return {"system": [{"type": "text", "text": system_text},
                       {"type": "text", "text": "second",
                        "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": turn}]}


# --- stages that behave -------------------------------------------------
stage("shout", '''
def apply(system, ctx):
    for b in system:
        b["text"] = b["text"].upper()
    return {"judged": True, "blocks": len(system)}
STAGE = {"summary": "upper case", "writes": "prefix", "apply": apply}
''')
stage("sign", '''
def apply(message, ctx):
    message["content"] = message["content"] + " -- signed"
    return {"judged": True}
STAGE = {"summary": "sign the turn", "writes": "tail", "apply": apply}
''')

print("\na stage of your own, from a directory of your own")
using("shout")
b = body()
reports = stages.run(b)
check("it ran, from outside our checkout",
      [n for n, _ in stages.enabled()], ["shout"])
check("and did what it said", b["system"][0]["text"], "X" * 200)
check("its report comes back under its name", reports["shout"]["blocks"], 2)
check("nothing was reported wrong", stages.verdict(reports), [])

print("\norder is the order you configured, not the order on disk")
using("sign,shout")
check("both run", [n for n, _ in stages.enabled()], ["sign", "shout"])
using("shout,sign")
check("and reversing the setting reverses them",
      [n for n, _ in stages.enabled()], ["shout", "sign"])
b = body()
stages.run(b)
check("each still writing only where its class lets it",
      (b["system"][0]["text"], b["messages"][0]["content"]),
      ("X" * 200, "hello -- signed"))

print("\nnone means none - the wrapper as a plain hop")
using("none")
check("nothing runs", stages.enabled(), [])
b = body()
check("and a request goes past untouched", (stages.run(b), b), ({}, body()))


# --- stages that do not ------------------------------------------------
print("\na stage cannot reach what its class was not given")
stage("greedy", '''
def apply(system, ctx):
    # There is no body here to reach into. That is the enforcement: it is not
    # that this is forbidden, it is that it was never handed over.
    return {"judged": True, "got": type(system).__name__,
            "messages_visible": hasattr(system, "get")}
STAGE = {"summary": "tries", "writes": "prefix", "apply": apply}
''')
using("greedy")
got = stages.run(body())["greedy"]
check("a prefix stage is handed the system field and nothing else",
      (got["got"], got["messages_visible"]), ("list", False))

print("\nand every way of being wrong costs the stage, never the request")
stage("reshaper", '''
def apply(system, ctx):
    system.append({"type": "text", "text": "mine"})
    return {"judged": True}
STAGE = {"summary": "adds a block", "writes": "prefix", "apply": apply}
''')
using("reshaper")
b = body()
check("a prefix stage that changes the shape is put down", stages.run(b), {})
check("and the request keeps the shape claude sent", len(b["system"]), 2)
check("with the reason recorded",
      "shape" in stages.broken().get("reshaper", ""), True)

stage("drifter", '''
N = [0]
def apply(system, ctx):
    N[0] += 1
    system[0]["text"] += f" {N[0]}"
    return {"judged": True}
STAGE = {"summary": "impure", "writes": "prefix", "apply": apply}
''')
using("drifter")
b = body()
stages.run(b)
check("a prefix stage that is not a pure function is put down",
      "pure function" in stages.broken().get("drifter", ""), True)

stage("thrower", '''
def apply(system, ctx):
    raise RuntimeError("nope")
STAGE = {"summary": "raises", "writes": "prefix", "apply": apply}
''')
stage("malformed", '''
STAGE = {"summary": "no apply", "writes": "prefix"}
''')
stage("wrongclass", '''
def apply(x, ctx):
    return None
STAGE = {"summary": "bad class", "writes": "everything", "apply": apply}
''')
using("thrower,malformed,wrongclass,missing,shout")
b = body()
reports = stages.run(b)
bad = stages.broken()
check("a stage that raises is put down",
      "RuntimeError" in bad.get("thrower", ""), True)
check("one with no apply never loads",
      "apply must be callable" in str(bad.get("malformed", "")), True)
check("one claiming a class that does not exist never loads",
      "writes must be one of" in str(bad.get("wrongclass", "")), True)
check("one configured and not on disk is named too",
      "no missing.py" in str(bad.get("missing", "")), True)
check("and the good stage after all four still ran",
      b["system"][0]["text"], "X" * 200)
check("four put down, one running", (len(bad), list(reports)),
      (4, ["shout"]))


# --- what a stage says about itself ------------------------------------
print("\nreporting: a gap is a stage saying it could not do its job")
stage("expects", '''
def apply(system, ctx):
    if "marker" in system[0]["text"]:
        return {"judged": True}
    return {"judged": True, "gap": "no marker in the prompt"}
STAGE = {"summary": "expects a marker", "writes": "prefix", "apply": apply}
''')
using("expects")
check("found what it wanted, so nothing is reported",
      stages.verdict(stages.run(body("marker"))), [])
check("did not, so it says which stage and what",
      stages.verdict(stages.run(body("nothing here"))),
      ["expects: no marker in the prompt"])

stage("quiet", '''
def apply(system, ctx):
    return {"chars": len(system[0]["text"])}
STAGE = {"summary": "never judges", "writes": "prefix", "apply": apply}
''')
using("quiet")
check("a stage that did not judge leaves the verdict open, not clean",
      stages.verdict(stages.run(body())), None)

print("\nwhat is available but switched off is as visible as what runs")
using("shout")
seen = {name: (writes, trouble) for name, writes, _, trouble
        in stages.listing()}
check("the running one, with no trouble", seen["shout"], ("prefix", None))
check("and one on disk that was not asked for", seen["sign"],
      ("tail", "not run"))

print("\nall pass" if not FAILED else f"\n{len(FAILED)} FAILED")
raise SystemExit(1 if FAILED else 0)
