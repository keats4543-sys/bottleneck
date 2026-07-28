"""The list on the screen, and the keys that drive it."""
import collections
import json
import os
import sys
import time

from . import config
from .config import (CTL, DANGEROUS, DEFAULT_DIR, NEEDS_ATTENTION, REFRESH,
                     ROLE, SPIN, SPINUP_TICK, STATES, SUB_INDENT, SUB_LINES,
                     TASKLINE, c)
from .catalog import catalog_note, random_name, session_list
from .heads import collect, fmt_age
from .panes import (auto_raise, claude_cmd, focus, kill_head, next_or_park,
                    park, pending, reaped, restart_here, send_go, spawn,
                    warm_claude)
from .store import (assign_slots, auto_enabled, by_slot, claim_group,
                    clear_attention, group_ids, group_label, mark_seen,
                    move_group, name_group, queue_load, queue_save, set_auto,
                    set_group, set_hold)
from .tmuxio import (dash_pane, dash_point, dash_register, dash_release,
                     invalidate, pane_in_mode, tmux, tmux_say, write_keys_conf)
from .transcript import clip


def mark_plain(h):
    """The leading mark without colour, for a row drawn as one solid bar."""
    return "›" if h["attention"] else ("●" if h["in_main"] else " ")


def sel(text, on):
    """Light up a name the arrow keys are pointing at."""
    return c("7", text) if on else text


def wrap_body(body, width):
    """The summary under a row: wrapped to the pane, and never more than a few.

    Capped because one chatty head must not push the rest of the queue off the
    bottom - the list only works if you can see all of it. The cap is
    BOTTLENECK_SUBLINES, and the last line ends in an ellipsis when there was
    more to say.
    """
    body = " ".join((body or "").split())
    if not body:
        return []
    room = max(16, width - SUB_INDENT - 1)
    out, line = [], ""
    for word in body.split(" "):
        if line and len(line) + 1 + len(word) > room:
            out.append(line)
            line = word
            if len(out) == SUB_LINES:
                break
        else:
            line = f"{line} {word}" if line else word
        # A single word longer than the pane - a path, a URL - gets cut rather
        # than pushing the row sideways.
        while len(line) > room:
            out.append(line[:room])
            line = line[room:]
            if len(out) == SUB_LINES:
                break
        if len(out) == SUB_LINES:
            break
    if line and len(out) < SUB_LINES:
        out.append(line)
    if len(out) == SUB_LINES:
        used = len(" ".join(out))
        if used < len(body):
            out[-1] = clip(out[-1] + " …", room)
    return [" " * SUB_INDENT + o for o in out]


def wrap_task(h, width):
    """The prompt line under a row, or nothing at all.

    One line, never wrapped: it is the standing fact about a head, and the head
    is not asking you to read it - it is there so the summary above has
    something to be about. Two rows of prompt would cost the list a head.
    """
    task = (h.get("task") or "").strip()
    if not task or TASKLINE == "off":
        return []
    if TASKLINE != "all" and not h.get("attention"):
        return []
    room = max(16, width - SUB_INDENT - 1)
    return [" " * SUB_INDENT + clip("· " + task, room)]


def paint(text):
    """Put a frame on the screen without blanking it first.

    The obvious way to redraw is home, erase everything, write the new frame -
    and it means that between the erase and the write there is a moment where
    the screen is empty. tmux is free to send the client whatever the pane
    holds at any point in that, so the list flickers, and the faster it redraws
    the more often you catch it. That is not free to leave alone now: a pane
    coming up redraws twice a second.

    So nothing is erased ahead of time. Each line is written over whatever was
    on that row and clears the rest of the row behind itself, and one erase at
    the end takes away the rows a shorter frame left behind. Every cell either
    holds new content or is cleared in the same write - there is no moment in
    between for anything to see.
    """
    lines = text.split("\n")
    sys.stdout.write("\033[H" + "\033[K\n".join(lines) + "\033[K\033[J")
    sys.stdout.flush()


# Set by SIGWINCH, read by the wait loop. The pane changing shape is the one
# thing that makes what is on screen wrong immediately rather than in a second
# or two - every line is laid out for a width the pane no longer has - and it
# is exactly what happens when a head lands beside the dashboard. Waiting out
# the rest of the refresh leaves the list truncated or stretched in the
# meantime, which is the shifting you see and then watch settle.
_resized = False


def _on_winch(sig, frame):
    global _resized
    _resized = True


def spinner(now=None):
    """Which frame the spinner is on, from the clock rather than a counter.

    Nothing has to be threaded through the redraw or reset when a row appears
    and disappears: every spinner on the screen is on the same frame, which is
    what you would want anyway, and the loop only has to redraw often enough
    for it to move. See SPINUP_TICK.
    """
    now = time.time() if now is None else now
    return SPIN[int(now / max(0.05, SPINUP_TICK)) % len(SPIN)]


