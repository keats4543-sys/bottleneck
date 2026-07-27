"""Names for heads, and a book of the sessions we have seen.

Claude keeps a head's name only while it runs, so a session you closed is
otherwise findable by guid alone. Everything seen gets written down.
"""
import json
import os
import time

from . import config
from .config import CATALOG, PROJECTS, SESSIONS, c
from .procs import FOREIGN, proc_start, session_records
from .store import read_json, write_json
from .transcript import tail_lines
from .heads import collect


# ---------------------------------------------------------------- the catalog
#
# Claude keeps a session id and, if you set one, a name - but only while the
# head is alive. Once it exits, the name is only recoverable by reading the
# transcript. So we keep our own book: every head the dashboard sees gets
# written down, and nothing is ever named after a guid.

ADJECTIVES = ("amber brisk calm clever coral dusk eager fern flint golden "
              "hazel iron jade keen lunar mellow north opal pearl quiet river "
              "slate swift tidal umber vivid warm wren zinc bright still").split()


NOUNS = ("otter falcon cedar harbor lantern meadow anchor badger comet delta "
         "ember fjord grove heron isle kestrel larch marsh nectar orchard "
         "pike quarry ridge summit thicket vale willow yarrow basin cliff").split()


def random_name(taken=()):
    """A short name you can say out loud. Never a guid.

    Seeded from the clock and the pid rather than `random`, so two heads started
    in the same second by different processes do not collide.
    """
    taken = set(taken)
    seed = int(time.time() * 1000) ^ (os.getpid() << 12)
    for i in range(400):
        n = seed + i * 2654435761
        name = f"{ADJECTIVES[(n >> 7) % len(ADJECTIVES)]}-{NOUNS[(n >> 17) % len(NOUNS)]}"
        if name not in taken:
            return name
        if i > 200:                      # crowded - fall back to a suffix
            name = f"{name}-{(n >> 3) % 100:02d}"
            if name not in taken:
                return name
    return f"head-{int(time.time()) % 100000}"


def catalog_load():
    try:
        with open(CATALOG) as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def catalog_save(book):
    tmp = CATALOG + ".tmp"
    try:
        with open(tmp, "w") as fh:
            json.dump(book, fh, indent=1, sort_keys=True)
        os.replace(tmp, CATALOG)
    except OSError:
        pass


# How long a last_seen may sit in memory before the file has to hear about it.
#
# The clock is not what makes the book dirty. last_seen moves on every pass by
# definition, so comparing whole records made every refresh a rewrite: the whole
# catalogue - which grows with every session you have ever run - re-serialised
# sorted and indented, written to a scratch file and renamed, every couple of
# seconds, for a timestamp read at a resolution of minutes. So a name, a
# directory, or a session we have not seen before is what counts as a change.
#
# The timestamp still has to land, or "when did I last touch this" would be as
# stale as the last real change - which for a head that quietly ran all
# afternoon is the morning. So it forces a write of its own, occasionally.
CATALOG_FLUSH = 60

_flushed = 0.0


def catalog_note(heads):
    """Write down every head we can see. Cheap enough to run each refresh."""
    global _flushed
    book = catalog_load()
    dirty = False
    now = time.time()
    for h in heads:
        sid = h.get("session_id")
        if not sid:
            continue
        rec = book.get(sid, {})
        was = dict(rec)
        rec.setdefault("first_seen", now)
        rec["last_seen"] = now
        if h.get("cwd"):
            rec["cwd"] = h["cwd"]
        # A name Claude generated for itself beats nothing, but never overwrite
        # a name you chose with one it made up later.
        name = h.get("name") or ""
        looks_like_guid = bool(name) and sid.startswith(name.split("-")[0][:8])
        # "derived" means Claude named it after the folder, so every head in one
        # repo gets the same name. That must not displace a name already on the
        # books - one you chose, or the summary read out of the transcript.
        weak = h.get("name_source") == "derived" and rec.get("name")
        if name and not looks_like_guid and not weak and rec.get("name") != name:
            if not rec.get("pinned"):
                rec["name"] = name
        if not rec.get("name"):
            # It arrived unnamed, or named after its own guid. Give it a real
            # one now - by the time you come looking, the guid tells you nothing.
            rec["name"] = random_name({r.get("name") for r in book.values()})
            rec["auto_named"] = True
        book[sid] = rec
        dirty = dirty or told(rec) != told(was)
    if dirty or now - _flushed >= CATALOG_FLUSH:
        catalog_save(book)
        _flushed = now
    return book


def told(rec):
    """A record without the part of it that moves on its own."""
    return {k: v for k, v in rec.items() if k != "last_seen"}


