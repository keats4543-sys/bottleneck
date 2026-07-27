"""What the machine says is running: /proc, and Claude's own list."""
import json
import os
import signal
import subprocess
import time

from . import config
from .config import AGENTS_TTL, SESSIONS


# ------------------------------------------------------------------ process info

def _stat_fields(pid):
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            data = fh.read()
        return data[data.rindex(b")") + 2:].split()
    except (OSError, ValueError, IndexError):
        return None


def proc_start(pid):
    f = _stat_fields(pid)
    try:
        return f[19].decode() if f else None
    except IndexError:
        return None


def ppid_of(pid):
    f = _stat_fields(pid)
    try:
        return int(f[1]) if f else None
    except (IndexError, ValueError):
        return None


def ancestors(pid, limit=25):
    chain, cur = [], pid
    for _ in range(limit):
        cur = ppid_of(cur)
        if not cur or cur <= 1:
            break
        chain.append(cur)
    return chain


def tty_of(pid):
    try:
        fd = os.readlink(f"/proc/{pid}/fd/0")
        return fd.replace("/dev/", "") if fd.startswith("/dev/pts") else None
    except OSError:
        return None


def claude_procs():
    """Every claude-ish process on the box, straight from /proc.

    The safety net for a head that has lost its session file: if it is running,
    it shows up here even when nothing else knows about it.
    """
    known = set()
    try:
        for fname in os.listdir(SESSIONS):
            if fname.endswith(".json"):
                try:
                    with open(os.path.join(SESSIONS, fname)) as fh:
                        known.add(json.load(fh).get("pid"))
                except (OSError, ValueError):
                    pass
    except OSError:
        pass

    rows = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                cmd = fh.read().replace(b"\0", b" ").decode(errors="replace").strip()
        except OSError:
            continue
        if not cmd:
            continue
        looks_claude = ("/.local/share/claude/versions/" in cmd
                        or cmd.startswith("claude ") or cmd == "claude"
                        or "/.local/bin/claude" in cmd)
        if not looks_claude:
            continue
        if " bg-pty-host " in cmd or cmd.startswith("claude bg-pty-host"):
            role = "pty-host"
        elif " bg-spare " in cmd:
            role = "spare"
        elif " daemon run" in cmd:
            role = "daemon"
        elif pid in known:
            role = "head"
        else:
            role = "orphan?"
        rows.append({"pid": pid, "role": role, "tracked": pid in known,
                     "cmd": cmd[:140]})
    rows.sort(key=lambda r: (r["role"], r["pid"]))
    return rows


_agents = {"at": 0.0, "rows": []}


def agents_json():
    """`claude agents --json` - the supported list of running heads.

    It carries pid, cwd, kind, name, sessionId and status: everything the
    session files under ~/.claude/sessions carry, from an interface meant to be
    read. It is not the first choice only because it costs about a second - a
    whole node startup - against a fraction of a millisecond for the files, and
    the dashboard redraws every second.

    So the files are the fast path and this is what happens when they are not
    there: a layout change, a version that stops writing them, a permissions
    problem. Cached, because the fallback should not cost a second a redraw.
    """
    if time.time() - _agents["at"] < AGENTS_TTL:
        return _agents["rows"]
    rows = []
    try:
        r = subprocess.run(["claude", "agents", "--json"],
                           capture_output=True, text=True, timeout=20)
        if r.returncode == 0:
            got = json.loads(r.stdout or "[]")
            rows = [x for x in got if isinstance(x, dict)]
    except (OSError, ValueError, subprocess.SubprocessError):
        rows = []
    _agents["at"], _agents["rows"] = time.time(), rows
    return rows


# The key we add to a record that did not come out of our own home.
#
# It means: the pid on this record belongs to another machine's numbering. Our
# /proc has never heard of it, and the number is not free either - some local
# process may well be wearing it - so anything that would read it as a local pid
# has to look here first. Liveness, the walk up the tree to find a pane, and
# above all the signal in kill_head.
FOREIGN = "_foreign"


def session_records():
    """Every head Claude Code is running, best source first.

    Every home, not just ours: under WSL the claude you launched may be the
    Windows one, whose files are over on the mounted drive. Ours is read first
    so a duplicate - the same session id turning up in both, which a symlinked
    or shared home would do - is kept as the local copy, whose pid we can
    actually use.
    """
    out, seen = [], set()
    for at, where in enumerate(config.SESSION_DIRS):
        try:
            files = os.listdir(where)
        except OSError:
            continue
        for fname in files:
            if not fname.endswith(".json"):
                continue
            try:
                with open(os.path.join(where, fname)) as fh:
                    got = json.load(fh)
            except (OSError, ValueError):
                continue
            if not isinstance(got, dict):
                continue
            sid = got.get("sessionId") or ""
            if sid and sid in seen:
                continue
            if sid:
                seen.add(sid)
            if at:
                got[FOREIGN] = where
            out.append(got)
    if out:
        return out
    # Nothing on disk. Either there are genuinely no heads - in which case this
    # returns an empty list too and costs one call every 20s - or the files have
    # moved, and this keeps the dashboard working through it.
    return agents_json()


def kill_pid(pid, label="process"):
    import signal
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return f"{label} {pid} was already gone"
    except PermissionError:
        return f"not allowed to kill {pid}"
    for _ in range(20):
        time.sleep(0.2)
        if not os.path.isdir(f"/proc/{pid}"):
            return f"killed {label} {pid}"
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
    time.sleep(0.5)
    return (f"killed {label} {pid} (needed SIGKILL)"
            if not os.path.isdir(f"/proc/{pid}")
            else f"{label} {pid} will not die - check it by hand")
