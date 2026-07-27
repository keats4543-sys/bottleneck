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
#   ~/.tmux.conf                                  one `source-file` line
#   ~/.claude/settings.json                       4 hooks (global - every project)
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
HOOK_CMD='python3 "$HOME"/.claude/hooks/bottleneck-attention.py'
MODE="${1:-mount}"
SESSION_NAME="${BOTTLENECK_TMUX_SESSION:-bottleneck}"

hooks_py() {  # $1 = add|remove
python3 - "$1" "$HOOK_CMD" <<'PY'
import json, os, sys
mode, cmd = sys.argv[1], sys.argv[2]
p = os.path.expanduser("~/.claude/settings.json")
events = ("Notification", "Stop", "UserPromptSubmit", "SessionEnd")

try:
    with open(p) as fh:
        cfg = json.load(fh)
except FileNotFoundError:
    cfg = {}
except ValueError:
    sys.exit(f"{p} is not valid JSON - fix it by hand first")

hooks = cfg.get("hooks") or {}
changed = False

for ev in events:
    entries = hooks.get(ev) or []
    kept = [g for g in entries
            if not any(h.get("command") == cmd for h in (g.get("hooks") or []))]
    if mode == "remove":
        if len(kept) != len(entries):
            changed = True
        if kept:
            hooks[ev] = kept
        elif ev in hooks:
            del hooks[ev]
            changed = True
    else:
        if len(kept) != len(entries):      # already ours
            continue
        entries.append({"hooks": [{"type": "command", "command": cmd, "timeout": 5}]})
        hooks[ev] = entries
        changed = True

if hooks:
    cfg["hooks"] = hooks
elif "hooks" in cfg:
    del cfg["hooks"]

if changed:
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(cfg, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, p)
print("hooks", "added" if mode == "add" else "removed" if changed else "already clean")
PY
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
    echo "mounted. start with:  bottleneck start"
    ;;
--unmount|unmount)
    # Deliberately does NOT kill the tmux session. Every head in it is a live
    # Claude Code process with your work in it; uninstalling a dashboard is no
    # reason to end them. Close the session yourself once they are done.
    strip_tmux_block
    hooks_py remove
    unlink_ours "$BINDIR/bottleneck" "$BINDIR/bottleneck-new" "$BINDIR/bn" "$HOOK_DST"
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