def render(heads, width=None, note="", auto=None, selected=""):
    if width is None:
        try:
            width = os.get_terminal_size().columns
        except OSError:
            width = 100
    lines = []
    # Only heads you can answer from here. Counting the others would be
    # promising work the keys then decline to do.
    n = sum(1 for h in heads if h["attention"] and not h.get("elsewhere"))
    away = sum(1 for h in heads if h.get("elsewhere"))
    tag = "" if auto is None else ("  auto" if auto else "  auto:off")
    hdr = (f" bottleneck  {len(heads) - away} heads  {n} need you"
           + (f"  +{away} elsewhere" if away else "") + f"{tag}  "
           f"{time.strftime('%H:%M:%S')}")
    lines.append(c("1;97;44", hdr.ljust(width)[:width]))

    # Two columns before the number: the state mark, and the arrow-key cursor.
    narrow = width < 76
    if narrow:
        lines.append(c("90", f"  {'#':>2} {'STATE':<8} NAME"))
    else:
        lines.append(c("90", f"  {'#':>2} {'STATE':<8} {'NAME':<22} {'AGE':>5}"))

    # Two different questions, and they were being answered by one highlight.
    # `here` is which head is open beside you - a fact about the panes, marked
    # across the whole row. `pick` is which row your arrow keys are on - a
    # choice, marked on the name, because you can point at a head without
    # opening it.
    here = (next((h for h in heads if h.get("active")), None)
            or next((h for h in heads if h["in_main"]), None))
    pick = next((h for h in heads if h["session_id"] == selected), None)

    # Headings only once groups are in use. Someone who never touches them
    # should not pay a line of height for a feature they are not using.
    grouped = any(h.get("group") for h in heads)
    seen_group = object()
    seen_away = False

    for h in heads:
        # Everything unanswerable is sorted to the end, so one heading here
        # divides the queue from the things you are only watching. It ends the
        # grouping above it too: a group's heading reprinted down here would
        # read as more of that group rather than as the other side of a line.
        if h.get("elsewhere") and not seen_away:
            seen_away = True
            seen_group = object()
            title = "elsewhere - watched, not answerable from here"
            rule = "─" * max(0, width - len(title) - 4)
            lines.append(c("90", f" {title} {rule}"))
        if grouped and not h.get("elsewhere") and h.get("group", "") != seen_group:
            seen_group = h.get("group", "")
            title = (f"{h['group_label']}  [{seen_group}]" if seen_group
                     else "unassigned")
            # The heading of the group you are standing in brightens - enough
            # to keep your bearings after a jump, without a second cursor.
            on_group = pick is not None and pick.get("group", "") == seen_group
            rule = "─" * max(0, width - len(title) - 4)
            lines.append(c(("1;97" if on_group else "1;94") if seen_group
                           else ("97" if on_group else "90"),
                           f" {title} {rule}"))
        i = h.get("slot") or 0
        _, label, colour = STATES[h["state"]]
        mark = c("1;93", "›") if h["attention"] else (c("1;96", "●") if h["in_main"] else " ")
        body = h["reason"] or h["step"] or ""
        # A pane that is still coming up has nothing to report but the fact
        # that it is still coming up, and a line that never changes reads as a
        # dashboard that has stopped rather than as a head that has not
        # started. The spinner is the whole of the difference. It goes on the
        # summary rather than in the state column so that a row asking you
        # something keeps the plain WAITING every other row wears.
        if h.get("spin"):
            body = f"{spinner()} {body}"
        # Tag backgrounded heads always: they have no pane and ignore /exit, so
        # without a marker they look like ordinary heads that refuse to die.
        name = h["name"]
        if h["kind"] in ("bg", "background"):
            name = name[:17] + " ·bg"
        on_row = h is pick
        cursor = "▸" if on_row else " "

        if narrow:
            plain = f"{cursor}{i:>2} {label:<8} {name[:width-15]}"
            painted = (f"{cursor}{i:>2} {c(colour, label.ljust(8))} "
                       f"{sel(name[:width-15], on_row)}")
        else:
            age = fmt_age(h["idle_for"])
            plain = f"{cursor}{i:>2} {label:<8} {name[:22]:<22} {age:>5}"
            painted = (f"{cursor}{i:>2} {c(colour, label.ljust(8))} "
                       f"{sel(f'{name[:22]:<22}', on_row)} {age:>5}")
        # The head open beside you gets the whole row, so you can find it
        # without reading. A bar cannot hold the state colours - every reset
        # inside it would end the background - so that row goes plain.
        if h is here:
            lines.append(c("1;97;44", (mark_plain(h) + plain).ljust(width)[:width]))
        else:
            lines.append(mark + painted)
        # What you asked for, above what came back, because that is the order
        # they happened in and the row below is the answer to this one. Dimmer
        # than the summary either way: it is context you glance at, and on an
        # attention row it must not compete with the line you are there to read.
        for out in wrap_task(h, width):
            lines.append(c("90", out))
        # The summary gets its own line, wrapped. On one row it was the first
        # casualty of a narrow pane - and it is the part you are reading the
        # list for. A wrapped line costs a row of height and always says
        # something whole.
        for out in wrap_body(body, width):
            lines.append(c("37" if h["attention"] else "90", out))

    if not heads:
        lines.append(c("90", "  no heads yet - press n to start one"))
    if note:
        lines.append("")
        lines.append(c("1;93", "  " + note[:width - 3]))
    return "\n".join(lines)


