#!/usr/bin/env python3
"""hook.py - the wire between one head's tool calls and its group's board.

Wired to SessionStart / UserPromptSubmit / PreToolUse / PostToolUse / SessionEnd
in ~/.claude/settings.json by install.sh, alongside the attention hook. It does
two things and nothing else:

  - **writes down what this head did** that a sibling would want to know: what
    it is for, and every file it wrote, edited or deleted.
  - **hands this head its group's queue**, at the moments where a head can
    actually take delivery of something - the start of a turn, the start of a
    session, or the instant before it edits a file somebody else has moved
    under it.

The board is per group. A head in no group reads nothing and writes nothing, so
this costs a lone session one dictionary lookup per tool call.

Never blocks and never fails a tool call: always exits 0, and says nothing at
all when it has nothing to say.
"""
import importlib.util
import json
import os
import sys

# The board by its path, not `from bottleneck.modules.share import board` -
# which would run this module's __init__ and, through it, the core's config.py:
# a config file read and, on WSL, a glob over a Windows mount. That is a third
# of the cost of this hook, paid on every Edit, for constants it never touches.
# board.py imports nothing but the standard library precisely so this can be
# done, and it is the only file of the module a hook needs.
_HERE = os.path.dirname(os.path.realpath(__file__))

try:
    _spec = importlib.util.spec_from_file_location(
        "bottleneck_share_board", os.path.join(_HERE, "board.py"))
    board = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(board)
except Exception:                                   # not a checkout any more
    board = None


def emit(event, context="", decision="", reason=""):
    """The one place anything is printed. Silence is the default answer."""
    out = {"hookEventName": event}
    if context:
        out["additionalContext"] = context
    if decision:
        out["permissionDecision"] = decision
        out["permissionDecisionReason"] = reason
    if len(out) > 1:
        print(json.dumps({"hookSpecificOutput": out}))


def main():
    try:
        ev = json.load(sys.stdin)
    except Exception:
        return
    if board is None or not board.ENABLED or not isinstance(ev, dict):
        return

    sid = board.safe_sid(ev.get("session_id") or "")
    if not sid:
        return
    hook = ev.get("hook_event_name") or ""
    cwd = ev.get("cwd") or ""

    if hook == "SessionEnd":
        board.leave(sid)
        return

    tool_call = hook in ("PreToolUse", "PostToolUse")
    if hook == "PostToolUse" and board.SOURCE != "hook":
        # Somebody else is watching the working tree - the dashboard, out of
        # the transcripts. Wiring should already have left this event out; this
        # is what makes a stale settings.json harmless rather than a source of
        # every file change being written down twice.
        return
    if tool_call and not board.group_of(sid):
        # The fast path, and the reason a lone head can afford this hook at
        # all: no group means nobody to tell and nothing to be told, so the
        # answer is one small file read and no lock, no write and no output.
        return
    if not tool_call:
        board.register(sid, cwd)

    # ------------------------------------------------- the flushable events
    #
    # A head can only take delivery of news between turns. These are the two
    # places where it is not in the middle of something: it has just been given
    # a prompt, or it has just started.
    if hook == "UserPromptSubmit":
        prompt = str(ev.get("prompt") or "").strip()
        # The first thing you type is what the head is for. Nothing else in the
        # session says it as plainly, and asking you to say it twice - once to
        # the head and once to bottleneck - is how a feature stops being used.
        if prompt and not board.goal_of(sid):
            board.set_goal(sid, prompt, cwd)
        emit(hook, board.brief(sid, cwd))
        return

    if hook == "SessionStart":
        emit(hook, board.brief(sid, cwd))
        return

    tool = ev.get("tool_name") or ""
    inp = ev.get("tool_input") if isinstance(ev.get("tool_input"), dict) else {}

    if hook == "PreToolUse":
        if tool not in board.WRITERS:
            return
        # The third flushable moment, and the only one that is not a boundary:
        # you are about to write a file somebody else has just written. That
        # cannot wait for the next prompt, so the queue comes with it - the
        # warning and the reason for it arrive together.
        notes = [n for n in (board.clash(sid, os.path.join(cwd, p) if not
                                         os.path.isabs(p) else p)
                             for p in board.paths_of(tool, inp)) if n]
        if not notes:
            return
        head = "\n".join(notes)
        if board.GUARD == "ask":
            emit(hook, decision="ask", reason=head)
            return
        emit(hook, board.brief(sid, cwd, extra=head) or head)
        return

    if hook != "PostToolUse":
        return

    # What the call did to the working tree - the same reading of it the
    # dashboard's harvester makes from the transcript, because it is the same
    # function. All this side knows that the other does not is that it happened
    # a moment ago rather than up to a refresh ago.
    for kind, path in board.touches(tool, inp, cwd):
        if kind == "read":
            board.note_read(sid, path)
        else:
            board.note_touch(sid, path, kind, cwd)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