def _scan_meta(lines, into):
    for line in lines:
        try:
            d = json.loads(line)
        except (ValueError, TypeError):
            continue
        t = d.get("type")
        if t == "custom-title" and d.get("customTitle"):
            into["custom"] = d["customTitle"]
        elif t == "agent-name" and d.get("agentName"):
            into["agent"] = d["agentName"]
        elif t == "ai-title" and d.get("aiTitle"):
            into["ai"] = d["aiTitle"]
        if d.get("cwd"):
            into["cwd"] = d["cwd"]
        if d.get("gitBranch"):
            into["branch"] = d["gitBranch"]


def session_meta(path):
    """Name, directory and branch for one transcript.

    Only for backfilling sessions that ended before the catalog existed. Titles
    are appended as they change, so the last one wins and the tail is where to
    look - these files run to tens of megabytes, and reading dozens of them
    whole would make the list too slow to be worth having. The head is read only
    to fill in what the tail lacked.
    """
    blank = {"custom": "", "agent": "", "ai": "", "cwd": "", "branch": ""}
    meta = dict(blank)
    _scan_meta(tail_lines(path, count=800), meta)
    if not (meta["custom"] or meta["agent"] or meta["ai"]) or not meta["cwd"]:
        head = dict(blank)
        try:
            with open(path, errors="replace") as fh:
                _scan_meta([line for _, line in zip(range(300), fh)], head)
        except OSError:
            pass
        for k, v in head.items():
            if not meta[k] and v:
                meta[k] = v
    return meta


def transcripts():
    """(mtime, path, session_id) for every transcript on disk."""
    out, seen = [], set()
    for projects in config.PROJECT_DIRS:
        for proj in os.listdir(projects) if os.path.isdir(projects) else []:
            d = os.path.join(projects, proj)
            if not os.path.isdir(d):
                continue
            for fname in os.listdir(d):
                if not fname.endswith(".jsonl") or fname[:-6] in seen:
                    continue
                path = os.path.join(d, fname)
                try:
                    out.append((os.path.getmtime(path), path, fname[:-6]))
                except OSError:
                    continue
                seen.add(fname[:-6])
    out.sort(reverse=True)
    return out


def catalog_backfill(limit=None, verbose=False):
    """Fold sessions that predate the catalog into it, newest first."""
    book = catalog_load()
    added = 0
    for mtime, path, sid in transcripts():
        if limit is not None and added >= limit:
            break
        rec = book.get(sid, {})
        if rec.get("name") and rec.get("cwd"):
            continue
        meta = session_meta(path)
        name = meta["custom"] or meta["agent"] or meta["ai"]
        if not name:
            # Nothing to go on - give it a real name rather than a guid stub.
            name = random_name({r.get("name") for r in book.values()})
            rec["auto_named"] = True
        rec["name"] = rec.get("name") or name
        rec["cwd"] = rec.get("cwd") or meta["cwd"]
        if meta["branch"]:
            rec.setdefault("branch", meta["branch"])
        rec.setdefault("first_seen", mtime)
        rec["last_seen"] = max(rec.get("last_seen", 0), mtime)
        book[sid] = rec
        added += 1
        if verbose:
            print(f"  {name}  ({sid[:8]}, {os.path.basename(rec['cwd'] or '?')})")
    if added:
        catalog_save(book)
    return added


def live_ids():
    """Session ids running right now."""
    out = set()
    for rec in session_records():
        sid = rec.get("sessionId")
        if not sid:
            continue
        # A foreign head's pid is not ours to look up - /proc would be answering
        # about a different process that happens to share the number.
        if rec.get(FOREIGN):
            out.add(sid)
        elif os.path.isdir("/proc/%s" % rec.get("pid")):
            out.add(sid)
    return out


def session_list(limit=15, where=None, hide_live=False):
    """Prior sessions, newest first, ready to resume."""
    # Catch up before listing, so this is right whether or not a dashboard is
    # running: heads alive now, then anything from before the catalogue existed.
    book = catalog_note(collect())
    if not book:
        catalog_backfill()
        book = catalog_load()
    live = live_ids()
    rows = []
    for sid, rec in book.items():
        cwd = rec.get("cwd") or ""
        if where and os.path.abspath(cwd) != os.path.abspath(where):
            continue
        if hide_live and sid in live:
            continue
        rows.append({
            "session_id": sid,
            "name": rec.get("name") or sid[:8],
            "cwd": cwd,
            "branch": rec.get("branch") or "",
            "last_seen": rec.get("last_seen") or 0,
            "live": sid in live,
            # A pinned name is one you chose, held against the name the head
            # runs under. Worth saying, or the two can differ with nothing on
            # screen to explain why.
            "pinned": bool(rec.get("pinned")),
        })
    rows.sort(key=lambda r: -r["last_seen"])
    return rows[:limit] if limit else rows
