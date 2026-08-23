# SCIS FPGA: batching campaign

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

**Batching is worth 3.0x.** Without it the board sustains 35,000
metrics/s; at `TX_BATCH_MAX_PACKETS = 28` it sustains
104,985 metrics/s, both at under 0.1 % loss, on the same fabric and the
same 15 MHz SPI. The mechanism is visible directly in the data: a datagram costs
a fixed nine-transaction SPI sequence whatever it carries, so putting N metrics
in one datagram divides that cost by N. `campaign_batching.pdf` panel (b) shows
the datagram rate the transmit chip has to sustain *falling* as offered load
rises past 40 kpps, because the batch fills faster.

**Batching raises the wedge threshold too.** Overload closes the RX W5500 socket
permanently -- SW3 does not clear it, only a reflash does. Where that happens:
batch 1 at 40,000 pps; batch 4 at 90,000 pps; batch 16 never, up to the wire limit; batch 28 never, up to the wire limit. So the batch size that carries the most traffic is also the one that
survives being overloaded, which is not obviously how it had to come out.

**Socket placement barely matters.** Eight sockets, one socket on 9217
(FIFO 0, top arbiter priority) and one socket on 9219 (FIFO 3) land within
4.7 % of each other at every batch size -- and that figure is one
ladder rung, not a real difference: the rungs near the knee are 5 000 pps apart.
The ceiling therefore sits upstream of the priority arbiter, which has nothing to
arbitrate while only one channel is active. Where placement *does* show up is
past the knee, where a single hot socket degrades much harder than the spread.

**CAN is bus-limited, not FPGA-limited.** 1,472 frames/s at 125 kbit/s,
and the FPGA never drops a frame until the STM32 itself cannot enqueue. The
FPGA's CAN receiver has headroom it never got to use; finding its real ceiling
needs a faster bus (`CLKS_PER_BIT` in `top.vhd`, with matching STM32 bit timing),
which is a change to two subsystems and was left alone here.

**Batching decides whether Ethernet can starve CAN.** Running both at once, the
worst CAN frame loss anywhere in the ladder is 89.312 % at batch 1; 93.145 % at batch 4; 0.425 % at batch 16; 0.486 % at batch 28. That is the
result to act on: the two channels share one transmit chip, and a small batch
leaves CAN exposed to an Ethernet overload while a large one does not.

**Past the knee it holds, then steps down.** Five repeats per rate on the
batch-16 build: 60k offered delivers 59,998, 100k offered delivers 100,000, 105k offered delivers 105,000, 110k offered delivers 105,744, 120k offered delivers 105,803, 140k offered delivers 80,814. Delivered throughput does not collapse when the
offered rate exceeds the knee -- it plateaus near the knee value while loss rises
to absorb the excess, and only steps down further out. Spread across repeats is
at or below 3.7 % everywhere, so this is the board's behaviour and
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
livelock: **340 consecutive `Sn_TX_FSR` reads spanning
1.16 ms** against a 2.18 ms watchdog threshold, with the SPI bus
never idle for more than 25 clocks -- so `cs_idle_max`, the liveness measure the
other testbenches rely on, sees nothing at all. Across that run no `Sn_RX_RSR`
read occurs, by construction: any other transaction would have broken the run.

Three things bound how much this matters.

*It needs a badly degraded link.* The livelock appears once the chip's wire time
reaches 200 us per datagram. As built it is about 5 us, so the link
has to be roughly **40x slower than 100 Mbit** -- which is why it
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

## Provenance

| arm | TX_BATCH_MAX_PACKETS | bitstream md5 | built |
|---|---|---|---|
| `batch1` | 1 | `69d5f6163038` | 2026-08-21T16:14:51 |
| `batch4` | 4 | `ce36f9c1cf59` | 2026-08-21T16:05:47 |
| `batch16` | 16 | `d0f2e561f1a1` | 2026-08-21T15:56:03 |
| `batch28` | 28 | `28b235e47b7f` | 2026-08-21T16:23:53 |

## Ethernet only: sustained rate under 0.1 % loss (packets/s)

| batch | 8 sockets | 1 socket 9217 | 1 socket 9219 | wedged at |
|---|---|---|---|---|
| 1 | 35,000 | 34,999 | 34,998 | 40,000 |
| 4 | 80,000 | 79,988 | 79,991 | 90,000 |
| 16 | 100,000 | 104,986 | 104,955 | never |
| 28 | 104,985 | 104,965 | 105,000 | never |

## What batching buys

| batch | sustained (8 sockets) | vs batch 1 | achieved batch at the knee | us per metric |
|---|---|---|---|---|
| 1 | 35,000 | 1.00x | 1.00 | 28.57 |
| 4 | 80,000 | 2.29x | 3.98 | 12.50 |
| 16 | 100,000 | 2.86x | 12.47 | 10.00 |
| 28 | 104,985 | 3.00x | 17.79 | 9.53 |

## CAN only (125 kbit/s bus, FPGA is the DUT)

The 0.1 % budget lands right on the 1400 frames/s rung, where a handful of
frames decides pass or fail (8 of 7366 on one arm), so the knee column is
threshold-sensitive there. The robust number is the last column: the highest
rate the bus carried with the source reporting nothing undelivered.

