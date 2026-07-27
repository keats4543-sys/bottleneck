"""Building the layout, with tmux faked.

`bottleneck start` is the one command a test cannot simply run - it ends in
attach or switch-client, which needs a terminal. So it never ran here, and a
missing import in it reached a user instead. It runs now, against a fake tmux,
which is enough to walk every line of it.
"""
import os
import sys

from harness import bn as m

CALLS = []
PANES = [""]          # what dash_pane() should say
HAS_SESSION = [True]  # what `tmux has-session` should report


def fake_tmux(*args, **kw):
    CALLS.append(args)
    if args and args[0] == "has-session" and not HAS_SESSION[0]:
        return None          # tmux(check=True) returns None when it fails
    return type("R", (), {"returncode": 0})()


def fake_tmux_out(*args):
    CALLS.append(args)
    return "%99"


m.tmux = fake_tmux
m.tmux_out = fake_tmux_out
m.dash_pane = lambda: PANES[0]
m.tmux_say = lambda text: CALLS.append(("say", text))

FAILED = []


def check(label, got, want):
    ok = got == want
    print(("  ok   " if ok else "  FAIL ") + label.ljust(48)
          + ("" if ok else f"got {got!r}, wanted {want!r}"))
    if not ok:
        FAILED.append(label)


def ran(verb):
    return [a for a in CALLS if a and a[0] == verb]


def under(env):
    """Run start() with the attach step stubbed, and report the tmux calls."""
    CALLS.clear()
    real_exec, real_env = os.execvp, dict(os.environ)
    os.execvp = lambda *a: CALLS.append(("execvp",) + a)
    if env:
        os.environ["TMUX"] = env
    else:
        os.environ.pop("TMUX", None)
    try:
        return m.start()
    finally:
        os.execvp = real_exec
        os.environ.clear()
        os.environ.update(real_env)


print("\nno session yet")
PANES[0] = ""
HAS_SESSION[0] = False
check("returns cleanly", under("x"), 0)
check("makes the session", bool(ran("new-session")), True)
check("and does not also add a window", ran("new-window"), [])
check("marks the pane as the dashboard",
      any(a[:2] == ("set", "-p") and m.ROLE in a for a in CALLS), True)

print("\nsession up, but no live dashboard in it")
PANES[0] = ""
HAS_SESSION[0] = True
under("x")
check("builds one in its own window", bool(ran("new-window")), True)
check("and reads the pane id back rather than guessing",
      any("-P" in a and "#{pane_id}" in a for a in CALLS), True)

print("\nsession up with a dashboard already")
PANES[0] = "%5"
under("x")
check("does not build a second one", ran("new-window"), [])
check("inside tmux it switches the client", bool(ran("switch-client")), True)

print("\nfrom outside tmux")
PANES[0] = "%5"
under("")
check("it attaches instead", bool(ran("execvp")), True)

print("\nall pass" if not FAILED else f"\n{len(FAILED)} FAILED")
sys.exit(1 if FAILED else 0)
