#!/usr/bin/env python3
"""Capture raw return datagrams under multi-socket load and classify them.

Answers one question: at what offered rate does the FPGA start emitting
datagrams that are not a whole number of 9-byte records, and are those
datagrams carrying fresh metrics or re-sent stale ones?

A datagram whose length is not a multiple of 9 is discarded whole by
scis.unpack_all, so its metrics are lost. Records that were already delivered
in an earlier datagram are stale TX-buffer content, not new data.

Run with nothing else touching the board.
"""
import collections
import socket
import sys
import threading
import time

import scis


def capture(rate, nports, dur=3.0, settle=1.5):
    host = scis.resolve_host_ip()
    ports = scis.ALL_PORTS[:nports]
    socks = {}
    for p in ports:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 << 20)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1 << 20)
        s.bind((host, p))
        s.settimeout(0.2)
        socks[p] = s

    cap, running = [], True

    def rxloop(p):
        while running:
            try:
                d, _ = socks[p].recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                return
            cap.append(d)

    ths = [threading.Thread(target=rxloop, args=(p,), daemon=True) for p in ports]
    for t in ths:
        t.start()
    time.sleep(0.3)

    ids = [scis.WIDE_BAND_IDS[i] for i in range(nports)]
    pacer = scis.Pacer(rate)
    pacer.start()
    t0 = time.perf_counter()
    sent = 0
    while time.perf_counter() < t0 + dur and sent < scis.SEQ_SPAN:
        i = sent % nports
        socks[ports[i]].sendto(scis.pack(ids[i], sent), (scis.FPGA_RX_IP, ports[i]))
        sent += 1
        pacer.wait()
    time.sleep(settle)
    running = False
    for t in ths:
        t.join(timeout=0.5)
    for s in socks.values():
        s.close()

    aligned = [d for d in cap if len(d) % 9 == 0 and d]
    ragged = [d for d in cap if len(d) % 9 != 0]

    def records(d):
        out = []
        for off in range(0, len(d) - 8, 9):
            r = scis.unpack(d[off:off + 9])
            if r:
                out.append(r)
        return out

    delivered = set()
    for d in aligned:
        delivered.update(records(d))

    stale = fresh = 0
    for d in ragged:
        for r in records(d):
            if r in delivered:
                stale += 1
            else:
                fresh += 1

    oversize = [d for d in cap if len(d) > 144]
    return {
        "sent": sent, "datagrams": len(cap),
        "aligned": len(aligned), "ragged": len(ragged),
        "oversize": len(oversize),
        "biggest": max((len(d) for d in cap), default=0),
        "delivered": len(delivered),
        "stale": stale, "fresh": fresh,
        "lost": sent - len(delivered),
        "sizes": dict(sorted(collections.Counter(len(d) for d in cap).items())[:6]),
    }


if __name__ == "__main__":
    print(f"{'ports':>5} {'rate':>7} {'sent':>6} {'dgrams':>7} {'ragged':>7} "
          f"{'>144B':>6} {'biggest':>8} {'uniq':>6} {'lost':>6} {'stale':>7} {'fresh':>6}")
    for nports, rate in [(8, 3000), (8, 8000), (8, 16000), (8, 20000), (8, 24000), (1, 20000)]:
        r = capture(rate, nports)
        print(f"{nports:>5} {rate:>7} {r['sent']:>6} {r['datagrams']:>7} {r['ragged']:>7} "
              f"{r['oversize']:>6} {r['biggest']:>8} {r['delivered']:>6} {r['lost']:>6} "
              f"{r['stale']:>7} {r['fresh']:>6}")
        time.sleep(1.0)
