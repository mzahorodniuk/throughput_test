#!/usr/bin/env python3
"""Dump every datagram the board sends for a small controlled burst.

test_batch_integrity says how many records are displaced; this says what the
bytes actually look like. A run of one repeated byte means something re-sent a
byte it was already holding; a block of zeros means the transmit buffer was
never written there but Sn_TX_WR was advanced past it anyway; a clean record
shifted by one means a byte went missing upstream.

Usage:
    python3 test_batch_dump.py --n 32 --gap 0
"""

import argparse
import socket
import struct
import sys
import time

import scis


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9217)
    ap.add_argument("--n", type=int, default=32)
    ap.add_argument("--gap", type=float, default=0.0,
                    help="seconds between packets (0 = as fast as the host can)")
    ap.add_argument("--drain", type=float, default=1.0)
    ap.add_argument("--max-dump", type=int, default=12)
    args = ap.parse_args()

    host_ip = scis.resolve_host_ip()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((host_ip, args.port))
    sock.settimeout(0.2)

    dest = (scis.FPGA_RX_IP, args.port)
    ident = scis.WIDE_BAND_IDS[0]

    # drain anything left over
    try:
        while True:
            sock.recvfrom(2048)
    except socket.timeout:
        pass

    sent = []
    t0 = time.perf_counter()
    for i in range(args.n):
        pkt = struct.pack("!3sHi", b"V01", ident, scis.SEQ_MIN + i)
        sock.sendto(pkt, dest)
        sent.append(pkt)
        if args.gap:
            time.sleep(args.gap)
    t_sent = time.perf_counter()

    grams = []
    deadline = time.perf_counter() + args.drain
    while time.perf_counter() < deadline:
        try:
            data, _ = sock.recvfrom(4096)
        except socket.timeout:
            continue
        grams.append(data)
    sock.close()

    print(f"sent {args.n} packets in {(t_sent - t0) * 1e6:.0f} us "
          f"({args.n / (t_sent - t0):.0f} pkt/s offered)")
    print(f"got  {len(grams)} datagrams, {sum(len(g) for g in grams)} bytes "
          f"(expected {args.n * 9})\n")

    for k, g in enumerate(grams[:args.max_dump]):
        tag = "ok " if len(g) % 9 == 0 else "BAD"
        print(f"[{k:>3}] {tag} len={len(g)}")
        for off in range(0, len(g), 9):
            rec = g[off:off + 9]
            mark = "  " if rec[:3] == b"V01" else "<<"
            print(f"        {off:>4}: {rec.hex(' ')} {mark}")
        print()

    if len(grams) > args.max_dump:
        print(f"... {len(grams) - args.max_dump} more datagrams, "
              f"sizes {[len(g) for g in grams[args.max_dump:]]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