def render_sessions(rows, width=None):
    if width is None:
        try:
            width = os.get_terminal_size().columns
        except OSError:
            width = 100
    now = time.time()
    # Most sessions sit in the same folder on the same branch, so printing that
    # on every row buys nothing. Show it only where it differs, and give the
    # space to the names instead - they are what you are picking by.
    home_dir = os.path.abspath(DEFAULT_DIR)
    common = collections.Counter(r["branch"] for r in rows if r["branch"])
    usual = common.most_common(1)[0][0] if common else ""

    wheres = []
    for r in rows:
        bits = []
        if r["cwd"] and os.path.abspath(r["cwd"]) != home_dir:
            bits.append(os.path.basename(r["cwd"]) or r["cwd"])
        if r["branch"] and r["branch"] != usual:
            bits.append(r["branch"])
        wheres.append("  ".join(bits))

    wide = max([len(w) for w in wheres] + [0])
    name_w = max(18, width - 12 - min(wide, 34))
    hdr = f" {'#':>2} {'AGE':>5}  {'NAME':<{name_w}}"
    if wide:
        hdr += "  WHERE"
    lines = [c("90", hdr[:width])]
    for i, (r, where) in enumerate(zip(rows, wheres), 1):
        age = fmt_age(now - r["last_seen"]) if r["last_seen"] else "-"
        row = (f" {i:>2} " + c("90", age.rjust(5)) + "  "
               + c("1;96" if r["live"] else "97", r["name"][:name_w].ljust(name_w)))
        if where:
            row += "  " + c("90", where[:34])
        if r["live"]:
            row += c("1;96", "  live")
        # Only worth saying on a live row: that is the only time a pin is
        # holding anything off, and the only time the two names can differ.
        if r.get("pinned") and r["live"]:
            row += c("90", " pinned")
        lines.append(row)
    if not rows:
        lines.append(c("90", "  no prior sessions catalogued"))
    return "\n".join(lines)


HELP = ("↑↓ select  ←→ group  ⏎ open  1-9 focus  j next/park  G group  N name  [ ] rank  h hold  "
        "a auto  n new  r resume  x kill  c clear  g go  R reload  q quit  "
        "esc back")


# What watch() returns when you press R. Not an exit code - main() reads it and
# hands the pane to a fresh copy of this file.
RESTART = "restart"


def next_key(prompt="", timeout=5.0):
    """One keystroke, no Enter. Empty if you wait too long or change your mind.

    Raw and unbuffered, like the main loop: an arrow pressed at this prompt
    arrives as three bytes, and taking one of them through sys.stdin would leave
    the other two to turn up later as keys nobody pressed.
    """
    import select as _sel
    if prompt:
        sys.stdout.write("\n  " + c("1;93", prompt))
        sys.stdout.flush()
    if not sys.stdin.isatty():
        return ""
    if not _sel.select([sys.stdin], [], [], timeout)[0]:
        return ""
    key, _ = split_key(os.read(sys.stdin.fileno(), 64).decode("utf-8", "replace"))
    if len(key) != 1 or key in ("\x1b", "\x03", "\r", "\n"):
        return ""              # an arrow or Esc here means "changed my mind"
    return key


ARROWS = {"[A": "up", "[B": "down", "[C": "right", "[D": "left",
          "OA": "up", "OB": "down", "OC": "right", "OD": "left",
          "[5~": "pgup", "[6~": "pgdn"}


def arrow_of(seq):
    """Which way a sequence points, ignoring any modifier the terminal added.

    Ctrl or shift with an arrow arrives as ESC [ 1;5 A and page keys as
    ESC [ 6;5 ~ - the same key with a parameter in the middle. Strip the
    parameter and it is the key we already know.
    """
    if seq.startswith("[") and ";" in seq:
        head, _, tail = seq[1:].partition(";")
        final = tail[-1:]
        # A letter carries the key itself and the parameter is noise; a "~"
        # needs the number in front of it to say which key it was.
        seq = "[" + (head + final if final == "~" else final)
    return ARROWS.get(seq, "")


def split_key(buf):
    """One keystroke off the front of what the terminal sent, and the rest.

    Terminals send an arrow as three bytes in a single write, so the fix that
    matters is not waiting longer for the rest - it is never having lost it.
    Read whatever arrived, then cut one key off the front here.

    This replaced a loop that read a byte, then waited on select() for the next
    one. sys.stdin is buffered: the first read pulled all three bytes in and
    returned the ESC, select() then saw an empty file descriptor because the
    rest was sitting in Python's buffer, and the "[" came back a moment later
    as a keystroke - which is the group-demote key. Every arrow press ended in
    "not in a group". No timeout would have fixed that; the bytes were never
    late, they were already read.
    """
    if not buf:
        return "", ""
    if buf[0] != "\x1b":
        return buf[0], buf[1:]
    if len(buf) == 1:
        return "\x1b", ""                       # Esc on its own
    if buf[1] not in ("[", "O"):
        return "\x1b" + buf[1], buf[2:]         # Alt and a key
    at = 2
    while at < len(buf) and not ("@" <= buf[at] <= "~"):
        at += 1
    if at >= len(buf):
        return "", ""                           # cut short - drop it
    return "\x1b" + buf[1:at + 1], buf[at + 1:]


