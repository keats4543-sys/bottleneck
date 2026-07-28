"""One list of every head, with everything the dashboard needs on it."""
import os
import time

from . import config
from .config import NEEDS_ATTENTION, QUIET_SECS, STALL_SECS, STATES
from .procs import FOREIGN, local_live, session_records, tty_of
from .store import (ack_ts, assign_slots, binds_apply, bound_pane, buried_load,
                    claims_apply, grave_stale, group_label, group_rank,
                    group_ids, held_at, hold_stale, queue_load, read_attention,
                    set_hold, unbind, unbury)
from .tmuxio import (dash_pane, locate, pane_window, panes_by_id,
                     panes_by_pid)
from .transcript import read_step, subagent_seen, transcript_for


def collect():
    # A head launched with a group asked for it before it had a session id to
    # hang it on. It has one now, so settle up before reading the queue.
    records = list(session_records())
    named = [(s.get("name") or "", s.get("sessionId") or "") for s in records]
    claims_apply(named)
    # The same handover, for the pane a head was opened into rather than the
    # group it was launched for.
    binds = binds_apply(named)

    book = queue_load()
    graves = buried_load()
    order = group_ids(book)
    panes, cursor = panes_by_pid()
    main_win = pane_window(dash_pane()) if panes else ""
    now = time.time()
    heads = []

    for s in records:
        pid = s.get("pid")
        if not isinstance(pid, int):
            continue

        # A foreign head's pid belongs to another machine's numbering, so /proc
        # cannot answer for it - and would answer about the wrong process if
        # some local one happened to be wearing the number. There is no liveness
        # question we can ask, and the age of the session file's own stamp used
        # to stand in for one: an hour without a write and the head was buried.
        #
        # That stamp is not a heartbeat. Claude writes it when the status
        # changes and at no other time - measured on this box, a head in the
        # middle of a tool call with its file untouched for 230 seconds - so its
        # age is the time since the last transition, not the time since the head
        # last did anything. Which buried precisely the wrong heads: one parked
        # waiting on you for an hour, and one two hours into a single long turn.
        # Both alive, both sat in tmux where you could still talk to them, both
        # dropped out of the queue that exists to hold them.
        #
        # So the record existing is the whole of the evidence. What the head is
        # doing is read from its transcript below, and if that has gone quiet
        # the row says STALLED and for how long - a fact you can act on, rather
        # than a burial we cannot support.
        foreign = bool(s.get(FOREIGN))
        live = True if foreign else local_live(s)
        sid = s.get("sessionId") or ""
        cwd = s.get("cwd") or ""
        raw = (s.get("status") or "").lower()

        step, last_ts, ask, task, kids = "", 0.0, "", "", 0
        tpath = transcript_for(sid, cwd) if sid else None
        if tpath:
            step, last_ts, ask, task, kids = read_step(tpath)
            # An entry timestamp is written by whoever wrote the transcript, and
            # for a head across a WSL mount that is the Windows clock, which can
            # sit an hour or more from this one - so an idle_for computed
            # against our `now` from their clock is off by the skew. The mtime
            # is read here and cannot disagree with us. Newest of the two, which
            # is also the answer when the entries carry no timestamp at all.
            try:
                last_ts = max(last_ts, os.path.getmtime(tpath))
            except OSError:
                pass
            # While agents are out, their work is this head's work. Counting it
            # as such is what stops a head that dispatched an hour of research
            # from reading as quiet after five minutes of not typing.
            if kids:
                last_ts = max(last_ts, subagent_seen(tpath))
        if not last_ts:
            last_ts = (s.get("statusUpdatedAt") or s.get("updatedAt") or 0) / 1000.0

        attn = read_attention(sid) if sid else None
        idle_for = now - last_ts if last_ts else 0.0

        # The hook's own message says the same thing every time - "Claude needs
        # your permission to use Bash" - so prefer what the transcript says the
        # head is actually holding out for, and keep the hook's line as backup.
        if not live:
            state, reason = "DEAD", ""
        elif attn and attn.get("kind") == "permission":
            state, reason = "BLOCKED", ask or attn.get("message", "waiting on you")
        elif raw in ("waiting", "needs_input", "permission_prompt"):
            state, reason = "WAITING", ask or "harness wants input"
        elif attn and attn.get("kind") == "notify":
            state, reason = "WAITING", ask or attn.get("message", "notification")
        # Before the stop flag, and before idle. A head that starts async agents
        # ends its turn to wait for them, so both of those fire and both are
        # wrong: it has not finished and it wants nothing from you. It is the
        # one state where work is going on somewhere this head is not.
        elif kids and idle_for <= QUIET_SECS:
            state, reason = "WORKING", (
                f"waiting on {kids} subagent" + ("s" if kids > 1 else ""))
        elif kids and idle_for <= STALL_SECS:
            # Quiet, but agents that read and search for a living are quiet for
            # minutes at a time and that is them working. Say how long, and
            # leave it out of the queue.
            state, reason = "WORKING", (
                f"waiting on {kids} subagent" + ("s" if kids > 1 else "")
                + f" - quiet for {fmt_age(idle_for)}")
        elif kids:
            # Nothing from the head and nothing from any of its agents for a
            # good while. Something is stuck, and saying so beats claiming work
            # is happening on no evidence at all.
            state, reason = "STALLED", (
                f"{kids} subagent" + ("s" if kids > 1 else "")
                + f" out, quiet for {fmt_age(idle_for)}")
        elif attn and attn.get("kind") == "stop":
            state, reason = "DONE", ask or "turn finished, unread"
        elif raw == "idle":
            if step and last_ts > ack_ts(sid) + 1:
                state, reason = "DONE", ask or "turn finished, unread"
            else:
                state, reason = "IDLE", ""
        elif idle_for > STALL_SECS:
            state, reason = "STALLED", f"quiet for {fmt_age(idle_for)}"
        elif idle_for > QUIET_SECS:
            # Still working as far as anyone can tell, and has been at this one
            # step for a while - a build, a test run, a long read. Worth
            # saying, not worth calling you over.
            state, reason = "WORKING", f"quiet for {fmt_age(idle_for)}"
        else:
            state, reason = "WORKING", ""

        # A head whose terminal we closed on purpose. There is no /proc to ask
        # about it - that is why closing the pane was all we could do - so the
        # row stands on what we do know: we took its terminal away at a known
        # moment and it has written nothing since. It is not proof the process
        # is gone. It is better evidence than the session file it left behind,
        # which says only that it once existed, and which nothing on this side
        # of the mount can remove. Anything written since and the burial is
        # wrong and lets go of itself, the way a hold does.
        grave = graves.get(sid) if sid else None
        if grave:
            if grave_stale(grave, last_ts):
                unbury(sid)
            else:
                state, reason = "DEAD", "killed - its pane is gone, quiet since"

        # A hold is against one finished state, not against the head. Anything
        # new - a fresh turn, another tool call - and it has gone stale, so it
        # lets go by itself rather than making you remember to.
        hold = held_at(book, sid)
        if hold:
            if hold_stale(state, last_ts, hold):
                set_hold(sid, 0)
                book["hold"].pop(sid, None)
                hold = 0.0
            elif state in NEEDS_ATTENTION:
                state, reason = "HELD", reason or "held for later"

        # locate() walks up the local process tree from this pid. For a
        # foreign head that tree is somebody else's, so it is not walked: the
        # head gets no pane, and focus() says so rather than moving whatever
        # local process the number landed on.
        pane_id, pane_target, win = (
            locate(pid, panes) if live and not foreign else (None, None, None))
        # Nothing found by walking, and there would be nothing to walk for a
        # foreign head anyway. If we opened this head ourselves we wrote down
        # where, so ask that - and let it go the moment the pane is not there,
        # or the note would go on naming a pane somebody else now has.
        if not pane_id and live:
            noted = bound_pane(binds, sid)
            if noted:
                known = panes_by_id().get(noted)
                if known:
                    pane_id, pane_target, win = noted, known[1], known[2]
                else:
                    unbind(sid)
        gid = book["of"].get(sid, "")

        # A head you cannot answer from here. It has no pane to be brought into
        # and its pid is not ours to signal, so every key that acts on a head
        # would decline: it can be read and nothing else. Being in the queue
        # would mean the go-on key offering you something it cannot then show
        # you, and the count in the header promising work you cannot do.
        elsewhere = foreign and not pane_id

        heads.append({
            "pid": pid,
            "name": s.get("name") or sid[:8] or str(pid),
            "name_source": s.get("nameSource") or "",
            "session_id": sid,
            "cwd": cwd,
            "kind": s.get("kind") or "?",
            # Carried onto the row so nothing downstream has to
            # remember to ask where this head came from.
            "foreign": foreign,
            "elsewhere": elsewhere,
            "state": state,
            "reason": reason,
            "step": step,
            "task": task,
            "idle_for": idle_for,
            "last_ts": last_ts,
            "tty": tty_of(pid) if live and not foreign else None,
            "pane_id": pane_id,
            "pane": pane_target,
            "in_main": bool(win and main_win and win == main_win),
            "active": bool(pane_id and pane_id in cursor),
            "priority": STATES[state][0],
            "attention": state in NEEDS_ATTENTION,
            "group": gid,
            "group_label": group_label(book, gid) if gid else "",
            "group_rank": group_rank(book, gid, order) if gid else len(order),
            "held": bool(hold and state == "HELD"),
        })

    # Group before need: the go-on key walks the list, so ordering the groups
    # here is what makes it clear one group before it offers you the next.
    # Anything unanswerable sinks below everything else, whatever state it is
    # in and whatever group it was put in: a BLOCKED head on the other side of
    # the machine is still not the one to look at before a working head here.
    heads.sort(key=lambda h: (h["elsewhere"], h["group_rank"], h["priority"],
                              -h["idle_for"]))
    return assign_slots(heads)


def fmt_age(secs):
    secs = int(max(0, secs))
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h{(secs % 3600) // 60:02d}"
    return f"{secs // 86400}d{(secs % 86400) // 3600:02d}h"
