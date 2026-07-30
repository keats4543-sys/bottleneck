"""What a head is launched with, and what happens when the wrapper is not there.

The module's whole job is one answer: what should this head be opened with. This
file holds it to the rule that matters when the answer is "nothing" - a wrapper
that is down, or will not start, must cost you the rewrite and never the head.

It used to test choosing between two backends. There is one now, which is the
point of the commit that deleted the other: heads depended on a checkout outside
this repository, and the drift between its copy of the rules and ours was a
thing that had to be checked because it could not be shared.

No network beyond loopback, and nothing outside a temp directory is written.
"""
import http.server
import os
import socket
import tempfile
import threading

from harness import bn as m                  # the core: env_prefix
from bottleneck import modules as reg
from bottleneck.modules.kernel import wrap

TMP = tempfile.mkdtemp(prefix="bottleneck-kernel-")
wrap.LOG = os.path.join(TMP, "usage.jsonl")

# Nothing here may start a real service. A test that calls env() would otherwise
# spawn a real server on the real port and outlive the run - that happened, and
# went unnoticed because a probe finding *something* listening reads the same as
# a probe finding ours.
os.environ["BOTTLENECK_KERNEL_AUTOSTART"] = "0"
wrap.AUTOSTART = False

FAILED = []


def check(label, got, want):
    ok = got == want
    print(("  ok   " if ok else "  FAIL ") + label.ljust(54)
          + ("" if ok else f"\n         got  {got!r}\n         want {want!r}"))
    if not ok:
        FAILED.append(label)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Probe(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *a):
        pass


def serving(port):
    srv = http.server.HTTPServer(("127.0.0.1", port), Probe)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


port = free_port()
wrap.PORT = port

print("\ndown, and not coming up")
check("nothing is listening", wrap.up(), False)
check("so a head is launched with nothing rather than not launched",
      reg.environ({"cwd": "/tmp"}), {})
check("the launch line is what it always was", m.env_prefix({"cwd": "/tmp"}), "")
check("and the status line does not claim it is up", reg.status([]), [])


print("\nup")
srv = serving(port)
try:
    check("the probe finds it", wrap.up(), True)
    check("a head is pointed at it",
          reg.environ({"cwd": "/tmp"}),
          {"ANTHROPIC_BASE_URL": f"http://127.0.0.1:{port}"})
    check("which is what reaches the command line",
          m.env_prefix({"cwd": "/tmp"}),
          f"ANTHROPIC_BASE_URL=http://127.0.0.1:{port} ")
    check("and the status line says so", reg.status([]), ["kernel"])

    print("\nand says when a stage cannot do its job")
    # The mark is a file because the rewrite happens in the wrapper's process
    # and the status line is drawn in the dashboard's.
    from bottleneck.modules import kernel as k
    wrap.mark_gap(["identity: a sentence matched nothing"])
    k._GAP[0] = 0                       # the 30s cache, not what is under test
    check("the status line marks it rather than reading as fine",
          reg.status([]), ["kernel!"])
    check("and the reason is available in words",
          k.gap(), ["identity: a sentence matched nothing"])
    wrap.mark_gap([])
    k._GAP[0] = 0
    check("cleared once a later request is judged clean",
          reg.status([]), ["kernel"])
finally:
    srv.shutdown()

check("once it goes away, a head goes straight to the API again",
      (wrap.up(), reg.environ({})), (False, {}))

print("\nall pass" if not FAILED else f"\n{len(FAILED)} FAILED")
raise SystemExit(1 if FAILED else 0)