def leave_copy_mode(pane):
    """Come back out of copy-mode, so the arrow keys reach this program.

    The mouse is on, and one scroll or drag over the list puts the pane into
    copy-mode. tmux then owns the arrows - they move its cursor through the
    scrollback and never arrive here, which reads exactly like arrow keys being
    eaten. Nothing in a dashboard that redraws every couple of seconds wants a
    scrollback, so it steps back out.
    """
    if not pane or not pane_in_mode(pane):
        return False
    tmux("send-keys", "-t", pane, "-X", "cancel")
    return True


def current_head(heads):
    """The head these keys act on: the one you are in, else the one on show."""
    return (next((h for h in heads if h.get("active")), None)
            or next((h for h in heads if h["in_main"]), None))


def group_keys(heads):
    """The groups in the order they appear, unassigned included."""
    out = []
    for h in heads:
        gid = h.get("group", "")
        if gid not in out:
            out.append(gid)
    return out


def move_selection(heads, picked, step):
    """Up and down: the next row, wherever it is.

    This used to stay inside a group until you pressed left to step out, which
    made the arrows modal - the same key doing different things depending on a
    state you had to remember, and announcing it. Groups are a jump of their
    own now, so up and down can be the boring thing everybody expects.
    """
    if not heads:
        return picked
    at = next((n for n, h in enumerate(heads)
               if h["session_id"] == picked), 0)
    return heads[max(0, min(len(heads) - 1, at + step))]["session_id"]


def jump_group(heads, picked, step):
    """The first head of the group before or after this one."""
    if not heads:
        return picked
    here = next((h for h in heads if h["session_id"] == picked), heads[0])
    keys = group_keys(heads)
    gid = here.get("group", "")
    pos = keys.index(gid) if gid in keys else 0
    want = keys[max(0, min(len(keys) - 1, pos + step))]
    return next(h for h in heads if h.get("group", "") == want)["session_id"]


def queue_key(key, heads, selected=""):
    """The group and hold keys. Returns the line to show; never raises.

    They act on the row you have pointed at, not on the head that happens to be
    open - the point of a selection is grouping or holding a head without
    having to open it first.
    """
    cur = (next((h for h in heads if h["session_id"] == selected), None)
           or current_head(heads))
    if not cur:
        return "no head selected - bring one up first"
    # Everything here is written down against a session id, and a pane that is
    # still starting has none - a group or a hold filed under the pane would be
    # about a head that does not exist, and would still be there once one did.
    # The group asked for at launch is already claimed against the name.
    if cur.get("pending"):
        return f"{cur['name']} is still starting - wait for it to come up"
    sid, name = cur["session_id"], cur["name"]

    if key == "h":
        if cur["held"]:
            set_hold(sid, 0)
            return f"{name} is back in the queue"
        set_hold(sid, cur.get("last_ts") or time.time())
        return f"{name} held - it drops below the heads still working"

    if key == "G":
        book = queue_load()
        order = group_ids(book)
        menu = "  ".join(f"{g}:{group_label(book, g)}" for g in order[:6])
        got = next_key(f"group for {name}? [1-9, 0 to clear]"
                       + (f"   {menu}" if menu else ""))
        if not got:
            return "cancelled"
        if got == "0":
            set_group(sid, "")
            return f"{name} is unassigned"
        if not got.isdigit():
            return f"'{got}' is not a group"
        set_group(sid, got)
        book = queue_load()
        # A group nobody has named reads as "group 2" everywhere it appears,
        # which is the moment naming it is worth mentioning - and the moment
        # you are least likely to go looking for a key that does it.
        hint = "" if book["names"].get(got) else "   N names it"
        return f"{name} joins {group_label(book, got)}{hint}"

    if key == "N":
        gid = cur["group"]
        if not gid:
            return f"{name} is not in a group - press G first"
        was = queue_load()["names"].get(gid, "")
        label = ask(f"name for group {gid}"
                    + (f" [{was}]" if was else "") + ", - to clear: ").strip()
        if not label:
            # Enter is "changed my mind", not "call it nothing" - clearing a
            # name you typed on purpose should take saying so.
            return f"group {gid} keeps {was}" if was else "cancelled"
        if label == "-":
            name_group(gid, "")
            return f"group {gid} goes back to its number"
        name_group(gid, label)
        return f"group {gid} is {label}"

    # [ and ] - move this head's whole group up or down the ranking.
    if not cur["group"]:
        return f"{name} is not in a group - press G first"
    at = move_group(cur["group"], -1 if key == "[" else 1)
    book = queue_load()
    return f"{group_label(book, cur['group'])} is now {ordinal(at)} of {len(group_ids(book))}"


