"""High-rate load core for the batching campaign.

Four properties of `scis.py` stop it measuring the current (batching) bitstream.
All four are harness limits, not board limits.

1. **One sequence space.** `scis.pack` hides the sequence number in one device's
   safe band, so only `SEQ_SPAN` = 90 112 probes are distinguishable and
   `run_step` must stop there -- a 0.6 s window at 150 kpps. Here the probe key
   is `(port, identifier, seq)` and all eight wide-band IDs are used on every
   port: 8 x 8 x 90 112 = 5.8 M probes. Every ID is a 0..90 degC device and
   every value stays inside 1..89 degC, so a test packet still cannot trip the
   interlock.

2. **Sender and receiver shared a GIL.** `scis.Receiver` runs eight blocking-recv
   threads in the process that also paces the send loop. Measured on this host:
   a bare `sendto` loop reaches 381 k pps, but `test_eth_ceiling.py` asked for
   1.4 M pps and achieved 90 k at *zero* loss -- the ceiling in that CSV is the
   harness. Senders and receivers are separate processes here.

3. **`SO_RCVBUF` was a no-op.** `scis.Receiver.RCVBUF` asks for 8 MiB, but
   `net.core.rmem_max` on this host is 212 992, so the kernel grants ~0.4 MiB --
   a few hundred small datagrams. Buffering cannot be relied on, so the receive
   loop is made as short as possible instead: it only copies bytes during the
   step and parses afterwards.

4. **Four cores.** Sender and receiver process counts are bounded accordingly.

Sockets are created *before* the fork, so children send from and receive on
exactly the bound sockets the W5500 sockets are paired with -- no two processes
bind the same port.

Wire ceiling, which bounds every Ethernet number here: a 9-byte UDP payload is a
64-byte Ethernet frame plus preamble and inter-frame gap = 84 B = 672 bit, so a
100 Mbit/s link carries at most 148 810 packet/s. `enp2s0` is 1 Gbit while both
W5500s are 100 Mbit, so above that rate the *switch* discards and the loss is
not the FPGA's. `over_wire_limit` flags any such step.
"""

import os
import pickle
import socket
import struct
import sys
import tempfile
import time
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import scis  # noqa: E402

WIRE_LIMIT_PPS = int(100e6 / (84 * 8))           # 148 810
PROBE_IDS = list(scis.WIDE_BAND_IDS)             # all eight, on every port
SEQ_SPAN = scis.SEQ_SPAN                         # 90 112 per (port, id) slot
NCPU = os.cpu_count() or 4

_FMT = scis._FMT
_PROTO = scis.PROTOCOL
_SEQ_MIN = scis.SEQ_MIN
_PKT = scis.PACKET_SIZE


def slots_for(ports, ids=None):
    ids = PROBE_IDS if ids is None else ids
    return [(p, i) for p in ports for i in ids]


def _spool():
    fd, path = tempfile.mkstemp(prefix="hiload-", suffix=".pkl")
    os.close(fd)
    return path


# ---------------------------------------------------------------------------
#  Bound sockets, shared by every child
# ---------------------------------------------------------------------------

class Rig:
    """The eight bound UDP sockets, plus fork helpers.

    Nothing here receives or sends in the parent: the parent only forks, waits
    and merges. That keeps the measuring process off the critical path.
    """

    def __init__(self, host_ip, ports=None, rcvbuf=32 * 1024 * 1024):
        self.host_ip = host_ip
        self.ports = list(scis.ALL_PORTS if ports is None else ports)
        self.socks = {}
        for port in self.ports:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 * 1024 * 1024)
            s.bind((host_ip, port))
            self.socks[port] = s
        self.granted_rcvbuf = self.socks[self.ports[0]].getsockopt(
            socket.SOL_SOCKET, socket.SO_RCVBUF)

    def close(self):
        for s in self.socks.values():
            s.close()


