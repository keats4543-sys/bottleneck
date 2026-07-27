#!/usr/bin/env python3
"""attention.py - feeds `bottleneck` the one thing it cannot infer: the moment
a head starts or stops needing you.

Wired to Notification / Stop / UserPromptSubmit / SessionEnd in
~/.claude/settings.json by install.sh. Writes
~/.bottleneck/attention/<session_id>.json on demand, and clears it the moment
you type at that head.

Never blocks and never fails a tool call: always exits 0.
"""

import json
import os
import sys
import time

STATE = os.path.expanduser(os.environ.get("BOTTLENECK_STATE") or "~/.bottleneck")
ATTN = os.path.join(STATE, "attention")
ACKS = os.path.join(STATE, "acks")


def main():
    try:
        ev = json.load(sys.stdin)
    except Exception:
        return

    sid = ev.get("session_id") or ""
    if not sid or "/" in sid or sid.startswith("."):
        return

    hook = ev.get("hook_event_name") or ""
    path = os.path.join(ATTN, sid + ".json")

    # You just engaged with this head, or it went away: no longer our problem.
    if hook in ("UserPromptSubmit", "SessionEnd"):
        try:
            os.remove(path)
        except OSError:
            pass
        try:
            os.makedirs(ACKS, exist_ok=True)
            open(os.path.join(ACKS, sid), "w").close()
        except OSError:
            pass
        return

    msg = str(ev.get("message") or "").strip()
    if hook == "Notification":
        low = msg.lower()
        kind = "permission" if ("permission" in low or "approve" in low) else "notify"
    elif hook == "Stop":
        kind, msg = "stop", "turn finished, unread"
    else:
        return

    try:
        os.makedirs(ATTN, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump({
                "session_id": sid,
                "kind": kind,
                "message": msg[:160],
                "cwd": ev.get("cwd") or "",
                "at": time.time(),
            }, fh)
        os.replace(tmp, path)
    except OSError:
        pass


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
