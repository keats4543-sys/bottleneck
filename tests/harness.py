"""One handle on the whole package, for tests that want to stub things out.

The program used to be a single file, so a test could set `m.focus = fake` and
every caller saw it. Split across modules, `from .tmuxio import dash_pane` gives
each importer its own binding, and patching the module that defines a name
misses everyone who already imported it.

So this patches every module that holds the name, which is what the old
single-module version did in effect. Reading falls back through the modules in
dependency order, so `bn.render`, `bn.SLOTS` and `bn.collect` all resolve
without the test caring where they live.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Somewhere empty, before anything reads it. config.py loads ~/.bottleneck/config
# at import, which is the point of it - but it would mean the person running
# these tests could fail them with a line in their own config file, and a test
# that passes on one machine because of a setting is worse than no test. Every
# test that writes state already points that file at a temp directory; this is
# the same rule, applied to the settings and applied before the import.
os.environ["BOTTLENECK_STATE"] = tempfile.mkdtemp(prefix="bottleneck-tests-")

from bottleneck import (catalog, cli, config, heads, panes, procs, store,  # noqa: E402
                        tmuxio, transcript, ui)

MODULES = (config, store, procs, tmuxio, transcript, catalog, heads, panes,
           ui, cli)


class Package:
    def __getattr__(self, name):
        for mod in MODULES:
            if hasattr(mod, name):
                return getattr(mod, name)
        raise AttributeError(f"nothing in bottleneck is called {name!r}")

    def __setattr__(self, name, value):
        landed = False
        for mod in MODULES:
            if hasattr(mod, name):
                setattr(mod, name, value)
                landed = True
        if not landed:
            raise AttributeError(
                f"nothing in bottleneck is called {name!r} - a test that stubs "
                f"a name nothing uses is testing itself")


bn = Package()
