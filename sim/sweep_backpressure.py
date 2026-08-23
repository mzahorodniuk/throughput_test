#!/usr/bin/env python3
"""Sweep the W5500 transmit-buffer backpressure livelock in simulation.

The livelock is the one failure mode that cannot be provoked from the bench:
the SPI controller is slower than the 100 Mbit wire, so on real hardware the
chip's transmit buffer never stays full long enough. In simulation the wire
speed is a generic, so the margin can be measured directly.

What each axis answers:

  wire     how slow the chip's wire has to get before the controller livelocks.
           5000 ns/datagram is the honest 100 Mbit figure.
  drain    1 = in-flight bytes still occupy the buffer (what the chip does);
           0 = the whole buffer frees on SEND, which is tb_w5500_tx's model.
           The 0 column is why this was never seen before.
  batch    TX_BATCH_MAX_PACKETS. A bigger batch parks more bytes in the buffer
           per datagram, so it should reach the stall sooner.
  txbuf    per-socket transmit buffer. 2048 is the chip default.
  sockets  does one socket's full buffer block the other seven?

Writes a tidy CSV: one row per run.

    python3 sweep_backpressure.py --out results/sim/backpressure.csv
"""

import argparse
import csv
import os
import re
import subprocess
import sys

FPGA = "/home/misha/FPGA-centric-SCIS/hardware/fpga/ccgm1a1"
ENV = "/opt/oss-cad-suite/environment"
TB = "tb_w5500_tx_backpressure"

# (label, generics dict)
def build_matrix():
    runs = []

    # 1. How slow does the wire have to be? This is the margin measurement.
    for ns in (5000, 10000, 20000, 50000, 100000, 200000, 400000, 700000,
               1000000, 2000000):
        runs.append(("wire", dict(G_BATCH=16, G_PACKETS=400, G_TX_FIXED_NS=ns,
                                  G_DRAIN=1, G_SOCKETS=1, G_FEED_SOCKETS=1,
                                  G_TXBUF=2048)))

    # 2. The model comparison: same slow wire, buffer freed instantly on SEND.
    for drain in (0, 1):
        runs.append(("drain_model",
                     dict(G_BATCH=16, G_PACKETS=400, G_TX_FIXED_NS=1000000,
                          G_DRAIN=drain, G_SOCKETS=1, G_FEED_SOCKETS=1,
                          G_TXBUF=2048)))

    # 3. Does a bigger batch reach the stall sooner? N*9 must stay under 256.
    for b in (1, 4, 8, 16, 28):
        runs.append(("batch", dict(G_BATCH=b, G_PACKETS=400,
                                   G_TX_FIXED_NS=400000, G_DRAIN=1,
                                   G_SOCKETS=1, G_FEED_SOCKETS=1, G_TXBUF=2048)))

    # 4. Buffer size: a smaller buffer is the same condition reached sooner.
    for buf in (2048, 1024, 512, 256):
        runs.append(("txbuf", dict(G_BATCH=16, G_PACKETS=400,
                                   G_TX_FIXED_NS=100000, G_DRAIN=1,
                                   G_SOCKETS=1, G_FEED_SOCKETS=1, G_TXBUF=buf)))

    # 5. Blast radius: one socket stalled, do the others still get served?
    for fed in (1, 2, 4, 8):
        runs.append(("sockets", dict(G_BATCH=16, G_PACKETS=400,
                                     G_TX_FIXED_NS=1000000, G_DRAIN=1,
                                     G_SOCKETS=8, G_FEED_SOCKETS=fed,
                                     G_TXBUF=2048)))

    # 6. The asymmetric case the claim is actually about: socket 0 driven into
    #    backpressure while socket 1 has traffic waiting. Even feeding cannot
    #    show this -- it halves the pressure on each socket's own buffer and the
    #    stall never starts -- so socket 0 takes all but one packet in N.
    for every in (8, 16, 32):
        runs.append(("hot_socket", dict(G_BATCH=16, G_PACKETS=400,
                                        G_TX_FIXED_NS=1000000, G_DRAIN=1,
                                        G_SOCKETS=8, G_FEED_SOCKETS=2,
                                        G_TXBUF=2048, G_HOT_EVERY=every)))
    return runs


PATS = {
    "fed": r"fed\s+:\s+(\d+) of (\d+)",
    "datagrams": r"transmitted\s+:\s+(\d+) datagrams, (\d+) records",
    "per_socket": r"datagrams/sck:\s+([\d ]+)",
    "min_free": r"min free tx\s+:\s+(-?\d+) B",
    "fsr": r"Sn_TX_FSR\s+:\s+(\d+) reads, longest run (\d+) back-to-back, spanning (\d+) fs",
    "rsr": r"Sn_RX_RSR\s+:\s+(\d+) reads total",
    "spi": r"spi txns\s+:\s+(\d+)\s+longest quiet bus: (\d+) clks",
    "result": r"RESULT=(\S+)",
}


