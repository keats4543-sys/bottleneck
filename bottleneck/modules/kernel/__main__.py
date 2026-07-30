"""`python -m bottleneck.modules.kernel [port]` - run the built-in wrapper.

An entry point of its own rather than running wrap.py directly, for two
reasons: wrap.py imports its stages relatively, so it needs a package to be
relative to, and importing it as `__main__` when the package has already
imported it gives two module objects and a warning about it.
"""
import sys

from .wrap import serve


serve(int(sys.argv[1]) if len(sys.argv) > 1 else None)
