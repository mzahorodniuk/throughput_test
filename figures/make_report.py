#!/usr/bin/env python3
"""Assemble RESULTS.md: narrative plus tables computed from the CSVs.

The tables come from summarize.py at build time rather than being pasted in, so
the prose and the data cannot drift. Re-run after any new arm.

    /home/misha/FPGA-centric-SCIS/.venv/bin/python make_report.py
"""

import io
import os
import sys
import contextlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import load
import summarize

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def tables():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        summarize.main()
    return buf.getvalue()


def fact(arms):
    """A few numbers the prose quotes directly, pulled from the data."""
    f = {}
    b1 = next((a for a in arms.values() if a["batch"] == 1), None)
    b16 = next((a for a in arms.values() if a["batch"] == 16), None)
    best = None
    for a in arms.values():
        kn = load.knee(a["rows"], "eth_spread")
        if kn and (best is None or kn["delivered_pps"] > best[1]):
            best = (a["batch"], kn["delivered_pps"])
    f["best_batch"], f["best_pps"] = best if best else (None, 0)
    k1 = load.knee(b1["rows"], "eth_spread") if b1 else None
    f["b1_pps"] = k1["delivered_pps"] if k1 else 0
    f["gain"] = (f["best_pps"] / f["b1_pps"]) if f["b1_pps"] else 0
    f["wedge1"] = (b1["summary"].get("eth_spread_wedged_at") if b1 else None)
    kc = load.knee(b16["rows"], "can") if b16 else None
    f["can_pps"] = kc["delivered_pps"] if kc else 0
    # Worst combined-arm CAN loss per batch size -- computed, because the
    # honest sentence differs per arm (0.000 % at batch 16, 93 % at batch 4).
    parts = []
    for a in sorted(arms.values(), key=lambda x: x["batch"] or 0):
        vals = [r["can_loss_pct"] for r in a["rows"]
                if r["test"].startswith("both_") and r["can_loss_pct"] is not None]
        if vals:
            parts.append(f"{max(vals):.3f} % at batch {a['batch']}")
    f["can_cost_line"] = "; ".join(parts) if parts else "not measured"

    parts_w = []
    for a in sorted(arms.values(), key=lambda x: x["batch"] or 0):
        w = sorted({int(v) for k, v in a["summary"].items()
                    if k.endswith("_wedged_at")})
        parts_w.append(f"batch {a['batch']} at {min(w):,} pps" if w
                       else f"batch {a['batch']} never, up to the wire limit")
    f["wedge_line"] = "; ".join(parts_w)

    worst_place = 0.0
    for a in arms.values():
        vals = []
        for tst in ("eth_spread", "eth_p9217", "eth_p9219"):
            kn = load.knee(a["rows"], tst)
            if kn:
                vals.append(kn["delivered_pps"])
        if len(vals) > 1 and max(vals):
            worst_place = max(worst_place,
                              100.0 * (max(vals) - min(vals)) / max(vals))
    f["place_spread"] = worst_place

    rr = load.repeat_rows()
    if rr:
        f["repeat_line"] = ", ".join(
            f"{r['rate']/1000:.0f}k offered delivers {r['median']:,.0f}"
            for r in rr)
        f["repeat_worst"] = max(r["spread_pct"] for r in rr)
    else:
        f["repeat_line"] = "not measured"
        f["repeat_worst"] = 0.0

    sim = load.sim_rows()
    w = sorted([r for r in sim if r["axis"] == "wire"], key=lambda r: r["wire_ns"])
    first = next((r for r in w if r["livelock"]), None)
    f["live_ns"] = first["wire_ns"] if first else None
    f["live_span_ms"] = first["fsr_span_us"] / 1000.0 if first else None
    f["live_runs"] = first["fsr_longest_run"] if first else None
    f["live_factor"] = (first["wire_ns"] / 5000.0) if first else None
    return f


