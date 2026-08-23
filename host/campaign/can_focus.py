#!/usr/bin/env python3
"""CAN-side re-measurement for the thesis chapter, fixing three things in
run_campaign.py that the campaign CSVs inherit.

1.  The CAN offered rate was `sent / can_duration` with the *nominal* duration.
    The real window is start()->stop() including the serial round trips, which
    is 5.26 s for a nominal 5.0 s, so every CAN rate in the campaign CSVs is
    5.3 % high.  Here the window is timed on the host and reported.

2.  Nothing recorded *where* the missing frames sat.  A handful of frames at
    the very end of the window is a capture-boundary artefact; the same count
    spread through the run is real loss.  This writes both.

3.  "What CAN costs Ethernet" compared the Ethernet-only ladder against the
    combined ladder, and the two do not share their rungs -- the Ethernet-only
    one has a 105 000 pps rung, the combined one jumps 90 000 -> 100 000.  Here
    every rung is run twice back to back, Ethernet alone then Ethernet with CAN,
    so the pair is comparable.

Ethernet stays at or below 105 000 pps.  That is the last rung the Ethernet path
holds inside the 0.1 % budget on this build; at 110 000 it already loses 5.2 %
over eight sockets and 86.7 % over one, so a comparison further up would measure
that collapse and not the interaction between the two channels.

    ./can_focus.py            # both arms
    ./can_focus.py --arm can  # CAN-only ladder
"""
import argparse
import csv
import json
import os
import statistics
import subprocess
import threading
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import hiload
import nucleo
import scis

OUT = os.path.join(os.path.dirname(__file__), "results-canfocus")

CAN_LADDER = [200, 600, 1000, 1200, 1300, 1400, 1450, 1500, 1600]
CAN_REPEATS_FROM = 1200          # rungs at or above this are repeated
CAN_REPEATS = 3
CAN_DURATION = 5.0

ETH_RUNGS = [0, 20000, 60000, 90000, 100000, 105000]
BOTH_DURATION = 5.0
BOTH_DRAIN = 0.8
BOTH_CAN_RATE = 1400
BOTH_REPEATS = 2
# The CAN source has to run strictly *inside* the receive window, or the frames
# it emits before the receivers arm and after they exit are counted as sent and
# never seen -- a fixed boundary term that looks exactly like FPGA loss.  The
# receivers live [0, duration+drain] from the step's start and the UDP senders
# [0.15, 0.15+duration], so CAN gets [0.6, duration+0.3]: half a second of
# margin at each end, entirely inside both.
CAN_LEAD_IN = 0.6
CAN_LEAD_OUT = 0.7   # CAN also stops before the UDP senders do, at 5.15 s


