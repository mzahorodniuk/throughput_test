#!/usr/bin/env python3
"""Burst-drain sweep: the FPGA's service rate vs number of hot RX sockets.

The ramp tests (T1-T3) find the rate at which loss *starts*, which is below the
rate the pipeline can actually service -- loss appears when instantaneous
coincidences overflow, not when mean capacity is hit. This measures capacity
directly, and does it without sustained overload, so it cannot wedge the board
the way a 32 kpps ladder step does.

Method: dump a burst into the W5500's RX socket buffers faster than the FPGA can
drain them (each socket holds 2 KB = ~120 packets of 8-byte header + 9-byte
payload), then time the echoes. While the buffer is non-empty the echo rate *is*
the service rate, in packets/s, with no host pacing involved.

Why sweep the number of hot sockets: the RX FSM's cost per packet depends on it
(w5500_state_machine.vhd:492 only advances the socket counter when a socket is
empty, so one hot socket pays 7 wasted Sn_RX_RSR polls per packet), while the TX
FSM's cost per packet is identical either way (:596-733 runs the same nine
transactions regardless of socket). So:

    drain rises with k, then plateaus  ->  RX polling binds at k=1,
                                           the TX chip binds above it
    drain keeps rising to k=8          ->  RX SPI binds everywhere, TX never does
    drain flat for every k             ->  neither model holds; instrument the
                                           spi_busy duty cycle instead

Usage:
    python3 test_burst_drain.py                 # k = 1,2,4,8
    python3 test_burst_drain.py --k 1 8 --reps 40
"""

import argparse
import socket
import statistics
import sys
import time

import scis

# 2 KB socket buffer / 17 bytes per packet = ~120. Stay under it so a run
# measures the drain rate rather than the overflow behaviour.
DEFAULT_BURST = 100


class TimedReceiver(scis.Receiver):
    """scis.Receiver plus an arrival timestamp per packet.

    Subclassed rather than edited into scis.py because the ramp tests share that
    module and do not need the extra per-packet work.
    """

    def reset(self):
        super().reset()
        # Rebound, not cleared: a thread mid-append keeps writing to the old
        # list, which is exactly what we want it to do.
        self.arrivals = {p: [] for p in self.ports}

    def _loop(self, port):
        sock = self.socks[port]
        while self._running:
            try:
                data, _ = sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                return
            t = time.perf_counter()

            with self.lock:
                if data == scis.ALERT_INTERLOCK:
                    self.interlock += 1
                    continue
                if data == scis.ALERT_ALMOSTFULL:
                    self.almostfull += 1
                    continue

                # One datagram may carry several metrics once the bitstream
                # batches them. Timestamping each record with the datagram's
                # arrival is what makes the drain rate come out in metrics/s
                # rather than datagrams/s.
                records, junk = scis.unpack_all(data)
                self.junk += junk

                for identifier, seq in records:
                    if identifier == scis.CAN_IDENTIFIER:
                        self.can_count += 1
                        self.can_seqs.add(seq)
                        continue

                    self.count[port] += 1
                    if seq in self.seqs[port]:
                        self.dupes[port] += 1
                    else:
                        self.seqs[port].add(seq)
                    self.arrivals[port].append(t)


def burst_once(receiver, ports, n_per_port, drain=0.6):
    """One burst. Returns a dict of measurements, or None if the host dropped."""
    k = len(ports)
    total = n_per_port * k

    # Build every datagram before timing anything -- struct.pack inside the send
    # loop would cap the offered rate below the drain rate we are trying to beat.
    ids = [scis.WIDE_BAND_IDS[i % len(scis.WIDE_BAND_IDS)] for i in range(k)]
    plan = [(receiver.socks[ports[i % k]],
             scis.pack(ids[i % k], i),
             (scis.FPGA_RX_IP, ports[i % k]))
            for i in range(total)]

    receiver.reset()
    drops_before = scis.udp_drops(scis.ALL_PORTS)

    t0 = time.perf_counter()
    for sock, pkt, dest in plan:
        sock.sendto(pkt, dest)
    t_sent = time.perf_counter()

    time.sleep(drain)
    drops_after = scis.udp_drops(scis.ALL_PORTS)
    snap = receiver.snapshot()

    with receiver.lock:
        stamps = sorted(t for lst in receiver.arrivals.values() for t in lst)

    host_drops = (drops_after - drops_before) if drops_before >= 0 else -1
    offered = total / (t_sent - t0) if t_sent > t0 else 0.0

    # Rate over the middle 80 % of arrivals: the head can overlap the send loop
    # and the tail is the queue running dry, neither of which is service rate.
    rate = None
    if len(stamps) >= 20:
        lo = int(len(stamps) * 0.1)
        hi = int(len(stamps) * 0.9)
        span = stamps[hi - 1] - stamps[lo]
        if span > 0:
            rate = (hi - 1 - lo) / span

    return {
        "sent": total,
        "returned": snap["unique"],
        "dupes": snap["dupes"],
        "junk": snap["junk"],
        "interlock": snap["interlock"],
        "almostfull": snap["almostfull"],
        "offered_pps": offered,
        "drain_pps": rate,
        "host_drops": host_drops,
        "valid": host_drops == 0 and rate is not None,
    }