def ordinal(n):
    if 10 <= n % 100 <= 20:
        return f"{n}th"
    return f"{n}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th') }".replace(" ", "")


# What a prompt returns when you press Esc. Not "" - an empty line is an answer
# and usually means "take the default", which is the opposite of leaving.
BACK = object()


def ask(prompt):
    """Read a line, with Esc as the way back out. Returns BACK if you press it.

    The line is read a keystroke at a time rather than handed to readline,
    because readline cannot see Esc. In cooked mode the terminal holds the line
    until you press Enter, so the key that means "no, take me back" arrives -
    if at all - only after you have committed the very line it meant to
    abandon. Pressing n and then wanting out was a thing you could not say.

    So: the same raw read the main loop uses, the same split_key, and the echo
    done here. Backspace and Ctrl-U work; an arrow is swallowed rather than
    printed as a stray bracket, which is what the terminal would do with it.
    """
    if not sys.stdin.isatty():
        sys.stdout.write("\n  " + prompt)
        sys.stdout.flush()
        try:
            line = sys.stdin.readline()
        except Exception:
            return BACK
        # End of input is not a blank answer. A step that will not take one and
        # asks again would otherwise be asked forever, with nothing left to
        # answer it - so read it as the way out, which is what it is.
        return line.strip() if line else BACK

    def done(value):
        sys.stdout.write("\033[?25l")
        sys.stdout.flush()
        return value

    sys.stdout.write("\033[?25h\n  " + prompt)
    sys.stdout.flush()
    buf, pending = "", ""
    while True:
        if not pending:
            try:
                pending = os.read(sys.stdin.fileno(), 64).decode("utf-8", "replace")
            except OSError:
                pending = ""
            if not pending:
                return done(BACK)       # the terminal went away
        key, pending = split_key(pending)
        if not key:
            continue
        if key in ("\r", "\n"):
            return done(buf.strip())
        # Esc, and Ctrl-C, which means the same thing at a prompt: this was a
        # question you did not want to answer, not a program you wanted to end.
        if key in ("\x1b", "\x03"):
            return done(BACK)
        if key.startswith("\x1b"):
            continue                    # an arrow: nothing here to move over
        if key in ("\x7f", "\x08"):
            if buf:
                buf = buf[:-1]
                sys.stdout.write("\b \b")
                sys.stdout.flush()
            continue
        if key == "\x15":               # Ctrl-U, the usual "start again"
            sys.stdout.write("\b \b" * len(buf))
            sys.stdout.flush()
            buf = ""
            continue
        if key < " ":
            continue
        buf += key
        sys.stdout.write(key)
        sys.stdout.flush()


def flow(*steps):
    """Ask a run of questions, with Esc as the way back through them.

    Each step is called with the answers so far and returns its answer, BACK to
    step back a question, or None to stay on this one - which is how a step
    says "that is not a directory" without throwing away the two answers you
    already gave. Returns the answers, or None if you backed out of the first
    question, which is how you leave a flow you did not mean to start.

    Pressing n used to commit you to three questions: the only ways out were to
    answer all of them or to kill something. One key that means "no" all the
    way back out is the whole point.
    """
    answers = []
    at = 0
    while at < len(steps):
        got = steps[at](answers)
        if got is BACK:
            at -= 1
            if at < 0:
                return None
            answers = answers[:at]
            continue
        if got is None:
            continue                    # stay put; the step has said why
        answers = answers[:at] + [got]
        at += 1
    return answers


def complain(line):
    """Say why an answer will not do, without leaving the question.

    Printed under the prompt rather than kept for the dashboard's note line:
    the note only appears once the flow is over, which is no use to someone
    still standing in it.
    """
    sys.stdout.write("\n  " + c("1;93", line))
    sys.stdout.flush()



# ----------------------------------------------------------- the control fifo
#
# The keys outside the dashboard used to each start a python process, which
# cost upwards of a tenth of a second before any work began - most of the wait
# between pressing Alt+Enter and the pane moving. Everything they needed was
# already here, in a program that was already running, so now the key writes a
# line and this reads it.
#
# Held open for writing as well as reading, on purpose: with a reader always
# attached the fifo never reports end-of-file into select(), and a key pressed
# with nothing to say never blocks the shell that wrote it.

def ctl_open():
    """The fifo the tmux bindings talk to, or None if it cannot be had."""
    try:
        if os.path.exists(CTL):
            os.unlink(CTL)      # a fifo from a dashboard that died owns nothing
        os.mkfifo(CTL, 0o600)
        return os.open(CTL, os.O_RDWR | os.O_NONBLOCK)
    except OSError:
        return None


def ctl_close(fd):
    try:
        if fd is not None:
            os.close(fd)
    except OSError:
        pass
    try:
        os.unlink(CTL)
    except OSError:
        pass


