"""The thin wrapper: what it changes, what it must never change, and a request
driven through the real server with a stand-in for the API.

The rules under test are the three in wrap.py, and they are all about prompt
caching. Caching is a byte-prefix match, so a rewrite that varies between two
requests in a session, or that moves a cache_control, turns a cache read into a
cache write and costs money instead of saving it. Two of them were learned the
hard way: a live head 400'd because a replacement emptied a system block, and
the first fix for that moved a breakpoint.

Nothing here talks to the real API. The upstream is a local HTTP server, so the
streaming, the header forwarding and the metering are all exercised against
something whose answers this file decides.
"""
import http.client
import http.server
import json
import os
import socket
import sys
import tempfile
import threading

# The checkout root, five levels up. The core's tests get this from harness.py;
# this one needs no harness - the wrapper imports nothing but the standard
# library, which is the point of it.
sys.path.insert(0, os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "..")))

from bottleneck.modules.kernel import stages, wrap
from bottleneck.modules.kernel.stages import identity as ident

TMP = tempfile.mkdtemp(prefix="bottleneck-wrap-")
wrap.LOG = os.path.join(TMP, "usage.jsonl")
ident.KERNEL_FILE = os.path.join(TMP, "kernel.md")
open(ident.KERNEL_FILE, "w").write("BE TERSE. Lead with the finding.")

# The wrapper runs whatever is configured and nothing by default, so a test of
# the wrapper has to say what it is wrapping.
os.environ["BOTTLENECK_KERNEL_STAGES"] = "identity"
stages.forget()

# Nothing here may start a real service. See wrap.AUTOSTART.
os.environ["BOTTLENECK_KERNEL_AUTOSTART"] = "0"

FAILED = []


def check(label, got, want):
    ok = got == want
    print(("  ok   " if ok else "  FAIL ") + label.ljust(56)
          + ("" if ok else f"\n         got  {got!r}\n         want {want!r}"))
    if not ok:
        FAILED.append(label)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# The three blocks a live head actually sends: the identity split across two of
# them, the second being nothing but the sentence, and the bulk in the third.
def stock():
    return [
        {"type": "text", "text": "You are Claude Code, Anthropic's official "
                                 "CLI for Claude.\ncc-billing-header: v=1;"},
        {"type": "text", "text": "You are an interactive agent that helps "
                                 "users with software engineering tasks.",
         "cache_control": {"type": "ephemeral", "ttl": "1h"}},
        {"type": "text", "text": "\nIMPORTANT: tools, tone and rules follow.",
         "cache_control": {"type": "ephemeral", "ttl": "1h"}},
    ]


print("\nthe shape of the system field is never touched")
was = stock()
now = ident.rewrite_system(json.loads(json.dumps(was)), ident.kernel_text())
check("the same number of blocks", len(now), len(was))
check("each keeping its own cache_control, exactly where it was",
      [b.get("cache_control") for b in now],
      [b.get("cache_control") for b in was])
check("and no block left empty - the 400 a live head actually hit",
      [i for i, b in enumerate(now) if not b["text"]], [])


print("\nthe identity is replaced, not argued with")
whole = "\n".join(b["text"] for b in now)
check("the stock CLI sentence is gone",
      "official CLI for Claude" in whole, False)
check("so is the interactive-agent sentence",
      "interactive agent that helps" in whole, False)
check("the kernel is there", "BE TERSE" in whole, True)
check("in the block the replacement emptied, which is where the identity was",
      now[1]["text"].startswith("# Operating Identity"), True)
check("and the rest of claude's prompt is untouched",
      now[2]["text"], was[2]["text"])


print("\nevery rewrite is a pure function of the text")
again = ident.rewrite_system(json.loads(json.dumps(stock())),
                             ident.kernel_text())
check("the same bytes in, the same bytes out",
      [b["text"] for b in again], [b["text"] for b in now])
check("a thousand times over", all(
    [b["text"] for b in ident.rewrite_system(json.loads(json.dumps(stock())),
                                             ident.kernel_text())]
    == [b["text"] for b in now] for _ in range(50)), True)


