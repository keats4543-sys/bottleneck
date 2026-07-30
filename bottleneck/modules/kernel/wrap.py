"""wrap.py - the system prompt, rewritten on its way past. Nothing else.

A demonstration that this class of feature belongs to a module rather than to a
separate program: about two hundred lines, standard library only, no venv and
no dependency. It exists to prove the shape works, not to replace a proxy that
does the job properly - see the module README for which to run.

What it is: an HTTP proxy on loopback that claude talks to because
ANTHROPIC_BASE_URL says so. Everything it receives it forwards to the real API
with the headers it came with, and streams the answer straight back. The only
thing it changes is the text of the system prompt.

Three rules it will not break, all of them about prompt caching. Caching is a
byte-prefix match, so anything that alters the prefix differently between two
requests in a session turns a cache read into a cache write and costs money
rather than saving it.

  1. The shape of the system field is never touched. The same blocks in the
     same order, each keeping its own cache_control exactly where claude put
     it. Only the text inside a block changes.
  2. Every rewrite is a pure function of the text it is given. No clock, no
     counter, no request id - the same bytes in must always give the same bytes
     out, on the first request of a session and the four hundredth.
  3. A block is never left empty. Claude Code sends parts of its identity as
     blocks of their own, and excising one of those outright leaves "", which
     the API rejects with a 400 - so the kernel goes in the block that was
     emptied, which is where the identity it replaces used to be anyway.

And one rule about being wrong. The sentences excised are pinned to an exact
release of Claude Code; a future one that rewords its opening matches nothing,
the stock identity survives, and the kernel goes in beside it. Nothing fails,
which is the problem - so a real system prompt that any excision missed is
recorded in the usage log, left as a mark for the status line, and reported by
`bottleneck kernel`. The sentences live in identity.json, one copy, read by
both backends.
"""
import http.client
import http.server
import json
import os
import re
import socket
import sys
import threading
import time


HERE = os.path.dirname(os.path.abspath(__file__))
UPSTREAM = os.environ.get("BOTTLENECK_KERNEL_UPSTREAM", "api.anthropic.com")
PORT = int(os.environ.get("BOTTLENECK_KERNEL_PORT", "8791") or 8791)
KERNEL_FILE = os.environ.get("BOTTLENECK_KERNEL_FILE",
                             os.path.join(HERE, "kernel.md"))
IDENTITY_FILE = os.environ.get("BOTTLENECK_KERNEL_IDENTITY",
                               os.path.join(HERE, "identity.json"))
LOG = os.path.expanduser(
    os.environ.get("BOTTLENECK_KERNEL_LOG",
                   os.path.join(os.environ.get("BOTTLENECK_STATE")
                                or "~/.bottleneck", "kernel", "usage.jsonl")))

# Off means: use it if it is already up, never bring it up. Tests set this, and
# they must - a test that calls env() would otherwise spawn a real server on a
# real port and outlive the run. That happened, on the module's default port,
# and it went unnoticed because a probe finding *something* listening reads the
# same as a probe finding ours.
AUTOSTART = (os.environ.get("BOTTLENECK_KERNEL_AUTOSTART", "1").strip().lower()
             not in ("0", "no", "off", "false"))

PROBE = "/health-probe"
INTERCEPT = ("/v1/messages", "/v1/messages/count_tokens")

# Never forwarded: framing and connection management belong to each hop of the
# conversation separately. Everything else goes through untouched - the
# authorization header above all, which is not ours to read or to change.
HOP = {"host", "content-length", "connection", "keep-alive", "te", "upgrade",
       "transfer-encoding", "proxy-authorization", "proxy-connection",
       "accept-encoding"}

HEADER = "# Operating Identity\n\n"

# The sentences that make claude claude, read from the one file that has them.
# Not written here: the external backend needs the same list, and two copies of
# a pattern pinned to somebody else's release notes is one copy that gets
# updated. See identity.json, and `identity_gap()` for how they are held level.
_LOADED = None


def identity(reload=False):
    """[{name, pattern, witness, re}] - what a rewrite is expected to excise.

    Never raises. An unreadable or malformed source excises nothing, which then
    shows up as every excision missing on the first real prompt - loud, rather
    than a rewrite that quietly does half its job.
    """
    global _LOADED
    if _LOADED is not None and not reload and _LOADED[0] == IDENTITY_FILE:
        return _LOADED[1]
    out = []
    try:
        with open(IDENTITY_FILE) as fh:
            for row in (json.load(fh).get("excise") or []):
                out.append(dict(row, re=re.compile(row["pattern"])))
    except (OSError, ValueError, KeyError, TypeError, re.error):
        out = []
    _LOADED = (IDENTITY_FILE, out)
    return out


