"""Shared core for the SCIS FPGA throughput tests.

The measurement idea in one line: the FPGA echoes every metric packet it
accepts, so throughput is `packets that came back / packets we sent`, counted
entirely on this host. Nothing has to be instrumented on the board.

    host .106 --UDP 9 bytes--> W5500 RX .100:9217+k --SPI--> data concentrator
                                                                    |
                            W5500 TX .101 --UDP--> host .106:9217+TUSER

For UDP the return port is the port it arrived on (`udp_packet_adapter.vhd:160`,
`s_axis.tuser <= ruser`); for CAN it is always 9218 (`top.vhd:478`,
`CAN_TUSER => "001"`). So a receiver that binds all eight ports catches both.

Two things stop a naive sent-vs-received counter from lying:

  * Every packet carries a **sequence number** in its 4-byte value field, which
    round-trips untouched. That turns "150 missing" into "*which* 150", which is
    what separates a buffer overflow (one contiguous burst of seqs) from a rate
    limit (gaps spread evenly). Duplicates and reordering are counted, and are
    not loss.
  * **Host-side drops are excluded, not blamed on the FPGA.** If our own socket
    buffer overflows the packets are gone before Python sees them, which looks
    exactly like FPGA loss. `udp_drops()` reads /proc/net/udp around each step so
    those runs can be thrown away instead of recorded.
"""

import csv
import os
import socket
import struct
import sys
import threading
import time

# ---------------------------------------------------------------------------
#  Wire format and network layout -- all fixed by the HDL, do not "improve"
# ---------------------------------------------------------------------------

# hardware/hdl/top.vhd:353 -- the RX W5500 ("receive_first")
FPGA_RX_IP = "192.168.2.100"
# software_infrastructure/testing_scripts/net_utils.py
HOST_IP_FALLBACK = "192.168.2.106"

BASE_PORT = 9217
NUM_PORTS = 8                      # 9217..9224, socket_amount => 8 in top.vhd
ALL_PORTS = list(range(BASE_PORT, BASE_PORT + NUM_PORTS))

# CAN metrics always come back on TUSER=001 -> 9218 (top.vhd:478).
CAN_RETURN_PORT = BASE_PORT + 1

# udp_packet_adapter.vhd:133-142 accepts a payload only if it is exactly 9 bytes
# and starts with 'V'. Anything else is read out of the W5500 (paying the full
# SPI cost) and then discarded.
PACKET_SIZE = 9
PROTOCOL = b"V01"
_FMT = "!3sHi"                     # 3s protocol, H identifier, i Q22.10 value

# telemetry_sender.vhd:31-36 injects these as bare ASCII, not V01 packets.
ALERT_INTERLOCK = b"INTERLOCK"
ALERT_ALMOSTFULL = b"ALMOSTFULL"

# Q22.10: the metric server recovers the float as raw/1024.
Q_SCALE = 1 << 10

# Devices whose critical band is wide enough to carry a sequence number without
# tripping the interlock. All eight are 0..90 degC, Ethernet UDP, Medium
# priority (slow_control_catalog.json), giving ~90k usable seq values each.
# One per socket so each port's traffic is independently identifiable.
WIDE_BAND_IDS = [0xE140, 0xE141, 0xE142, 0xE143, 0xE1C0, 0xE1C1, 0xE1C2, 0xE1C3]

# The identifiers this harness transmits. Everything else on these ports is
# foreign traffic and must not be counted as a returned probe.
OUR_IDS = frozenset(WIDE_BAND_IDS)

# Stay strictly inside 0..90 degC so no reading can trip the interlock (which
# latches until SW3 is pressed and would poison every later step).
SEQ_MIN = 1 * Q_SCALE              #  1.0 degC
SEQ_MAX = 89 * Q_SCALE             # 89.0 degC
SEQ_SPAN = SEQ_MAX - SEQ_MIN       # 90112 distinct sequence values