print("\na body it cannot read goes through as it came")
check("not json at all", wrap.rewrite_body(b"{not json"), (b"{not json", {}))
check("json with nothing a stage is ever handed in it",
      wrap.rewrite_body(b'{"model":"m"}'), (b'{"model":"m"}', {}))
kernel_was, ident.KERNEL_FILE = ident.KERNEL_FILE, os.path.join(TMP, "gone.md")
check("and with no kernel configured, nothing is rewritten",
      json.loads(wrap.rewrite_body(json.dumps(
          {"system": stock()}).encode())[0])["system"][1]["text"],
      stock()[1]["text"])
ident.KERNEL_FILE = kernel_was


print("\na reword of claude's opening is loud, not silent")
# The failure this whole file cannot otherwise catch: the sentences excised are
# pinned to one release of Claude Code. A later one that says the same thing
# differently matches nothing, and the kernel goes in *beside* the identity it
# was meant to replace - a session that reads as working and is not. So a
# system prompt big enough to be real, with an excision that found nothing in
# it, has to leave a mark somewhere a person will see.
BIG = "\n\nfiller that makes this a real prompt rather than a probe. " * 200


def reworded():
    was = stock()
    was[1]["text"] = ("You are an interactive coding agent that helps users "
                      "with software engineering work.")
    was[2]["text"] += BIG
    return was


body, reports = wrap.rewrite_body(json.dumps({"system": reworded()}).encode())
missed = reports["identity"].get("missed")
check("the excision that no longer matches is named",
      missed, ["interactive-agent"])
check("and the one that still does is not",
      reports["identity"]["excised"], ["cli-sentence"])
check("the reworded sentence is still in what goes to the API - we did not "
      "guess", "interactive coding agent" in json.dumps(json.loads(body)), True)
check("so the request now carries two identities, which is the point",
      "BE TERSE" in json.dumps(json.loads(body)), True)

wrap.mark_gap(stages.verdict(reports))
check("the miss is left where a dashboard in another process can find it",
      wrap.gap(), ["identity: interactive-agent matched nothing, so the stock "
                   "identity is still in the prompt beside the kernel"])

_, clean = wrap.rewrite_body(json.dumps(
    {"system": [dict(b, text=b["text"] + BIG) for b in stock()]}).encode())
check("a prompt it fully handled reports nothing missed",
      stages.verdict(clean), [])
wrap.mark_gap(stages.verdict(clean))
check("and clears the mark, so it describes the last prompt not the worst",
      wrap.gap(), [])

# The mark must survive a probe. Before there was a verdict separate from the
# gaps, claude checking its quota looked exactly like a clean rewrite and wiped
# the failure off the status line before anyone saw it.
wrap.mark_gap(stages.verdict(reports))
small = json.dumps({"system": [{"type": "text", "text": "quota"}]}).encode()
probe = stages.verdict(wrap.rewrite_body(small)[1])
check("a probe too small to be a real prompt reaches no verdict", probe, None)
wrap.mark_gap(probe)
check("so it leaves a real failure on the status line", len(wrap.gap()), 1)
wrap.mark_gap([])

id_was, ident.IDENTITY_FILE = ident.IDENTITY_FILE, os.path.join(TMP, "gone.json")
ident.identity(reload=True)
check("and no source of sentences at all is itself the loudest case",
      wrap.rewrite_body(json.dumps({"system": reworded()}).encode()
                        )[1]["identity"]["missed"],
      [f"no excisions loaded - {ident.IDENTITY_FILE}"])
ident.IDENTITY_FILE = id_was
ident.identity(reload=True)
check("the real source loads, with a version it was confirmed against",
      [(r["name"], bool(r.get("matched"))) for r in ident.identity()],
      [("cli-sentence", True), ("interactive-agent", True)])


print("\ntoken counts, including the cache write nobody bills you for twice")
stream = (b'event: message_start\n'
          b'data: {"type":"message_start","message":{"model":"claude-opus-5",'
          b'"usage":{"input_tokens":12,"cache_creation_input_tokens":63268,'
          b'"cache_read_input_tokens":0}}}\n\n'
          b'event: message_delta\n'
          b'data: {"type":"message_delta","usage":{"output_tokens":42}}\n\n')
