#!/usr/bin/env python3
"""Separate the RX W5500's cost from the TX W5500's, from the host alone.

The two chips sit on independent SPI masters (`spi_busy_0` / `spi_busy_1` in
top.vhd) with independent state machines -- the RX instance runs "receive_first"
(top.vhd:351), the TX instance "send_first" (top.vhd:317) -- so they work
concurrently and end-to-end throughput is min(RX rate, TX rate). The burst-drain
test measures that minimum. This one measures the RX side on its own, which is
what identifies which of the two is binding.

The trick: a 9-byte payload that does *not* start with 'V' passes the length
check at udp_packet_adapter.vhd:138, so the FPGA pays the identical SPI cost to
read it out of the W5500, and is then dropped at :154 before the FIFO. Same RX
work, zero TX work, nothing echoed.

So: queue J such packets in one socket, then P real ones behind them. The real
ones cannot be serviced until the junk ahead of them has been chewed through, so

    time to last echo  =  J x T_rx  +  P x T_loop  +  const

and the slope over J is the RX chip's cost per packet, in isolation.

    slope ~= T_loop   ->  RX is the bottleneck; the TX chip has headroom
    slope <  T_loop   ->  TX is the bottleneck; RX is waiting on it

Usage:
    python3 test_rx_only.py
    python3 test_rx_only.py --port 9219 --reps 15
"""

import argparse
import socket
import statistics
import struct
import sys
import time

import scis

# Same 9 bytes and same wire cost as a metric packet, but 'X01' != 'V01' so the
# adapter drops it after the read instead of forwarding it to the FIFO.
JUNK_PROTOCOL = b"X01"

# The socket buffer is 2 KB / 17 bytes = ~120 packets. J + PROBES must stay under
# that or the W5500 discards the tail and the timing means nothing.
PROBES = 20
JUNK_LADDER = [0, 20, 40, 60, 80]


def junk(seq):
    return struct.pack("!3sHi", JUNK_PROTOCOL, scis.WIDE_BAND_IDS[0],
                       scis.SEQ_MIN + (seq % scis.SEQ_SPAN))


class TimedReceiver(scis.Receiver):
    """Receiver that timestamps arrivals (see test_burst_drain.py)."""

    def reset(self):
        super().reset()
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
                if data in (scis.ALERT_INTERLOCK, scis.ALERT_ALMOSTFULL):
                    if data == scis.ALERT_INTERLOCK:
                        self.interlock += 1
                    else:
                        self.almostfull += 1
                    continue

                records, junk = scis.unpack_all(data)
                self.junk += junk

                for _, seq in records:
                    self.count[port] += 1
                    if seq in self.seqs[port]:
                        self.dupes[port] += 1
                    else:
                        self.seqs[port].add(seq)
                    self.arrivals[port].append(t)


def run_once(receiver, port, n_junk, probes=PROBES, drain=0.6):
    sock = receiver.socks[port]
    dest = (scis.FPGA_RX_IP, port)

    plan = [junk(i) for i in range(n_junk)]
    plan += [scis.pack(scis.WIDE_BAND_IDS[0], i) for i in range(probes)]

    receiver.reset()
    drops_before = scis.udp_drops(scis.ALL_PORTS)

    t0 = time.perf_counter()
    for pkt in plan:
        sock.sendto(pkt, dest)
    t_sent = time.perf_counter()

    time.sleep(drain)
    drops_after = scis.udp_drops(scis.ALL_PORTS)
    snap = receiver.snapshot()

    with receiver.lock:
        stamps = sorted(t for lst in receiver.arrivals.values() for t in lst)

    host_drops = (drops_after - drops_before) if drops_before >= 0 else -1
    if not stamps or host_drops != 0:
        return None

    return {
        "n_junk": n_junk,
        "returned": snap["unique"],
        "echoed_junk": snap["junk"],
        "interlock": snap["interlock"],
        "send_us": (t_sent - t0) * 1e6,
        "to_first_us": (stamps[0] - t0) * 1e6,
        "to_last_us": (stamps[-1] - t0) * 1e6,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=9217)
    ap.add_argument("--reps", type=int, default=15)
    ap.add_argument("--probes", type=int, default=PROBES)
    ap.add_argument("--junk", type=int, nargs="+", default=JUNK_LADDER)
    args = ap.parse_args()

    host_ip = scis.resolve_host_ip()
    print(f"host {host_ip}  ->  FPGA RX {scis.FPGA_RX_IP}:{args.port}")
    print(f"{args.probes} real packets queued behind J junk packets, "
          f"{args.reps} reps each\n")
    print(f"{'J junk':>7} {'returned':>9} {'junk echoed':>12} "
          f"{'to last echo':>14} {'delta/junk':>12}")
    print("-" * 60)

    receiver = TimedReceiver(host_ip)
    receiver.start()

    medians = {}
    try:
        run_once(receiver, args.port, 0, args.probes)      # warm-up, discarded
        time.sleep(0.25)

        for n_junk in args.junk:
            rows = []
            for _ in range(args.reps):
                r = run_once(receiver, args.port, n_junk, args.probes)
                time.sleep(0.2)
                if r:
                    rows.append(r)
            if not rows:
                print(f"{n_junk:>7}   no valid reps")
                continue

            last = statistics.median(r["to_last_us"] for r in rows)
            medians[n_junk] = last
            base = medians.get(0)
            per = ((last - base) / n_junk) if (base and n_junk) else float("nan")
            print(f"{n_junk:>7} {statistics.median(r['returned'] for r in rows):>9.0f} "
                  f"{sum(r['echoed_junk'] for r in rows):>12} "
                  f"{last:>13.0f}us {per:>11.2f}us")
    except KeyboardInterrupt:
        print("\ninterrupted")
    finally:
        receiver.stop()
        receiver.close()

    if len(medians) >= 3:
        xs = sorted(medians)
        ys = [medians[x] for x in xs]
        mean_x = statistics.fmean(xs)
        mean_y = statistics.fmean(ys)
        num = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
        den = sum((x - mean_x) ** 2 for x in xs)
        slope = num / den
        print("\n" + "=" * 60)
        print(f"RX-only cost per packet:  {slope:.2f} us  "
              f"({1e6 / slope:,.0f} pps if RX ran alone)")
        print(f"Full loop (from burst-drain): compare against that number --")
        print(f"  slope ~= full-loop cost  ->  RX binds")
        print(f"  slope <  full-loop cost  ->  TX binds")
    return 0


if __name__ == "__main__":
    sys.exit(main())