# The CAN path has its own device and therefore its own safe band. CAN ID 2 is
# Room_Humidity (20..80 %RH); the FPGA maps metric_id <= rx_id & "000000", so it
# arrives as identifier 0x0080. These MUST match SEQ_MIN / SEQ_SPAN in
# firmware/src/main.cpp.
CAN_ID = 2
CAN_IDENTIFIER = CAN_ID << 6       # 0x0080
CAN_SEQ_MIN = 21 * Q_SCALE         # 21.0 %RH
CAN_SEQ_MAX = 79 * Q_SCALE         # 79.0 %RH
CAN_SEQ_SPAN = CAN_SEQ_MAX - CAN_SEQ_MIN   # 59392 distinct sequence values


def pack(identifier, seq):
    """Build a 9-byte V01 packet carrying `seq` in the value field."""
    return struct.pack(_FMT, PROTOCOL, identifier, SEQ_MIN + (seq % SEQ_SPAN))


def unpack(payload):
    """Return (identifier, seq) for a V01 packet, or None if it is not one.

    The sequence number is decoded against whichever band that identifier uses,
    so Ethernet and CAN packets can share one receiver in the combined test.
    """
    if len(payload) != PACKET_SIZE:
        return None
    proto, identifier, raw = struct.unpack(_FMT, payload)
    if proto != PROTOCOL:
        return None
    if identifier == CAN_IDENTIFIER:
        return identifier, (raw - CAN_SEQ_MIN) % CAN_SEQ_SPAN
    return identifier, (raw - SEQ_MIN) % SEQ_SPAN


def unpack_all(payload):
    """Return (records, junk) for a datagram carrying one or more metrics.

    The FPGA concatenates up to `g_BATCH_PACKETS` metrics into a single
    datagram (`metric_packet_manager.vhd`), so a datagram is N*9 bytes. N=1 is
    exactly the old one-metric-per-datagram format, which is why this also
    reads an un-batched bitstream without a flag.

    Alerts are never batched -- `telemetry_sender` can only inject one from
    IDLE, and it does not reach IDLE until the batch's tlast -- so callers can
    keep checking for them on the whole datagram before calling this.
    """
    if not payload or len(payload) % PACKET_SIZE != 0:
        return [], 1
    records, junk = [], 0
    for off in range(0, len(payload), PACKET_SIZE):
        parsed = unpack(payload[off:off + PACKET_SIZE])
        if parsed is None:
            junk += 1
        else:
            records.append(parsed)
    return records, junk


def resolve_host_ip(target_ip=FPGA_RX_IP):
    """Local IP to bind to, reusing the SCIS repo's helper when it is present.

    That helper explains *how* to fix a missing 192.168.2.106 instead of dying
    with a bare Errno 99, which is worth the import dance.
    """
    helper_dir = os.path.expanduser(
        "~/FPGA-centric-SCIS/software_infrastructure/testing_scripts"
    )
    if os.path.isdir(helper_dir):
        sys.path.insert(0, helper_dir)
        try:
            from net_utils import resolve_host_ip as _resolve
            return _resolve(target_ip)
        except ImportError:
            pass
    override = os.environ.get("SCIS_HOST_IP")
    if override:
        return override
    return HOST_IP_FALLBACK


# ---------------------------------------------------------------------------
#  Host-side loss guard
# ---------------------------------------------------------------------------

def udp_drops(ports):
    """Total kernel UDP receive drops across `ports`, from /proc/net/udp.

    Compared before and after every step. A step where this moved measured the
    harness, not the FPGA, and must be discarded.
    """
    wanted = {int(p) for p in ports}
    total = 0
    try:
        with open("/proc/net/udp") as fh:
            next(fh)                                # header
            for line in fh:
                f = line.split()
                # local_address is "IP:PORT" in hex; drops is the last column.
                if int(f[1].split(":")[1], 16) in wanted:
                    total += int(f[-1])
    except (OSError, ValueError, IndexError, StopIteration):
        return -1                                   # unavailable, not zero
    return total


# ---------------------------------------------------------------------------
#  Pacing
# ---------------------------------------------------------------------------

