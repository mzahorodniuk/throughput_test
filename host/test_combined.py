#!/usr/bin/env python3
"""Combined CAN + Ethernet load (T5) -- do the two channels share one budget?

Both paths converge on a single TX W5500. If that chip is the throughput
bottleneck, then CAN frames and UDP packets compete for **one shared output
budget**, and the combined total cannot exceed what Ethernet alone achieves.
This is the only test that can show that, because it is the only one where both
inputs run at once.

Two questions, and they need different settings:

  1. Does one channel starve the other?
     Needs the arbiter to actually be contended. TUSER *is* the arrival socket
     and the output arbiter is strict highest-priority-first, so UDP on 9217
     (FIFO 0) outranks CAN (FIFO 1) while 9219 (FIFO 3) does not. But below the
     drain limit the FIFOs empty between packets and nothing can starve whatever
     the priorities say -- so the Ethernet load has to reach saturation.

         python3 test_combined.py --eth-port 9217 --max-rate 22000
         python3 test_combined.py --eth-port 9219 --max-rate 22000

  2. What is the combined ceiling?
     Needs BOTH channels at maximum: Ethernet spread over every socket (~30k,
     versus ~21k on one) and CAN near its 1468 f/s bus limit.

         python3 test_combined.py --spread --can-rate 1400 --max-rate 28000

     Watch the `total` column. If it flattens at roughly the Ethernet-only
     figure, the TX W5500 is a shared bottleneck and CAN frames displace UDP
     packets one for one.

WARNING: ~32k pps closes the RX W5500 socket permanently and needs a
power-cycle. With CAN running the TX path carries more, so the priority FIFOs
can back up and stall the RX path at a *lower* UDP rate than 32k. Keep
--max-rate at or below 28000 and watch the AF (ALMOSTFULL) column.
"""

import argparse
import sys
import time

import scis
from nucleo import Nucleo

# Must reach into saturation to test the arbiter at all. T1/T3 put the
# single-socket knee at ~21k and T2 the 8-socket knee at ~30k; 32k wedges the
# board, so the ladder stops short of it.
LADDER = [100, 500, 2000, 8000, 12000, 16000, 20000, 22000, 24000, 26000, 28000]

