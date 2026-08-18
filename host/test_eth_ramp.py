#!/usr/bin/env python3
"""Ethernet throughput ramp for the SCIS FPGA (T0-T3).

Sends 9-byte V01 packets to the RX W5500 at a rising rate and counts what the TX
W5500 echoes back, until packets start going missing. The rate at which loss
first appears is the answer.

    T0  sanity      10 pps, one port          -- proves the path before measuring
    T1  single      ramp on one port          -- per-socket ceiling
    T2  spread      ramp across all 8 ports   -- does round-robin help?
    T3  priority    ramp on port 9217 only    -- best case the arbiter can give

T2 vs T1 is the interesting comparison: the RX state machine polls all eight
sockets round-robin, so hammering one socket wastes seven eighths of the polls.

Usage:
    sudo chrt -f 80 python3 test_eth_ramp.py            # all of T0..T3
    python3 test_eth_ramp.py --test t1
    python3 test_eth_ramp.py --test t1 --max-rate 8000
"""

import argparse
import sys
import time

import scis

# Geometric below 16k, then finer. Measured behaviour: >=16k is clean, 32k wedges
# the board permanently, so the interesting region is in between and jumping
# straight from 16k to 32k both skips the knee and costs a power-cycle.
# The RX and TX W5500s are separate chips on separate SPI masters, so their costs
# overlap: the SPI-bound ceiling is ~37-48 kpps, and the knee should be under it.
LADDER = [100, 250, 500, 1000, 2000, 4000, 8000, 12000, 16000,
          18000, 20000, 22000, 24000, 26000, 28000, 30000, 32000]


def make_sender(receiver, ports):
    """Return send_fn(seq) that round-robins `seq` across `ports`.

    Sends from the bound receive socket for that port, so the source and
    destination ports match -- the W5500 sockets are a fixed port pair.
    """
    n = len(ports)
    socks = [receiver.socks[p] for p in ports]
    ids = [scis.WIDE_BAND_IDS[i % len(scis.WIDE_BAND_IDS)] for i in range(n)]
    dests = [(scis.FPGA_RX_IP, p) for p in ports]

    def send(seq):
        i = seq % n
        socks[i].sendto(scis.pack(ids[i], seq), dests[i])

    return send


def sanity(receiver, port):
    """T0: 100 packets at 10 pps. Everything after this assumes it passed."""
    print(f"\n=== T0 sanity: 100 packets @ 10 pps on port {port} ===")
    send = make_sender(receiver, [port])
    receiver.reset()

    pacer = scis.Pacer(10)
    pacer.start()
    for seq in range(100):
        send(seq)
        pacer.wait()

    time.sleep(1.0)
    snap = receiver.snapshot()

    print(f"  sent 100, unique back {snap['unique']}, dupes {snap['dupes']}, "
          f"junk {snap['junk']}, INTERLOCK {snap['interlock']}")

    if snap["unique"] == 100 and snap["interlock"] == 0:
        print("  PASS")
        return True

    print("  FAIL -- do not trust any ramp result until this passes.")
    if snap["unique"] == 0:
        print("    Nothing came back. Check: bitstream flashed, cabling,")
        print("    192.168.2.106/24 on the host NIC, and that the metric server")
        print("    is stopped (it owns UDP 9217-9224).")
    elif snap["interlock"]:
        print("    INTERLOCK fired -- press SW3 on the board to clear it.")
    return False


