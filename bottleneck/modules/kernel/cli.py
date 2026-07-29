"""`bottleneck kernel` - what the proxy is doing, and bringing it up."""
from . import proxy


def kernel_cmd(cmd, rest):
    what = (rest[0] if rest else "").strip().lower()

    if not proxy.PRESENT:
        print(f"no proxy checkout at {proxy.ROOT}")
        print("  heads launch straight to the API; nothing here is doing "
              "anything")
        print("  BOTTLENECK_KERNEL_ROOT=<path> if it lives somewhere else")
        return 1

    if what == "start":
        if proxy.up():
            print(f"already up on {proxy.base_url()}")
            return 0
        print(f"starting {proxy.ROOT} ...")
        if proxy.start(wait=10):
            print(f"up on {proxy.base_url()}")
            return 0
        print(f"did not come up - see {proxy.ROOT}/log/proxy.log")
        return 1

    if what and what not in ("status", ""):
        print(f"unknown: bottleneck kernel {what}")
        return 2

    live = proxy.up()
    print(f"{'up  ' if live else 'down'} {proxy.base_url()}   {proxy.ROOT}")
    chars = proxy.kernel_chars()
    if chars:
        print(f"     identity kernel: {chars} chars, replacing the stock block")
    if not live:
        print("     heads opened now would go straight to the API"
              + ("" if proxy.AUTOSTART else " (autostart is off)"))
        print("     `bottleneck kernel start` brings it up")
        return 0
    rows, sent, back, cached = proxy.usage()
    if rows:
        print(f"     last {rows} requests: {sent:,} in, {back:,} out, "
              f"{cached:,} cache-read")
    else:
        print("     nothing metered yet")
    return 0