class Pacer:
    """Deadline pacer for a target packets-per-second rate.

    time.sleep() cannot pace above roughly 1 kpps -- its granularity is ~1 ms and
    it always overshoots -- so this busy-waits on perf_counter() once the gap is
    small. That burns a core, which is the price of an honest send rate.
    """

    def __init__(self, rate):
        self.interval = 1.0 / rate
        self.next_t = None

    def start(self):
        self.next_t = time.perf_counter()

    def wait(self):
        self.next_t += self.interval
        while True:
            remaining = self.next_t - time.perf_counter()
            if remaining <= 0:
                return
            if remaining > 0.002:
                # Sleep away the bulk, busy-wait only the last ~2 ms.
                time.sleep(remaining - 0.002)
            else:
                # sleep(0) yields the GIL. Without it this loop spins in pure
                # Python and starves the Receiver threads in this same process,
                # so packets pile up in the socket buffer and get dropped -- host
                # loss that would look exactly like the FPGA losing them.
                time.sleep(0)


# ---------------------------------------------------------------------------
#  Receiver
# ---------------------------------------------------------------------------

class Receiver:
    """Binds the eight return ports and records everything that comes back.

    One blocking-recv thread per port rather than a non-blocking poll loop: a
    busy poll across eight sockets drops packets at exactly the rates we care
    about, and would show up as FPGA loss.

    The sockets are also what we *send* from, so the source port matches the
    destination port -- the W5500 sockets are configured as a fixed port pair.
    """

    RCVBUF = 8 * 1024 * 1024

    def __init__(self, host_ip, ports=ALL_PORTS):
        self.host_ip = host_ip
        self.ports = list(ports)
        self.socks = {}
        self._threads = []
        self._running = False
        self.lock = threading.Lock()
        self.reset()

        for port in self.ports:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, self.RCVBUF)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024 * 1024)
            s.bind((host_ip, port))
            s.settimeout(0.2)
            self.socks[port] = s

    def reset(self):
        """Clear counters between ramp steps.

        CAN-sourced packets are tracked separately from UDP-sourced ones: they
        have an independent sequence space, and in the combined test both are
        arriving at once.
        """
        with self.lock:
            self.seqs = {p: set() for p in self.ports}    # unique seqs per port
            self.count = {p: 0 for p in self.ports}       # incl. duplicates
            self.dupes = {p: 0 for p in self.ports}
            self.reorders = {p: 0 for p in self.ports}
            self._last_seq = {p: None for p in self.ports}
            self.can_seqs = set()
            self.can_count = 0
            self.can_dupes = 0
            self.interlock = 0
            self.almostfull = 0
            self.junk = 0

    def start(self):
        self._running = True
        for port in self.ports:
            t = threading.Thread(target=self._loop, args=(port,), daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self):
        self._running = False
        for t in self._threads:
            t.join(timeout=1.0)
        self._threads = []

    def close(self):
        for s in self.socks.values():
            s.close()

    def _loop(self, port):
        sock = self.socks[port]
        while self._running:
            try:
                data, _ = sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                return

            with self.lock:
                if data == ALERT_INTERLOCK:
                    self.interlock += 1
                    continue
                if data == ALERT_ALMOSTFULL:
                    self.almostfull += 1
                    continue

                records, junk = unpack_all(data)
                self.junk += junk

                for identifier, seq in records:
                    # Anything we did not send is somebody else's traffic.
                    # can_metric_source is a passive tap, so every ID on the
                    # bus arrives here -- 0x40 as well as the documented
                    # 0x80. Counting those as UDP metrics inflates `unique`
                    # and fails T0 against a perfectly healthy board.
                    if identifier not in OUR_IDS:
                        self.can_count += 1
                        if seq in self.can_seqs:
                            self.can_dupes += 1
                        else:
                            self.can_seqs.add(seq)
                        continue

                    self.count[port] += 1
                    if seq in self.seqs[port]:
                        self.dupes[port] += 1
                    else:
                        self.seqs[port].add(seq)

                    prev = self._last_seq[port]
                    if prev is not None and seq < prev:
                        self.reorders[port] += 1
                    self._last_seq[port] = seq

    # -- results ----------------------------------------------------------
    def unique_total(self):
        with self.lock:
            return sum(len(s) for s in self.seqs.values())

    def snapshot(self):
        with self.lock:
            return {
                "unique": sum(len(s) for s in self.seqs.values()),
                "received": sum(self.count.values()),
                "dupes": sum(self.dupes.values()),
                "reorders": sum(self.reorders.values()),
                "can_unique": len(self.can_seqs),
                "can_received": self.can_count,
                "can_dupes": self.can_dupes,
                "can_seqs": set(self.can_seqs),
                "interlock": self.interlock,
                "almostfull": self.almostfull,
                "junk": self.junk,
                "per_port": {p: len(self.seqs[p]) for p in self.ports},
            }