def ramp(name, receiver, ports, ladder, duration):
    print(f"\n=== {name}: ports {ports[0]}"
          f"{'-' + str(ports[-1]) if len(ports) > 1 else ''} ===")
    recorder = scis.RampRecorder(f"eth_{name.lower()}.csv", name)
    send = make_sender(receiver, ports)

    def step(rate):
        achieved, sent, snap, drops, gaps = scis.run_step(
            send, receiver, rate, duration, ports=scis.ALL_PORTS
        )
        return recorder.add(rate, achieved, sent, snap, drops, gaps)

    def alive():
        return scis.probe_alive(send, receiver, ports)

    # Pre-flight: after a power-cycle the board needs a few seconds to come up.
    # Without this the first ladder step fails, find_knee records it as the first
    # failure, and every clean step above it is then filed as non-monotonic --
    # so a perfectly good run reports no knee at all.
    for attempt in range(6):
        if alive():
            break
        print(f"  board not answering yet, waiting... ({attempt + 1}/6)")
        time.sleep(5.0)
    else:
        raise scis.BoardWedged(0)

    try:
        knee, found = scis.find_knee(step, ladder, alive_fn=alive)
    except scis.BoardWedged:
        recorder.close()      # keep the rows up to the wedge -- they are valid
        raise
    recorder.close()

    if knee and found:
        print(f"\n>>> {name} knee: ~{knee} packets/s sustained with <0.1% loss")
    elif knee:
        print(f"\n>>> {name}: >= {knee} packets/s clean -- NO knee found.")
        print(f"    The ladder topped out at {knee} without losing a packet, so")
        print(f"    this is a lower bound, not a measurement. Raise --max-rate.")
    else:
        print(f"\n>>> {name}: no clean rate found -- check T0 and host drops")
    return knee, found


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--test", choices=["all", "t0", "t1", "t2", "t3"], default="all")
    ap.add_argument("--duration", type=float, default=3.0,
                    help="seconds per ramp step (default 3)")
    ap.add_argument("--max-rate", type=int, default=None,
                    help="cap the ladder, e.g. once you know roughly where the knee is")
    ap.add_argument("--skip-sanity", action="store_true",
                    help="skip T0 (not recommended)")
    args = ap.parse_args()

    ladder = [r for r in LADDER if args.max_rate is None or r <= args.max_rate]

    host_ip = scis.resolve_host_ip()
    print(f"host {host_ip}  ->  FPGA RX {scis.FPGA_RX_IP}:"
          f"{scis.BASE_PORT}-{scis.BASE_PORT + scis.NUM_PORTS - 1}")

    receiver = scis.Receiver(host_ip)
    receiver.start()
    results = {}

    try:
        if args.test in ("all", "t0") and not args.skip_sanity:
            if not sanity(receiver, 9219) and args.test == "all":
                return 1
        if args.test in ("all", "t1"):
            results["T1 single port 9219"] = ramp("T1", receiver, [9219], ladder, args.duration)
        if args.test in ("all", "t2"):
            results["T2 spread over 8"] = ramp("T2", receiver, scis.ALL_PORTS, ladder, args.duration)
        if args.test in ("all", "t3"):
            results["T3 priority port 9217"] = ramp("T3", receiver, [9217], ladder, args.duration)
    except scis.BoardWedged as exc:
        if exc.rate == 0:
            print("\n*** board never came up ***")
            print("    It did not answer a 10 pps trickle after 30 s. Power-cycle")
            print("    it, check the cabling, and confirm the bitstream is flashed.")
            return 1
        print(f"\n*** {exc} ***")
        print("    The board stopped answering even at a trickle, so every")
        print("    later step would just report 100% loss against a dead board.")
        print("    Stopping here -- this IS the result: the RX W5500 socket")
        print("    closed and does not reopen (README.md:72). Power-cycle the")
        print("    board before the next run; SW3 only clears the interlock.")
        print(f"\n    Re-run below the wedge point:")
        print(f"        python3 test_eth_ramp.py --max-rate {exc.rate // 2}")
    except KeyboardInterrupt:
        print("\ninterrupted")
    finally:
        receiver.stop()
        receiver.close()

    if len(results) > 1:
        print("\n" + "=" * 56)
        print("SUMMARY (sustained packets/s at <0.1% loss)")
        for name, (knee, found) in results.items():
            mark = "" if found else "   (>= ladder max, NOT a knee)"
            print(f"  {name:<26} {knee if knee else 'n/a'}{mark}")

        capped = [n for n, (_, f) in results.items() if not f]
        if capped:
            print("\n  Comparing these is meaningless: the runs marked above never")
            print("  lost a packet, so they all just report the top of the ladder.")
            print("  Re-run with a higher --max-rate to find the real knees.")
        else:
            t1 = results.get("T1 single port 9219")
            t2 = results.get("T2 spread over 8")
            if t1 and t2 and t1[0] and t2[0]:
                print(f"\n  Spreading across 8 sockets is "
                      f"{t2[0] / t1[0]:.2f}x the single-socket rate.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
