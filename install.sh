#!/usr/bin/env bash
# bottleneck mount/unmount.
#
#   bash install.sh              mount (idempotent)
#   bash install.sh --unmount    unmount, leave the checkout and your state alone
#   bash install.sh --purge      unmount and delete the state directory too
#
# Mounting symlinks this checkout into place and touches exactly four things
# outside it:
#   ~/.local/bin/{bottleneck,bottleneck-new,bn}   symlinks to bin/
#   ~/.claude/hooks/bottleneck-attention.py       symlink to hooks/attention.py
#   ~/.claude/hooks/bottleneck-<module>.py        one per module that wants one
#   ~/.tmux.conf                                  one `source-file` line
#   ~/.claude/settings.json                       hooks (global - every project)
# Symlinks, so editing the checkout takes effect at once with no reinstall.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE="${BOTTLENECK_STATE:-$HOME/.bottleneck}"
BINDIR="$HOME/.local/bin"
HOOK_SRC="$REPO/hooks/attention.py"
HOOK_DST="$HOME/.claude/hooks/bottleneck-attention.py"
TMUXCONF="$HOME/.tmux.conf"
MARK="# >>> bottleneck >>>"
ENDMARK="# <<< bottleneck <<<"
# -S skips site processing, which is a third of python's startup and nothing a
# hook wants: they are standard library only, on purpose. A hook is a command
# claude spawns per event, so startup is nearly the whole cost of one.
HOOK_CMD='python3 -S "$HOME"/.claude/hooks/bottleneck-attention.py'
MODE="${1:-mount}"
SESSION_NAME="${BOTTLENECK_TMUX_SESSION:-bottleneck}"

# What the modules in this checkout want installed, asked of them rather than
# listed here: {name, hook, events} for each. This script knows what a claude
# hook is and nothing whatever about what a module does with one, so a branch
# that carries a module adds no line to this file.
#
# Empty when python cannot answer - no modules, or a broken checkout. Mounting
# the core alone is still the right outcome then.
modules_json() {
    BOTTLENECK_STATE="$STATE" python3 -c 'import sys, json; sys.path.insert(0, sys.argv[1]); from bottleneck import modules; print(json.dumps(modules.wiring()))' "$REPO" 2>/dev/null || echo "[]"
}

MODULES_JSON="$(modules_json)"
[ -n "$MODULES_JSON" ] || MODULES_JSON="[]"

hooks_py() {  # $1 = add|remove
python3 - "$1" "$HOOK_CMD" "$MODULES_JSON" <<'HOOKSPY'
import json, os, sys
mode, cmd, modules_json = sys.argv[1:4]
p = os.path.expanduser("~/.claude/settings.json")
hookdir = os.path.expanduser("~/.claude/hooks")

# The core's own hook, and then whatever the modules asked for. A module names
# its events and its matchers; where its hook is installed and how it is
# started is this script's business, and the same for all of them.
WIRING = [(cmd, [("Notification", None), ("Stop", None),
                 ("UserPromptSubmit", None), ("SessionEnd", None)])]
for mod in json.loads(modules_json or "[]"):
    WIRING.append(('python3 -S "$HOME"/.claude/hooks/bottleneck-%s.py' % mod["name"],
                   [(ev, matcher) for ev, matcher in mod["events"]]))

try:
    with open(p) as fh:
        cfg = json.load(fh)
except FileNotFoundError:
    cfg = {}
except ValueError:
    sys.exit(f"{p} is not valid JSON - fix it by hand first")

hooks = cfg.get("hooks") or {}
before = json.dumps(cfg, sort_keys=True)

# Every command we might ever have installed, so that ours come out of every
# event whether or not we want it today. A module that has stopped asking for
# PostToolUse - or has been taken out of the checkout - must not leave wiring
# behind pointing at a hook that is not there any more.
ours = {command for command, _ in WIRING}
for name in sorted(os.listdir(hookdir)) if os.path.isdir(hookdir) else []:
    if name.startswith("bottleneck-") and name.endswith(".py"):
        ours.add('python3 -S "$HOME"/.claude/hooks/%s' % name)
        ours.add('python3 "$HOME"/.claude/hooks/%s' % name)

for ev in list(hooks):
    kept = [g for g in hooks.get(ev) or []
            if not any(h.get("command") in ours for h in (g.get("hooks") or []))]
    if kept:
        hooks[ev] = kept
    else:
        del hooks[ev]

if mode == "add":
    for command, events in WIRING:
        for ev, matcher in events:
            entry = {"hooks": [{"type": "command", "command": command,
                                "timeout": 5}]}
            if matcher:
                entry["matcher"] = matcher
            hooks.setdefault(ev, []).append(entry)

if hooks:
    cfg["hooks"] = hooks
elif "hooks" in cfg:
    del cfg["hooks"]

changed = json.dumps(cfg, sort_keys=True) != before
if changed:
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(cfg, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, p)
print("hooks", ("added" if mode == "add" else "removed") if changed
      else ("already wired" if mode == "add" else "already clean"))
HOOKSPY
}

