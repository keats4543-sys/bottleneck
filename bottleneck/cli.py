"""Every command, and the one function that decides which you meant."""
import json
import os
import subprocess
import sys
import time

from . import config
from .config import (DANGEROUS, DEFAULT_DIR, NEEDS_ATTENTION, ROLE,
                     SESSION, STATES, VERSION, c)
from .catalog import (catalog_backfill, catalog_load, catalog_save, live_ids,
                      random_name, session_list)
from .heads import collect
from .panes import (auto_raise, claude_cmd, focus, kill_head, next_or_park,
                    park, reload_all, restart_here, send_go, spawn, start)
from .tmuxio import write_keys_conf
from .procs import claude_procs, kill_pid, proc_start
from .store import (assign_slots, auto_enabled, buried_load, by_slot,
                    claim_group, clear_attention, disband_group, grave_stale,
                    group_ids, group_label, group_rank, mark_seen, move_group,
                    name_group, queue_load, queue_save, set_auto, set_group,
                    set_hold, unbury)
from .transcript import transcript_for
from .tmuxio import (dash_pane, pane_window, tmux, tmux_out, tmux_say)
from .ui import (HELP, RESTART, bar_text, ordinal, render, render_sessions,
                 watch)

from .config import ATTN

from .config import ACKS, HOME, SESSIONS

def last_write(rec, sid):
    """When a head last wrote anything, as well as this side can tell.

    For a head we cannot ask /proc about, this is the whole of the liveness
    question. The record's own stamp moves only when the status changes, so the
    transcript is the better witness where there is one; the newest of the two
    is the answer either way.
    """
    stamps = [(rec.get("statusUpdatedAt") or rec.get("updatedAt") or 0) / 1000.0]
    path = transcript_for(sid, rec.get("cwd") or "") if sid else None
    if path:
        try:
            stamps.append(os.path.getmtime(path))
        except OSError:
            pass
    return max(stamps)


USAGE = """bottleneck - one tmux view over every local Claude Code head.

The layout is one window, two panes: this dashboard on the left, whichever head
you are working with on the right. Heads you are not looking at wait in their own
hidden windows; picking one moves its pane in beside the dashboard.

  bottleneck start         build the two-pane layout and attach
  bottleneck              just the dashboard (what runs in the left pane)
  bottleneck new [name]    open a new head as a window (-r resume, -c continue)
                           -g <n> puts it in a group as soon as it appears
  bottleneck list          one-shot table
  bottleneck json          machine-readable
  bottleneck focus <n>     move head n into the main window
  bottleneck next [--jump] the next head needing attention, skipping the one up
  bottleneck send-go       press Enter at the head you are in, then move on
  bottleneck auto [on|off] raise waiting heads on their own (default on)
  bottleneck sessions      prior sessions by name, newest first (--all, --here)
  bottleneck resume <n>    reopen one of them, in its own directory
  bottleneck name <n> <s>  rename a session in the catalogue, and pin it
  bottleneck unpin <n>     let the name it runs under win again
  bottleneck index         fold pre-catalogue sessions in by reading transcripts
  bottleneck count | bar   for the tmux status line
  bottleneck clear <sid>   drop an attention flag ("all" for every one)
  bottleneck kill <n>      stop a head by number, name or raw pid (needs --yes)
  bottleneck ps            every claude process on the box, tracked or not
  bottleneck reap          clear records of heads that are already gone
  bottleneck reload        re-read the tmux config, restart the dashboard pane
  bottleneck keys          which key does what (--write regenerates keys.conf)
  bottleneck group <n> <g> put head n in a priority group ("none" to clear)
  bottleneck groups        who is in what, in priority order
  bottleneck group name <n> <label>   call a group something
  bottleneck group up|down <n>        move a group in the ranking
  bottleneck group disband <n>        take a group apart, freeing its heads
  bottleneck claim <name> <g>  group a head that has not started yet
  bottleneck hold <n>      done, but sorts below the heads still working

Single keys in the dashboard - no prefix:
  up/down select a row  left/right jump a group      Enter opens it
  1-9 focus by number   Alt+o swaps list and head
  j next / park + cycle n new head                  r resume
  x kill head           c clear flags              g go to head pane   q quit
  a toggle auto-raise   R reload after editing the checkout
  G group this head     N name that group     [ ] move it up/down
  G then r renames a group by number, [ ] ranks it, d disbands it
  h hold it back

The number on a row belongs to the head, not the row: the queue reorders itself
as heads wait, so a head keeps its number until it is gone. The head you are
sitting in has its name lit.

The go-on key (j here, Alt+j anywhere, Alt+Enter to press Enter first) never
dead-ends: it takes the next head that wants you; with the queue empty it puts
the head you just answered back in its own window and shows this dashboard;
pressed again it walks the heads that are still working, one per press.

Names: Claude only keeps a head's name while it runs, so this keeps its own book
at ~/.bottleneck/catalog.json - every head the dashboard sees is written down
with its name, directory and branch. Nothing is ever left named after a guid: a
head started without a name gets a short one you can say out loud. `r` in the
dashboard, or `bottleneck sessions`, lists them for resuming.

Auto-raise: while the dashboard runs it watches the queue and brings the top
waiting head into the main pane by itself, so you do not have to be looking at
anything. It never displaces a head you are typing in, nor one that already
wants you, nor an unread head it raised for you - those keep the pane and the
queue reorders behind them. `j` walks the queue by hand. With the main pane
empty and your cursor on this list, the raise brings the cursor with it: there
is nothing to displace, so the head arrives ready to answer.
"""