| batch | knee (0.1 % budget) | loss there | source could not enqueue from | highest rate carried cleanly |
|---|---|---|---|---|
| 1 | 1,264 | 0.000 % | never | 1,468 |
| 4 | 1,263 | 0.000 % | never | 1,472 |
| 16 | 1,472 | 0.068 % | 1,532 | 1,472 |
| 28 | 1,474 | 0.000 % | 1,533 | 1,474 |

Every arm: the FPGA delivered every frame that reached the bus until the
STM32 itself ran out of mailboxes. The CAN path is bus-limited, not FPGA-limited,
and its real ceiling is still unmeasured.

## Ethernet + CAN on one transmit chip

| batch | config | Ethernet alone | with CAN 1400 f/s | cost | CAN loss over the run |
|---|---|---|---|---|---|
| 1 | 8 sockets | 35,000 | 30,000 | -5.0k | +0.203 .. +89.312 % |
| 1 | 1 socket 9217 | 34,999 | 29,977 | -5.0k | +0.000 .. +60.150 % |
| 1 | 1 socket 9219 | 34,998 | 29,978 | -5.0k | +0.000 .. +0.426 % |
| 4 | 8 sockets | 80,000 | 79,993 | -0.0k | +0.000 .. +93.145 % |
| 4 | 1 socket 9217 | 79,988 | 79,976 | -0.0k | +0.000 .. +0.467 % |
| 4 | 1 socket 9219 | 79,991 | 79,992 | --0.0k | +0.000 .. +0.243 % |
| 16 | 8 sockets | 100,000 | 99,989 | -0.0k | +0.000 .. +0.223 % |
| 16 | 1 socket 9217 | 104,986 | 89,983 | -15.0k | +0.000 .. +0.102 % |
| 16 | 1 socket 9219 | 104,955 | 89,973 | -15.0k | +0.000 .. +0.425 % |
| 28 | 8 sockets | 104,985 | 100,000 | -5.0k | +0.020 .. +0.486 % |
| 28 | 1 socket 9217 | 104,965 | 89,972 | -15.0k | +0.000 .. +0.486 % |
| 28 | 1 socket 9219 | 105,000 | 89,959 | -15.0k | +0.000 .. +0.426 % |

## Framing integrity at overload

Datagrams whose length is not a multiple of 9 bytes, and the largest
record count seen in one datagram. `TX_BATCH_MAX_PACKETS` is the
configured ceiling, so anything above it is mis-framing, not batching.

| batch | worst ragged datagrams in a step | largest records/datagram seen |
|---|---|---|
| 1 | 44,284 | 162 (> configured 1) |
| 4 | 46,910 | 143 (> configured 4) |
| 16 | 146 | 33 (> configured 16) |
| 28 | 208 | 45 (> configured 28) |

## Repeatability (batch 16, five repeats per rate)

| offered | delivered median | range | spread |
|---|---|---|---|
| 60,000 | 59,998 | 59,997 .. 60,000 | 0.0 % |
| 100,000 | 100,000 | 99,763 .. 100,000 | 0.2 % |
| 105,000 | 105,000 | 104,988 .. 105,000 | 0.0 % |
| 110,000 | 105,744 | 104,878 .. 105,775 | 0.8 % |
| 120,000 | 105,803 | 103,624 .. 107,660 | 3.7 % |
| 140,000 | 80,814 | 80,745 .. 80,876 | 0.2 % |

At and below the knee the spread is negligible. Above it the delivered rate
plateaus near the knee rather than collapsing, which two earlier single-shot
ladders disagreed about -- hence the repeats.

## Transmit-buffer livelock (simulation)

- watchdog threshold: `WATCHDOG_CLKS` 65535 at 30 MHz = **2.18 ms**
- highest wire time with no livelock: **100 us/datagram** (longest stall 106 us)
- lowest wire time that livelocks: **200 us/datagram** (stall 1.16 ms, 340 back-to-back Sn_TX_FSR reads)
- as built the wire costs ~5 us/datagram, so the livelock needs the link about **40x slower** than 100 Mbit

| axis | setting | longest stall | verdict |
|---|---|---|---|
| drain_model | buffer freed on SEND (tb_w5500_tx model) | 0 | NO-LIVELOCK |
| drain_model | buffer freed as the wire drains (the chip) | 11.56 ms | LIVELOCK |
| txbuf | TX buffer 2048 B | 0.11 ms | NO-LIVELOCK |
| txbuf | TX buffer 1024 B | 0.11 ms | NO-LIVELOCK |
| txbuf | TX buffer 512 B | 0.11 ms | NO-LIVELOCK |
| txbuf | TX buffer 256 B | 42.59 ms | LIVELOCK |
| sockets | 1 of 8 sockets fed | 11.56 ms | LIVELOCK |
| sockets | 2 of 8 sockets fed | 0 | NO-LIVELOCK |
| sockets | 4 of 8 sockets fed | 0 | NO-LIVELOCK |
| sockets | 8 of 8 sockets fed | 0 | NO-LIVELOCK |
