import argparse
import json
import sys

from . import prefetch
from .api import MAX_WAIT_S, handle
from .dem.store import TileStore
from .dem.terrain import Terrain


def main(argv=None):
    ap = argparse.ArgumentParser(prog="visible-zmanim")
    sub = ap.add_subparsers(dest="cmd", required=True)
    z = sub.add_parser("zmanim", help="print the API response; pass API parameters as key=value")
    z.add_argument("params", nargs="+")
    p = sub.add_parser("prefetch", help="download terrain for regions into the pinned store")
    p.add_argument("regions", nargs="*")
    p.add_argument("--all", action="store_true")
    p.add_argument("--plan", action="store_true", help="only count the blocks")
    sub.add_parser("regions", help="list known regions")
    sub.add_parser("cache", help="show cache size and evict over the cap")
    a = ap.parse_args(argv)

    if a.cmd == "zmanim":
        bad = [kv for kv in a.params if "=" not in kv]
        if bad:
            ap.error(f"zmanim parameters must look like key=value, got: {' '.join(bad)}")
        params = dict(kv.split("=", 1) for kv in a.params)
        status, body = handle(params)
        # The horizon is computed in a thread of this process, so keep it alive until it's done.
        while status == 202:
            print("computing the horizon (the first point in a new area downloads terrain)...", file=sys.stderr, flush=True)
            status, body = handle({**params, "wait": MAX_WAIT_S})
        print(json.dumps(body, indent=1))
        return 0 if status < 300 else 1
    if a.cmd == "regions":
        for name, bbox in prefetch.REGIONS.items():
            print(name, bbox)
        return 0
    if a.cmd == "cache":
        st = TileStore()
        print(f"cache {st.cache_size() / 1e9:.2f} GB of {st.cap / 1e9:.0f} GB, freed {st.evict() / 1e9:.2f} GB")
        return 0
    names = list(prefetch.REGIONS) if a.all else a.regions
    unknown = [n for n in names if n not in prefetch.REGIONS]
    if unknown or not names:
        print(f"unknown or missing regions: {unknown}", file=sys.stderr)
        return 2
    terrain = Terrain()
    for name in names:
        if a.plan:
            jobs, _ = prefetch.plan(terrain, prefetch.REGIONS[name])
            counts = {}
            for cog, lv, blocks in jobs:
                family = cog.key.split("_")[0]
                counts[f"{family} L{lv}"] = counts.get(f"{family} L{lv}", 0) + len(blocks)
            print(name, sum(counts.values()), "blocks", dict(sorted(counts.items())), flush=True)
        else:
            prefetch.run(name, terrain, log=lambda m: print(m, flush=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
