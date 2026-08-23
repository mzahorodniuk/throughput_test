#!/usr/bin/env python3
"""Push past the T1/T2/T3 knee to find the real ceiling, loss and all.

test_eth_ramp.py stops climbing after two consecutive steps over the 0.1%
loss bar -- that is the right behaviour for "sustained, lossless throughput"
but it means the ladder never shows you what happens above the knee: does
delivered throughput plateau, or does it fall off a cliff, or does the board
wedge?

This script keeps stepping regardless of loss %, and only stops on its own
when the board actually wedges (probe_alive fails), matching the same guard
test_eth_ramp.py uses -- so it is safe to leave running, but NOT loss-gated.

Usage:
    python3 test_eth_ceiling.py --ports 9217 --start 80000 --stop 150000 --step 10000
    python3 test_eth_ceiling.py --ports all  --start 15000 --stop 40000  --step 2500
"""

import argparse
import sys

import scis


def make_sender(receiver, ports):
    n = len(ports)
    socks = [receiver.socks[p] for p in ports]
    ids = [scis.WIDE_BAND_IDS[i % len(scis.WIDE_BAND_IDS)] for i in range(n)]
    dests = [(scis.FPGA_RX_IP, p) for p in ports]

    def send(seq):
        i = seq % n
        socks[i].sendto(scis.pack(ids[i], seq), dests[i])

    return send


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ports", default="9217",
                     help="'all' for the 8-port spread (T2), or a single port (T1/T3)")
    ap.add_argument("--start", type=int, required=True)
    ap.add_argument("--stop", type=int, required=True)
    ap.add_argument("--step", type=int, required=True)
    ap.add_argument("--duration", type=float, default=5.0,
                     help="seconds per step (default 5 -- longer than the ramp's 3s "
                          "for a steadier achieved/unique reading)")
    args = ap.parse_args()

    ports = scis.ALL_PORTS if args.ports == "all" else [int(args.ports)]

    host_ip = scis.resolve_host_ip()
    print(f"host {host_ip}  ->  FPGA RX {scis.FPGA_RX_IP}:{ports}")

    receiver = scis.Receiver(host_ip)
    receiver.start()
    send = make_sender(receiver, ports)

    def alive():
        return scis.probe_alive(send, receiver, ports)

    recorder = scis.RampRecorder("eth_ceiling.csv", f"ceiling_{args.ports}")

    try:
        if not alive():
            print("board not answering at a trickle -- power-cycle before running this")
            return 1

        rate = args.start
        while rate <= args.stop:
            achieved, sent, snap, host_drops, gaps = scis.run_step(
                send, receiver, rate, args.duration, ports=scis.ALL_PORTS
            )
            loss, valid = recorder.add(rate, achieved, sent, snap, host_drops, gaps)

            # Use the actual elapsed time, not args.duration: a high enough rate
            # exhausts SEQ_SPAN (90112 unique values) before the step's wall-clock
            # budget runs out, so the step ends early (see scis.run_step).
            elapsed = sent / achieved if achieved else args.duration
            delivered = snap["unique"] / elapsed
            print(f"         -> delivered ~{delivered:,.0f} unique pkts/s over the step "
                  f"(sent {sent}, requested {rate})")

            if loss > 50.0:
                print("  loss > 50%% -- probing whether the board is still alive...")
                if not alive():
                    print(f"\n*** WEDGED at {rate} pps *** -- board stopped answering "
                          f"even a 10 pps trickle. Power-cycle it; SW3 will not fix this.")
                    break

            rate += args.step
    except KeyboardInterrupt:
        print("\ninterrupted")
    finally:
        recorder.close()
        receiver.stop()
        receiver.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
