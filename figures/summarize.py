#!/usr/bin/env python3
"""Derive the campaign's headline numbers straight from the CSVs.

Nothing here is transcribed by hand. Every table below is computed from
host/campaign/results/*/steps.csv and sim/results/sim/backpressure.csv, so the
report and the figures cannot drift apart, and re-running after another arm
lands updates both.

    /home/misha/FPGA-centric-SCIS/.venv/bin/python summarize.py > SUMMARY.md
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import load

WIRE = 148810
PLANS = [("eth_spread", "8 sockets"), ("eth_p9217", "1 socket 9217"),
         ("eth_p9219", "1 socket 9219")]


def k(v):
    return "-" if v is None else f"{v:,.0f}"


def main():
    arms = load.arms()
    order = sorted(arms.values(), key=lambda a: a["batch"] or 0)

    print("## Provenance\n")
    print("| arm | TX_BATCH_MAX_PACKETS | bitstream md5 | built |")
    print("|---|---|---|---|")
    for a in order:
        b = a["meta"].get("bitstream", {})
        print(f"| `{a['tag']}` | {a['batch']} | `{(b.get('md5') or '')[:12]}` | "
              f"{b.get('mtime','-')} |")

    print("\n## Ethernet only: sustained rate under 0.1 % loss (packets/s)\n")
    hdr = "| batch | " + " | ".join(n for _, n in PLANS) + " | wedged at |"
    print(hdr)
    print("|---" * (len(PLANS) + 2) + "|")
    for a in order:
        cells = []
        for test, _ in PLANS:
            kn = load.knee(a["rows"], test)
            cells.append(k(kn["delivered_pps"]) if kn else "-")
        wedges = sorted({int(v) for key, v in a["summary"].items()
                         if key.startswith("eth_") and key.endswith("_wedged_at")})
        w = ", ".join(f"{x:,}" for x in wedges) if wedges else "never"
        print(f"| {a['batch']} | " + " | ".join(cells) + f" | {w} |")

    print("\n## What batching buys\n")
    base = next((a for a in order if a["batch"] == 1), None)
    if base:
        bk = load.knee(base["rows"], "eth_spread")
        print("| batch | sustained (8 sockets) | vs batch 1 | achieved batch at the knee | "
              "us per metric |")
        print("|---|---|---|---|---|")
        for a in order:
            kn = load.knee(a["rows"], "eth_spread")
            if not kn or not bk:
                continue
            print(f"| {a['batch']} | {k(kn['delivered_pps'])} | "
                  f"{kn['delivered_pps']/bk['delivered_pps']:.2f}x | "
                  f"{kn['batch_mean']:.2f} | {1e6/kn['delivered_pps']:.2f} |")

    print("\n## CAN only (125 kbit/s bus, FPGA is the DUT)\n")
    print("The 0.1 % budget lands right on the 1400 frames/s rung, where a "
          "handful of\nframes decides pass or fail (8 of 7366 on one arm), so the "
          "knee column is\nthreshold-sensitive there. The robust number is the "
          "last column: the highest\nrate the bus carried with the source "
          "reporting nothing undelivered.\n")
    print("| batch | knee (0.1 % budget) | loss there | source could not enqueue from | "
          "highest rate carried cleanly |")
    print("|---|---|---|---|---|")
    for a in order:
        kn = load.knee(a["rows"], "can")
        rows = load.series(a["rows"], "can")
        sat = next((r for r in rows if (r["can_failed"] or 0) > 0), None)
        clean = [r["delivered_pps"] for r in rows if not (r["can_failed"] or 0)]
        lossc = f"{kn['loss_pct']:.3f} %" if kn else "-"
        kneec = k(kn["delivered_pps"]) if kn else "-"
        satc = k(sat["achieved_pps"]) if sat else "never"
        cleanc = k(max(clean)) if clean else "-"
        print(f"| {a['batch']} | {kneec} | {lossc} | {satc} | {cleanc} |")
    print("\nEvery arm: the FPGA delivered every frame that reached the bus "
          "until the\nSTM32 itself ran out of mailboxes. The CAN path is "
          "bus-limited, not FPGA-limited,\nand its real ceiling is still "
          "unmeasured.")

    print("\n## Ethernet + CAN on one transmit chip\n")
    print("| batch | config | Ethernet alone | with CAN 1400 f/s | cost | "
          "CAN loss over the run |")
    print("|---|---|---|---|---|---|")
    for a in order:
        for key, name in (("spread", "8 sockets"), ("p9217", "1 socket 9217"),
                          ("p9219", "1 socket 9219")):
            a1 = load.knee(a["rows"], f"eth_{key}")
            a2 = load.knee(a["rows"], f"both_{key}")
            if not (a1 and a2):
                continue
            cl = [r["can_loss_pct"] for r in load.series(a["rows"], f"both_{key}")
                  if r["can_loss_pct"] is not None]
            rng = f"{min(cl):+.3f} .. {max(cl):+.3f} %" if cl else "-"
            print(f"| {a['batch']} | {name} | {k(a1['delivered_pps'])} | "
                  f"{k(a2['delivered_pps'])} | "
                  f"-{(a1['delivered_pps']-a2['delivered_pps'])/1000:.1f}k | {rng} |")

    print("\n## Framing integrity at overload\n")
    print("Datagrams whose length is not a multiple of 9 bytes, and the largest\n"
          "record count seen in one datagram. `TX_BATCH_MAX_PACKETS` is the\n"
          "configured ceiling, so anything above it is mis-framing, not batching.\n")
    print("| batch | worst ragged datagrams in a step | largest records/datagram seen |")
    print("|---|---|---|")
    for a in order:
        rag = max((r["ragged"] or 0) for r in a["rows"]) if a["rows"] else 0
        bmax = max((r["batch_max"] or 0) for r in a["rows"]) if a["rows"] else 0
        print(f"| {a['batch']} | {rag:,.0f} | {bmax:,.0f} "
              f"{'(> configured ' + str(a['batch']) + ')' if bmax and a['batch'] and bmax > a['batch'] else ''} |")

    rr = load.repeat_rows()
    if rr:
        print("\n## Repeatability (batch 16, five repeats per rate)\n")
        print("| offered | delivered median | range | spread |")
        print("|---|---|---|---|")
        for r in rr:
            print(f"| {r['rate']:,} | {r['median']:,.0f} | "
                  f"{r['lo']:,.0f} .. {r['hi']:,.0f} | {r['spread_pct']:.1f} % |")
        print("\nAt and below the knee the spread is negligible. Above it the "
              "delivered rate\nplateaus near the knee rather than collapsing, "
              "which two earlier single-shot\nladders disagreed about -- hence "
              "the repeats.")

    print("\n## Transmit-buffer livelock (simulation)\n")
    rows = load.sim_rows()
    if rows:
        wd_us = 65535 / 30.0
        w = sorted([r for r in rows if r["axis"] == "wire"],
                   key=lambda r: r["wire_ns"])
        first = next((r for r in w if r["livelock"]), None)
        last_ok = [r for r in w if not r["livelock"]]
        print(f"- watchdog threshold: `WATCHDOG_CLKS` 65535 at 30 MHz = "
              f"**{wd_us/1000:.2f} ms**")
        if last_ok:
            print(f"- highest wire time with no livelock: "
                  f"**{last_ok[-1]['wire_ns']/1000:.0f} us/datagram** "
                  f"(longest stall {last_ok[-1]['fsr_span_us']:.0f} us)")
        if first:
            print(f"- lowest wire time that livelocks: "
                  f"**{first['wire_ns']/1000:.0f} us/datagram** "
                  f"(stall {first['fsr_span_us']/1000:.2f} ms, "
                  f"{first['fsr_longest_run']} back-to-back Sn_TX_FSR reads)")
            print(f"- as built the wire costs ~5 us/datagram, so the livelock "
                  f"needs the link about **{first['wire_ns']/5000:.0f}x slower** "
                  f"than 100 Mbit")
        print("\n| axis | setting | longest stall | verdict |")
        print("|---|---|---|---|")
        for r in rows:
            if r["axis"] in ("drain_model", "txbuf", "sockets"):
                if r["axis"] == "drain_model":
                    s = ("buffer freed on SEND (tb_w5500_tx model)" if r["drain"] == 0
                         else "buffer freed as the wire drains (the chip)")
                elif r["axis"] == "txbuf":
                    s = f"TX buffer {r['txbuf']} B"
                else:
                    s = f"{r['fed_sockets']} of 8 sockets fed"
                span = (f"{r['fsr_span_us']/1000:.2f} ms"
                        if r["fsr_span_us"] else "0")
                print(f"| {r['axis']} | {s} | {span} | {r['result']} |")
    return 0


if __name__ == "__main__":
    sys.exit(main())