# ---------------------------------------------------------------------------
#  Receiver child: copy during the step, parse after it
# ---------------------------------------------------------------------------

def _child_recv(rig, my_ports, run_for, out_path):
    poll = __import__("select").poll()
    fd2port = {}
    for p in my_ports:
        s = rig.socks[p]
        s.setblocking(False)
        poll.register(s.fileno(), __import__("select").POLLIN)
        fd2port[s.fileno()] = p
    recvs = {p: rig.socks[p].recv for p in my_ports}

    captured = []                      # (port, bytes) -- parsed after the step
    append = captured.append
    deadline = time.perf_counter() + run_for
    while True:
        remain = deadline - time.perf_counter()
        if remain <= 0:
            break
        for fd, _ev in poll.poll(min(200.0, remain * 1000.0)):
            port = fd2port[fd]
            rc = recvs[port]
            while True:                # drain this socket before polling again
                try:
                    append((port, rc(4096)))
                except BlockingIOError:
                    break
                except OSError:
                    break

    # ---- post-processing, off the critical path -----------------------------
    ILK, AF = scis.ALERT_INTERLOCK, scis.ALERT_ALMOSTFULL
    ours = scis.OUR_IDS
    unpack = struct.unpack
    bitmaps = {}                       # (port, ident) -> bytearray bitmap
    count = dupes = datagrams = dg_records = junk = ragged = foreign = 0
    interlock = almostfull = 0
    can_seqs, can_count, can_dupes = set(), 0, 0
    dg_hist = {}

    for port, data in captured:
        if data == ILK:
            interlock += 1
            continue
        if data == AF:
            almostfull += 1
            continue
        n, rem = divmod(len(data), _PKT)
        if rem or n == 0:
            ragged += 1
            junk += 1
            continue
        datagrams += 1
        dg_records += n
        dg_hist[n] = dg_hist.get(n, 0) + 1
        for off in range(0, n * _PKT, _PKT):
            proto, ident, raw = unpack(_FMT, data[off:off + _PKT])
            if proto != _PROTO:
                junk += 1
                continue
            if ident == scis.CAN_IDENTIFIER:
                # CAN is matched on its exact identifier, never on "not ours".
                # At overload the transmit path emits mis-framed datagrams whose
                # 9-byte slices decode to arbitrary identifiers; filing those as
                # CAN inflated can_unique past can_sent and produced *negative*
                # CAN loss (-14.8 % on a batch-1 arm) -- the giveaway that the
                # attribution was wrong rather than the board generous.
                can_count += 1
                seq = (raw - scis.CAN_SEQ_MIN) % scis.CAN_SEQ_SPAN
                if seq in can_seqs:
                    can_dupes += 1
                else:
                    can_seqs.add(seq)
                continue
            if ident not in ours:
                # Neither ours nor CAN: somebody else's traffic, or a record
                # recovered from a mis-framed datagram. Counted, never credited.
                foreign += 1
                continue
            seq = (raw - _SEQ_MIN) % SEQ_SPAN
            key = (port, ident)
            bm = bitmaps.get(key)
            if bm is None:
                bm = bitmaps[key] = bytearray(SEQ_SPAN // 8 + 1)
            byte, bit = divmod(seq, 8)
            mask = 1 << bit
            if bm[byte] & mask:
                dupes += 1
            else:
                bm[byte] |= mask
                count += 1

    payload = {
        "bitmaps": {k: zlib.compress(bytes(v), 1) for k, v in bitmaps.items()},
        "count": count, "dupes": dupes, "datagrams": datagrams,
        "dg_records": dg_records, "dg_hist": dg_hist, "junk": junk,
        "ragged": ragged, "interlock": interlock, "almostfull": almostfull,
        "can_seqs": can_seqs, "can_count": can_count, "can_dupes": can_dupes,
        "foreign": foreign,
    }
    with open(out_path, "wb") as fh:
        pickle.dump(payload, fh, protocol=pickle.HIGHEST_PROTOCOL)


# ---------------------------------------------------------------------------
#  Sender child
# ---------------------------------------------------------------------------

def _child_send(rig, my_slots, rate, duration, limit_per_slot, out_path):
    BURST = 8
    pack = struct.pack
    sendtos = [rig.socks[p].sendto for p, _ in my_slots]
    dests = [(scis.FPGA_RX_IP, p) for p, _ in my_slots]
    idents = [i for _, i in my_slots]
    nslots = len(my_slots)
    seqs = [0] * nslots
    total_limit = limit_per_slot * nslots

    t0 = time.perf_counter()
    deadline = t0 + duration
    sent = 0
    k = 0
    while sent < total_limit:
        now = time.perf_counter()
        if now >= deadline:
            break
        target = int((now - t0) * rate) + 1
        burst = 0
        while sent < target and sent < total_limit and burst < BURST:
            j = k % nslots
            s = seqs[j]
            try:
                sendtos[j](pack(_FMT, _PROTO, idents[j],
                                _SEQ_MIN + (s % SEQ_SPAN)), dests[j])
            except OSError:
                pass
            seqs[j] = s + 1
            sent += 1
            burst += 1
            k += 1
        if burst == 0:
            due = t0 + sent / rate
            dt = due - time.perf_counter()
            if dt > 0.0004:
                time.sleep(dt * 0.85)
    elapsed = time.perf_counter() - t0
    with open(out_path, "wb") as fh:
        pickle.dump({"sent": sent, "elapsed": elapsed,
                     "per_slot_sent": {my_slots[j]: seqs[j]
                                       for j in range(nslots)}}, fh)


# ---------------------------------------------------------------------------
#  One measured step
# ---------------------------------------------------------------------------

def run_step(rig, ports, rate, duration, nsend=2, nrecv=2, drain=0.8,
             ids=None, limit_per_slot=None):
    """Offer `rate` pps over `ports` for `duration` s; return one result dict.

    Receivers are started first and run for duration+drain so nothing that the
    board emits late is counted as lost. `host_drops` is the /proc/net/udp delta
    exactly as scis does it: a step where our own socket queue overflowed says
    nothing about the board and is marked valid=0.
    """
    slots = slots_for(ports, ids)
    if limit_per_slot is None:
        limit_per_slot = SEQ_SPAN
    nsend = max(1, min(nsend, len(slots)))
    nrecv = max(1, min(nrecv, len(rig.ports)))

    drops_before = scis.udp_drops(rig.ports)

    # -- receivers ----------------------------------------------------------
    rgroups = [rig.ports[i::nrecv] for i in range(nrecv)]
    rgroups = [g for g in rgroups if g]
    rkids = []
    for g in rgroups:
        path = _spool()
        pid = os.fork()
        if pid == 0:
            try:
                _child_recv(rig, g, duration + drain, path)
            finally:
                os._exit(0)
        rkids.append((pid, path))

    time.sleep(0.15)                   # let the receivers arm before load starts

    # -- senders ------------------------------------------------------------
    sgroups = [slots[i::nsend] for i in range(nsend)]
    sgroups = [g for g in sgroups if g]
    share = rate / len(sgroups)
    skids = []
    for g in sgroups:
        path = _spool()
        pid = os.fork()
        if pid == 0:
            try:
                _child_send(rig, g, share, duration, limit_per_slot, path)
            finally:
                os._exit(0)
        skids.append((pid, path))

    sent, elapsed, per_slot_sent = 0, 0.0, {}
    for pid, path in skids:
        os.waitpid(pid, 0)
        try:
            with open(path, "rb") as fh:
                d = pickle.load(fh)
            sent += d["sent"]
            elapsed = max(elapsed, d["elapsed"])
            per_slot_sent.update(d["per_slot_sent"])
        except Exception:
            pass
        finally:
            os.unlink(path)

    # -- merge receivers ----------------------------------------------------
    agg = {"count": 0, "dupes": 0, "datagrams": 0, "dg_records": 0, "junk": 0,
           "ragged": 0, "interlock": 0, "almostfull": 0, "can_count": 0,
           "can_dupes": 0, "foreign": 0}
    dg_hist, can_seqs, bitmaps = {}, set(), {}
    for pid, path in rkids:
        os.waitpid(pid, 0)
        try:
            with open(path, "rb") as fh:
                d = pickle.load(fh)
            for k in agg:
                agg[k] += d[k]
            for k, v in d["dg_hist"].items():
                dg_hist[k] = dg_hist.get(k, 0) + v
            can_seqs |= d["can_seqs"]
            bitmaps.update(d["bitmaps"])
        except Exception:
            pass
        finally:
            os.unlink(path)

    drops_after = scis.udp_drops(rig.ports)
    host_drops = (drops_after - drops_before
                  if drops_before >= 0 and drops_after >= 0 else -1)

    unique, gaps = _gap_stats(bitmaps, per_slot_sent)
    elapsed = elapsed or duration
    achieved = sent / elapsed if elapsed else 0.0
    batch = agg["dg_records"] / agg["datagrams"] if agg["datagrams"] else 0.0

    return {
        "requested_pps": rate,
        "achieved_pps": round(achieved, 1),
        "sent": sent,
        "elapsed": round(elapsed, 4),
        "received": agg["count"] + agg["dupes"],
        "unique": unique,
        "loss_pct": round(100.0 * (1.0 - unique / sent), 4) if sent else 0.0,
        "delivered_pps": round(unique / elapsed, 1) if elapsed else 0.0,
        "dupes": agg["dupes"],
        "datagrams": agg["datagrams"],
        "batch_mean": round(batch, 3),
        "batch_max": max(dg_hist) if dg_hist else 0,
        "ragged": agg["ragged"],
        "junk": agg["junk"],
        "almostfull": agg["almostfull"],
        "interlock": agg["interlock"],
        "can_unique": len(can_seqs),
        "can_received": agg["can_count"],
        "foreign": agg["foreign"],
        "host_drops": host_drops,
        "gap_kind": gaps["kind"],
        "longest_gap": gaps["longest"],
        "over_wire_limit": 1 if rate > WIRE_LIMIT_PPS else 0,
        "valid": 1 if host_drops == 0 else 0,
        "dg_hist": dg_hist,
        "can_seqs": can_seqs,
    }


def _gap_stats(bitmaps, per_slot_sent):
    """Unique probes returned, and the shape of what is missing.

    A buffer that overflowed loses one long contiguous run; a path that is
    simply too slow loses packets spread evenly. Classified per slot and then
    summarised, because with eight sockets a single global sequence would
    interleave eight independent streams and every gap would look 'spread'.
    """
    unique = 0
    longest_all = 0
    runs_all = missing_all = 0
    for key, nsent in per_slot_sent.items():
        blob = bitmaps.get(key)
        if nsent <= 0:
            continue
        if blob is None:
            missing_all += nsent
            runs_all += 1
            longest_all = max(longest_all, nsent)
            continue
        bm = zlib.decompress(blob)
        seen = 0
        cur = longest = runs = miss = 0
        prev_missing = False
        for seq in range(nsent):
            byte, bit = divmod(seq, 8)
            if bm[byte] >> bit & 1:
                seen += 1
                cur = 0
                prev_missing = False
            else:
                miss += 1
                cur += 1
                if cur > longest:
                    longest = cur
                if not prev_missing:
                    runs += 1
                prev_missing = True
        unique += seen
        longest_all = max(longest_all, longest)
        runs_all += runs
        missing_all += miss
    if missing_all == 0:
        return unique, {"kind": "none", "longest": 0}
    kind = ("bursty" if longest_all >= 8 or runs_all <= max(2, missing_all // 20)
            else "spread")
    return unique, {"kind": kind, "longest": longest_all}


def probe_alive(rig, ports, n=40, rate=20.0):
    r = run_step(rig, ports, rate, n / rate, nsend=1, nrecv=1, drain=1.2)
    return r["unique"] >= 0.9 * max(1, r["sent"]), r


class Observation:
    """A receive-only capture window that the caller can act inside.

    Needed because the CAN source runs independently: if it keeps sending while
    the receivers are already winding down, `sent` and `unique` cover different
    windows and the difference looks like FPGA loss. With this, the caller stops
    the Nucleo *inside* the window and the drain still catches the tail, so
    every frame counted as sent was sent while something was listening.
    """

    def __init__(self, rig, run_for, nrecv=2):
        self.rig = rig
        self.drops_before = scis.udp_drops(rig.ports)
        nrecv = max(1, min(nrecv, len(rig.ports)))
        groups = [g for g in (rig.ports[i::nrecv] for i in range(nrecv)) if g]
        self.kids = []
        for g in groups:
            path = _spool()
            pid = os.fork()
            if pid == 0:
                try:
                    _child_recv(rig, g, run_for, path)
                finally:
                    os._exit(0)
            self.kids.append((pid, path))
        time.sleep(0.15)               # let the receivers arm

    def finish(self):
        agg = {"count": 0, "dupes": 0, "datagrams": 0, "dg_records": 0,
               "junk": 0, "ragged": 0, "interlock": 0, "almostfull": 0,
               "can_count": 0, "can_dupes": 0, "foreign": 0}
        dg_hist, can_seqs = {}, set()
        for pid, path in self.kids:
            os.waitpid(pid, 0)
            try:
                with open(path, "rb") as fh:
                    d = pickle.load(fh)
                for k in agg:
                    agg[k] += d[k]
                for k, v in d["dg_hist"].items():
                    dg_hist[k] = dg_hist.get(k, 0) + v
                can_seqs |= d["can_seqs"]
            except Exception:
                pass
            finally:
                os.unlink(path)
        drops_after = scis.udp_drops(self.rig.ports)
        host_drops = (drops_after - self.drops_before
                      if self.drops_before >= 0 and drops_after >= 0 else -1)
        batch = agg["dg_records"] / agg["datagrams"] if agg["datagrams"] else 0.0
        return {
            "can_unique": len(can_seqs),
            "can_received": agg["can_count"],
            "can_dupes": agg["can_dupes"],
            "can_seqs": can_seqs,
            "foreign": agg["foreign"],
            "datagrams": agg["datagrams"],
            "batch_mean": round(batch, 3),
            "batch_max": max(dg_hist) if dg_hist else 0,
            "dg_hist": dg_hist,
            "interlock": agg["interlock"],
            "almostfull": agg["almostfull"],
            "ragged": agg["ragged"],
            "junk": agg["junk"],
            "host_drops": host_drops,
            "valid": 1 if host_drops == 0 else 0,
        }


def observe(rig, duration, drain=1.0, nrecv=2):
    """Blocking receive-only window (no UDP load offered)."""
    obs = Observation(rig, duration + drain, nrecv=nrecv)
    time.sleep(duration + drain)
    r = obs.finish()
    r["elapsed"] = duration
    return r


def can_gap(can_seqs, sent):
    """Gap shape for the CAN stream, whose sequence space is its own."""
    if sent <= 0:
        return "none", 0
    miss = [s for s in range(min(sent, scis.CAN_SEQ_SPAN)) if s not in can_seqs]
    if not miss:
        return "none", 0
    runs, longest, cur = 1, 1, 1
    for a, b in zip(miss, miss[1:]):
        if b == a + 1:
            cur += 1
            longest = max(longest, cur)
        else:
            runs += 1
            cur = 1
    kind = "bursty" if longest >= 8 or runs <= max(2, len(miss) // 20) else "spread"
    return kind, longest
