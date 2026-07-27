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
    # Which pid a session file names, and what it says that process's start
    # time was. The pid alone is not enough to call a process tracked: a file
    # is named after the pid that wrote it and stays there after that process
    # goes, so the number gets handed out again and the next claude to wear it
    # would be read as the head that file describes. procStart is what tells
    # the two apart - a process that has been running since the file was
    # written, or a different one wearing the number.
    known = {}
    try:
        for fname in os.listdir(SESSIONS):
            if fname.endswith(".json"):
                try:
                    with open(os.path.join(SESSIONS, fname)) as fh:
                        rec = json.load(fh)
                    if isinstance(rec, dict) and isinstance(rec.get("pid"), int):
                        known[rec["pid"]] = rec
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
        role = None
        if " bg-pty-host " in cmd or cmd.startswith("claude bg-pty-host"):
            role = "pty-host"
        elif " bg-spare " in cmd:
            role = "spare"
        elif " daemon run" in cmd:
            role = "daemon"
        # A file naming this pid is not enough - it has to be naming this
        # process. Otherwise a stale file dressed a recycled pid up as the head
        # it used to describe, which is the one row in `ps` you would trust.
        tracked = pid in known and local_live(known[pid])
        if role is None:
            role = "head" if tracked else "orphan?"
        rows.append({"pid": pid, "role": role, "tracked": tracked,
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


def local_live(rec):
    """Is this record's pid one of ours, still running, still the same process?

    Only ever asked of a local record. A foreign pid belongs to another
    machine's numbering, and /proc would answer in good faith about whatever
    local process happens to be wearing the number.
    """
    if rec.get(FOREIGN):
        return False
    pid = rec.get("pid")
    if not isinstance(pid, int):
        return False
    started = proc_start(pid)
    return started is not None and rec.get("procStart") in (None, started)


def _rank(rec):
    """How much a record is worth, when two of them name the same session.

    A live local record is the best thing to have: its pid can be walked to a
    pane and signalled. A foreign one is next - unknown liveness, but current.
    A local record whose process is gone is worst, and is the case that
    actually turns up: a session file is named after the pid that wrote it, so
    resuming a session gives it a second file, and the first one stays there
    until someone runs `bottleneck reap`. Newest first within a rank.
    """
    return (0 if local_live(rec) else 1 if rec.get(FOREIGN) else 2,
            -(rec.get("startedAt") or rec.get("updatedAt") or 0))


def session_records():
    """Every head Claude Code is running, best source first.

    Every home, not just ours: under WSL the claude you launched may be the
    Windows one, whose files are over on the mounted drive.

    One session can have more than one file. They are named after the pid that
    wrote them, and a session id outlives a process - resume one, or resume it
    on the other side of a WSL mount, and there are two files saying different
    things about the same head. So a collision is decided by _rank rather than
    by whichever home or directory entry came first: the file whose pid is a
    process we can actually use wins, and a leftover from a pid that has since
    gone loses to a foreign record that is still current. Left to reading
    order, a stale local file would win on nothing but its position and the
    head would read as dead - or, worse, hand its pid to a kill.
    """
    out, at_index = [], {}
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
            if at:
                got[FOREIGN] = where
            sid = got.get("sessionId") or ""
            if sid:
                seen = at_index.get(sid)
                if seen is not None:
                    if _rank(got) < _rank(out[seen]):
                        out[seen] = got
                    continue
                at_index[sid] = len(out)
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
