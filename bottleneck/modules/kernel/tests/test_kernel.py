"""Pointing a head at the external proxy, without the proxy being installed.

The module has two backends and this file is about one of them: the external
cc-kernel-proxy checkout. So it pins the backend rather than letting the module
choose, because the choice is exactly what the other test covers - and an
"external checkout missing" case that quietly fell back to the built-in wrapper
would be testing the fallback while claiming to test the absence.

The module's whole job is one answer: what should this head be launched with.
So these fake the proxy - a checkout that is only a config file, and a tiny
HTTP server standing in for the thing listening - and hold the module to the
rules that matter when it is wrong. A proxy that is missing, or down, or will
not start, must cost you the rewrite and never the head.

No network beyond loopback, and nothing outside a temp directory is written.
"""
import http.server
import json
import os
import socket
import tempfile
import threading

from harness import bn as m                  # the core: env_prefix
from bottleneck import modules as reg
from bottleneck.modules.kernel import proxy as p

os.environ["BOTTLENECK_KERNEL_BACKEND"] = "proxy"

TMP = tempfile.mkdtemp(prefix="bottleneck-kernel-")
# Nothing here may start a real service. See wrap.AUTOSTART.
os.environ["BOTTLENECK_KERNEL_AUTOSTART"] = "0"

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


print("\na checkout that is not there is a backend that does nothing")
p.ROOT = os.path.join(TMP, "nowhere")
p.PRESENT = False
check("no environment for a head", reg.environ({"cwd": "/tmp"}), {})
check("so the launch line is what it always was", m.env_prefix({}), "")
check("and nothing on the status line", reg.status([]), [])


print("\nreading the proxy's own configuration, not our own copy of it")
root = os.path.join(TMP, "proxy")
os.makedirs(os.path.join(root, "kernels"), exist_ok=True)
os.makedirs(os.path.join(root, "log"), exist_ok=True)
port = free_port()
open(os.path.join(root, "config.toml"), "w").write(
    f"[server]\nhost = \"127.0.0.1\"\nport = {port}\n")
open(os.path.join(root, "kernels", "identity.md"), "w").write("x" * 1724)
p.ROOT = root
p.PRESENT = True
check("the port comes from its config", p.port(), port)
check("and the address we hand a head is built from it",
      p.base_url(), f"http://127.0.0.1:{port}")
check("we can say how much identity text it injects", p.kernel_chars(), 1724)

with open(os.path.join(root, "log", "usage.jsonl"), "w") as fh:
    for n in (1, 2):
        fh.write(json.dumps({"input_tokens": 100 * n, "output_tokens": n,
                             "cache_read_input_tokens": 1000 * n}) + "\n")
    fh.write("not json at all\n")
check("its meter is read, and a bad line does not stop the count",
      p.usage(), (2, 300, 3, 3000))


print("\nthe proxy keeps its own copy of the sentences, and is held to ours")
# The duplication that cannot be removed: the external proxy is a separate
# program with its own config. So identity.json stays the source and the two
# are compared, because a reword applied to one copy and not the other is a
# rewrite that stopped working with nothing to show for it.
from bottleneck.modules.kernel import wrap
config = os.path.join(root, "config.toml")
base = open(config).read()


def with_patterns(patterns):
    open(config, "w").write(base + "".join(
        f'\n[[rewrite.replacements]]\npattern = {json.dumps(p)}\nreplace = ""\n'
        for p in patterns))


want = [row["pattern"] for row in wrap.identity()]
with_patterns(want)
check("in step with identity.json, there is nothing to report", p.gap(), [])
with_patterns(want[:1])
check("a sentence identity.json excises and the proxy does not is named",
      p.gap(), ["config.toml does not excise interactive-agent"])
with_patterns(want + [r"You are a helpful assistant\.\s*"])
check("and so is one the proxy excises that identity.json has never heard of",
      [line.split(":")[0] for line in p.gap()],
      ["config.toml excises a pattern identity.json does not"])

with_patterns(want)
dump = os.path.join(root, "log", "last_request.json")
open(dump, "w").write(json.dumps({"system": [
    {"type": "text", "text": "# Operating Identity\n\nx" + "y" * 4000},
    {"type": "text", "text": "You are an interactive agent that helps users "
                             "with software engineering tasks."}]}))
check("a stock sentence still in the body it actually sent is a live failure",
      p.gap(), ["interactive-agent survived a real prompt"])
open(dump, "w").write(json.dumps({"system": [
    {"type": "text", "text": "# Operating Identity\n\nx" + "y" * 4000}]}))
check("and a body it rewrote cleanly says nothing", p.gap(), [])
os.remove(dump)


print("\ndown, and not coming up")
p.AUTOSTART = False
check("nothing is listening", p.up(), False)
check("so a head is launched with nothing rather than not launched",
      reg.environ({"cwd": "/tmp"}), {})
check("and the status line does not claim it is up", reg.status([]), [])


print("\nup")
srv = serving(port)
try:
    check("the probe finds it", p.up(), True)
    check("a head is pointed at it",
          reg.environ({"cwd": "/tmp"}),
          {"ANTHROPIC_BASE_URL": f"http://127.0.0.1:{port}"})
    check("which is what reaches the command line",
          m.env_prefix({"cwd": "/tmp"}),
          f"ANTHROPIC_BASE_URL=http://127.0.0.1:{port} ")
    check("and the status line says so", reg.status([]), ["kernel"])
finally:
    srv.shutdown()

check("once it goes away, a head goes straight to the API again",
      (p.up(), reg.environ({})), (False, {}))

print("\nand with no checkout, the module falls back to the one it carries")
# The whole point of shipping a wrapper in the module: a machine with no
# external proxy still gets the rewrite, from wrap.py, with nothing to install.
p.PRESENT = False
os.environ.pop("BOTTLENECK_KERNEL_BACKEND", None)
reg.forget()
from bottleneck.modules import kernel as k
check("the backend chooses itself", k.backend(), "builtin")
check("and it is the wrapper in this directory",
      k.chosen().__name__.endswith("wrap"), True)
os.environ["BOTTLENECK_KERNEL_BACKEND"] = "proxy"
reg.forget()
check("naming the external one explicitly still overrides that",
      k.backend(), "proxy")
os.environ.pop("BOTTLENECK_KERNEL_BACKEND", None)

print("\nall pass" if not FAILED else f"\n{len(FAILED)} FAILED")
raise SystemExit(1 if FAILED else 0)