got = wrap.usage_of([stream])
check("the input side, off message_start", got.get("input_tokens"), 12)
check("the output side, off the final message_delta",
      got.get("output_tokens"), 42)
check("the cache it wrote - the expensive request of any session",
      got.get("cache_creation_input_tokens"), 63268)
check("and which model answered", got.get("model"), "claude-opus-5")


print("\na request driven through the real server")
SEEN = {}


class Upstream(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        SEEN["path"] = self.path
        SEEN["auth"] = self.headers.get("authorization")
        SEEN["version"] = self.headers.get("anthropic-version")
        SEEN["body"] = json.loads(self.rfile.read(
            int(self.headers.get("Content-Length") or 0)) or b"{}")
        # Streamed in pieces, so what comes back proves it was not buffered
        # whole before the first byte reached the client.
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for piece in stream.split(b"\n\n"):
            if not piece.strip():
                continue
            blob = piece + b"\n\n"
            self.wfile.write(b"%x\r\n" % len(blob) + blob + b"\r\n")
            self.wfile.flush()
        self.wfile.write(b"0\r\n\r\n")


up_port = free_port()
upstream = http.server.ThreadingHTTPServer(("127.0.0.1", up_port), Upstream)
threading.Thread(target=upstream.serve_forever, daemon=True).start()

# The wrapper speaks HTTPS to the real API; here it speaks plain HTTP to the
# stand-in. That is the one thing faked, and it is the transport rather than
# anything this file is testing.
wrap.http.client.HTTPSConnection = (
    lambda host, timeout=None: http.client.HTTPConnection("127.0.0.1", up_port,
                                                          timeout=timeout))
port = free_port()
threading.Thread(target=wrap.serve, args=(port,), daemon=True).start()
for _ in range(40):
    if wrap.up(port):
        break
    __import__("time").sleep(0.05)

check("the wrapper is listening", wrap.up(port), True)

conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
conn.request("POST", "/v1/messages",
             body=json.dumps({"model": "claude-opus-5", "stream": True,
                              "system": stock(),
                              "messages": [{"role": "user", "content": "hi"}]}),
             headers={"content-type": "application/json",
                      "authorization": "Bearer sk-not-real",
                      "anthropic-version": "2023-06-01"})
resp = conn.getresponse()
body = resp.read()
conn.close()

check("the answer comes back with the upstream's status", resp.status, 200)
check("and the stream arrives intact", b"message_delta" in body, True)
check("the authorization header was forwarded untouched",
      SEEN.get("auth"), "Bearer sk-not-real")
check("and the api version with it", SEEN.get("version"), "2023-06-01")
sent = SEEN["body"]["system"]
check("the API was sent the same number of blocks claude sent",
      len(sent), len(stock()))
check("with the kernel in it", "BE TERSE" in json.dumps(sent), True)
check("the stock identity excised", "official CLI" in json.dumps(sent), False)
check("and every cache_control where it started",
      [b.get("cache_control") for b in sent],
      [b.get("cache_control") for b in stock()])

__import__("time").sleep(0.3)
rows = [json.loads(l) for l in open(wrap.LOG) if l.strip()]
check("the request was metered", bool(rows), True)
check("as a rewritten one", rows[-1].get("rewritten"), True)
check("with the cache write recorded",
      rows[-1].get("cache_creation_input_tokens"), 63268)
check("and the status it got", rows[-1].get("status"), 200)

print("\nwhat it will not do")
before = len(rows)
conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
conn.request("POST", "/v1/other", body="{}",
             headers={"content-type": "application/json"})
conn.getresponse().read()
conn.close()
__import__("time").sleep(0.3)
rows = [json.loads(l) for l in open(wrap.LOG) if l.strip()]
check("a path it does not intercept is passed through unrewritten",
      rows[-1].get("rewritten"), False)

upstream.shutdown()

print("\nall pass" if not FAILED else f"\n{len(FAILED)} FAILED")
raise SystemExit(1 if FAILED else 0)
