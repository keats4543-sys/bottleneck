"""Every name a module uses is a name it can see.

Splitting one file into ten turns every shared name into an import someone has
to remember, and the ones you find by running the program are only the ones on
paths you ran. `bottleneck start` attaches to tmux, so it never runs in a test -
and that is exactly where the first missing import was found, by a user.

So this reads the modules instead of running them: walk each function, work out
which names it can see - its own arguments and locals, the module's globals and
imports, the builtins - and report any load of a name that is none of those.
"""
import ast
import builtins
import os
import sys

PKG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "bottleneck")
BUILTINS = set(dir(builtins)) | {"__file__", "__name__", "__doc__", "__spec__",
                                 "__package__", "__loader__"}
FAILED = []


def args_of(fn):
    a = fn.args
    got = {x.arg for x in a.args + a.kwonlyargs + getattr(a, "posonlyargs", [])}
    for extra in (a.vararg, a.kwarg):
        if extra:
            got.add(extra.arg)
    return got


def bound_by(node, into):
    """Names this statement binds, at whatever level it appears."""
    for sub in ast.walk(node):
        if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            into.add(sub.name)
        elif isinstance(sub, ast.Name) and isinstance(sub.ctx, (ast.Store, ast.Del)):
            into.add(sub.id)
        elif isinstance(sub, (ast.Import, ast.ImportFrom)):
            for alias in sub.names:
                into.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(sub, ast.ExceptHandler) and sub.name:
            into.add(sub.name)
        elif isinstance(sub, (ast.Global, ast.Nonlocal)):
            into.update(sub.names)
        elif isinstance(sub, ast.Lambda):
            # A lambda's arguments are visible inside it, and a lambda is the
            # one scope that has no name of its own to walk into.
            into.update(args_of(sub))
    return into


def check_module(path):
    tree = ast.parse(open(path).read(), filename=path)
    module_names = bound_by(tree, set())
    bad = []

    NESTED = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)

    def loads_here(node):
        """Name loads in this scope, stopping at the edge of an inner one."""
        for sub in ast.iter_child_nodes(node):
            if isinstance(sub, NESTED):
                continue                    # its own scope, checked separately
            if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
                yield sub
            yield from loads_here(sub)

    def scopes_in(node):
        """Every inner function and lambda, wherever it is written."""
        for sub in ast.iter_child_nodes(node):
            if isinstance(sub, NESTED):
                yield sub
            else:
                yield from scopes_in(sub)

    def check_scope(node, visible):
        if isinstance(node, ast.ClassDef):
            inner = set(visible) | bound_by(node, set())
        elif isinstance(node, ast.Lambda):
            inner = set(visible) | args_of(node)
        else:
            inner = set(visible) | args_of(node) | bound_by(node, set())
        body = node.body if isinstance(node.body, list) else [node.body]
        for stmt in body:
            # A def written directly in this body is its own scope, not a
            # statement to read through: its arguments are not ours.
            if isinstance(stmt, NESTED):
                check_scope(stmt, inner)
                continue
            for name in loads_here(stmt):
                if name.id not in inner and name.id not in BUILTINS:
                    bad.append((name.lineno, name.id))
            for sub in scopes_in(stmt):
                check_scope(sub, inner)

    for name in loads_here(tree):
        if name.id not in module_names and name.id not in BUILTINS:
            bad.append((name.lineno, name.id))
    for scope in scopes_in(tree):
        check_scope(scope, module_names)
    # A name can be flagged more than once; report each once, in order.
    seen, out = set(), []
    for line, name in bad:
        if name not in seen:
            seen.add(name)
            out.append((line, name))
    return out


print("\nevery module can see every name it uses")
for fname in sorted(os.listdir(PKG)):
    if not fname.endswith(".py"):
        continue
    missing = check_module(os.path.join(PKG, fname))
    if missing:
        FAILED.append(fname)
        print(f"  FAIL {fname}")
        for line, name in missing:
            print(f"         {fname}:{line}  {name} is not defined or imported")
    else:
        print(f"  ok   {fname}")

for extra in ("bin/bottleneck", "hooks/attention.py", "tools/demo.py",
              "tools/ansi2svg.py"):
    path = os.path.join(os.path.dirname(PKG), extra)
    if not os.path.exists(path):
        continue
    missing = check_module(path)
    if missing:
        FAILED.append(extra)
        print(f"  FAIL {extra}")
        for line, name in missing:
            print(f"         {extra}:{line}  {name} is not defined or imported")
    else:
        print(f"  ok   {extra}")

print("\nall pass" if not FAILED else f"\n{len(FAILED)} FAILED")
sys.exit(1 if FAILED else 0)