strip_tmux_block() {
    [ -f "$TMUXCONF" ] || return 0
    python3 - "$TMUXCONF" "$MARK" "$ENDMARK" <<'PY'
import os, sys
path, mark, end = sys.argv[1], sys.argv[2], sys.argv[3]
lines = open(path).read().splitlines(True)
out, skip = [], False
for l in lines:
    if l.strip() == mark:
        skip = True
        continue
    if l.strip() == end:
        skip = False
        continue
    if not skip:
        out.append(l)
text = "".join(out).strip()
if text:
    open(path, "w").write(text + "\n")
else:
    os.remove(path)          # we created it; leave no empty file behind
    print("removed", path)
PY
}

unlink_ours() {   # remove each path, but only if it is a symlink into $REPO
    local p target kept=0 gone=0
    for p in "$@"; do
        [ -e "$p" ] || [ -L "$p" ] || continue
        if [ -L "$p" ] && target="$(readlink -f "$p" 2>/dev/null)" \
           && [ "${target#"$REPO"/}" != "$target" ]; then
            rm -f "$p"
            gone=$((gone + 1))
        else
            echo "left $p alone - not a symlink into $REPO" >&2
            kept=$((kept + 1))
        fi
    done
    if [ "$kept" -gt 0 ]; then
        echo "removed $gone symlink(s), left $kept in place"
    else
        echo "removed $gone symlink(s)"
    fi
}

link() {   # $1 = source, $2 = destination. Never clobbers a real file.
    if [ -L "$2" ] || [ ! -e "$2" ]; then
        ln -sfn "$1" "$2"
    else
        echo "refusing to replace $2 - it is a real file, not a symlink" >&2
        exit 1
    fi
}