def seq_forensics(can_seqs, sent, failed=0):
    """Where the missing frames sit, as a fraction of the window.

    The scan has to run over sent+failed, not over sent.  The firmware bumps its
    sequence counter on every attempt, including the ones that found no free
    mailbox, so the sequence numbers that reached the wire are scattered over
    0..sent+failed-1.  Scanning only the first `sent` of them counts the skipped
    numbers as losses -- worth 600 phantom frames at the top of the ladder.
    `lost` below is the frames that got a mailbox and still did not arrive; it is
    the number to quote, and it needs no scan at all.
    """
    tried = sent + failed
    if sent <= 0:
        return {}
    miss = [s for s in range(min(tried, scis.CAN_SEQ_SPAN)) if s not in can_seqs]
    if not miss:
        return {"missing": 0, "lost": sent - len(can_seqs), "in_last_1pct": 0,
                "in_first_1pct": 0, "longest_run": 0, "runs": 0,
                "first_at": -1, "last_at": -1}
    edge = max(1, tried // 100)
    runs, longest, cur = 1, 1, 1
    for a, b in zip(miss, miss[1:]):
        if b == a + 1:
            cur += 1
            longest = max(longest, cur)
        else:
            runs += 1
            cur = 1
    return {
        "missing": len(miss),
        "lost": sent - len(can_seqs),
        "in_last_1pct": sum(1 for s in miss if s >= tried - edge),
        "in_first_1pct": sum(1 for s in miss if s < edge),
        "longest_run": longest,
        "runs": runs,
        "first_at": round(miss[0] / tried, 4),
        "last_at": round(miss[-1] / tried, 4),
    }


def can_step(rig, nu, rate, duration, nrecv=2):
    """One CAN-only step with the send window timed on the host.

    Order matters.  The receivers are armed first and the source is started
    *after* them, so no frame is emitted before anything is listening; and the
    window is timed from the RATE acknowledgement to the STOP acknowledgement,
    which brackets the true send window to about a millisecond.  Starting the
    source first (as run_campaign.py does) puts the fork and the 0.15 s arming
    sleep inside the send window but outside the timed one, which is worth 3 %
    on the rate.
    """
    obs = hiload.Observation(rig, duration + CAN_LEAD_IN + 1.5, nrecv=nrecv)
    time.sleep(CAN_LEAD_IN)
    nu.start(rate)
    t0 = time.monotonic()
    time.sleep(duration)
    nu.stop()
    window = time.monotonic() - t0
    st = nu.stat()
    r = obs.finish()

    sent = st.get("sent", 0)
    failed = st.get("failed", 0)
    uniq = r["can_unique"]
    row = {
        "requested_pps": rate,
        "window_s": round(window, 4),
        "can_sent": sent,
        "can_failed": failed,
        "can_unique": uniq,
        "can_dupes": r["can_dupes"],
        "can_tec": st.get("tec", 0),
        # attempts/window is what the generator actually asked the bus for;
        # sent/window is what the bus carried.
        "attempted_fps": round((sent + failed) / window, 1) if window else 0.0,
        "carried_fps": round(sent / window, 1) if window else 0.0,
        "delivered_fps": round(uniq / window, 1) if window else 0.0,
        "can_loss_pct": round(100.0 * (1 - uniq / sent), 4) if sent else 0.0,
        "enqueue_fail_pct": round(100.0 * failed / (sent + failed), 4) if sent + failed else 0.0,
        "batch_mean": r["batch_mean"],
        "batch_max": r["batch_max"],
        "ragged": r["ragged"],
        "junk": r["junk"],
        "host_drops": r["host_drops"],
        "valid": r["valid"],
    }
    row.update(seq_forensics(r["can_seqs"], sent, failed))
    return row


def arm_can(rig, nu, writer):
    print("\n=== arm A: CAN only, host-timed window ===")
    print(f"{'req':>5} {'rep':>3} {'window':>7} {'sent':>6} {'fail':>5} {'carried':>8} "
          f"{'deliv':>8} {'loss%':>7} {'miss':>5} {'tail':>5} {'run':>4} {'tec':>4}")
    rows = []
    for rate in CAN_LADDER:
        n = CAN_REPEATS if rate >= CAN_REPEATS_FROM else 1
        for rep in range(n):
            r = can_step(rig, nu, rate, CAN_DURATION)
            r["arm"], r["repeat"] = "can", rep
            writer(r)
            rows.append(r)
            print(f"{rate:>5} {rep:>3} {r['window_s']:>7.3f} {r['can_sent']:>6} "
                  f"{r['can_failed']:>5} {r['carried_fps']:>8.1f} {r['delivered_fps']:>8.1f} "
                  f"{r['can_loss_pct']:>7.3f} {r.get('missing',0):>5} "
                  f"{r.get('in_last_1pct',0):>5} {r.get('longest_run',0):>4} {r['can_tec']:>4}")
            time.sleep(0.5)
    return rows


def arm_both(rig, nu, writer):
    print("\n=== arm B: matched rungs, Ethernet alone vs Ethernet + CAN ===")
    print(f"{'eth_req':>8} {'rep':>3} {'can':>4} {'eth_deliv':>10} {'eth_loss%':>10} "
          f"{'can_sent':>9} {'can_loss%':>10} {'batch':>6} {'tec':>4}")
    rows = []
    for rep in range(BOTH_REPEATS):
        for rung in ETH_RUNGS:
            for with_can in (False, True):
                box = {}

                def can_inside():
                    """Run the CAN source inside the receive window."""
                    time.sleep(CAN_LEAD_IN)
                    nu.start(BOTH_CAN_RATE)
                    t = time.monotonic()
                    time.sleep(BOTH_DURATION + BOTH_DRAIN - CAN_LEAD_IN - CAN_LEAD_OUT)
                    nu.stop()
                    box["window"] = time.monotonic() - t
                    box["stat"] = nu.stat()

                th = threading.Thread(target=can_inside) if with_can else None
                if th:
                    th.start()
                if rung == 0:
                    r = hiload.observe(rig, BOTH_DURATION, drain=BOTH_DRAIN, nrecv=2)
                    r.update({"sent": 0, "unique": 0, "loss_pct": 0.0,
                              "delivered_pps": 0.0, "achieved_pps": 0.0})
                else:
                    r = hiload.run_step(rig, scis.ALL_PORTS, rung, BOTH_DURATION,
                                        nsend=2, nrecv=2, drain=BOTH_DRAIN)
                if th:
                    th.join()
                window = box.get("window", 0.0)
                st = box.get("stat", {"sent": 0, "failed": 0, "tec": 0})
                sent = st.get("sent", 0)
                uniq = r["can_unique"]
                row = {
                    "arm": "both", "repeat": rep,
                    "requested_pps": rung, "with_can": int(with_can),
                    "window_s": round(window, 4),
                    "eth_sent": r["sent"], "eth_unique": r["unique"],
                    "eth_loss_pct": r["loss_pct"],
                    "eth_delivered_pps": r["delivered_pps"],
                    "can_sent": sent, "can_failed": st.get("failed", 0),
                    "can_unique": uniq, "can_tec": st.get("tec", 0),
                    "can_loss_pct": round(100.0 * (1 - uniq / sent), 4) if sent else 0.0,
                    "can_delivered_fps": round(uniq / window, 1) if window else 0.0,
                    "batch_mean": r["batch_mean"], "batch_max": r["batch_max"],
                    "ragged": r["ragged"], "junk": r["junk"],
                    "host_drops": r["host_drops"], "valid": r["valid"],
                }
                row.update(seq_forensics(r["can_seqs"], sent, st.get("failed", 0))
                           if with_can else {})
                writer(row)
                rows.append(row)
                print(f"{rung:>8} {rep:>3} {'yes' if with_can else 'no':>4} "
                      f"{row['eth_delivered_pps']:>10,.0f} {row['eth_loss_pct']:>10.3f} "
                      f"{sent:>9} {row['can_loss_pct']:>10.3f} "
                      f"{row['batch_mean']:>6} {row['can_tec']:>4}")
                time.sleep(0.5)
    return rows


FIELDS = ["arm", "repeat", "requested_pps", "with_can", "window_s",
          "can_sent", "can_failed", "can_unique", "can_dupes", "can_tec",
          "attempted_fps", "carried_fps", "delivered_fps", "can_loss_pct",
          "enqueue_fail_pct", "can_delivered_fps",
          "eth_sent", "eth_unique", "eth_loss_pct", "eth_delivered_pps",
          "missing", "lost", "in_last_1pct", "in_first_1pct", "longest_run", "runs",
          "first_at", "last_at",
          "batch_mean", "batch_max", "ragged", "junk", "host_drops", "valid"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", choices=["can", "both", "all"], default="all")
    args = ap.parse_args()

    os.makedirs(OUT, exist_ok=True)
    host = scis.resolve_host_ip()
    rig = hiload.Rig(host)
    nu = nucleo.Nucleo()
    print(f"host {host} -> FPGA {scis.FPGA_RX_IP}, nucleo {nu.ping()}")

    fh = open(os.path.join(OUT, "steps.csv"), "w", newline="")
    w = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore")
    w.writeheader()

    def writer(row):
        w.writerow(row)
        fh.flush()

    try:
        if args.arm in ("can", "all"):
            arm_can(rig, nu, writer)
        if args.arm in ("both", "all"):
            arm_both(rig, nu, writer)
    finally:
        fh.close()
        nu.close()
        rig.close()

    bit = "/home/misha/FPGA-centric-SCIS/hardware/fpga/ccgm1a1/bitstream.bit"
    meta = {
        "collected_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "bitstream_md5": subprocess.run(["md5sum", bit], capture_output=True,
                                        text=True).stdout.split()[0],
        "can_ladder": CAN_LADDER, "can_repeats": CAN_REPEATS,
        "can_duration": CAN_DURATION,
        "eth_rungs": ETH_RUNGS, "both_duration": BOTH_DURATION,
        "both_can_rate": BOTH_CAN_RATE, "both_repeats": BOTH_REPEATS,
        "note": "host-timed CAN window; matched Ethernet rungs with/without CAN",
    }
    with open(os.path.join(OUT, "meta.json"), "w") as mh:
        json.dump(meta, mh, indent=2, sort_keys=True)
    print(f"\nwrote {OUT}/steps.csv")


if __name__ == "__main__":
    main()
