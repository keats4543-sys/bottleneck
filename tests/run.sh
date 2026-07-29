#!/usr/bin/env bash
# Run every test. They use fakes throughout: no tmux command runs, no pane
# moves, and nothing outside a temp directory is written.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

# Point state at a throwaway directory before python starts: the paths are
# module constants read at import, so this makes the promise above structural
# rather than a thing each test has to remember.
BOTTLENECK_STATE="$(mktemp -d)"
export BOTTLENECK_STATE
trap 'rm -rf "$BOTTLENECK_STATE"' EXIT

fail=0

# The core's tests, then every module's own, wherever they are. A module keeps
# its tests beside its code - that is what makes it one directory to carry
# between branches - so this discovers them rather than listing them, and a
# branch that adds a module adds no line here either. PYTHONPATH is what lets
# a module's test say `from harness import bn`.
# Each test is run with exactly the modules it is about, and no others.
#
# The core's tests get none. They describe what bottleneck does, and a module
# that happens to be checked out must not be able to change the answer: a
# module adding a segment to the status line turned a core test red without
# touching a line of core code, which is a test suite reporting the checkout
# rather than the code. A module's own tests get that module, taken from the
# path it lives at, so they never have to name themselves twice.
for t in test_*.py ../bottleneck/modules/*/tests/test_*.py; do
    [ -f "$t" ] || continue
    case "$t" in
        ../bottleneck/modules/*) want="${t#../bottleneck/modules/}"
                                 want="${want%%/*}" ;;
        *) want="none" ;;
    esac
    echo "--- $t"
    BOTTLENECK_MODULES="$want" \
    PYTHONPATH="$PWD${PYTHONPATH:+:$PYTHONPATH}" python3 "$t" || fail=1
done

# nullglob, so a checkout carrying no modules compiles what it has rather than
# handing python a pattern that matched nothing. The core is the case with none.
shopt -s nullglob
python3 -m py_compile ../bin/bottleneck ../hooks/*.py ../bottleneck/*.py \
    ../bottleneck/modules/*.py ../bottleneck/modules/*/*.py
shopt -u nullglob

# The README pictures are generated from the real renderer, so a change that
# breaks one breaks the build rather than the reader's first impression of the
# project. The generators are not part of bottleneck and are not in the repo, so
# this runs only where someone has them - which is where a picture would be
# redrawn, and so the only place the check can do any good.
if [ -f ../tools/demo.py ]; then
    python3 ../tools/demo.py --svg "$BOTTLENECK_STATE/demo.svg" >/dev/null
    # The second picture only hand-writes the head's half; its left pane is
    # the same renderer, so it breaks the same way and is checked the same way.
    python3 ../tools/demo_side.py --svg "$BOTTLENECK_STATE/demo-side.svg" >/dev/null
    python3 - "$BOTTLENECK_STATE/demo.svg" "$BOTTLENECK_STATE/demo-side.svg" <<'PY'
import sys, xml.dom.minidom as d
for path in sys.argv[1:]:
    svg = open(path).read()
    d.parseString(svg)                   # it has to be valid XML
    # The animation must be markup, not a stylesheet: hosts sanitise <style>
    # out of the SVGs they serve, and the picture would sit frozen on frame one.
    assert "<style" not in svg, f"{path}: back to CSS - it will not animate"
    assert svg.count("<animate ") >= 2, f"{path}: no animation in the picture"
    assert 'opacity="1"' in svg, f"{path}: nothing visible without animation"
PY
    echo "--- pictures ok"
else
    echo "--- pictures skipped (not in this checkout)"
fi
bash -n ../bin/bottleneck-new
bash -n ../install.sh
echo "--- syntax ok"

exit "$fail"