def min_system_chars():
    """Below this, a system prompt is a probe and a miss means nothing.

    Claude Code opens with tens of thousands of characters. The quota check it
    makes at startup carries almost none, and calling that a failed excision
    would cry wolf on every session before it began.
    """
    try:
        with open(IDENTITY_FILE) as fh:
            return int(json.load(fh).get("min_system_chars") or 2000)
    except (OSError, ValueError, TypeError):
        return 2000


def kernel_text():
    try:
        with open(KERNEL_FILE) as fh:
            return fh.read().strip()
    except OSError:
        return ""


def rewrite_text(text, hit=None):
    """The stock identity out. A pure function of `text` - see rule 2.

    `hit` is a set collecting the name of every excision that matched; what is
    missing from it afterwards is what this build of Claude Code no longer says
    the way we think it does.
    """
    for row in identity():
        found, count = row["re"].subn("", text)
        if count and hit is not None:
            hit.add(row["name"])
        text = found
    return text


def rewrite_system(system, kernel, hit=None):
    """The system field, rewritten in place. Same shape, different words."""
    if not kernel:
        return system
    inject = HEADER + kernel + "\n\n"
    if isinstance(system, str):
        return inject + rewrite_text(system, hit)
    if not isinstance(system, list):
        return system

    out, originals = [], []
    for block in system:
        if isinstance(block, dict) and block.get("type") == "text":
            block = dict(block)
            originals.append(block.get("text") or "")
            block["text"] = rewrite_text(block["text"], hit)
        else:
            originals.append(None)
        out.append(block)

    def is_text(b):
        return isinstance(b, dict) and b.get("type") == "text"

    # The block a replacement emptied, if any, else the first. Rule 3: putting
    # the kernel where the identity was keeps every block non-empty without
    # dropping one, and dropping one would move a caching breakpoint.
    at = next((i for i, b in enumerate(out) if is_text(b) and not b["text"]),
              next((i for i, b in enumerate(out) if is_text(b)), None))
    if at is not None:
        out[at]["text"] = inject + out[at]["text"]
    # Anything still empty keeps what it came with: better an identity we
    # failed to excise than a request the API refuses.
    for i, block in enumerate(out):
        if is_text(block) and not block["text"] and originals[i]:
            block["text"] = originals[i]
    return out


def system_chars(system):
    if isinstance(system, str):
        return len(system)
    if not isinstance(system, list):
        return 0
    return sum(len(b.get("text") or "") for b in system
               if isinstance(b, dict) and b.get("type") == "text")


def rewrite_body(raw):
    """(body, missed) - the body with its system prompt rewritten, or as it came.

    Anything unparseable goes through untouched. A proxy that drops a request
    it did not understand is a proxy that breaks a session it could have left
    alone.

    `missed` is the names of the excisions that found nothing in a system
    prompt big enough that they should have, or None when this was not a body
    to rewrite. Empty means every sentence we expect to remove was there and
    was removed - the only outcome in which the kernel is the only identity in
    the request.
    """
    try:
        body = json.loads(raw)
    except (ValueError, TypeError):
        return raw, None
    if not isinstance(body, dict) or "system" not in body:
        return raw, None
    kernel = kernel_text()
    if not kernel:
        return raw, None
    hit = set()
    body["system"] = rewrite_system(body["system"], kernel, hit)
    missed = None
    if system_chars(body["system"]) >= min_system_chars():
        missed = [row["name"] for row in identity() if row["name"] not in hit]
        if not identity():
            missed = ["identity.json unreadable"]
    return json.dumps(body).encode(), missed


def mark_file():
    return os.path.join(os.path.dirname(LOG), "identity-missed.json")


def mark_gap(missed):
    """Leave the miss where a dashboard in another process can find it.

    The rewrite happens here; the status line is drawn somewhere else. A file
    is the only thing the two share, and it is written on a miss and removed on
    a clean rewrite so it always describes the last real prompt rather than the
    worst one ever seen.
    """
    try:
        if missed:
            os.makedirs(os.path.dirname(LOG), exist_ok=True)
            with open(mark_file(), "w") as fh:
                json.dump({"ts": time.strftime("%FT%T%z"), "missed": missed},
                          fh)
        elif os.path.exists(mark_file()):
            os.remove(mark_file())
    except OSError:
        pass


def gap():
    """The excisions that missed the last real system prompt. [] when clean."""
    try:
        with open(mark_file()) as fh:
            return list(json.load(fh).get("missed") or [])
    except (OSError, ValueError, TypeError):
        return []


def note_usage(row):
    """One line per request. Best effort - metering never fails a request."""
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a") as fh:
            fh.write(json.dumps(row) + "\n")
    except OSError:
        pass