case "$MODE" in
mount)
    command -v tmux >/dev/null || { echo "tmux is not installed" >&2; exit 1; }
    command -v claude >/dev/null || echo "note: claude is not on PATH yet" >&2

    mkdir -p "$BINDIR" "$HOME/.claude/hooks" "$STATE/attention" "$STATE/acks"
    chmod +x "$REPO/bin/bottleneck" "$REPO/bin/bottleneck-new" "$HOOK_SRC"

    link "$REPO/bin/bottleneck"     "$BINDIR/bottleneck"
    link "$REPO/bin/bottleneck-new" "$BINDIR/bottleneck-new"
    link "$REPO/bin/bottleneck"     "$BINDIR/bn"
    link "$HOOK_SRC"                "$HOOK_DST"
    echo "linked: bottleneck, bottleneck-new, bn -> $REPO/bin"

    # A hook per module, named after it, and whatever state directories it
    # asked for. Everything here is driven by what the modules said they
    # wanted; nothing in this file knows one from another.
    for d in $(BOTTLENECK_STATE="$STATE" python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); from bottleneck import modules; print("\n".join(modules.state_dirs()))' "$REPO" 2>/dev/null); do
        mkdir -p "$STATE/$d"
    done
    printf '%s' "$MODULES_JSON" | python3 -c 'import json,sys; print("\n".join("%s\t%s" % (m["name"], m["hook"]) for m in json.load(sys.stdin) or []))' 2>/dev/null \
    | while IFS="$(printf '\t')" read -r name hook; do
        [ -n "$name" ] || continue
        chmod +x "$hook" 2>/dev/null || true
        link "$hook" "$HOME/.claude/hooks/bottleneck-$name.py"
        echo "module: $name -> ~/.claude/hooks/bottleneck-$name.py"
    done

    # And the ones that are no longer wanted: a module turned off, or gone from
    # the branch since the last mount, leaves a hook file behind that nothing
    # refers to any more.
    #
    # Whichever checkout it points into. That used to be limited to symlinks
    # into this one, and the case it got wrong is now the ordinary one: with a
    # branch checked out in its own worktree, mounting the core over a mounted
    # module left ~/.claude/hooks/bottleneck-share.py pointing into the
    # worktree. Harmless - the settings sweep above takes its wiring out either
    # way, so nothing runs it - but a link nothing wants, sat where a hook goes,
    # and it accumulates one per branch you try.
    #
    # Two things keep that safe. Only ever a symlink: a real file there is
    # somebody's own hook and none of our business. And only in the
    # bottleneck-*.py namespace, which is ours to keep tidy - the same names
    # the settings sweep above already claims, for the same reason.
    WANTED_NAMES="$(printf '%s' "$MODULES_JSON" | python3 -c 'import json,sys; print(" ".join(m["name"] for m in json.load(sys.stdin) or []))' 2>/dev/null || true)"
    for p in "$HOME"/.claude/hooks/bottleneck-*.py; do
        [ -L "$p" ] || continue
        n="$(basename "$p" .py)"; n="${n#bottleneck-}"
        [ "$n" = "attention" ] && continue
        case " $WANTED_NAMES " in *" $n "*) continue ;; esac
        rm -f "$p"
        echo "module: $n unwired"
    done

    case ":$PATH:" in
        *":$BINDIR:"*) ;;
        *) echo "note: $BINDIR is not on your PATH - add it to your shell rc" >&2 ;;
    esac

    strip_tmux_block
    printf '%s\nsource-file %s\n%s\n' "$MARK" "$REPO/tmux.conf" "$ENDMARK" >> "$TMUXCONF"
    echo "tmux: ~/.tmux.conf sources $REPO/tmux.conf"

    hooks_py add
    tmux source-file "$TMUXCONF" 2>/dev/null && echo "tmux: reloaded" || true
    echo
    BOTTLENECK_STATE="$STATE" python3 "$REPO/bin/bottleneck" modules 2>/dev/null || true
    echo
    echo "mounted. start with:  bottleneck start"
    ;;
--unmount|unmount)
    # Deliberately does NOT kill the tmux session. Every head in it is a live
    # Claude Code process with your work in it; uninstalling a dashboard is no
    # reason to end them. Close the session yourself once they are done.
    strip_tmux_block
    hooks_py remove
    # Every hook we have ever installed, not only the ones this checkout would
    # install today: a module removed from the branch since mounting still has
    # its symlink sitting there, and leaving it is leaving a hook behind.
    # unlink_ours refuses anything that is not a symlink into this checkout.
    unlink_ours "$BINDIR/bottleneck" "$BINDIR/bottleneck-new" "$BINDIR/bn" \
                "$HOOK_DST" "$HOME"/.claude/hooks/bottleneck-*.py
    echo
    echo "unmounted. the checkout and $STATE are untouched."
    if tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
        echo "the '$SESSION_NAME' tmux session is still running, heads and all -"
        echo "  tmux attach -t $SESSION_NAME     to get back to them"
        echo "  tmux kill-session -t $SESSION_NAME   to end them, when you are done"
    fi
    echo "re-mount with:  bash $REPO/install.sh"
    ;;
--purge|purge)
    bash "$0" --unmount || true
    rm -rf "$STATE"
    echo "deleted $STATE (names, flags, catalogue)"
    echo "the checkout at $REPO is still there - delete it by hand"
    ;;
*)
    sed -n '2,14p' "$0"
    exit 2
    ;;
esac
