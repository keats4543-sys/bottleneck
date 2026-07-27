"""Which key does what, from ~/.bottleneck/config.

The keys lived in three places that had to agree and could not be told
otherwise: a tracked tmux.conf, the fast bindings a running dashboard installs
against its own pane, and the slow ones it puts back when it stands down.

They are one list now. These tests hold that list to three things: the defaults
are exactly what was bound before any of this was configurable, a config file
really does change every one of the three, and a key taken out of the config
comes off the keyboard rather than surviving in tmux.conf.

No tmux runs here - the generated file is checked as text. tests/run.sh has
already proved the same file parses in a real tmux.
"""
import os

from harness import bn as m

FAILED = []


def check(label, got, want):
    ok = got == want
    print(("  ok   " if ok else "  FAIL ") + label.ljust(52)
          + ("" if ok else f"got {got!r}, wanted {want!r}"))
    if not ok:
        FAILED.append(label)


def with_config(**settings):
    """Rebuild the key table as if these were in ~/.bottleneck/config."""
    was = {k: os.environ.get(k) for k in settings}
    try:
        for k, v in settings.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return {act: m.config._keys(act.upper(), spec)
                for act, spec in m.config.KEY_DEFAULTS.items()}
    finally:
        for k, v in was.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


print("\nthe defaults are what was bound before any of this")
check("the go-on key, all three ways",
      m.config.KEY_DEFAULTS["next"], "M-j,M-a,prefix:a")
check("send-and-go, all three ways",
      m.config.KEY_DEFAULTS["sendgo"], "M-Enter,prefix:M-Enter,prefix:Enter")
check("and nothing has been quietly dropped",
      sorted(m.config.KEY_DEFAULTS),
      ["dash", "left", "next", "reload", "right", "sendgo", "start", "swap"])

print("\na key spec says which table it belongs in")
check("a bare key is no-prefix, so it works mid-typing",
      m.config._keys("__x__", "M-j"), [("M-j", "root")])
check("prefix: puts it behind the prefix",
      m.config._keys("__x__", "prefix:a"), [("a", "prefix")])
check("several, in the order you wrote them",
      m.config._keys("__x__", "prefix:Enter, M-'"),
      [("Enter", "prefix"), ("M-'", "root")])
check("an empty value binds nothing at all",
      m.config._keys("__x__", ""), [])
# tmux has no key with a colon in it today. Guessing it never will is not worth
# the line it saves, so anything that is not "prefix:" stays part of the key.
check("only prefix: is a table, not any colon",
      m.config._keys("__x__", "C-:"), [("C-:", "root")])

print("\nthe config file changes it")
keys = with_config(BOTTLENECK_KEY_SENDGO="prefix:Enter,M-'")
check("send-and-go moves where you put it",
      keys["sendgo"], [("Enter", "prefix"), ("M-'", "root")])
check("and nothing else moves with it",
      keys["next"], [("M-j", "root"), ("M-a", "root"), ("a", "prefix")])
keys = with_config(BOTTLENECK_KEY_SENDGO="")
check("emptying it binds nothing", keys["sendgo"], [])

print("\nquotes come off in pairs, or not at all")
# The key for "sent, next" on a terminal that has taken Alt+Enter is very likely
# to be M-'. Stripping quotes one end at a time ate the last character of it,
# and tmux answered "unknown key: M-".
here = os.path.join(os.environ["BOTTLENECK_STATE"], "config")
os.makedirs(os.path.dirname(here), exist_ok=True)
with open(here, "w") as fh:
    fh.write("BOTTLENECK_KEY_SENDGO=M-'\nBOTTLENECK_KEY_DASH=\"M-d\"\n")
was_conf, m.config.CONFIG = m.config.CONFIG, here
for k in ("BOTTLENECK_KEY_SENDGO", "BOTTLENECK_KEY_DASH"):
    os.environ.pop(k, None)
try:
    m.config.load_config()
    check("a trailing quote that opens nothing is part of the key",
          os.environ.get("BOTTLENECK_KEY_SENDGO"), "M-'")
    check("a matched pair still comes off",
          os.environ.get("BOTTLENECK_KEY_DASH"), "M-d")
finally:
    m.config.CONFIG = was_conf
    for k in ("BOTTLENECK_KEY_SENDGO", "BOTTLENECK_KEY_DASH"):
        os.environ.pop(k, None)

print("\nall three binding sets follow the same list")
was = m.config.KEYS
m.config.KEYS = with_config(BOTTLENECK_KEY_NEXT="C-Space",
                            BOTTLENECK_KEY_SENDGO="M-'")
try:
    fast = m.dash_keys("%9")
    check("the fast keys the dashboard installs",
          [k for k, _, _ in fast if "send-keys" in _ or True][:1], ["C-Space"])
    check("aimed at its own pane",
          fast[0][2], "send-keys -t %9 j")
    slow = m._fallback_keys("/opt/bn/bottleneck")
    check("the slow ones it puts back",
          [k for k, _, _ in slow][:1], ["C-Space"])
    check("by path, not by name",
          slow[0][2], "run-shell -b '/opt/bn/bottleneck next --jump'")

    conf = m.keys_conf("/opt/bn/bottleneck")
    check("and the file tmux reads before any dashboard is up",
          'bind -n "C-Space" run-shell -b "/opt/bn/bottleneck next --jump"' in conf,
          True)

    print("\nthe generated file is authoritative, not additive")
    # tmux.conf binds the defaults so a tmux that has never run bottleneck still
    # works. Without clearing them first, taking a key out of the config would
    # leave it bound - and handing Alt+Enter back to your terminal would not.
    for key in ("M-j", "M-a", "M-Enter", "M-d", "M-o", "M-Left"):
        if f'unbind -q -n "{key}"' not in conf:
            check(f"{key} is cleared before anything is bound", False, True)
            break
    else:
        check("every default is cleared before anything is bound", True, True)
    check("including the prefix ones", 'unbind -q "a"' in conf, True)
    check("the key we removed stays gone",
          'bind -n "M-j"' in conf, False)

    print("\nkey names are quoted, because one of them opens a string")
    check("M-' survives into the file",
          '''bind -n "M-'"''' in conf, True)
    check("and so do the ordinary ones",
          'bind -n "M-d"' in conf, True)
finally:
    m.config.KEYS = was

print("\nwriting it out")
dest = os.path.join(os.environ["BOTTLENECK_STATE"], "keys-test.conf")
check("it lands where it says", m.write_keys_conf(dest), dest)
check("and says the same thing", open(dest).read(), m.keys_conf())
check("a path it cannot write is not a crash",
      m.write_keys_conf("/proc/nope/keys.conf"), "")

print()
print("all pass" if not FAILED else f"{len(FAILED)} FAILED")
raise SystemExit(1 if FAILED else 0)