def main(argv):
    os.makedirs(ATTN, exist_ok=True)
    os.makedirs(ACKS, exist_ok=True)
    cmd = argv[0] if argv else "watch"
    rest = argv[1:]

    if cmd in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    if cmd in ("-V", "--version", "version"):
        print(f"bottleneck {VERSION}")
        return 0
    if cmd == "new":
        # The launcher is its own script - shell suits opening a window better
        # than Python does. Prefer the one beside us, so a checkout works even
        # when it is not on PATH.
        here = os.path.join(os.path.dirname(os.path.realpath(__file__)),
                            "bottleneck-new")
        try:
            os.execv(here, [here, *rest]) if os.access(here, os.X_OK) \
                else os.execvp("bottleneck-new", ["bottleneck-new", *rest])
        except OSError as exc:
            print(f"bottleneck new: cannot run bottleneck-new ({exc})",
                  file=sys.stderr)
            return 1
    if cmd == "start":
        return start()
    if cmd in ("reload", "restart"):
        print(reload_all())
        return 0
    if cmd == "keys":
        # What the config says, and what tmux will be told. Printed rather than
        # only written, because a key that does nothing gives you no way to tell
        # a typo in the config from a terminal eating the keystroke - and those
        # are the two things it is ever going to be.
        if "--write" in rest:
            where = write_keys_conf()
            print(where or "could not write the keys file")
            return 0 if where else 1
        for act, keys in config.KEYS.items():
            shown = ", ".join(("prefix " + k) if t == "prefix" else k
                              for k, t in keys) or "(unbound)"
            print(f"  {act:<8} {shown}")
        print(f"\n  set BOTTLENECK_KEY_<ACTION>=... in {config.CONFIG}")
        print(f"  written to {config.KEYS_CONF} on start, watch and reload")
        return 0
    if cmd == "json":
        print(json.dumps(collect(), indent=2))
        return 0
    if cmd == "list":
        print(render(collect()))
        return 0
    if cmd == "count":
        print(sum(1 for h in collect() if h["attention"]))
        return 0
    if cmd == "bar":
        # Kept for a status line still wired to `#(bottleneck bar)`, and for
        # anyone who wants the string from a shell. The tmux config no longer
        # uses it: a running dashboard publishes the same text to a server
        # option, which tmux reads without starting anything.
        print(bar_text(collect()), end="")
        return 0
    if cmd == "reap":
        # Clear records left behind by heads that are already gone, so dead ids
        # stop showing up here and in `claude agents`.
        import shutil
        n = 0
        graves = buried_load()
        seen_sids = set()
        # Every home, not just ours. A head on the other side of a WSL mount
        # leaves its record over there, and nothing else ever takes it away:
        # its pid is not ours to check, so the /proc rule below cannot speak
        # for it, and it came back on every refresh as a head that was fine.
        # What can speak for it is that we closed its terminal ourselves and it
        # has written nothing since - which is what a burial records, and what
        # the row has been saying since. See bury() in store.py.
        for at, where in enumerate(config.SESSION_DIRS):
            for f in sorted(os.listdir(where)) if os.path.isdir(where) else []:
                if not f.endswith(".json"):
                    continue
                path = os.path.join(where, f)
                try:
                    d = json.load(open(path))
                except (OSError, ValueError):
                    continue
                sid = d.get("sessionId") or ""
                seen_sids.add(sid)
                grave = graves.get(sid)
                gone = (grave is not None
                        and not grave_stale(grave, last_write(d, sid)))
                if not at and not gone:
                    gone = not os.path.isdir(f"/proc/{d.get('pid')}")
                if not gone:
                    continue
                try:
                    os.remove(path)
                except OSError as exc:
                    print(f"could not remove {path}: {exc}")
                    continue
                unbury(sid)
                seen_sids.discard(sid)
                print(f"removed stale session file: {f} ({d.get('name')})")
                n += 1

        # Burials for heads whose record has gone anyway - nothing left to be
        # about.
        for sid in list(graves):
            if sid not in seen_sids:
                unbury(sid)

        live_jobs = set()
        for f in os.listdir(SESSIONS) if os.path.isdir(SESSIONS) else []:
            try:
                live_jobs.add(json.load(open(os.path.join(SESSIONS, f))).get("jobId"))
            except (OSError, ValueError):
                pass
        jobs = os.path.join(HOME, ".claude", "jobs")
        for entry in sorted(os.listdir(jobs)) if os.path.isdir(jobs) else []:
            d = os.path.join(jobs, entry)
            if os.path.isdir(d) and entry not in live_jobs:
                shutil.rmtree(d, ignore_errors=True)
                print(f"removed stale job record: {entry}")
                n += 1

        for line in tmux_out("list-panes", "-a", "-F",
                             "#{pane_id}\t#{pane_dead}").splitlines():
            pane, _, dead = line.partition("\t")
            if dead == "1":
                tmux("kill-pane", "-t", pane)
                print(f"closed dead pane: {pane}")
                n += 1

        print(f"reaped {n}" if n else "nothing to reap")
        return 0

    if cmd == "ps":
        rows = claude_procs()
        if not rows:
            print("no claude processes")
            return 0
        print(f"{'PID':>8}  {'ROLE':<9} {'TRACKED':<7} COMMAND")
        for r in rows:
            print(f"{r['pid']:>8}  {r['role']:<9} {'yes' if r['tracked'] else '-':<7} "
                  f"{r['cmd'][:90]}")
        return 0

    if cmd == "todash":
        # Put the cursor back on the dashboard pane, from wherever you are.
        pane = dash_pane()
        if not pane:
            tmux_say("bottleneck: no dashboard pane - run `bottleneck start`")
            return 1
        sess = tmux_out("display-message", "-p", "-t", pane, "#{session_name}")
        tmux("select-window", "-t", pane)
        tmux("select-pane", "-t", pane)
        if sess:
            tmux("switch-client", "-t", sess)
        return 0

    if cmd in ("swap", "toggle"):
        # One key for both directions: from a head it puts you on the list,
        # from the list it puts you back in the head. Reaching for the mouse to
        # get at the queue is exactly the tax this is meant to remove.
        pane = dash_pane()
        if not pane:
            tmux_say("bottleneck: no dashboard pane - run `bottleneck start`")
            return 1
        me = os.environ.get("TMUX_PANE", "")
        target = pane
        if me == pane:
            win = pane_window(pane)
            others = [p for p in tmux_out("list-panes", "-t", win, "-F",
                                          "#{pane_id}").splitlines()
                      if p.strip() and p.strip() != pane]
            if not others:
                tmux_say("bottleneck: nothing beside the dashboard yet")
                return 0
            target = others[0].strip()
        tmux("select-window", "-t", target)
        tmux("select-pane", "-t", target)
        return 0

    if cmd == "focus":
        if not rest:
            print("usage: bottleneck focus <n|name>", file=sys.stderr)
            return 2
        heads = collect()
        key = rest[0]
        target = by_slot(heads, key) if key.isdigit() else None
        if not target:
            target = next((h for h in heads
                           if key in (h["name"], h["session_id"])
                           or h["session_id"].startswith(key)), None)
        if not target:
            print("no match", file=sys.stderr)
            return 1
        return 0 if focus(target, heads) else 1
    if cmd == "kill":
        if not rest:
            print("usage: bottleneck kill <n|name|session-id>  [--yes]", file=sys.stderr)
            return 2
        heads = collect()
        key = rest[0]
        target = by_slot(heads, key) if key.isdigit() else None
        if not target:
            target = next((h for h in heads
                           if key in (h["name"], h["session_id"], str(h["pid"]))
                           or h["session_id"].startswith(key)), None)
        if not target and key.isdigit():
            # Not a listed head, but a raw pid: allow it if it really is claude.
            row = next((r for r in claude_procs() if r["pid"] == int(key)), None)
            if row:
                if "--yes" not in rest:
                    print(f"pid {row['pid']}  {row['role']}  {row['cmd']}")
                    print("re-run with --yes to kill it", file=sys.stderr)
                    return 1
                print(kill_pid(row["pid"], row["role"]))
                return 0
        if not target:
            print("no match  (try `bottleneck ps` to see every claude process)",
                  file=sys.stderr)
            return 1
        if "--yes" not in rest:
            print(f"{target['name']}  pid {target['pid']}  {target['kind']}  "
                  f"{target['state']}  {target['pane'] or target['tty'] or 'no tty'}")
            print("re-run with --yes to kill it", file=sys.stderr)
            return 1
        print(kill_head(target))
        return 0

    if cmd in ("group", "groups"):
        heads = collect()
        book = queue_load()
        if cmd == "groups" or not rest:
            order = group_ids(book)
            if not order:
                print("no groups yet - `bottleneck group <head> <n>` makes one")
                return 0
            for at, gid in enumerate(order, 1):
                members = [h["name"] for h in heads if h["group"] == gid]
                print(f"{at}. [{gid}] {group_label(book, gid)}"
                      f"  {', '.join(members) if members else '-'}")
            loose = [h["name"] for h in heads if not h["group"]]
            if loose:
                print(f"   unassigned  {', '.join(loose)}")
            return 0
        if rest[0] == "name" and len(rest) >= 3:
            label = name_group(rest[1], " ".join(rest[2:]))
            print(f"group {rest[1]} is now {label}" if label
                  else f"group {rest[1]} goes back to its number")
            return 0
        if rest[0] == "disband" and len(rest) >= 2:
            gid = str(rest[1])
            was = group_label(book, gid)
            freed = disband_group(gid)
            if freed is None:
                print(f"no group {gid}", file=sys.stderr)
                return 1
            print(f"{was} disbanded"
                  + (f" - {freed} head{'' if freed == 1 else 's'} unassigned"
                     if freed else " - it was empty"))
            return 0
        if rest[0] in ("up", "down") and len(rest) >= 2:
            at = move_group(rest[1], -1 if rest[0] == "up" else 1)
            if not at:
                print(f"no group {rest[1]}", file=sys.stderr)
                return 1
            print(f"{group_label(queue_load(), rest[1])} is now {ordinal(at)}")
            return 0
        if len(rest) < 2:
            print("usage: bottleneck group <head> <n|none>\n"
                  "       bottleneck group name <n> <label>\n"
                  "       bottleneck group disband <n>\n"
                  "       bottleneck group up|down <n>", file=sys.stderr)
            return 2
        target = by_slot(heads, rest[0]) if rest[0].isdigit() else None
        target = target or next((h for h in heads if rest[0] in
                                 (h["name"], h["session_id"])), None)
        if not target:
            print(f"no head called {rest[0]} is running - `bottleneck claim "
                  f"{rest[0]} {rest[1]}` groups it when it starts",
                  file=sys.stderr)
            return 1
        gid = "" if rest[1] in ("none", "0", "clear") else rest[1]
        set_group(target["session_id"], gid)
        print(f"{target['name']} -> "
              f"{group_label(queue_load(), gid) if gid else 'unassigned'}")
        return 0

    if cmd == "claim":
        # What `bottleneck new -g` calls. The head is a second away from
        # existing and has no session id yet, so the group is claimed against
        # the name it will launch under. A session id we already know goes
        # straight in the book instead - no waiting, no guessing.
        if len(rest) < 2:
            print("usage: bottleneck claim <name|session-id> <n|none>",
                  file=sys.stderr)
            return 2
        who = rest[0]
        gid = "" if rest[1] in ("none", "0", "clear") else rest[1]
        if who in catalog_load() or who in live_ids():
            set_group(who, gid)
            print(f"{who[:8]} -> "
                  f"{group_label(queue_load(), gid) if gid else 'unassigned'}")
            return 0
        claim_group(who, gid)
        print(f"{who} will join {group_label(queue_load(), gid)} when it starts"
              if gid else f"claim on {who} dropped")
        return 0

    if cmd in ("hold", "unhold"):
        if not rest:
            print(f"usage: bottleneck {cmd} <n|name>", file=sys.stderr)
            return 2
        heads = collect()
        target = by_slot(heads, rest[0]) if rest[0].isdigit() else None
        target = target or next((h for h in heads if rest[0] in
                                 (h["name"], h["session_id"])), None)
        if not target:
            print("no match", file=sys.stderr)
            return 1
        if cmd == "unhold":
            set_hold(target["session_id"], 0)
            print(f"{target['name']} is back in the queue")
        else:
            set_hold(target["session_id"], target.get("last_ts") or time.time())
            print(f"{target['name']} held - below the heads still working")
        return 0

    if cmd == "clear":
        if not rest:
            print("usage: bottleneck clear <session-id|name|all>", file=sys.stderr)
            return 2
        heads = collect()
        if rest[0] == "all":
            print(f"cleared {sum(clear_attention(h['session_id']) for h in heads)}")
            return 0
        for h in heads:
            if rest[0] in (h["session_id"], h["name"]) \
                    or h["session_id"].startswith(rest[0]):
                clear_attention(h["session_id"])
                print(f"cleared {h['name']}")
                return 0
        print("no match", file=sys.stderr)
        return 1
    if cmd in ("sessions", "ls"):
        limit, where, hide_live = 15, None, False
        i = 0
        while i < len(rest):
            a = rest[i]
            if a in ("-a", "--all"):
                limit = 0
            elif a == "--limit" and i + 1 < len(rest):
                i += 1
                limit = int(rest[i])
            elif a == "--here":
                where = os.getcwd()
            elif a == "--dir" and i + 1 < len(rest):
                i += 1
                where = os.path.expanduser(rest[i])
            elif a == "--dead":
                hide_live = True
            else:
                print(f"unknown option {a}", file=sys.stderr)
                return 2
            i += 1
        rows = session_list(limit=limit, where=where, hide_live=hide_live)
        print(render_sessions(rows))
        return 0
    if cmd == "newname":
        print(random_name({r["name"] for r in session_list(limit=0)}))
        return 0
    if cmd == "index":
        n = catalog_backfill(verbose=True)
        print(f"catalogued {n}" if n else "catalog already current")
        return 0
    if cmd in ("name", "unpin"):
        # A name in our book is pinned, so nothing overwrites it later - and
        # unpin is how you take that back. A pin only ever holds against the
        # name the head runs under, so unpinning a session that has ended
        # changes nothing you can see until it runs again.
        if not rest or (cmd == "name" and len(rest) < 2):
            print(f"usage: bottleneck {cmd} <n|session-id|name>"
                  + (" <new name>" if cmd == "name" else ""), file=sys.stderr)
            return 2
        rows = session_list(limit=0)
        key = rest[0]
        if key.isdigit() and 1 <= int(key) <= len(rows):
            sid = rows[int(key) - 1]["session_id"]
        else:
            sid = next((r["session_id"] for r in rows
                        if r["session_id"].startswith(key) or r["name"] == key), None)
        if not sid:
            print("no match", file=sys.stderr)
            return 1
        book = catalog_load()
        rec = book.setdefault(sid, {})
        if cmd == "name":
            rec["name"] = " ".join(rest[1:])
            rec["pinned"] = True
            catalog_save(book)
            print(f"{sid[:8]} is now {rec['name']}")
            return 0
        was = rec.get("name") or sid[:8]
        if not rec.pop("pinned", None):
            print(f"{was} was not pinned")
            return 0
        catalog_save(book)
        print(f"{was} is unpinned - the name it runs under wins again")
        return 0
    if cmd == "resume":
        if not rest:
            print(render_sessions(session_list()))
            print("\nusage: bottleneck resume <n|name|session-id>", file=sys.stderr)
            return 2
        rows = session_list(limit=0)
        key = rest[0]
        if key.isdigit() and 1 <= int(key) <= len(rows):
            row = rows[int(key) - 1]
        else:
            row = next((r for r in rows if r["name"] == key
                        or r["session_id"].startswith(key)), None)
        if not row:
            print("no match  (try `bottleneck sessions`)", file=sys.stderr)
            return 1
        if row["live"]:
            print(f"{row['name']} is running right now - focus it instead "
                  f"(or `bottleneck new -r {row['session_id']} --fork` to branch a copy)",
                  file=sys.stderr)
            return 1
        where = row["cwd"] if os.path.isdir(row["cwd"] or "") else DEFAULT_DIR
        spawn(claude_cmd(f"--resume {row['session_id']}{DANGEROUS}"),
              where, row["name"], collect())
        print(f"resuming {row['name']} in {where}")
        return 0
    if cmd == "auto":
        if rest and rest[0] in ("on", "off"):
            set_auto(rest[0] == "on")
        elif rest and rest[0] == "toggle":
            set_auto(not auto_enabled())
        elif rest:
            print("usage: bottleneck auto [on|off|toggle]", file=sys.stderr)
            return 2
        print("on" if auto_enabled() else "off")
        return 0
    if cmd in ("next", "send-go"):
        # These are the keys the dashboard serves down its control fifo, and
        # this is what happens when nothing is listening on it - a dashboard
        # that has just stopped, or `bottleneck next` typed at a shell. Same
        # work, one process later.
        if cmd == "send-go":
            note, problem = send_go(collect(), os.environ.get("TMUX_PANE", ""))
            if problem:
                tmux_say(f"bottleneck: {note}")
            return 0

        heads = collect()
        jump = "--jump" in rest

        if not jump:
            want = [h for h in heads if h["attention"]]
            want = [h for h in want if not h["in_main"]] or want
            if not want:
                print("nothing needs attention", file=sys.stderr)
                return 1
            first = next((h for h in want if h["pane_id"]), None) or want[0]
            print(f"{first['state']}\t{first['name']}"
                  f"\t{first['pane'] or first['tty'] or '-'}"
                  f"\t{first['reason'] or first['step']}")
            return 0

        # Bound to a key, so nothing may be printed: tmux run-shell turns any
        # output at all into a popup window, which is the thing being avoided.
        # Anything worth saying goes to the status line, and we always succeed.
        note, problem = next_or_park(heads)
        if problem:
            tmux_say(f"bottleneck: {note}")
        return 0
    if cmd in ("watch", "dash"):
        rc = watch()
        return restart_here() if rc == RESTART else rc

    print(f"unknown command: {cmd}\n{__doc__}", file=sys.stderr)
    return 2

def run(argv=None):
    """Entry point. Keeps the shim in bin/ down to a couple of lines."""
    try:
        return main(sys.argv[1:] if argv is None else argv)
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        return 0