def ctl_read(fd):
    """The verbs waiting on the fifo, as (verb, argument) pairs."""
    try:
        raw = os.read(fd, 4096).decode("utf-8", "replace")
    except OSError:
        return []
    out = []
    for line in raw.splitlines():
        verb, _, arg = line.strip().partition(" ")
        if verb:
            out.append((verb, arg.strip()))
    return out


def do_ctl(verb, arg, heads):
    """Run one line off the control fifo. Returns (note, problem).

    Deliberately small: a verb here is a key someone pressed somewhere else in
    tmux, and the keys that tmux can serve on its own never get this far.
    """
    if verb == "sendgo":
        return send_go(heads, arg)
    if verb in ("next", "j"):
        return next_or_park(heads)
    return f"unknown control verb: {verb}", True


# ------------------------------------------------------------ the status line

BAR_COLOURS = {"BLOCKED": "colour196", "WAITING": "colour214",
               "DONE": "colour77", "STALLED": "colour170"}


def bar_text(heads):
    """The attention counter for the tmux status line, or "" when nothing waits.

    One function because there are two callers and they must agree: the
    dashboard publishes this every refresh, and `bottleneck bar` still prints it
    for anyone who has wired the status line up the old way.
    """
    want = [h for h in heads if h["attention"] and not h.get("elsewhere")]
    if not want:
        return ""
    worst = want[0]["state"]
    return (f"#[fg=colour232,bg={BAR_COLOURS.get(worst, 'colour250')},bold] "
            f"{len(want)} {worst.lower()} #[default]")


_bar_said = None


def publish_bar(heads):
    """Put the counter where tmux can read it without running a program.

    Only when it changes. The text is the same for most of a minute at a time,
    and a tmux call is a fork and a socket round-trip - about 11ms - which is
    not worth spending to write down what is already there.
    """
    global _bar_said
    said = bar_text(heads)
    if said == _bar_said:
        return
    tmux("set", "-s", config.BAR_OPT, said) if said \
        else tmux("set", "-s", "-u", config.BAR_OPT)
    _bar_said = said


def retract_bar():
    """Take the counter down. Nothing is maintaining it once we stop."""
    global _bar_said
    _bar_said = None
    tmux("set", "-s", "-u", config.BAR_OPT)