# Comfortably below the ~1468 f/s bus ceiling, so anything CAN loses is the
# FPGA's arbiter rather than the wire. Raise it with --can-rate for question 2.
CAN_RATE = 500


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", default="/dev/ttyACM0", help="Nucleo serial port")
    ap.add_argument("--eth-port", type=int, default=9219, choices=scis.ALL_PORTS,
                    help="single UDP port to load (default 9219)")
    ap.add_argument("--spread", action="store_true",
                    help="spread UDP over every socket except CAN's (9218), for "
                         "the combined-maximum test")
    ap.add_argument("--can-rate", type=int, default=CAN_RATE,
                    help=f"CAN frames/s (default {CAN_RATE}; bus limit ~1468)")
    ap.add_argument("--duration", type=float, default=5.0)
    ap.add_argument("--max-rate", type=int, default=None,
                    help="cap the Ethernet ladder (keep <= 28000; 32k wedges the board)")
    args = ap.parse_args()

    ladder = [r for r in LADDER if args.max_rate is None or r <= args.max_rate]

    if args.spread:
        # Leave 9218 to CAN so each channel owns its own priority FIFOs and the
        # measurement is not confounded by them sharing one.
        eth_ports = [p for p in scis.ALL_PORTS if p != scis.CAN_RETURN_PORT]
        label = "spread"
        print(f"Ethernet spread over {len(eth_ports)} sockets "
              f"(all but {scis.CAN_RETURN_PORT}, which is CAN's), "
              f"CAN at {args.can_rate} fps")
        print("Measuring the COMBINED ceiling: watch whether `total` flattens.\n")
    else:
        eth_ports = [args.eth_port]
        label = str(args.eth_port)
        note = ("UDP outranks CAN -- starvation possible once saturated"
                if args.eth_port < scis.CAN_RETURN_PORT
                else "CAN outranks UDP -- CAN should survive")
        print(f"Ethernet load on {args.eth_port}, CAN at {args.can_rate} fps")
        print(f"Arbiter: {note}\n")

    host_ip = scis.resolve_host_ip()
    nucleo = Nucleo(args.port)
    receiver = scis.Receiver(host_ip)
    receiver.start()

    recorder = scis.RampRecorder(f"combined_{label}.csv", "T5", print_header=False)
    print(f"{'eth pps':>8} {'eth uniq':>9} {'eth loss%':>10} {'can sent':>9} "
          f"{'can uniq':>9} {'can loss%':>10} {'total pps':>10} {'AF':>4}")
    print("-" * 82)

    socks = [receiver.socks[p] for p in eth_ports]
    dests = [(scis.FPGA_RX_IP, p) for p in eth_ports]
    idents = [scis.WIDE_BAND_IDS[i % len(scis.WIDE_BAND_IDS)]
              for i in range(len(eth_ports))]
    n = len(eth_ports)

    def send_eth(seq):
        i = seq % n
        socks[i].sendto(scis.pack(idents[i], seq), dests[i])

    worst_can = 0.0
    worst_eth = 0.0
    top_rate = 0
    best_total = 0.0

    try:
        for rate in ladder:
            receiver.reset()
            drops_before = scis.udp_drops(scis.ALL_PORTS)

            nucleo.start(args.can_rate)
            pacer = scis.Pacer(rate)
            pacer.start()
            t0 = time.perf_counter()
            eth_sent = 0
            while time.perf_counter() - t0 < args.duration:
                send_eth(eth_sent)
                eth_sent += 1
                pacer.wait()
            elapsed = time.perf_counter() - t0
            nucleo.stop()
            time.sleep(1.0)

            st = nucleo.stat()
            snap = receiver.snapshot()
            host_drops = scis.udp_drops(scis.ALL_PORTS) - drops_before

            eth_loss = 100.0 * (1 - snap["unique"] / eth_sent) if eth_sent else 0.0
            can_loss = (100.0 * (1 - snap["can_unique"] / st["sent"])
                        if st["sent"] else 0.0)
            # The number that answers question 2: everything the board actually
            # delivered per second, from both inputs combined.
            total = (snap["unique"] + snap["can_unique"]) / elapsed

            flag = "  <-- HOST DROPPED" if host_drops else ""
            print(f"{rate:>8} {snap['unique']:>9} {eth_loss:>10.3f} "
                  f"{st['sent']:>9} {snap['can_unique']:>9} {can_loss:>10.3f} "
                  f"{total:>10.0f} {snap['almostfull']:>4}{flag}")

            recorder.write_row(
                requested_pps=rate, achieved_pps=round(eth_sent / elapsed, 1),
                sent=eth_sent, received=snap["received"], unique=snap["unique"],
                loss_pct=round(eth_loss, 3), dupes=snap["dupes"],
                almostfull=snap["almostfull"], interlock=snap["interlock"],
                host_drops=host_drops, valid=int(host_drops == 0),
            )
            recorder.write_row(
                test="T5-can", requested_pps=args.can_rate,
                achieved_pps=round(st["sent"] / elapsed, 1),
                sent=st["sent"], received=snap["can_received"],
                unique=snap["can_unique"], loss_pct=round(can_loss, 3),
                dupes=snap["can_dupes"], host_drops=host_drops,
                valid=int(host_drops == 0),
            )

            if host_drops:
                continue
            worst_can = max(worst_can, can_loss)
            worst_eth = max(worst_eth, eth_loss)
            best_total = max(best_total, total)
            top_rate = rate

            if can_loss > 50.0:
                print(f"\n  CAN starved: {can_loss:.1f}% of CAN frames lost while "
                      f"UDP ran at {rate} pps on port {args.eth_port}.")
                break
            if eth_loss > 50.0:
                print(f"\n  Ethernet collapsed at {rate} pps -- stopping before "
                      f"the socket closes.")
                break
    except KeyboardInterrupt:
        print("\ninterrupted")
    finally:
        nucleo.close()
        receiver.stop()
        receiver.close()
        recorder.close()

    print(f"\n>>> peak combined delivery: {best_total:.0f} packets/s "
          f"(UDP up to {top_rate} pps + CAN at {args.can_rate} fps)")
    print(f">>> worst loss:  CAN {worst_can:.3f}%   Ethernet {worst_eth:.3f}%")

    # "CAN survived" is only half the story. If the *higher priority* channel is
    # the one losing packets, the loss is upstream of the arbiter entirely and
    # priority is not protecting anything -- which is the opposite of what a
    # bare "no starvation" verdict implies.
    if worst_eth > 1.0 and worst_can < 1.0:
        higher = "Ethernet" if args.eth_port < scis.CAN_RETURN_PORT else "CAN"
        print(f"\n    NOTE: Ethernet lost {worst_eth:.1f}% while CAN lost none"
              f" -- and {higher} holds")
        print("    the higher priority. The arbiter is therefore NOT where packets")
        print("    are being dropped: they are lost at RX W5500 ingestion, before")
        print("    reaching a priority FIFO. Priority affects ordering, not loss.")

    if args.spread:
        # Ethernet alone reached ~30k (T2). If the combined total lands there
        # too, the two inputs are sharing one output budget.
        print("\n    Compare against Ethernet alone (T2, ~30000 pps):")
        print("      total ~= 30000  -> ONE shared budget; CAN displaces UDP 1:1")
        print("      total  > 30000  -> the inputs are not competing for output")
    elif worst_can < 1.0:
        if top_rate < 16000:
            print("\n    CAN was never starved -- but the Ethernet load stayed well")
            print("    below the ~21k drain limit, so the arbiter was never")
            print("    contended. Re-run with a higher --max-rate to test it.")
        else:
            print("\n    CAN survived with Ethernet at saturation: the strict")
            print("    priority arbiter does NOT starve lower-priority channels.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
