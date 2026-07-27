"""Tests for the bottleneck session catalogue. Writes only to a temp dir."""
import json
import os
import shutil
import tempfile

from harness import bn as m

TMP = tempfile.mkdtemp(prefix="bottleneck-test-")
m.CATALOG = os.path.join(TMP, "catalog.json")
m.live_ids = lambda: set()
m.collect = lambda: []          # keep real sessions out of the test catalogue

results = []


def check(label, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        print(f"        got={got!r}\n        want={want!r}")
    results.append(ok)
    return ok


def head(sid, name, cwd="/home/dev/proj"):
    return {"session_id": sid, "name": name, "cwd": cwd}


# --- names are never guids ---------------------------------------------------
n = m.random_name()
check("random name has two words", len(n.split("-")), 2)
check("random name is not a guid", any(ch.isdigit() for ch in n.split("-")[0]), False)

many = set()
for _ in range(50):
    many.add(m.random_name(many))
check("random names avoid collisions", len(many), 50)

crowded = set()
for a in m.ADJECTIVES:
    for b in m.NOUNS:
        crowded.add(f"{a}-{b}")
check("still yields a name when the whole space is taken",
      m.random_name(crowded) not in crowded, True)

# --- the catalogue remembers -------------------------------------------------
m.catalog_note([head("aaa", "grains"), head("bbb", "vol-desk")])
book = m.catalog_load()
check("both heads written down", sorted(book), ["aaa", "bbb"])
check("name kept", book["aaa"]["name"], "grains")
check("directory kept", book["aaa"]["cwd"], "/home/dev/proj")

# a head that later reports a different name updates the book
m.catalog_note([head("aaa", "grains-v2")])
check("name follows a rename", m.catalog_load()["aaa"]["name"], "grains-v2")

# ...unless you pinned it yourself
book = m.catalog_load()
book["aaa"]["name"] = "my choice"
book["aaa"]["pinned"] = True
m.catalog_save(book)
m.catalog_note([head("aaa", "something-claude-made-up")])
check("a pinned name is never overwritten",
      m.catalog_load()["aaa"]["name"], "my choice")

# ...until you take the pin off, after which the running name wins again
m.main(["unpin", "aaa"])
check("unpinning drops the flag", "pinned" in m.catalog_load()["aaa"], False)
check("but leaves the name it was defending",
      m.catalog_load()["aaa"]["name"], "my choice")
m.catalog_note([head("aaa", "what-it-runs-as")])
check("which the next refresh then overwrites",
      m.catalog_load()["aaa"]["name"], "what-it-runs-as")
check("unpinning twice is not an error", m.main(["unpin", "aaa"]), 0)
check("unpinning something that does not exist says so",
      m.main(["unpin", "no-such-session"]), 1)
check("and unpin with no argument is a usage error", m.main(["unpin"]), 2)
check("a listed row says whether it is pinned",
      "pinned" in m.session_list(limit=0)[0], True)

# a head whose name is just its guid prefix gets a real name instead
m.catalog_note([head("ccc12345", "ccc12345")])
got = m.catalog_load()["ccc12345"]
check("guid-looking name replaced, not stored", got["name"] == "ccc12345", False)
check("replacement is a real name", len(got["name"].split("-")) >= 2, True)
check("and is marked as ours", got.get("auto_named"), True)

# an unnamed head is named too
m.catalog_note([{"session_id": "eee", "name": "", "cwd": "/tmp"}])
check("unnamed head gets a name", bool(m.catalog_load()["eee"]["name"]), True)

# a folder-derived name must not displace a name already on the books
m.catalog_note([dict(head("fff", "Research grain carry spreads"))])
m.catalog_note([dict(head("fff", "proj"), name_source="derived")])
check("derived name does not displace a stored one",
      m.catalog_load()["fff"]["name"], "Research grain carry spreads")

# ...but a derived name is better than none at all
m.catalog_note([dict(head("ggg", "proj"), name_source="derived")])
check("derived name used when nothing is stored",
      m.catalog_load()["ggg"]["name"], "proj")

# last_seen advances, first_seen does not
before = m.catalog_load()["bbb"]
m.catalog_note([head("bbb", "vol-desk")])
after = m.catalog_load()["bbb"]
check("first_seen is stable", after["first_seen"], before["first_seen"])
check("last_seen moves forward", after["last_seen"] >= before["last_seen"], True)

# --- listing -----------------------------------------------------------------
rows = m.session_list(limit=0)
check("listing returns what we stored", sorted(r["session_id"] for r in rows),
      ["aaa", "bbb", "ccc12345", "eee", "fff", "ggg"])
check("no row is named after its guid",
      [r["name"] for r in rows if r["session_id"].startswith(r["name"][:6])], [])

rows = m.session_list(limit=0, where="/nowhere")
check("filtering by directory works", rows, [])

# render must not blow up on odd input
m.render_sessions(m.session_list(limit=0), width=60)
m.render_sessions([], width=60)
check("render survives an empty list", True, True)

# --- the file is not rewritten for a clock tick ------------------------------
# last_seen moves every pass by definition, so comparing whole records made
# every refresh a rewrite of the whole book - which grows with every session you
# have ever run. Only a real change writes now; the timestamp lands on a timer
# of its own.


def written_at():
    return os.stat(m.CATALOG).st_mtime_ns


m.catalog_note([head("bbb", "vol-desk")])       # settle any pending write
stamp = written_at()
m.catalog_note([head("bbb", "vol-desk")])
check("nothing new means nothing written", written_at(), stamp)
m.catalog_note([head("bbb", "vol-desk", cwd="/somewhere/else")])
check("a change is written at once",
      m.catalog_load()["bbb"]["cwd"], "/somewhere/else")

m.catalog_note([head("hhh", "brand-new")])
check("so is a head we have not seen before",
      "hhh" in m.catalog_load(), True)

# ...but the timestamp still has to land, or "when did I last touch this" would
# be as stale as the last real change.
before = m.catalog_load()["bbb"]["last_seen"]
m.catalog_note([head("bbb", "vol-desk", cwd="/somewhere/else")])
check("an unwritten last_seen is still returned in the book",
      m.catalog_note([head("bbb", "vol-desk", cwd="/somewhere/else")]
                     )["bbb"]["last_seen"] > before, True)
m._flushed = 0.0                                # as if the flush were overdue
m.catalog_note([head("bbb", "vol-desk", cwd="/somewhere/else")])
check("and the timer flushes it to the file on its own",
      m.catalog_load()["bbb"]["last_seen"] > before, True)

# --- corrupt catalogue is survivable ----------------------------------------
with open(m.CATALOG, "w") as fh:
    fh.write("{ this is not json")
check("corrupt catalogue reads as empty", m.catalog_load(), {})
m.catalog_note([head("ddd", "recovered")])
check("and is rewritten cleanly", m.catalog_load()["ddd"]["name"], "recovered")

shutil.rmtree(TMP, ignore_errors=True)
print()
print(f"{sum(results)}/{len(results)} passed")
raise SystemExit(0 if all(results) else 1)
