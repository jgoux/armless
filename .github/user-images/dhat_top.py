#!/usr/bin/env python3
"""The top allocation sites of a dhat profile (dhat-heap.json) at t-gmax, the instant the heap was largest: bytes and blocks live there, per site,
with the frames as dhat gives them (innermost first), kept to armless's own (a symbol of armless, not of core, alloc or std with armless only in a type
argument; a site with none shows its first 4 frames).
Stdlib only, offline. Usage: dhat_top.py dhat-heap.json [N=25]"""
import json, sys

FRAMES = 12  # the armless frames shown per site
STD = ("core::", "alloc::", "std::", "<core::", "<alloc::", "<std::", "<[", "<&", "<*")


def ours(frame):
    sym = frame.split(": ", 1)[-1]  # after the address
    return "armless" in sym and not sym.startswith(STD)


def mib(n):
    return f"{n / 1048576:.2f} MiB"


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    path, top = argv[1], int(argv[2]) if len(argv) > 2 else 25
    with open(path) as f:
        d = json.load(f)
    ftbl = d["ftbl"]
    sites = sorted((p for p in d["pps"] if p.get("gb", 0) > 0), key=lambda p: -p["gb"])
    total, blocks = sum(p["gb"] for p in sites), sum(p.get("gbk", 0) for p in sites)
    print(f"{d.get('cmd', '?')}")
    print(f"at t-gmax ({d['tg']} {d.get('tu', 'µs')} of {d['te']}): {mib(total)} in {blocks} blocks, {len(sites)} sites; top {min(top, len(sites))} by bytes")
    for i, p in enumerate(sites[:top], 1):
        frames = [ftbl[x] for x in p["fs"] if x != 0]  # 0 is "[root]"
        mine = [x for x in frames if ours(x)] or frames[:4]
        print(f"\n#{i} {mib(p['gb'])} ({p['gb']} B, {100 * p['gb'] / total:.1f}%) in {p.get('gbk', 0)} blocks")
        for x in mine[:FRAMES]:
            print(f"    {x}")
        if len(mine) > FRAMES:
            print(f"    ... {len(mine) - FRAMES} more")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