NARRATIVE = """# SCIS FPGA: batching campaign

Measured on the GateMate CCGM1A1 board with two W5500s, over Ethernet only, CAN
only, and both at once, at four values of `TX_BATCH_MAX_PACKETS`. Plus the one
failure mode that cannot be reached from the bench, in simulation.

Everything here is reproducible from this directory:

    host/campaign/run_all_arms.sh 28 16 4 1      # measure every arm
    host/campaign/build_batch.sh <N>             # build + flash one batch size
    sim/sweep_backpressure.py                    # the livelock sweep
    figures/plot_campaign.py                     # the figures
    figures/make_report.py                       # this file

## Headline

**Batching is worth {gain:.1f}x.** Without it the board sustains {b1_pps:,.0f}
metrics/s; at `TX_BATCH_MAX_PACKETS = {best_batch}` it sustains
{best_pps:,.0f} metrics/s, both at under 0.1 % loss, on the same fabric and the
same 15 MHz SPI. The mechanism is visible directly in the data: a datagram costs
a fixed nine-transaction SPI sequence whatever it carries, so putting N metrics
in one datagram divides that cost by N. `campaign_batching.pdf` panel (b) shows
the datagram rate the transmit chip has to sustain *falling* as offered load
rises past 40 kpps, because the batch fills faster.

**Batching raises the wedge threshold too.** Overload closes the RX W5500 socket
permanently -- SW3 does not clear it, only a reflash does. Where that happens:
{wedge_line}. So the batch size that carries the most traffic is also the one that
survives being overloaded, which is not obviously how it had to come out.

**Socket placement barely matters.** Eight sockets, one socket on 9217
(FIFO 0, top arbiter priority) and one socket on 9219 (FIFO 3) land within
{place_spread:.1f} % of each other at every batch size -- and that figure is one
ladder rung, not a real difference: the rungs near the knee are 5 000 pps apart.
The ceiling therefore sits upstream of the priority arbiter, which has nothing to
arbitrate while only one channel is active. Where placement *does* show up is
past the knee, where a single hot socket degrades much harder than the spread.

**CAN is bus-limited, not FPGA-limited.** {can_pps:,.0f} frames/s at 125 kbit/s,
and the FPGA never drops a frame until the STM32 itself cannot enqueue. The
FPGA's CAN receiver has headroom it never got to use; finding its real ceiling
needs a faster bus (`CLKS_PER_BIT` in `top.vhd`, with matching STM32 bit timing),
which is a change to two subsystems and was left alone here.

**Batching decides whether Ethernet can starve CAN.** Running both at once, the
worst CAN frame loss anywhere in the ladder is {can_cost_line}. That is the
result to act on: the two channels share one transmit chip, and a small batch
leaves CAN exposed to an Ethernet overload while a large one does not.

**Past the knee it holds, then steps down.** Five repeats per rate on the
batch-16 build: {repeat_line}. Delivered throughput does not collapse when the
offered rate exceeds the knee -- it plateaus near the knee value while loss rises
to absorb the excess, and only steps down further out. Spread across repeats is
at or below {repeat_worst:.1f} % everywhere, so this is the board's behaviour and
not run-to-run noise. Two earlier single-shot ladders disagreed about this, which
is why the repeats exist.

## The transmit-buffer livelock

`CHECK_IF_FREE_SIZE_IS_AVAILABLE` has no way out. When `Sn_TX_FSR` reports less
than `minimum_free_tx_buffer_memory` (256) it returns to
`GET_TX_FREE_BUFFER_SIZE` (`w5500_state_machine.vhd:816`) and re-reads the same
socket -- no retry budget, no socket advance, no fall-through to the receive
branch.

The watchdog cannot catch it. `p_watchdog` (`w5500_state_machine.vhd:390`) zeroes
its counter whenever the state differs from the previous state, and a two-state
alternation changes state on every SPI transaction, so `WATCHDOG_CLKS` is never
reached. It detects a stuck state, not stalled progress.

Both of those are confirmed. `tb_w5500_tx_backpressure.vhd` reproduces the
livelock: **{live_runs} consecutive `Sn_TX_FSR` reads spanning
{live_span_ms:.2f} ms** against a 2.18 ms watchdog threshold, with the SPI bus
never idle for more than 25 clocks -- so `cs_idle_max`, the liveness measure the
other testbenches rely on, sees nothing at all. Across that run no `Sn_RX_RSR`
read occurs, by construction: any other transaction would have broken the run.

Three things bound how much this matters.

*It needs a badly degraded link.* The livelock appears once the chip's wire time
reaches {live_ns_us:.0f} us per datagram. As built it is about 5 us, so the link
has to be roughly **{live_factor:.0f}x slower than 100 Mbit** -- which is why it
has never been seen on the bench, and why this is a simulation result rather than
a measurement.

*Existing simulation could not have found it.* `tb_w5500_tx`'s behavioural chip
frees the whole transmit buffer the instant `SEND` is written, so `Sn_TX_FSR` is
never low and the branch is unreachable at any wire speed. Modelling the buffer
as freeing only as the wire drains -- what the chip does, since free space is not
free until it has been sent -- is the entire difference. The sweep runs both
models so that comparison is in the output rather than in a claim.

*Bigger batches are safer, not riskier.* At batch 1 the stall runs 37 ms; at
batch 28 it runs 1.8 ms. Fewer, larger datagrams mean fewer trips through the
check and more bytes retired per wire transaction.

Two boundary results worth recording. With a 256-byte transmit buffer, free space
can never *exceed* the 256-byte threshold, so the controller never transmits at
all -- a hard configuration constraint, not a performance note. And feeding two
or more sockets removes the livelock entirely at the same wire speed, because
each socket has its own buffer: a single hot socket is the dangerous shape.

## What was wrong with the old harness

Four things, all measurement-side, found while setting this up. They matter
because the numbers in `thrpt_test/README.md` were produced with them.

1. **One sequence space, 90 112 probes.** `scis.run_step` stops a step there, so
   at 150 kpps the window is 0.6 s. The probe key here is
   `(port, identifier, seq)` over all eight wide-band IDs on all eight ports:
   5.8 M probes. Every value still stays inside 1..89 degC, so no test packet can
   trip the interlock.
2. **Sender and receivers shared a GIL.** A bare `sendto` loop reaches 381 k pps
   on this host, but `test_eth_ceiling.py` asked for 1.4 M pps and achieved 90 k
   at *zero* loss -- the ceiling in that CSV is the harness, not the board.
   Senders and receivers are separate processes here.
3. **`SO_RCVBUF` was a no-op.** The harness asks for 8 MiB; `net.core.rmem_max`
   on this host is 212 992, so the kernel grants ~0.4 MiB. Raising it needs root,
   so instead the receive loop only copies bytes during the step and parses
   afterwards.
4. **Any unknown identifier was counted as CAN.** At overload the mis-framed
   datagrams decode to arbitrary identifiers, which inflated `can_unique` past
   `can_sent` and produced *negative* CAN loss. CAN is now matched on
   `CAN_IDENTIFIER` exactly.

One more that is not a harness bug but bounds every Ethernet number: `enp2s0` is
1 Gbit while both W5500s are 100 Mbit, so above **148 810 pps** (a 9-byte payload
is an 84-byte frame on the wire) the *switch* discards. Steps above that are run
and plotted but never used for a sustained-rate claim.

## Framing integrity at overload

Above the knee the return path emits datagrams whose length is not a multiple of
9 bytes, and datagrams carrying more records than `TX_BATCH_MAX_PACKETS` allows
-- 163 records in one datagram on a batch-1 build. Since the configured ceiling
is the number of records the controller will put in a datagram, anything above it
is mis-framing, not batching. The host discards a datagram whose length is not a
multiple of 9, so these show up as loss; a consumer that parsed them
optimistically would ingest garbage metric values instead. That matters for an
interlock system, where a mangled value can assert a global interlock from
nothing. The rate is strongly batch-dependent (tens of thousands of ragged
datagrams per step at batch 1 and 4, a couple of hundred at batch 16), which
points at the transmit pointer/length bookkeeping under backpressure rather than
at the batching logic itself.

## Figures

| file | what it shows |
|---|---|
| `campaign_eth_ramp` | delivered vs offered and loss vs offered, per batch size; the wedge |
| `campaign_socket_placement` | the three socket placements, and their sustained rates |
| `campaign_batching` | achieved batch size vs load, and the falling datagram rate |
| `campaign_batch_sweep` | sustained rate vs `TX_BATCH_MAX_PACKETS`, and the 1/N cost fit |
| `campaign_can` | CAN delivered vs offered, and where frames are actually lost |
| `campaign_combined` | what CAN costs Ethernet, and whether Ethernet starves CAN |
| `campaign_livelock` | the simulation sweep: threshold, batch effect, model comparison |

Each is written as both PDF and 600 dpi PNG into `figures/out/`, in the same
style as the repo's floorplan figures (DejaVu Sans 7 pt, Okabe-Ito, Type-42
fonts). `figures/cvd_check.py` computes the colour separations for the specific
pairs these panels place together rather than assuming the palette is safe.

---

"""


def main():
    arms = load.arms()
    if not arms:
        print("no results", file=sys.stderr)
        return 1
    f = fact(arms)
    f["live_ns_us"] = (f["live_ns"] / 1000.0) if f["live_ns"] else 0
    body = NARRATIVE.format(**f) + tables()
    out = os.path.join(ROOT, "RESULTS.md")
    with open(out, "w") as fh:
        fh.write(body)
    print("wrote", out, f"({len(body)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
