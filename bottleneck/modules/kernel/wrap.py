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

# The sentences that make claude claude. Removing them is what makes room for
# a different identity rather than a second one arguing with the first.
STOCK = [
    re.compile(r"You are Claude Code, Anthropic's official CLI for Claude\.\s*"),
    re.compile(r"You are an interactive agent that helps users with software "
               r"engineering tasks\.\s*"),
]


def kernel_text():
    try:
        with open(KERNEL_FILE) as fh:
            return fh.read().strip()
    except OSError:
        return ""


def rewrite_text(text):
    """The stock identity out. A pure function of `text` - see rule 2."""
    for pattern in STOCK:
        text = pattern.sub("", text)
    return text


def rewrite_system(system, kernel):
    """The system field, rewritten in place. Same shape, different words."""
    if not kernel:
        return system
    inject = HEADER + kernel + "\n\n"
    if isinstance(system, str):
        return inject + rewrite_text(system)
    if not isinstance(system, list):
        return system

    out, originals = [], []
    for block in system:
        if isinstance(block, dict) and block.get("type") == "text":
            block = dict(block)
            originals.append(block.get("text") or "")
            block["text"] = rewrite_text(block["text"])
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


def rewrite_body(raw):
    """The request body with its system prompt rewritten, or as it came.

    Anything unparseable goes through untouched. A proxy that drops a request
    it did not understand is a proxy that breaks a session it could have left
    alone.
    """
    try:
        body = json.loads(raw)
    except (ValueError, TypeError):
        return raw
    if not isinstance(body, dict) or "system" not in body:
        return raw
    kernel = kernel_text()
    if not kernel:
        return raw
    body["system"] = rewrite_system(body["system"], kernel)
    return json.dumps(body).encode()


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
        body = rewrite_body(raw) if rewrote else raw
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
    print(f"[kernel] {base_url(port)} -> https://{UPSTREAM}  "
          f"kernel={len(kernel)} chars  log={LOG}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    serve(int(sys.argv[1]) if len(sys.argv) > 1 else None)