def sweep(receiver, k_values, n_per_port, reps):
    print(f"\nburst {n_per_port} packets/socket, {reps} reps per k "
          f"(+1 discarded warm-up)\n")
    print(f"{'k':>3} {'ports':>12} {'offered':>9} {'drain pps':>11} "
          f"{'spread':>8} {'returned':>9} {'ILK':>4} {'AF':>4}")
    print("-" * 68)

    results = {}
    for k in k_values:
        ports = scis.ALL_PORTS[:k]
        rates = []
        offered = []
        returned = []
        alerts = [0, 0]
        discarded = 0

        for rep in range(reps + 1):
            r = burst_once(receiver, ports, n_per_port)
            time.sleep(0.25)
            if rep == 0:                 # warm-up: absorbs TX-side ARP, etc.
                continue
            if not r["valid"]:
                discarded += 1
                continue
            rates.append(r["drain_pps"])
            offered.append(r["offered_pps"])
            returned.append(r["returned"])
            alerts[0] += r["interlock"]
            alerts[1] += r["almostfull"]

        if not rates:
            print(f"{k:>3} {str(ports[0]) + '..' + str(ports[-1]):>12} "
                  f"  no valid reps ({discarded} discarded)")
            continue

        med = statistics.median(rates)
        spread = (max(rates) - min(rates)) / med * 100.0
        results[k] = (med, rates)
        label = f"{ports[0]}" if k == 1 else f"{ports[0]}-{ports[-1]}"
        print(f"{k:>3} {label:>12} {statistics.median(offered):>9.0f} "
              f"{med:>11.0f} {spread:>7.1f}% {statistics.median(returned):>9.0f} "
              f"{alerts[0]:>4} {alerts[1]:>4}"
              + (f"   ({discarded} discarded)" if discarded else ""))

        if not scis.probe_alive(
                lambda seq, p=ports[0]: receiver.socks[p].sendto(
                    scis.pack(scis.WIDE_BAND_IDS[0], seq), (scis.FPGA_RX_IP, p)),
                receiver, ports):
            print(f"\n*** board stopped answering after k={k} ***")
            print("    Power-cycle it before the next run. SW3 only clears the")
            print("    interlock latch, not a closed socket.")
            raise scis.BoardWedged(0)

    return results


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--k", type=int, nargs="+", default=[1, 2, 4, 8],
                    help="hot-socket counts to sweep (default 1 2 4 8)")
    ap.add_argument("--burst", type=int, default=DEFAULT_BURST,
                    help=f"packets per socket per burst (default {DEFAULT_BURST}, "
                         "socket buffer holds ~120)")
    ap.add_argument("--reps", type=int, default=25,
                    help="measured repetitions per k (default 25)")
    args = ap.parse_args()

    host_ip = scis.resolve_host_ip()
    print(f"host {host_ip}  ->  FPGA RX {scis.FPGA_RX_IP}:"
          f"{scis.BASE_PORT}-{scis.BASE_PORT + scis.NUM_PORTS - 1}")

    receiver = TimedReceiver(host_ip)
    receiver.start()

    try:
        results = sweep(receiver, args.k, args.burst, args.reps)
    except scis.BoardWedged:
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 1
    finally:
        receiver.stop()
        receiver.close()

    if len(results) > 1:
        print("\n" + "=" * 68)
        print("SERVICE RATE vs HOT SOCKETS")
        base = results[min(results)][0]
        for k in sorted(results):
            med = results[k][0]
            print(f"  k={k}: {med:>8.0f} pps   ({med / base:.2f}x k={min(results)})")
        top = max(results, key=lambda k: results[k][0])
        print(f"\n  Peak at k={top}: {results[top][0]:.0f} pps "
              f"= {1e6 / results[top][0]:.1f} us/packet")
    return 0


if __name__ == "__main__":
    sys.exit(main())