def run_one(gen, stop_ms, run_us, timeout):
    args = " ".join(f"-g{k}={v}" for k, v in gen.items())
    cmd = (f"source {ENV} >/dev/null 2>&1; "
           f"ghdl -r --std=08 -frelaxed {TB} {args} -gG_RUN_US={run_us} "
           f"--stop-time={stop_ms}ms --ieee-asserts=disable 2>&1")
    try:
        out = subprocess.run(["bash", "-c", cmd], cwd=FPGA, capture_output=True,
                             text=True, timeout=timeout).stdout
    except subprocess.TimeoutExpired:
        return None, "TIMEOUT"
    got = {}
    for key, pat in PATS.items():
        m = re.search(pat, out)
        got[key] = m.groups() if m else None
    return got, out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="results/sim/backpressure.csv")
    ap.add_argument("--stop-ms", type=int, default=120)
    ap.add_argument("--run-us", type=int, default=40000)
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--log", default="results/sim/backpressure.log")
    args = ap.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))
    outp = os.path.join(here, args.out)
    logp = os.path.join(here, args.log)
    os.makedirs(os.path.dirname(outp), exist_ok=True)

    fields = ["axis", "batch", "wire_ns", "drain", "sockets", "fed_sockets",
              "txbuf", "hot_every", "dg_socket0", "dg_socket1",
              "packets_fed", "packets_offered", "datagrams", "records",
              "min_free_b", "fsr_reads", "fsr_longest_run", "fsr_span_us",
              "rsr_reads", "spi_txns", "quiet_max_clks", "sockets_served",
              "result", "livelock"]
    rows = []
    with open(outp, "w", newline="") as fh, open(logp, "w") as lg:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for axis, gen in build_matrix():
            got, out = run_one(gen, args.stop_ms, args.run_us, args.timeout)
            lg.write(f"\n{'='*78}\naxis={axis} {gen}\n{'='*78}\n{out}\n")
            lg.flush()
            if got is None:
                print(f"  {axis:<12} {gen} -> TIMEOUT")
                continue
            per_sock = ([int(x) for x in got["per_socket"][0].split()]
                        if got["per_socket"] else [])
            row = {
                "axis": axis, "batch": gen["G_BATCH"],
                "wire_ns": gen["G_TX_FIXED_NS"], "drain": gen["G_DRAIN"],
                "sockets": gen["G_SOCKETS"], "fed_sockets": gen["G_FEED_SOCKETS"],
                "txbuf": gen["G_TXBUF"],
                "hot_every": gen.get("G_HOT_EVERY", 0),
                "dg_socket0": per_sock[0] if len(per_sock) > 0 else "",
                "dg_socket1": per_sock[1] if len(per_sock) > 1 else "",
                "packets_fed": int(got["fed"][0]) if got["fed"] else "",
                "packets_offered": int(got["fed"][1]) if got["fed"] else "",
                "datagrams": int(got["datagrams"][0]) if got["datagrams"] else "",
                "records": int(got["datagrams"][1]) if got["datagrams"] else "",
                "min_free_b": int(got["min_free"][0]) if got["min_free"] else "",
                "fsr_reads": int(got["fsr"][0]) if got["fsr"] else "",
                "fsr_longest_run": int(got["fsr"][1]) if got["fsr"] else "",
                "fsr_span_us": (round(int(got["fsr"][2]) / 1e9, 3)
                                if got["fsr"] else ""),
                "rsr_reads": int(got["rsr"][0]) if got["rsr"] else "",
                "spi_txns": int(got["spi"][0]) if got["spi"] else "",
                "quiet_max_clks": int(got["spi"][1]) if got["spi"] else "",
                "sockets_served": sum(1 for v in per_sock if v > 0),
                "result": got["result"][0] if got["result"] else "NO-OUTPUT",
            }
            row["livelock"] = 1 if row["result"] == "LIVELOCK" else 0
            w.writerow(row)
            fh.flush()
            rows.append(row)
            print(f"  {axis:<12} batch={row['batch']:<3} wire={row['wire_ns']:>8} "
                  f"drain={row['drain']} sck={row['sockets']}/{row['fed_sockets']} "
                  f"buf={row['txbuf']:<5} hot={row['hot_every']:<3} "
                  f"-> minfree={row['min_free_b']:>5} "
                  f"run={row['fsr_longest_run']:>6} span={row['fsr_span_us']:>9} us "
                  f"dg0={row['dg_socket0']} dg1={row['dg_socket1']} "
                  f"served={row['sockets_served']} {row['result']}")
    print(f"\nCSV: {outp}\nlog: {logp}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
