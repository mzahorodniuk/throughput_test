#!/usr/bin/env python3
"""One campaign arm: Ethernet-only, CAN-only and Ethernet+CAN, for one bitstream.

Run once per batch size. Everything lands under
`host/campaign/results/<tag>/` together with a `meta.json` recording the md5 of
the bitstream that produced it and the batching generics read out of the HDL, so
a number can always be traced back to a build.

    python3 run_campaign.py --tag batch16 --suite all
    python3 run_campaign.py --tag batch16 --suite eth --duration 3
    python3 run_campaign.py --tag batch1  --suite can

Why the ladder stops where it does: a 9-byte UDP payload is an 84-byte Ethernet
frame on the wire, so 100 Mbit/s carries 148 810 packet/s and nothing above that
can reach the FPGA at all. Steps above it are still run (they show what an
overloaded link does) but are flagged `over_wire_limit=1` and excluded from the
knee, because the discard happens in the switch.
"""

import argparse
import csv
import json
import os
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hiload
import meta as metamod
import scis

HERE = os.path.dirname(os.path.abspath(__file__))


FPGA_DIR = "/home/misha/FPGA-centric-SCIS/hardware/fpga/ccgm1a1"
OSS_ENV = "/opt/oss-cad-suite/environment"
_reflashes = [0]


