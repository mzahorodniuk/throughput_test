#!/usr/bin/env python3
"""Do batched datagrams arrive intact, at every offered rate?

Throughput is not the question here -- correctness is. A batched datagram is
N * 9 bytes with no delimiters, so a single spurious byte anywhere inside it
displaces every record behind it. Counting metrics that came back is blind to
that: the records are all present, they are just no longer parseable.

So this counts records that fail the 'V01' check rather than records that went
missing. That distinction is what identified the SPI TX underrun: at 90 kpps
all 200 metrics returned, but 110 of them were the previous record shifted by
one byte. A datagram of N metrics yielded exactly N-2 bad records.

The ladder matters because the defect only appears once batches grow: below
~40 kpps the queue never builds, every datagram holds one metric, and there is
no internal boundary to corrupt.

Acceptance: bad == 0 at every rate.

Usage:
    python3 test_batch_integrity.py
    python3 test_batch_integrity.py --port 9219 --count 400
"""

import argparse
import collections
import socket
import sys
import time

import scis

# The identifiers this harness transmits. Anything else on these ports is
# foreign traffic (the CAN tap forwards whatever is on the bus) and is counted
# separately rather than mistaken for a corrupted probe.
OUR_IDS = frozenset(scis.WIDE_BAND_IDS)

LADDER = [2000, 10000, 20000, 40000, 60000, 90000]


def run_rate(sock, port, rate, count, window=1.0):
    while True:                                  # drain anything left over
        try:
            sock.recvfrom(4096)
        except BlockingIOError:
            break

    pacer = scis.Pacer(rate)
    pacer.start()
    t0 = time.perf_counter()
    for i in range(count):
        sock.sendto(scis.pack(scis.WIDE_BAND_IDS[0], i), (scis.FPGA_RX_IP, port))
        pacer.wait()
    achieved = count / (time.perf_counter() - t0)

    sizes = collections.Counter()
    datagrams = records = valid = bad = foreign = 0
    seqs = set()
    sample = None

    deadline = time.perf_counter() + window
    while time.perf_counter() < deadline:
        try:
            data, _ = sock.recvfrom(4096)
        except BlockingIOError:
            continue
        if data in (scis.ALERT_INTERLOCK, scis.ALERT_ALMOSTFULL):
            continue
        datagrams += 1
        sizes[len(data)] += 1
        # Walk the datagram in 9-byte steps directly rather than via
        # unpack_all(), which reports only a count of bad records -- here the
        # raw bytes of the first bad one are the diagnostic.
        for off in range(0, len(data) - len(data) % scis.PACKET_SIZE,
                         scis.PACKET_SIZE):
            chunk = data[off:off + scis.PACKET_SIZE]
            records += 1
            parsed = scis.unpack(chunk)
            if parsed is None:
                bad += 1
                if sample is None:
                    sample = chunk
            elif parsed[0] in OUR_IDS:
                valid += 1
                seqs.add(parsed[1])
            else:
                foreign += 1

    return {
        "achieved": achieved, "datagrams": datagrams, "records": records,
        "valid": valid, "bad": bad, "foreign": foreign, "unique": len(seqs),
        "sizes": dict(sorted(sizes.items())), "sample": sample,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=9217)
    ap.add_argument("--count", type=int, default=200,
                    help="metrics per rate (default 200)")
    ap.add_argument("--rates", type=int, nargs="+", default=LADDER)
    args = ap.parse_args()

    host_ip = scis.resolve_host_ip()
    print(f"host {host_ip}  ->  FPGA RX {scis.FPGA_RX_IP}:{args.port}")
    print(f"{args.count} metrics per rate\n")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 << 20)
    sock.bind((host_ip, args.port))
    sock.setblocking(False)

    print(f"{'rate':>7} {'achieved':>9} {'dgrams':>7} {'records':>8} "
          f"{'valid':>6} {'bad':>5} {'uniq':>6}  sizes")
    print("-" * 86)

    total_bad = 0
    try:
        for rate in args.rates:
            r = run_rate(sock, args.port, rate, args.count)
            total_bad += r["bad"]
            print(f"{rate:>7} {r['achieved']:>9.0f} {r['datagrams']:>7} "
                  f"{r['records']:>8} {r['valid']:>6} {r['bad']:>5} "
                  f"{r['unique']:>6}  {r['sizes']}")
            if r["sample"]:
                print(f"         first bad record: {r['sample'].hex(' ')}")
            if r["foreign"]:
                print(f"         ({r['foreign']} foreign records ignored -- "
                      f"CAN bus is not quiet)")
            time.sleep(0.4)
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 1
    finally:
        sock.close()

    print()
    if total_bad == 0:
        print("BATCH INTEGRITY PASSED -- no displaced records at any rate")
        return 0
    print(f"BATCH INTEGRITY FAILED -- {total_bad} displaced record(s)")
    print("  A record that fails the 'V01' check inside an otherwise complete")
    print("  datagram means a spurious byte entered the burst; see the sample")
    print("  above, which should be the previous record shifted by one byte.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