def usage_of(chunks):
    """Token counts out of a streamed answer.

    Both events carry some of it: message_start has the input side, the final
    message_delta has the output. Cache *creation* is counted as well as cache
    reads - the request that writes a 60k-token cache is the expensive one in
    any session, and a meter that only reports reads makes it look free.
    """
    got = {}
    for line in b"".join(chunks).split(b"\n"):
        if not line.startswith(b"data:"):
            continue
        try:
            ev = json.loads(line[5:].strip() or b"{}")
        except ValueError:
            continue
        use = (ev.get("message") or {}).get("usage") or ev.get("usage") or {}
        for key in ("input_tokens", "output_tokens",
                    "cache_creation_input_tokens", "cache_read_input_tokens"):
            if isinstance(use.get(key), int):
                got[key] = use[key]
        if ev.get("type") == "message_start":
            got.setdefault("model", (ev.get("message") or {}).get("model"))
    return got


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "bottleneck-kernel"

    def log_message(self, *a):
        pass                            # the usage log is the log

    def do_GET(self):
        if self.path.startswith(PROBE):
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")
            return
        self.relay(b"")

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        self.relay(self.rfile.read(length) if length else b"")

    def relay(self, raw):
        rewrote = self.path.split("?")[0] in INTERCEPT
        body, missed = rewrite_body(raw) if rewrote else (raw, None)
        if missed is not None:
            mark_gap(missed)
        headers = {k: v for k, v in self.headers.items()
                   if k.lower() not in HOP}
        headers["Content-Length"] = str(len(body))
        started = time.time()
        try:
            conn = http.client.HTTPSConnection(UPSTREAM, timeout=600)
            conn.request(self.command, self.path, body=body, headers=headers)
            upstream = conn.getresponse()
        except Exception as exc:        # noqa: BLE001 - upstream is not ours
            self.send_response(502)
            self.send_header("Content-Length", "0")
            self.end_headers()
            note_usage({"ts": time.strftime("%FT%T%z"), "path": self.path,
                        "status": 502, "error": str(exc)[:200]})
            return

        # Chunked, whatever the upstream framing was: an answer is streamed
        # token by token and the whole point is that it arrives that way. A
        # proxy that reads the response to the end before saying anything turns
        # every turn into a wait with nothing on the screen.
        self.send_response(upstream.status)
        for key, val in upstream.getheaders():
            if key.lower() not in HOP:
                self.send_header(key, val)
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        seen = []
        try:
            while True:
                chunk = upstream.read(8192)
                if not chunk:
                    break
                if rewrote and len(seen) < 64:
                    seen.append(chunk)
                self.wfile.write(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
        except (BrokenPipeError, ConnectionResetError):
            return                      # the head went away mid-answer
        finally:
            conn.close()

        row = {"ts": time.strftime("%FT%T%z"), "path": self.path,
               "status": upstream.status, "rewritten": rewrote,
               "elapsed_s": round(time.time() - started, 2)}
        if missed:
            row["identity_missed"] = missed
        row.update(usage_of(seen))
        note_usage(row)


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def up(port=None, timeout=0.35):
    """True when something is answering the probe on this port."""
    port = PORT if port is None else port
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        conn.request("GET", PROBE)
        return conn.getresponse().status < 500
    except Exception:                   # noqa: BLE001 - not up is not up
        return False
    finally:
        try:
            conn.close()
        except Exception:               # noqa: BLE001
            pass


def base_url(port=None):
    return f"http://127.0.0.1:{PORT if port is None else port}"


def start(wait=3.0, port=None):
    """Bring the wrapper up if nothing is answering. True when it is up after.

    Detached, and never waited on beyond `wait`: this is called while a pane is
    being opened, and a wrapper that will not start must cost you the rewrite
    rather than the head.
    """
    import subprocess
    port = PORT if port is None else port
    if up(port):
        return True
    if not AUTOSTART:
        return False
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(os.path.join(os.path.dirname(LOG), "wrap.log"), "a") as log:
            subprocess.Popen([sys.executable, os.path.abspath(__file__),
                              str(port)],
                             stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                             start_new_session=True)
    except Exception:                   # noqa: BLE001
        return False
    until = time.time() + max(0.0, wait)
    while time.time() < until:
        if up(port):
            return True
        time.sleep(0.1)
    return False


def serve(port=None):
    """Run until killed. This is what `python -m ...wrap` does."""
    port = PORT if port is None else port
    srv = Server(("127.0.0.1", port), Handler)
    kernel = kernel_text()
    rows = identity()
    print(f"[kernel] {base_url(port)} -> https://{UPSTREAM}  "
          f"kernel={len(kernel)} chars  excise={len(rows)}  log={LOG}",
          flush=True)
    if not rows:
        print(f"[kernel] !! nothing to excise - {IDENTITY_FILE} is missing or "
              f"malformed, so the stock identity will survive", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    serve(int(sys.argv[1]) if len(sys.argv) > 1 else None)
