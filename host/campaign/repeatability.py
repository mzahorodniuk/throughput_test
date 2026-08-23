#!/usr/bin/env python3
"""How repeatable is a step? Run a few rates many times on one bitstream.

The ladders are single-shot, which is fine for locating a knee but says nothing
about spread -- and two batch-16 runs disagreed about what happens *above* the
knee (delivered plateauing near 105 k in one, falling to ~58 k in another). That
is either run-to-run variance or a difference in state, and a single-shot ladder
cannot tell them apart. This measures it.

Reports median and full range per rate, below the knee, at it, and above it.

    python3 repeatability.py --tag batch16 --reps 5
"""

import argparse
import csv
import json
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hiload
import meta as metamod
import scis

HERE = os.path.dirname(os.path.abspath(__file__))
RATES = [60000, 100000, 105000, 110000, 120000, 140000]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--reps", type=int, default=5)
    ap.add_argument("--duration", type=float, default=3.0)
    ap.add_argument("--rates", type=int, nargs="+", default=RATES)
    args = ap.parse_args()

    outdir = os.path.join(HERE, "results", args.tag)
    os.makedirs(outdir, exist_ok=True)
    m = metamod.collect(args.tag, extra={"repeatability": {
        "reps": args.reps, "rates": args.rates, "duration": args.duration}})
    metamod.write(os.path.join(outdir, "meta_repeat.json"), m)
    print(f"tag={args.tag} batch={m['hdl_generics'].get('TX_BATCH_MAX_PACKETS')} "
          f"md5={m['bitstream']['md5'][:12]}  reps={args.reps}")

    rig = hiload.Rig(scis.resolve_host_ip())
    path = os.path.join(outdir, "repeat.csv")
    fields = ["rate", "rep", "delivered_pps", "loss_pct", "batch_mean",
              "ragged", "host_drops", "valid"]
    rows = []
    try:
        with open(path, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=fields)
            w.writeheader()
            for rate in args.rates:
                got = []
                for rep in range(args.reps):
                    r = hiload.run_step(rig, scis.ALL_PORTS, rate, args.duration)
                    w.writerow({k: r.get(k) for k in fields}
                               | {"rate": rate, "rep": rep})
                    fh.flush()
                    rows.append((rate, r))
                    got.append(r["delivered_pps"])
                    if r["loss_pct"] > 50.0:
                        ok, _ = hiload.probe_alive(rig, scis.ALL_PORTS)
                        if not ok:
                            print(f"  wedged at {rate}; stopping")
                            raise SystemExit(0)
                    time.sleep(0.4)
                lo, hi = min(got), max(got)
                med = statistics.median(got)
                print(f"  {rate:>7} pps offered -> delivered median "
                      f"{med:>9,.0f}  range {lo:>9,.0f}..{hi:>9,.0f}  "
                      f"spread {100*(hi-lo)/hi if hi else 0:>5.1f} %")
    finally:
        rig.close()
    print(f"\nCSV: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