def reflash(max_tries=8):
    """Recover a wedged board by re-flashing the same bitstream.

    Overload closes the RX W5500 socket and it does not reopen; SW3 only clears
    the interlock latch. Re-flashing resets the FPGA, which re-runs the socket
    init, and that does bring it back (verified). Without this one wedge makes
    every later step in the run report a tidy 100 % loss against a dead board --
    which is exactly how a meaningless ladder gets turned into a knee.
    """
    if _reflashes[0] >= max_tries:
        print("  refusing to reflash again -- too many wedges this run")
        return False
    _reflashes[0] += 1
    print(f"  reflashing (#{_reflashes[0]}) ...", flush=True)
    r = subprocess.run(["bash", "-c",
                        f"source {OSS_ENV} && make upload_spi"],
                       cwd=FPGA_DIR, capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        print("  reflash FAILED:", r.stdout[-300:], r.stderr[-300:])
        return False
    time.sleep(4)
    return True


def ensure_alive(rig, ports, summary, why=""):
    """Probe, and if the board is dead reflash and probe again."""
    ok, _ = hiload.probe_alive(rig, ports)
    if ok:
        return True
    print(f"  board not answering{(' (' + why + ')') if why else ''}")
    summary.setdefault("reflashes", []).append(why)
    if not reflash():
        return False
    ok, _ = hiload.probe_alive(rig, ports)
    print("  recovered" if ok else "  still dead after reflash")
    return ok


def _quiet_stop(nu):
    """STOP the CAN source, tolerating a already-stopped / racing reply."""
    try:
        nu.stop()
    except Exception:
        pass

# Rungs are dense where the knees actually fall. Without batching the ceiling is
# in the 20-40k band (one datagram per metric costs the full nine-transaction
# SPI sequence), with batching it is just over 100k, so both regions need
# resolution or the sweep reports a knee that is really just the next rung down.
ETH_LADDER = [1000, 5000, 10000, 20000, 25000, 30000, 35000, 40000, 50000,
              60000, 80000, 90000, 100000, 105000, 110000, 115000, 120000,
              130000, 140000, 148000]
CAN_LADDER = [100, 200, 400, 600, 800, 1000, 1200, 1400, 1600]
BOTH_LADDER = [1000, 10000, 20000, 30000, 40000, 60000, 80000, 90000, 100000,
               110000, 120000]

FIELDS = ["test", "requested_pps", "achieved_pps", "delivered_pps", "sent",
          "received", "unique", "loss_pct", "dupes", "datagrams", "batch_mean",
          "batch_max", "ragged", "junk", "foreign", "almostfull", "interlock",
          "can_sent", "can_failed", "can_unique", "can_loss_pct", "can_tec",
          "host_drops", "gap_kind", "longest_gap", "over_wire_limit", "elapsed",
          "valid"]


class Out:
    def __init__(self, outdir, name):
        self.path = os.path.join(outdir, name)
        self.fh = open(self.path, "w", newline="")
        self.w = csv.DictWriter(self.fh, fieldnames=FIELDS, extrasaction="ignore")
        self.w.writeheader()
        self.rows = []

    def add(self, test, r, **extra):
        row = {k: r.get(k, "") for k in FIELDS}
        row["test"] = test
        row.update(extra)
        for k in FIELDS:
            row.setdefault(k, "")
        self.w.writerow(row)
        self.fh.flush()
        self.rows.append(row)
        return row

    def close(self):
        self.fh.close()


def fmt(r, label):
    return (f"  {label:<14} req={r['requested_pps']:>7} "
            f"ach={r['achieved_pps']:>9,.0f} deliv={r['delivered_pps']:>9,.0f} "
            f"loss={r['loss_pct']:>8.3f}% batch={r['batch_mean']:>5.2f}/{r['batch_max']:<2} "
            f"dg={r['datagrams']:>6} hd={r['host_drops']:>5} "
            f"ilk={r['interlock']} af={r['almostfull']} rag={r['ragged']} "
            f"gap={r['gap_kind']}/{r['longest_gap']}"
            + ("  [ABOVE WIRE LIMIT]" if r["over_wire_limit"] else "")
            + ("" if r["valid"] else "  [INVALID: host dropped]"))


def knee(rows, limit=0.1):
    """Highest offered rate sustained under `limit` % loss, wire-limited steps out.

    Reported as the *delivered* rate at that step, not the requested one: at the
    top of the ladder those differ, and delivered is what the system actually
    carried.
    """
    good = [r for r in rows
            if r["valid"] and not r["over_wire_limit"]
            and float(r["loss_pct"]) <= limit]
    if not good:
        return None
    best = max(good, key=lambda r: float(r["achieved_pps"]))
    return {
        "requested_pps": best["requested_pps"],
        "achieved_pps": float(best["achieved_pps"]),
        "delivered_pps": float(best["delivered_pps"]),
        "loss_pct": float(best["loss_pct"]),
        "batch_mean": float(best["batch_mean"]),
    }


# ---------------------------------------------------------------------------

def suite_eth(rig, out, args, summary):
    """Ethernet only. Three spreads, because where the load lands matters.

    spread  -- all eight sockets, the configuration the RX round-robin suits
    p9217   -- one socket, FIFO 0, the highest arbiter priority
    p9219   -- one socket, FIFO 3, lower priority than CAN
    """
    plans = [("spread", scis.ALL_PORTS), ("p9217", [9217]), ("p9219", [9219])]
    for name, ports in plans:
        print(f"\n--- ETH {name} ({len(ports)} socket(s)) ---")
        if not ensure_alive(rig, ports, summary, f"before eth_{name}"):
            print("  skipping: board will not come back")
            continue
        rows = []
        for rate in ETH_LADDER:
            r = hiload.run_step(rig, ports, rate, args.duration,
                                nsend=args.nsend, nrecv=args.nrecv)
            rows.append(out.add(f"eth_{name}", r))
            print(fmt(r, name))
            if r["loss_pct"] > 50.0:
                ok, _ = hiload.probe_alive(rig, ports)
                if not ok:
                    # The ladder stops here: every higher rate would only wedge
                    # it again, and the rows would be about a dead board.
                    print(f"  *** WEDGED at {rate} pps")
                    summary[f"eth_{name}_wedged_at"] = rate
                    reflash()
                    break
        summary[f"eth_{name}_knee"] = knee(rows)


def suite_can(rig, out, args, summary):
    """CAN only. The FPGA is the DUT; the Nucleo is just the traffic source.

    `failed` from the Nucleo is frames that never reached the bus because its TX
    mailboxes were full -- our limit, not the FPGA's. Loss with failed == 0 is
    the only thing that indicts the board.
    """
    try:
        import nucleo
    except SystemExit as e:
        print(f"  CAN skipped: {e}")
        summary["can_skipped"] = str(e)
        return
    try:
        nu = nucleo.Nucleo(args.serial)
    except Exception as e:
        print(f"  CAN skipped: cannot open {args.serial}: {e}")
        summary["can_skipped"] = f"{type(e).__name__}: {e}"
        return

    print(f"\n--- CAN only ({nu.ping()}) ---")
    if not ensure_alive(rig, [9219], summary, "before can"):
        print("  skipping CAN: board will not come back")
        nu.close()
        return
    rows = []
    try:
        for rate in CAN_LADDER:
            # Zero the firmware's counters, then open the capture window and
            # stop the source *inside* it, so every frame it reports as sent was
            # sent while the receivers were listening and the drain catches the
            # tail. Otherwise the boundary alone shows up as ~0.1 % loss.
            nu.start(rate)
            obs = hiload.Observation(rig, args.can_duration + 1.5,
                                     nrecv=args.nrecv)
            time.sleep(args.can_duration)
            nu.stop()
            st = nu.stat()
            r = obs.finish()
            r["elapsed"] = args.can_duration
            sent = st.get("sent", 0)
            uniq = r["can_unique"]
            kind, longest = hiload.can_gap(r["can_seqs"], sent)
            r2 = dict(r)
            r2.update({
                "requested_pps": rate,
                "achieved_pps": round(sent / args.can_duration, 1),
                "delivered_pps": round(uniq / args.can_duration, 1),
                "sent": sent, "unique": uniq,
                "received": r["can_received"],
                "loss_pct": round(100.0 * (1 - uniq / sent), 4) if sent else 0.0,
                "dupes": r["can_dupes"],
                "gap_kind": kind, "longest_gap": longest,
                "over_wire_limit": 0,
            })
            rows.append(out.add("can", r2, can_sent=sent,
                                can_failed=st.get("failed", 0),
                                can_unique=uniq,
                                can_loss_pct=r2["loss_pct"],
                                can_tec=st.get("tec", 0)))
            blame = ("HOST" if r["host_drops"] else
                     "BUS" if st.get("failed", 0) and r2["loss_pct"] <= 0.1 else
                     "STM32" if st.get("failed", 0) else
                     "FPGA" if r2["loss_pct"] > 0.1 else "-")
            print(f"  can  req={rate:>5} bus={r2['achieved_pps']:>8,.0f} f/s "
                  f"uniq={uniq:>6} loss={r2['loss_pct']:>7.3f}% "
                  f"failed={st.get('failed',0):>5} tec={st.get('tec',0):>3} "
                  f"batch={r['batch_mean']:>5.2f} blame={blame}")
            if blame == "BUS":
                print("  bus saturated -- the FPGA never got to show its own limit")
                summary["can_bus_saturated_at"] = rate
                break
            if blame == "FPGA":
                summary["can_fpga_loss_at"] = rate
                break
    finally:
        nu.close()
    summary["can_knee"] = knee(rows)


def suite_both(rig, out, args, summary):
    """Ethernet and CAN at once -- they share one TX W5500 and one arbiter.

    Run twice. UDP on 9217 is FIFO 0, which outranks CAN's FIFO 1; UDP on 9219
    is FIFO 3, which does not. Comparing the two is what shows whether strict
    priority actually protects the higher-priority channel, or whether the loss
    happens upstream of the arbiter where priority cannot help.
    """
    try:
        import nucleo
        nu = nucleo.Nucleo(args.serial)
    except Exception as e:
        print(f"  combined skipped: {e}")
        summary["both_skipped"] = str(e)
        return

    plans = [("spread", scis.ALL_PORTS), ("p9217", [9217]), ("p9219", [9219])]
    try:
        for name, ports in plans:
            print(f"\n--- ETH+CAN {name}  (CAN {args.can_rate} f/s) ---")
            if not ensure_alive(rig, ports, summary, f"before both_{name}"):
                print("  skipping: board will not come back")
                continue
            rows = []
            for rate in BOTH_LADDER:
                # run_step blocks for duration+drain; a timer stops CAN when
                # the UDP window closes so both streams cover one window.
                nu.start(args.can_rate)
                stopper = threading.Timer(args.duration + 0.45, _quiet_stop, [nu])
                stopper.start()
                r = hiload.run_step(rig, ports, rate, args.duration,
                                    nsend=args.nsend, nrecv=args.nrecv)
                stopper.cancel()
                _quiet_stop(nu)
                st = nu.stat()
                csent = st.get("sent", 0)
                cuniq = r["can_unique"]
                closs = round(100.0 * (1 - cuniq / csent), 4) if csent else 0.0
                rows.append(out.add(f"both_{name}", r, can_sent=csent,
                                    can_failed=st.get("failed", 0),
                                    can_unique=cuniq, can_loss_pct=closs,
                                    can_tec=st.get("tec", 0)))
                print(fmt(r, name) + f"  | CAN sent={csent} uniq={cuniq} "
                      f"loss={closs:.3f}%")
                if r["loss_pct"] > 50.0:
                    ok, _ = hiload.probe_alive(rig, ports)
                    if not ok:
                        print(f"  *** WEDGED at {rate} pps")
                        summary[f"both_{name}_wedged_at"] = rate
                        reflash()
                        break
            summary[f"both_{name}_knee"] = knee(rows)
    finally:
        nu.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tag", required=True, help="output dir name, e.g. batch16")
    ap.add_argument("--suite", default="all",
                    help="all, or a comma-separated subset of eth,can,both")
    ap.add_argument("--duration", type=float, default=3.0)
    ap.add_argument("--can-duration", type=float, default=5.0)
    ap.add_argument("--can-rate", type=int, default=1400,
                    help="CAN frames/s during the combined arm")
    ap.add_argument("--serial", default="/dev/ttyACM0")
    ap.add_argument("--nsend", type=int, default=2)
    ap.add_argument("--nrecv", type=int, default=2)
    args = ap.parse_args()

    outdir = os.path.join(HERE, "results", args.tag)
    os.makedirs(outdir, exist_ok=True)

    m = metamod.collect(args.tag, extra={
        "campaign": {
            "suite": args.suite, "duration": args.duration,
            "can_duration": args.can_duration, "can_rate": args.can_rate,
            "eth_ladder": ETH_LADDER, "can_ladder": CAN_LADDER,
            "both_ladder": BOTH_LADDER,
            "wire_limit_pps": hiload.WIRE_LIMIT_PPS,
            "nsend": args.nsend, "nrecv": args.nrecv,
        }})
    metamod.write(os.path.join(outdir, "meta.json"), m)
    print(f"tag={args.tag}  batch={m['hdl_generics'].get('TX_BATCH_MAX_PACKETS')}  "
          f"bitstream md5={m['bitstream']['md5']}")
    print(f"wire limit {hiload.WIRE_LIMIT_PPS:,} pps  ->  {outdir}")

    host_ip = scis.resolve_host_ip()
    rig = hiload.Rig(host_ip)
    print(f"host {host_ip}  SO_RCVBUF granted {rig.granted_rcvbuf/1024:.0f} KiB")

    summary = {}
    out = Out(outdir, "steps.csv" if args.suite == "all"
              else f"steps_{args.suite.replace(',', '_')}.csv")
    t0 = time.time()
    try:
        # Reflash rather than give up: a previous arm can leave the RX socket
        # closed, and refusing to start there silently drops a whole arm from
        # the sweep (it dropped batch 28 once already).
        if not ensure_alive(rig, [9219], summary, "at start of run"):
            print("board will not answer even after a reflash -- stopping")
            return 1
        print("board answers a trickle: OK")
        want = ({"eth", "can", "both"} if args.suite == "all"
                else {s.strip() for s in args.suite.split(",")})
        if "eth" in want:
            suite_eth(rig, out, args, summary)
        if "can" in want:
            suite_can(rig, out, args, summary)
        if "both" in want:
            suite_both(rig, out, args, summary)
    except KeyboardInterrupt:
        print("\ninterrupted")
        summary["interrupted"] = True
    finally:
        out.close()
        rig.close()
        summary["wall_seconds"] = round(time.time() - t0, 1)
        with open(os.path.join(outdir, "summary.json"), "w") as fh:
            json.dump(summary, fh, indent=2, sort_keys=True, default=str)

    print("\n=== knees (loss <= 0.1 %, wire-limited steps excluded) ===")
    for k, v in sorted(summary.items()):
        if k.endswith("_knee") and v:
            print(f"  {k:<22} {v['delivered_pps']:>9,.0f} pkt/s delivered "
                  f"(offered {v['achieved_pps']:,.0f}, batch {v['batch_mean']:.2f})")
        elif k.endswith("_knee"):
            print(f"  {k:<22} none found")
    print(f"\nCSV: {out.path}\nmeta: {outdir}/meta.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