def watch():
    import select
    import signal
    global _resized
    # Claim this pane before anything else looks for a dashboard: the mark on
    # its own is not enough to tell us apart from a pane that used to be one.
    # The configured keys, in case tmux was started before they were set. It is
    # sourced by ~/.tmux.conf, so writing it here does not install it - but a
    # reload from any direction now finds it current.
    write_keys_conf()
    me = os.environ.get("TMUX_PANE", "")
    if me:
        dash_register(me)
        tmux("set", "-p", "-t", me, ROLE, "dash")
        dash_point(me)
    # Off and running before the first redraw. If claude is not on our PATH this
    # takes a login shell to answer, and the alternative is paying for it with
    # the cursor sat in the new-head prompt.
    warm_claude()
    ctl = ctl_open()
    raw = None
    if sys.stdin.isatty():
        try:
            import termios
            import tty as ttymod
            raw = termios.tcgetattr(sys.stdin)
            ttymod.setcbreak(sys.stdin.fileno())
        except Exception:
            raw = None

    # Redraw when the pane changes shape rather than at the end of the refresh
    # it happened in. A head joining the window is a resize, and every line of
    # the list is laid out for the width it had a moment ago.
    winch = None
    if raw and hasattr(signal, "SIGWINCH"):
        try:
            winch = signal.signal(signal.SIGWINCH, _on_winch)
        except (OSError, ValueError):
            winch = None

    note = ""
    held = None
    picked = ""        # session id the arrow keys are on
    typed = ""         # bytes the terminal sent that we have not read yet
    try:
        # The alternate screen, because this is a screen and not a transcript.
        #
        # Repainting with home-and-clear on the primary screen scrolls the old
        # screenful into the terminal's scrollback, and tmux keeps every line of
        # that: measured here at 9KB a line, 616 lines a minute, 404MB held for
        # one dashboard that had been up a couple of days. The tmux server is
        # the process that has to carry out every pane move, so what it is
        # holding is not free - it was the largest resident object on the box
        # after the heads themselves, and the switching this exists to make fast
        # was waiting behind the paging it caused.
        #
        # None of that history was ever worth keeping: it is the same list,
        # redrawn. On the alternate screen the same 200 redraws leave nothing
        # behind (0 lines against 1,998), and tmux forwards the wheel as arrow
        # keys instead of dropping you into copy-mode - where, incidentally, the
        # `send-keys` that Alt+j turns into was being eaten rather than reaching
        # this loop.
        sys.stdout.write("\033[?1049h\033[?25l")
        while True:
            invalidate()          # a new cycle asks tmux fresh questions
            # Re-aimed every cycle rather than once at startup, so the fast
            # keys stand down when a second dashboard appears and come back
            # when it goes.
            dash_point(me)
            heads = collect()
            catalog_note(heads)
            # The panes we have opened that are not heads yet, on the list
            # ahead of everything else - they are the newest thing there is,
            # and one of them may be sat asking you whether it may read the
            # folder you started it in. Merged after the catalogue, which is a
            # book of heads and must not hear about panes, and before
            # everything that reads the list, all of which should.
            waiting_on = pending(heads)
            if waiting_on:
                heads = assign_slots(waiting_on + heads)
            publish_bar(heads)
            if auto_enabled():
                raised, held = auto_raise(heads, held, me)
                if raised:
                    note = f"raised {raised['name']} - {raised['state'].lower()}"
                    # The raise moved one head into the main window and any
                    # other out of it. That is the whole of what changed, so
                    # say so rather than reading every transcript again to be
                    # told the same thing.
                    for h in heads:
                        h["in_main"] = h["pid"] == raised["pid"]
            # A kill that has finished dying since the last pass. It outranks
            # whatever note is standing: you asked for it, it took a moment, and
            # this is the answer coming back.
            for said in reaped():
                note = said
            # Keep the selection on the head it was on. Rows reorder under it,
            # and a head can die while selected; land somewhere sensible then.
            if not any(h["session_id"] == picked for h in heads):
                cur = current_head(heads)
                picked = (cur or heads[0])["session_id"] if heads else ""
            frame = render(heads, note=note, auto=auto_enabled(),
                           selected=picked)
            if raw:
                frame += "\n\n" + c("90", "  " + HELP)
            # Anything that arrived while the frame was being built is about
            # the frame before it. Cleared here so the wait below only sees a
            # resize that happened to what is now on screen.
            _resized = False
            paint(frame)
            note = ""
            leave_copy_mode(me)

            # Faster while something is coming up, and only then: the spinner
            # has to move, and those are the seconds where the list is telling
            # you about something that changes by the second. Everything else
            # on the screen is worth REFRESH and no more.
            deadline = time.time() + (SPINUP_TICK if waiting_on else REFRESH)
            while True:
                if _resized:
                    break               # the frame on screen is the wrong shape
                left = deadline - time.time()
                if left <= 0:
                    break
                if not raw and ctl is None:
                    time.sleep(min(left, 0.5))
                    continue
                # Read raw and unbuffered. An arrow is three bytes in one write,
                # and anything that reads a byte at a time through sys.stdin
                # loses the other two into Python's buffer, where select() can
                # no longer see them.
                if not typed:
                    watching = ([sys.stdin] if raw else []) \
                        + ([ctl] if ctl is not None else [])
                    ready = select.select(watching, [], [], min(left, 0.25))[0]
                    if ctl is not None and ctl in ready:
                        acted = False
                        for verb, arg in ctl_read(ctl):
                            note, _ = do_ctl(verb, arg, heads)
                            acted = True
                        if acted:
                            held = None
                            break
                    if sys.stdin not in ready:
                        continue
                    typed = os.read(sys.stdin.fileno(), 64).decode(
                        "utf-8", "replace")
                key, typed = split_key(typed)
                if not key:
                    continue

                if key.startswith("\x1b") and len(key) > 1:
                    way = arrow_of(key[1:])
                    if way in ("left", "pgup", "right", "pgdn"):
                        picked = jump_group(heads, picked,
                                            -1 if way in ("left", "pgup") else 1)
                    elif way:
                        picked = move_selection(heads, picked,
                                                -1 if way == "up" else 1)
                    break

                if key in ("q", "\x03", "\x04"):
                    return 0
                if key in ("\r", "\n"):
                    # Enter opens what you have pointed at. Selecting and
                    # opening are separate now, so something has to join them.
                    sel_head = next((h for h in heads
                                     if h["session_id"] == picked), None)
                    if sel_head:
                        focus(sel_head, heads)
                        held = None
                    break

                if key.isdigit() and key != "0":
                    pick = by_slot(heads, key)
                    if pick:
                        focus(pick, heads)
                        held = None
                    else:
                        note = f"no head {key}"
                    break
                if key == "j":
                    # Walk the queue, and when it is empty park what is up and
                    # then cycle the heads still working - same as Alt+j.
                    note, _ = next_or_park(heads)
                    held = None
                    break
                if key == "g":
                    cur = next((h for h in heads if h["in_main"]), None)
                    if cur:
                        tmux("select-pane", "-t", cur["pane_id"])
                        held = None
                    else:
                        note = "no head in the main window"
                    break
                if key in ("G", "N", "h", "[", "]"):
                    note = queue_key(key, heads, picked)
                    held = None
                    break
                if key == "a":
                    on = set_auto(not auto_enabled())
                    held = None
                    note = ("auto-raise on - waiting heads come to you"
                            if on else "auto-raise off - nothing moves unless you say")
                    break
                if key == "c":
                    for h in heads:
                        clear_attention(h["session_id"])
                    note = "cleared all flags"
                    break
                if key == "n":
                    # Never leave a head unnamed - an unnamed one is only
                    # findable later by its guid, which is no way to find it.
                    suggested = random_name({r["name"] for r in session_list(limit=0)})

                    def ask_name(_):
                        got = ask(f"name for the new head [{suggested}]: ")
                        return got if got is BACK else (got or suggested)

                    # The group comes straight after the name, and Enter skips
                    # it: the head has no session id to hang a group on yet, so
                    # this claims one against the name and the next refresh
                    # hands it over.
                    def ask_group(_):
                        book = queue_load()
                        known = "  ".join(f"{g}:{group_label(book, g)}"
                                          for g in group_ids(book)[:6])
                        got = ask("group [1-9, Enter for none]"
                                  + (f"   {known}" if known else "") + ": ")
                        if got is BACK:
                            return got
                        got = "" if got in ("0", "none") else got
                        if got and not got.isdigit():
                            # Stay on the question. Losing the name you just
                            # typed over a mistyped group is a punishment for
                            # a keystroke.
                            complain(f"'{got}' is not a group")
                            return None
                        return got or ""

                    def ask_where(_):
                        got = ask(f"directory [{DEFAULT_DIR}]: ")
                        if got is BACK:
                            return got
                        where = os.path.expanduser(got or DEFAULT_DIR)
                        if not os.path.isdir(where):
                            complain(f"no such directory: {where}")
                            return None
                        return where

                    answers = flow(ask_name, ask_group, ask_where)
                    if answers is None:
                        note = "cancelled"
                        break
                    name, gid, where = answers
                    if gid:
                        claim_group(name, gid)
                    # Say that the keys were taken, on the spot. The row
                    # appears on the next redraw and says the rest.
                    if spawn(claude_cmd(f"--name {json.dumps(name)}{DANGEROUS}"),
                             where, name, heads):
                        note = f"starting {name} in {where}"
                        if gid:
                            note += (f" - joins {group_label(queue_load(), gid)}"
                                     f" when it comes up")
                    break
                if key == "r":
                    rows = session_list(limit=12, hide_live=True)
                    if not rows:
                        note = "no prior sessions to resume"
                        break
                    sys.stdout.write(
                        "\033[H\033[2J"
                        + c("1;97;44", " resume a session ".ljust(78)) + "\n"
                        + render_sessions(rows) + "\n")
                    sys.stdout.flush()
                    pick = ask("resume which? [number, Esc to cancel] ")
                    if pick is BACK or not pick:
                        note = "cancelled"
                        break
                    if not (pick.isdigit() and 1 <= int(pick) <= len(rows)):
                        note = f"no session {pick}"
                        break
                    row = rows[int(pick) - 1]
                    where = (row["cwd"] if os.path.isdir(row["cwd"] or "")
                             else DEFAULT_DIR)
                    if spawn(claude_cmd(f"--resume {row['session_id']}{DANGEROUS}"),
                             where, row["name"], heads):
                        note = f"resuming {row['name']}"
                    break
                if key == "x":
                    cur = (next((h for h in heads
                                 if h["session_id"] == picked), None)
                           or next((h for h in heads if h["in_main"]), None))
                    def ask_target(_):
                        got = ask("kill which? [number, or Enter for "
                                  f"{cur['name'] if cur else 'none'}] ")
                        if got is BACK:
                            return got
                        target = by_slot(heads, got) if got else cur
                        if not target:
                            complain(f"no head {got}" if got
                                     else "nothing selected to kill")
                            return None
                        return target

                    def ask_sure(answers):
                        target = answers[0]
                        got = ask(f"kill {target['name']} (pid {target['pid']}, "
                                  f"{target['kind']})? [y/N] ")
                        if got is BACK:
                            return got     # back to which head, not out
                        return got.lower() in ("y", "yes")

                    answers = flow(ask_target, ask_sure)
                    if answers is None:
                        note = "cancelled"
                        break
                    target, sure = answers
                    # The waiting happens on a thread; what it finds is picked
                    # up by the loop below within a refresh.
                    note = kill_head(target, wait=False) if sure else "cancelled"
                    break
                if key == "R":
                    # Pick up an edited checkout. Handled after the loop, so
                    # the terminal is back to normal before we hand over.
                    return RESTART
                if key == "?":
                    note = HELP
                    break
    except KeyboardInterrupt:
        return 0
    finally:
        # Cursor back before the screen, so it is the shell's own screen that
        # gets it: leaving the alternate screen first would put the show-cursor
        # onto the one we are about to abandon.
        sys.stdout.write("\033[?25h\033[?1049l")
        sys.stdout.flush()
        if winch is not None:
            try:
                signal.signal(signal.SIGWINCH, winch)
            except (OSError, ValueError):
                pass
        if raw:
            import termios
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, raw)
        # Take the fifo away before the mark: a key pressed during teardown
        # should fall back to starting its own process, not write into a pipe
        # nothing will ever read.
        ctl_close(ctl)
        retract_bar()
        # Give the pane back. Quitting leaves a shell here, and a shell is where
        # someone starts a head - which must not inherit our mark.
        if me:
            dash_release(me)
    return 0
