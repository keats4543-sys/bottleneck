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
for t in test_*.py; do
    echo "--- $t"
    python3 "$t" || fail=1
done

python3 -m py_compile ../bin/bottleneck ../hooks/attention.py ../bottleneck/*.py

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