def gap_pattern(received_seqs, sent_count):
    """Classify how the missing sequence numbers are distributed.

    A buffer that overflowed loses a contiguous run; a path that is simply too
    slow loses packets spread evenly. Telling those apart is the whole point of
    carrying sequence numbers, so it gets reported per step.
    """
    missing = [s for s in range(sent_count) if s not in received_seqs]
    if not missing:
        return "none", 0
    runs = 1
    for a, b in zip(missing, missing[1:]):
        if b != a + 1:
            runs += 1
    longest = cur = 1
    for a, b in zip(missing, missing[1:]):
        cur = cur + 1 if b == a + 1 else 1
        longest = max(longest, cur)
    # Few long runs => bursty (overflow). Many single gaps => steady rate limit.
    kind = "bursty" if longest >= 8 or runs <= max(2, len(missing) // 20) else "spread"
    return kind, longest


# ---------------------------------------------------------------------------
#  Ramp driver
# ---------------------------------------------------------------------------

CSV_FIELDS = [
    "test", "requested_pps", "achieved_pps", "sent", "received", "unique",
    "loss_pct", "dupes", "reorders", "almostfull", "interlock",
    "host_drops", "gap_kind", "longest_gap", "valid",
]


def results_path(name):
    here = os.path.dirname(os.path.abspath(__file__))
    out = os.path.join(here, "results")
    os.makedirs(out, exist_ok=True)
    return os.path.join(out, name)


class RampRecorder:
    """Writes one CSV row per ramp step and prints a readable table.

    `add()` is the Ethernet path (it derives everything from a Receiver
    snapshot); `write_row()` takes an already-built row for callers whose
    numbers come from somewhere else, such as the CAN test reading counters off
    the Nucleo.
    """

    def __init__(self, filename, test_name, print_header=True):
        self.path = results_path(filename)
        self.test_name = test_name
        self.rows = []
        self._fh = open(self.path, "w", newline="")
        self._w = csv.DictWriter(self._fh, fieldnames=CSV_FIELDS)
        self._w.writeheader()
        if print_header:
            print(f"{'rate':>8} {'achieved':>9} {'sent':>8} {'uniq':>8} "
                  f"{'loss%':>7} {'dup':>5} {'AF':>4} {'ILK':>4} {'drops':>6}  gaps")
            print("-" * 78)

    def write_row(self, **fields):
        """Write one row, defaulting any field the caller did not supply."""
        row = {k: fields.get(k, 0) for k in CSV_FIELDS}
        row["test"] = fields.get("test", self.test_name)
        row["gap_kind"] = fields.get("gap_kind", "none")
        self.rows.append(row)
        self._w.writerow(row)
        self._fh.flush()
        return row

    def add(self, requested, achieved, sent, snap, host_drops, gaps):
        unique = snap["unique"]
        loss = 100.0 * (1.0 - unique / sent) if sent else 0.0
        valid = host_drops == 0
        row = {
            "test": self.test_name,
            "requested_pps": requested,
            "achieved_pps": round(achieved, 1),
            "sent": sent,
            "received": snap["received"],
            "unique": unique,
            "loss_pct": round(loss, 3),
            "dupes": snap["dupes"],
            "reorders": snap["reorders"],
            "almostfull": snap["almostfull"],
            "interlock": snap["interlock"],
            "host_drops": host_drops,
            "gap_kind": gaps[0],
            "longest_gap": gaps[1],
            "valid": int(valid),
        }
        self.rows.append(row)
        self._w.writerow(row)
        self._fh.flush()

        flag = "" if valid else "  <-- HOST DROPPED, DISCARD"
        print(f"{requested:>8} {achieved:>9.1f} {sent:>8} {unique:>8} "
              f"{loss:>7.3f} {snap['dupes']:>5} {snap['almostfull']:>4} "
              f"{snap['interlock']:>4} {host_drops:>6}  {gaps[0]}{flag}")
        return loss, valid

    def close(self):
        self._fh.close()
        print(f"\nCSV: {self.path}")


def run_step(send_fn, receiver, rate, duration, drain=0.5, ports=ALL_PORTS):
    """One ramp step: send at `rate` for `duration`, drain, return the numbers.

    `send_fn(seq)` must transmit one packet carrying `seq`. Sequence numbers
    restart at 0 each step so the gap analysis is per-step.
    """
    receiver.reset()
    drops_before = udp_drops(ports)

    pacer = Pacer(rate)
    pacer.start()
    t0 = time.perf_counter()
    sent = 0
    deadline = t0 + duration
    while time.perf_counter() < deadline:
        send_fn(sent)
        sent += 1
        pacer.wait()
    elapsed = time.perf_counter() - t0

    time.sleep(drain)
    drops_after = udp_drops(ports)
    snap = receiver.snapshot()

    with receiver.lock:
        seen = set()
        for s in receiver.seqs.values():
            seen |= s
    gaps = gap_pattern(seen, sent)

    achieved = sent / elapsed if elapsed else 0.0
    host_drops = (drops_after - drops_before) if drops_before >= 0 else -1
    return achieved, sent, snap, host_drops, gaps


class BoardWedged(Exception):
    """The board stopped answering and did not come back on its own.

    Raised as soon as a liveness probe fails, so the ramp stops instead of
    "measuring" a dead board -- which otherwise yields a run of clean-looking
    100%-loss rows and a completely fictitious knee.
    """

    def __init__(self, rate):
        super().__init__(f"board stopped responding after {rate} pps")
        self.rate = rate


def probe_alive(send_fn, receiver, ports, n=20, rate=10.0):
    """Send `n` packets at a gentle rate and check most of them come back.

    Uses a rate far below any plausible knee, so a failure means the board is
    wedged rather than merely saturated.
    """
    receiver.reset()
    pacer = Pacer(rate)
    pacer.start()
    for seq in range(n):
        send_fn(seq)
        pacer.wait()
    time.sleep(1.0)
    return receiver.snapshot()["unique"] >= n * 0.9


def find_knee(step_fn, ladder, loss_limit=0.1, alive_fn=None):
    """Climb the ladder until packets are lost, then bisect for the knee.

    `alive_fn()` (optional) is called after any badly-failing step; if it
    returns False the board has wedged and BoardWedged is raised.

    Loss is assumed to rise with rate. When it does not -- a step passing above
    one that failed -- that is reported rather than quietly believed, because it
    means something other than a rate limit is going on and the "knee" would be
    meaningless.

    Returns (rate, found). `found` is False when the ladder ran out without ever
    losing packets: the answer is then ">= rate", NOT a knee. Reporting that as a
    knee would turn "we never pushed hard enough" into a fake measurement.
    """
    last_good = None
    first_bad = None
    consecutive_bad = 0
    non_monotonic = []

    def check(rate, loss):
        if alive_fn is not None and loss > 50.0 and not alive_fn():
            raise BoardWedged(rate)

    for rate in ladder:
        loss, valid = step_fn(rate)
        if not valid:
            continue
        if loss <= loss_limit:
            if first_bad is None:
                last_good = rate
            else:
                # Passed at a rate above one that already failed.
                non_monotonic.append((first_bad, rate))
            consecutive_bad = 0
        else:
            check(rate, loss)
            if first_bad is None:
                first_bad = rate
            consecutive_bad += 1
            if consecutive_bad >= 2:
                break

    for bad, good in non_monotonic:
        print(f"  ! {good} pps passed but {bad} pps did not -- loss is not rising"
              f" with rate, so this is not a clean rate limit.")

    if first_bad is None:
        # Never lost a packet. The ladder was too short, not the board too slow.
        return last_good, False
    if last_good is None or last_good >= first_bad:
        return last_good, True

    print(f"\nBisecting between {last_good} and {first_bad} pps...")
    lo, hi = last_good, first_bad
    while hi - lo > max(50, lo // 20):          # stop at 5% resolution
        mid = (lo + hi) // 2
        loss, valid = step_fn(mid)
        if not valid:
            break
        check(mid, loss)
        if loss <= loss_limit:
            lo = mid
        else:
            hi = mid
    return lo, True
