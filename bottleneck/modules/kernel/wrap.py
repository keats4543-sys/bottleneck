"""wrap.py - the claude call, wrapped, with the rewriting left to stages.

The hop every head talks to, and the whole of it: standard library only, no
venv and no dependency. It began as a demonstration that this class of feature
belongs in a module rather than in a separate program, and it is the only one
now - the external proxy it stood beside is retired.

What it is: an HTTP proxy on loopback that claude talks to because
ANTHROPIC_BASE_URL says so. Everything it receives it forwards to the real API
with the headers it came with, and streams the answer straight back.

What it changes is not decided here. This file knows how to be a hop in the
conversation and nothing about identities, memory or compaction - it hands the
body to `stages.run()` and forwards whatever comes back. Nothing is built in,
the identity rewrite least of all: it is stages/identity.py, listed in
stages.json, and deleting that line turns it off.

So the rules the rewrite is held to live with the registry that enforces them
(see stages/__init__.py). The two this file keeps are its own:

  Never fail a request over a stage. A stage that throws is put down and the
  turn goes on without it. It was a thing you added to a session and is never a
  reason to lose one.

  Never buffer the answer. It is streamed token by token and the whole point is
  that it arrives that way, so this re-chunks as it goes rather than reading to
  the end before saying anything.
"""
import http.client
import http.server
import json
import os
import socket
import sys
import threading
import time

from . import stages


HERE = os.path.dirname(os.path.abspath(__file__))
UPSTREAM = os.environ.get("BOTTLENECK_KERNEL_UPSTREAM", "api.anthropic.com")
# 8790, inherited from the proxy this replaced. Keeping the port means a head
# already running - launched with ANTHROPIC_BASE_URL baked into its environment,
# where it cannot be changed - keeps working across the swap.
PORT = int(os.environ.get("BOTTLENECK_KERNEL_PORT", "8790") or 8790)
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

# Off unless you ask. What it writes is the system field only - before and
# after - and never the messages: the system prompt is claude's boilerplate and
# your kernel, the messages are your conversation, and only one of those is
# something to leave lying in a file. Written to the state directory, never the
# checkout.
DUMP = (os.environ.get("BOTTLENECK_KERNEL_DUMP", "").strip().lower()
        in ("1", "yes", "on", "true"))

PROBE = "/health-probe"
INTERCEPT = ("/v1/messages", "/v1/messages/count_tokens")

# Never forwarded: framing and connection management belong to each hop of the
# conversation separately. Everything else goes through untouched - the
# authorization header above all, which is not ours to read or to change.
HOP = {"host", "content-length", "connection", "keep-alive", "te", "upgrade",
       "transfer-encoding", "proxy-authorization", "proxy-connection",
       "accept-encoding"}

def rewrite_body(raw):
    """(body, reports) - the body after every stage, or as it came.

    Anything unparseable goes through untouched. A proxy that drops a request it
    did not understand is a proxy that breaks a session it could have left
    alone - and the same goes for one no stage had anything to say about.
    """
    try:
        body = json.loads(raw)
    except (ValueError, TypeError):
        return raw, {}
    if not isinstance(body, dict) or not stages.enabled():
        return raw, {}
    if "system" not in body and "messages" not in body:
        return raw, {}                  # nothing any stage is handed is in it
    reports = stages.run(body, {"path": "/v1/messages"})
    # Re-serialised whether or not anything was reported: a stage that did its
    # work and said nothing about it is a stage whose work would be thrown away
    # here otherwise. Every request takes the same path, so the bytes stay
    # stable across a session either way, which is all caching asks.
    return json.dumps(body).encode(), reports


def dump_system(raw, after):
    """The system field as it arrived and as it leaves. Best effort, opt-in."""
    if not DUMP:
        return
    try:
        before = json.loads(raw).get("system")
    except (ValueError, TypeError, AttributeError):
        return
    if before is None:
        return
    try:
        path = os.path.join(os.path.dirname(LOG), "last_system.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            json.dump({"ts": time.strftime("%FT%T%z"),
                       "before": before, "after": after}, fh, indent=1)
    except (OSError, TypeError):
        pass


def mark_file():
    return os.path.join(os.path.dirname(LOG), "stage-gaps.json")


def mark_gap(gaps):
    """Leave what the stages reported where another process can find it.

    The rewrite happens here; the status line is drawn in the dashboard. A file
    is the only thing the two share. Written when a stage that judged the
    request found something wrong, removed when one judged it and did not - so
    it describes the last request anybody looked at rather than the worst one
    ever seen.

    `gaps` of None means no stage was in a position to judge: the mark is left
    exactly as it was, because a startup probe is not evidence of anything.
    """
    if gaps is None:
        return
    try:
        if gaps:
            os.makedirs(os.path.dirname(LOG), exist_ok=True)
            with open(mark_file(), "w") as fh:
                json.dump({"ts": time.strftime("%FT%T%z"), "gaps": gaps}, fh)
        elif os.path.exists(mark_file()):
            os.remove(mark_file())
    except OSError:
        pass


def gap():
    """What the stages could not do to the last request judged. [] when clean.

    Read by the dashboard, and by `bottleneck kernel`. Stages that would not
    load at all are in here too - a stage configured and silently absent is the
    same failure as one that ran and found nothing.
    """
    out = []
    try:
        with open(mark_file()) as fh:
            out = list(json.load(fh).get("gaps") or [])
    except (OSError, ValueError, TypeError):
        out = []
    return out + [f"{name}: {why}" for name, why in sorted(stages.broken().items())]


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
        body, reports = rewrite_body(raw) if rewrote else (raw, {})
        mark_gap(stages.verdict(reports))
        if rewrote and DUMP and reports.get("identity", {}).get("judged"):
            try:
                dump_system(raw, json.loads(body).get("system"))
            except (ValueError, TypeError):
                pass
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
        told = stages.gaps(reports)
        if told:
            row["stage_gaps"] = told
        for name, report in (reports or {}).items():
            for key, val in report.items():
                if key not in ("gap", "judged"):
                    row[f"{name}.{key}"] = val
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
    # Started as a module, not as a script. It loads stages by relative import
    # now, and a file run by path has no package to be relative to - which is
    # not a thing any test sees, because a test imports it. The first live head
    # after that change found it in one go.
    root = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
    env = dict(os.environ)
    env["PYTHONPATH"] = root + os.pathsep + env.get("PYTHONPATH", "")
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(os.path.join(os.path.dirname(LOG), "wrap.log"), "a") as log:
            subprocess.Popen([sys.executable, "-m",
                              "bottleneck.modules.kernel", str(port)],
                             cwd=root, env=env,
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
    running = stages.enabled()
    print(f"[kernel] {base_url(port)} -> https://{UPSTREAM}  "
          f"stages={','.join(n for n, _ in running) or 'none'}  log={LOG}",
          flush=True)
    for name, why in sorted(stages.broken().items()):
        print(f"[kernel] !! {name}: {why}", flush=True)
    if not running:
        print("[kernel] !! nothing configured to run - every request will be "
              "forwarded exactly as it came", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    serve(int(sys.argv[1]) if len(sys.argv) > 1 else None)
