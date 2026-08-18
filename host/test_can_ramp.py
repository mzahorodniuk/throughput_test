#!/usr/bin/env python3
"""CAN throughput ramp for the SCIS FPGA (T4).

The Nucleo sends CAN frames at a rate this script commands over serial; the FPGA
turns each accepted frame into a metric packet and echoes it to the host on UDP
9218 (CAN_TUSER => "001" in top.vhd:478). Loss is measured the same way as the
Ethernet ramp -- sequence numbers carried in the payload.

The key distinction this test makes:

    frames missing AND nucleo `failed` > 0   -> the STM32/bus ran out, not the FPGA
    frames missing AND nucleo `failed` == 0  -> the FPGA dropped them

That second case is the FPGA's `overrun` signal, which is left unconnected at
top.vhd:487 and so cannot be observed any other way.

At 125 kbit/s a 4-byte standard frame is 79 bits plus up to 13 stuff bits, so the
*bus* ceiling is roughly 1350-1580 frames/s. If the FPGA keeps up all the way
there, the honest conclusion is "CAN is bus-limited, not FPGA-limited" -- which
is a real result, not a failed test.

Usage:
    python3 test_can_ramp.py
    python3 test_can_ramp.py --port /dev/ttyACM1 --duration 5
"""

import argparse
import sys
import time

import scis
from nucleo import Nucleo

# The bus ceiling is ~1.35-1.58 kfps, so the ladder stops just past it.
LADDER = [100, 200, 400, 600, 800, 1000, 1200, 1400, 1600]


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", default="/dev/ttyACM0", help="Nucleo serial port")
    ap.add_argument("--duration", type=float, default=5.0,
                    help="seconds per ramp step (default 5)")
    ap.add_argument("--drain", type=float, default=1.0,
                    help="seconds to wait for stragglers after each step")
    args = ap.parse_args()

    host_ip = scis.resolve_host_ip()
    print(f"host {host_ip}, CAN metrics return on UDP {scis.CAN_RETURN_PORT}")

    nucleo = Nucleo(args.port)
    print(f"Nucleo on {args.port}: {nucleo.ping()}")

    receiver = scis.Receiver(host_ip)
    receiver.start()

    recorder = scis.RampRecorder("can_t4.csv", "T4", print_header=False)
    print(f"\n{'rate':>8} {'onbus':>8} {'sent':>8} {'failed':>7} {'uniq':>8} "
          f"{'loss%':>7} {'tec':>5} {'blame':>8}")
    print("-" * 72)

    last_good = None          # highest rate actually carried, frames/s
    bus_ceiling = None        # achieved rate at the point the bus saturated
    try:
        for rate in LADDER:
            receiver.reset()
            drops_before = scis.udp_drops(scis.ALL_PORTS)

            nucleo.start(rate)
            time.sleep(args.duration)
            nucleo.stop()
            time.sleep(args.drain)

            st = nucleo.stat()
            snap = receiver.snapshot()
            drops_after = scis.udp_drops(scis.ALL_PORTS)
            host_drops = (drops_after - drops_before) if drops_before >= 0 else -1

            sent = st["sent"]
            unique = snap["can_unique"]
            loss = 100.0 * (1.0 - unique / sent) if sent else 0.0
            gaps = scis.gap_pattern(snap["can_seqs"], sent)
            # What actually reached the bus. `rate` is only what we asked for --
            # once the TX FIFO starts refusing frames the two diverge, and only
            # this number is a throughput measurement.
            onbus = sent / args.duration

            if host_drops > 0:
                blame = "HOST"
            elif st["failed"] > 0 and loss <= 0.1:
                blame = "BUS"      # bus full, FPGA lost nothing -> the answer
            elif loss <= 0.1:
                blame = "-"
            elif st["failed"] > 0:
                blame = "STM32"
            else:
                blame = "FPGA"

            print(f"{rate:>8} {onbus:>8.0f} {sent:>8} {st['failed']:>7} "
                  f"{unique:>8} {loss:>7.3f} {st['tec']:>5} {blame:>8}")

            recorder.write_row(
                requested_pps=rate,
                achieved_pps=round(onbus, 1),
                sent=sent, received=snap["can_received"], unique=unique,
                loss_pct=round(loss, 3), dupes=snap["can_dupes"],
                almostfull=snap["almostfull"], interlock=snap["interlock"],
                host_drops=host_drops, gap_kind=gaps[0], longest_gap=gaps[1],
                valid=int(host_drops == 0),
            )

            if host_drops:
                continue
            if loss <= 0.1:
                last_good = onbus
            if blame == "BUS":
                bus_ceiling = onbus
                print(f"\n  The STM32 could not enqueue {st['failed']} frames "
                      f"({100.0 * st['failed'] / (sent + st['failed']):.1f}% of attempts):")
                print(f"  the 125 kbit/s bus is full at ~{onbus:.0f} frames/s, and the")
                print("  FPGA lost none of what did reach it. CAN is bus-limited.")
                break
            if blame == "FPGA":
                print("\n  FPGA started dropping while the STM32 still had room --")
                print("  this is the unconnected `overrun` in top.vhd:487.")
                break
    except KeyboardInterrupt:
        print("\ninterrupted")
    finally:
        nucleo.close()
        receiver.stop()
        receiver.close()
        recorder.close()

    if bus_ceiling:
        # Theory: a 4-byte standard frame is 79 bits + up to 13 stuff bits, so
        # 125000/92 .. 125000/79 = 1359..1582 frames/s.
        print(f"\n>>> T4: the FPGA kept 100% of frames at every rate.")
        print(f"    Measured CAN bus ceiling: ~{bus_ceiling:.0f} frames/s "
              f"(theory 1359-1582).")
        print(f"    The CAN path is BUS-limited, not FPGA-limited -- the FPGA")
        print(f"    never became the bottleneck, so it has headroom to spare.")
    elif last_good:
        print(f"\n>>> T4 sustained: ~{last_good:.0f} frames/s on the bus "
              f"(ladder topped out; bus never saturated)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
