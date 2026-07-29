"""Talking to cc-kernel-proxy: is it up, start it, what is it doing.

The proxy itself is not in this repository and is not this module's to own. It
is a separate checkout (~/cc-kernel-proxy) that sits between claude and
api.anthropic.com, rewrites the system prompt and meters the tokens. All this
module does is make sure a head is launched pointing at it.

Standard library only, and nothing here ever raises: it is called while a pane
is being opened, and a proxy that cannot be reached must cost you the rewrite,
never the head.
"""
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request


ROOT = os.path.expanduser(os.environ.get("BOTTLENECK_KERNEL_ROOT",
                                         "~/cc-kernel-proxy"))
# Off unless the checkout is actually there. A branch carrying this module on a
# machine without the proxy is a module that says so and does nothing.
PRESENT = os.path.isdir(ROOT)
AUTOSTART = (os.environ.get("BOTTLENECK_KERNEL_AUTOSTART", "1").strip().lower()
             not in ("0", "no", "off", "false"))
# How long to wait for a proxy that is starting. A head opening is a thing you
# are watching happen, so this is short on purpose: miss the window and the
# head launches without the proxy rather than making you wait for it.
WAIT = float(os.environ.get("BOTTLENECK_KERNEL_WAIT", "3") or 3)
PROBE = 0.35


def port():
    """The port the proxy's own config says it listens on."""
    try:
        with open(os.path.join(ROOT, "config.toml")) as fh:
            got = re.search(r"^\s*port\s*=\s*(\d+)", fh.read(), re.M)
        return int(got.group(1)) if got else 8790
    except (OSError, ValueError):
        return 8790


def base_url():
    return f"http://127.0.0.1:{port()}"


def up(timeout=PROBE):
    """True when something is listening and answering as the proxy.

    The same probe its own launcher uses, so "up" means the same thing to both
    and neither starts a second one on top of the other.
    """
    try:
        with urllib.request.urlopen(f"{base_url()}/health-probe",
                                    timeout=timeout) as r:
            return r.status < 500
    except urllib.error.HTTPError:
        return True                      # answering at all is the whole test
    except Exception:                    # noqa: BLE001 - not up, whatever went wrong
        return False


def python():
    venv = os.path.join(ROOT, "venv", "bin", "python")
    return venv if os.path.exists(venv) else "python3"


def start(wait=WAIT):
    """Start the proxy if nothing is listening. True when it is up after.

    Detached, with its own log, and never waited on beyond `wait`: this is
    called from a pane opening, and a proxy that will not start must not hold
    a head hostage.
    """
    if not PRESENT:
        return False
    if up():
        return True
    if not AUTOSTART:
        return False
    logs = os.path.join(ROOT, "log")
    try:
        os.makedirs(logs, exist_ok=True)
        with open(os.path.join(logs, "proxy.log"), "a") as log:
            subprocess.Popen([python(), "-m", "ccproxy"], cwd=ROOT,
                             stdout=log, stderr=log,
                             stdin=subprocess.DEVNULL,
                             start_new_session=True)
    except Exception:                    # noqa: BLE001
        return False
    until = time.time() + max(0.0, wait)
    while time.time() < until:
        if up():
            return True
        time.sleep(0.1)
    return False


def kernel_chars():
    """How much identity text the proxy is set up to inject, if we can tell."""
    try:
        with open(os.path.join(ROOT, "kernels", "identity.md")) as fh:
            return len(fh.read())
    except OSError:
        return 0


def usage(limit=200):
    """(requests, input, output, cache_read) from the proxy's own meter.

    Its log, not ours - the proxy is what sees the tokens, and a second count
    kept here would be a second count to disagree with it.
    """
    path = os.path.join(ROOT, "log", "usage.jsonl")
    rows = 0
    tally = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0}
    try:
        with open(path) as fh:
            for line in fh.readlines()[-limit:]:
                try:
                    got = json.loads(line)
                except ValueError:
                    continue
                rows += 1
                for key in tally:
                    val = got.get(key)
                    if isinstance(val, int):
                        tally[key] += val
    except OSError:
        return 0, 0, 0, 0
    return (rows, tally["input_tokens"], tally["output_tokens"],
            tally["cache_read_input_tokens"])
