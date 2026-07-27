"""Two processes changing one state file at once.

read_json then write_json is two steps, and a second process fits between them:
both read the same book, both add themselves to their own copy, and whichever
writes second puts back a book that never heard of the first.

That is how a live dashboard went missing from dash.json. A dashboard not in
the book has its pane's mark read as stale and cleared, and the count of
dashboards then disagrees with the number of dashboards - which is what decides
whether the movement keys can name a pane, so every M-j after that started a
program to work out where to go.

These tests run the real thing in real processes. They failed before the lock.
"""
import json
import os
import tempfile

from harness import bn as m

TMP = tempfile.mkdtemp()
FAILED = []
WRITERS = 12


def check(label, got, want):
    ok = got == want
    print(("  ok   " if ok else "  FAIL ") + label.ljust(48)
          + ("" if ok else f"\n         got  {got!r}\n         want {want!r}"))
    if not ok:
        FAILED.append(label)


def in_parallel(work, n=WRITERS):
    """Run work(i) in n forked processes, all released at the same moment."""
    r, w = os.pipe()
    kids = []
    for i in range(n):
        pid = os.fork()
        if pid == 0:                       # child
            try:
                os.close(w)
                os.read(r, 1)              # wait for the starting gun
                work(i)
            finally:
                os._exit(0)
        kids.append(pid)
    os.close(r)
    os.write(w, b"go" * n)                 # release them together
    os.close(w)
    for pid in kids:
        os.waitpid(pid, 0)


print("\nevery writer is in the book it joined")
path = os.path.join(TMP, "dash.json")


def register(i):
    m.update_json(path, lambda book: {**book, f"%{i}": {"pid": os.getpid()}})


in_parallel(register)
book = m.read_json(path, {})
check(f"all {WRITERS} claims survive", len(book), WRITERS)
check("and none of them is a fragment",
      sorted(book, key=lambda k: int(k[1:])), [f"%{i}" for i in range(WRITERS)])

print("\nand the file is never left unreadable")
path2 = os.path.join(TMP, "churn.json")
m.write_json(path2, {"seed": 1})


def churn(i):
    for _ in range(20):
        m.update_json(path2, lambda book: {**book, str(i): i})


in_parallel(churn, n=6)
with open(path2) as fh:
    check("still valid json after 120 writes", type(json.load(fh)), dict)
check("with every writer present", sorted(m.read_json(path2, {})),
      sorted(["seed"] + [str(i) for i in range(6)]))

print("\nthe scratch file is this process's own")
# One .tmp name shared by every writer means two of them open, truncate and
# write the same file before either renames it. Catching that by racing it is
# luck; the name is the fact, so the name is what is checked.
seen = []
real_replace = m.os.replace
m.os.replace = lambda src, dst: (seen.append(src), real_replace(src, dst))[1]
try:
    m.write_json(os.path.join(TMP, "one.json"), {"a": 1})
finally:
    m.os.replace = real_replace
check("the scratch file is named after this process",
      str(os.getpid()) in os.path.basename(seen[0]), True)
check("and the write landed", m.read_json(os.path.join(TMP, "one.json"), {}),
      {"a": 1})

print("\na change still happens when the lock cannot be taken")
locked = os.path.join(TMP, "nolock", "x.json")     # directory does not exist
m.update_json(locked, lambda book: {**book, "a": 1})
check("no lock, no crash", m.read_json(locked, {}), {})

print("\nremoving a claim keeps the others")
path3 = os.path.join(TMP, "drop.json")
m.write_json(path3, {f"%{i}": {"pid": i} for i in range(WRITERS)})


def drop(i):
    m.update_json(path3, lambda book: {p: v for p, v in book.items()
                                       if p != f"%{i}"})


in_parallel(drop, n=6)
left = m.read_json(path3, {})
check("only the dropped ones are gone",
      sorted(left, key=lambda k: int(k[1:])),
      [f"%{i}" for i in range(6, WRITERS)])

print("\nall pass" if not FAILED else f"\n{len(FAILED)} FAILED")
raise SystemExit(1 if FAILED else 0)
