# SCIS FPGA throughput test

Measures how many metric packets/second the GateMate SCIS board actually sustains,
on both of its input paths, by ramping the load up until packets start going missing.

**The FPGA is the device under test.** The Nucleo-F446RE is only a CAN traffic source,
and this PC generates the Ethernet load.

---

## How the measurement works

The FPGA echoes every packet it accepts, so throughput is just *what came back / what we
sent* — counted entirely on this host, with nothing instrumented on the board.

```
this PC .106 ──UDP, 9 bytes──> W5500 RX .100:9217+k ──SPI 15 MHz──> data concentrator
                                                                            │
   Nucleo ──CAN 125 kbit/s──> can_node ──> can_metric_adapter ──────────────┤
                                                                            ▼
                                 W5500 TX .101 ──UDP──> this PC .106:9217+TUSER
```

UDP packets return **on the port they arrived on** (`udp_packet_adapter.vhd:160`,
`s_axis.tuser <= ruser`). CAN metrics always return on **9218** (`top.vhd:478`,
`CAN_TUSER => "001"`). The harness binds all eight ports, so it catches both.

Two things stop a plain sent-vs-received counter from lying:

**Every packet carries a sequence number** in its 4-byte value field, which round-trips
untouched. That turns *"150 missing"* into *which* 150 — and the shape of the gaps tells
you what broke:

| Gap shape | Meaning |
|---|---|
| One long contiguous run (`bursty`) | A buffer overflowed — e.g. a 2 KB W5500 socket |
| Gaps spread evenly (`spread`) | A steady rate limit; the path is simply too slow |

Duplicates and reordering are counted separately, and are **not** loss.

**Host-side drops are excluded, not blamed on the FPGA.** If our own socket buffer
overflows, packets are gone before Python sees them — indistinguishable from FPGA loss.
Every step reads the `drops` column of `/proc/net/udp` before and after; a step where the
host dropped is marked `valid=0` and skipped. Likewise the CSV records the *achieved* send
rate, never the requested one, so a step that asked for 32 kpps but only managed 8 kpps
cannot masquerade as a clean pass.

Sequence numbers ride inside each device's critical band (Ethernet: 1–89 °C of a 0–90 °C
device; CAN: 21–79 %RH of a 20–80 %RH device) so **no test packet can trip the interlock**,
which would latch until SW3 and poison every later step.

---

## Bring-up (once)

1. **Flash the bitstream** — there is none in the repo.
   ```bash
   source <oss-cad-suite>/environment
   cd ~/FPGA-centric-SCIS/hardware/fpga/ccgm1a1 && make all
   ```

2. **Give this PC an address on the FPGA subnet.** `enp2s0` is currently down with no
   carrier and no 192.168.2.x exists anywhere on this machine.
   ```bash
   sudo ip addr add 192.168.2.106/24 dev enp2s0
   sudo ip link set enp2s0 up
   ```
   Cable `enp2s0` and both W5500 modules to the same switch.

3. **Free the UDP ports.** The Metric Packet Server owns 9217–9224 and will block the
   harness from binding them.
   ```bash
   cd ~/FPGA-centric-SCIS/software_infrastructure && docker compose stop metric-packet-server
   ```
   (Its `:8001` counters are scraped at 10 s intervals — far too coarse to find a knee.)

4. **Wire CAN** — config (A), two wires, matching `top.vhd:471-479` as built:
   `PB9 → PMOD B pin 7`, `PMOD B pin 8 → PB8`, **common ground**.
   On the Nucleo-F446RE those are D14 and D15 on the Arduino header (CN5 pins 9
   and 10). The old H743 pins PD1/PD0 do not exist on this package.

   **Both wires matter, and they fail asymmetrically.** Without `PB9 → pin 7` the
   FPGA receives nothing and it is obvious. Without `pin 8 → PB8` everything still
   *looks* fine — the FPGA receives every frame and the metrics flow — but the
   STM32 never sees the echo, so nothing is ever ACKed. The firmware measures this
   at boot and prints `rx wire (PB8): ... CONNECTED` or `... OPEN!`; the test is a
   pull-up/pull-down comparison, because under a pull-up alone an open pin and a
   healthy idle line read identically.

5. **Flash the Nucleo** (only needed for the CAN tests):
   ```bash
   cd firmware && pio run -t upload
   ```

6. **Reduce host jitter** (optional but recommended):
   ```bash
   sudo apt install ethtool && sudo ethtool -C enp2s0 rx-usecs 0
   ```

---

## Running the tests

```bash
cd host

# T0-T3: Ethernet. Start here.
sudo chrt -f 80 python3 test_eth_ramp.py

# T4: CAN
python3 test_can_ramp.py

# T5: both at once, twice -- the second run should starve CAN
python3 test_combined.py --eth-port 9219
python3 test_combined.py --eth-port 9217
```

Each writes a CSV to `host/results/`, one row per ramp step, ready to plot.

| # | Test | Question it answers |
|---|---|---|
| **T0** | 100 packets @ 10 pps | Does the path work at all? Everything else is meaningless if this fails |
| **T1** | Ramp on port 9219 | Per-socket ceiling |
| **T2** | Ramp across all 8 ports | Does spreading across sockets beat one hot socket? |
| **T3** | Ramp on port 9217 | Best case — the highest-priority FIFO |
| **T4** | CAN ramp | Is CAN bus-limited or FPGA-limited? |
| **T5** | CAN + Ethernet together | Does UDP starve CAN? |

Each ramp climbs `100 → 250 → 500 → 1k → 2k → 4k → 8k → 16k → 32k` pps, stops after two
consecutive lossy steps, then **bisects** between the last good and first bad rate to pin
the knee to ~5 %.

### The two comparisons worth making

**T2 vs T1.** The RX state machine polls all eight sockets round-robin, so hammering one
socket wastes seven eighths of the polls. If T2 is meaningfully higher than T1, spreading
load across sockets is a free win for anyone using this board.

**T5 on 9217 vs 9219.** The output arbiter is strict highest-priority-first, and TUSER *is*
the arrival socket. So UDP on 9217 (FIFO 0) outranks CAN (FIFO 1), while UDP on 9219
(FIFO 3) does not. CAN collapsing in the first case and surviving in the second confirms
the arbiter behaves as the HDL says — and quantifies what it costs.

---

## Measured results

GateMate M1A1, 30 MHz fabric, dual W5500, 3 s per step, 9-byte V01 packets.

| Configuration | Sustained (<0.1 % loss) | Failure mode |
|---|---|---|
| **One socket** (T1 port 9219, T3 port 9217) | **~21 000 pps** | Gradual: 0.006 % @18k → 0.31 % @24k |
| **Spread over 8 sockets** (T2) | **~30 000 pps** | Cliff: 0.002 % @30k → **95 % @32k, socket closes permanently** |
| **CAN** (T4) | **~1 468 frames/s** | Bus saturates; **FPGA never drops a frame** |

**Spreading load across all 8 UDP ports is worth ~40 % throughput** (21k → 30k) for no
hardware change. The RX state machine round-robins every socket, so with one hot socket
seven of every eight polls read `Sn_RX_RSR` on an empty socket — wasted transactions on the
15 MHz SPI bus that is already the bottleneck. Distribute devices across 9217–9224.

**Port number is irrelevant.** T1 (FIFO 3) and T3 (FIFO 0, highest priority) both land at
21 000 pps. The strict-priority arbiter has nothing to arbitrate when only one socket is
active, so the ceiling sits *upstream* of it, in the W5500 SPI/FSM path. Priority only
decides who wins when two channels compete — see T5.

**The bottleneck is SPI and FSM overhead, not the network.** 30 kpps of 9-byte packets is
only ~18 Mbit/s on the wire; the 100 Mbit link is never involved. At 30 kpps each packet has
a 33 µs budget against ~27 µs of TX-side SPI work — 82 % utilisation of the TX W5500, which
is where the ceiling comes from.

**The CAN path is bus-limited, not FPGA-limited.** Every frame that reached the bus arrived
at the host — 0.000 % loss at every rate from 100 to 1400 frames/s, and still 0.000 % at
saturation. The limit is the 125 kbit/s wire: at ~1600 frames/s requested, the STM32 could
not enqueue 8.9 % of its attempts (TX FIFO full) and only ~1468 frames/s reached the bus,
matching the 1359–1582 predicted for a 4-byte standard frame with bit stuffing.

Two things follow. The FPGA's CAN receiver has headroom it never got to use, so raising the
bitrate (`CLKS_PER_BIT` in `top.vhd`: 60 ⇒ 500 kbps, 30 ⇒ 1 Mbps, with matching STM32 bit
timing) is the only way to find its real ceiling. And the unconnected `overrun`
(`top.vhd:487`) never fired in practice — `sent` equalled `unique` at every step, so no
frame was silently lost. That risk is real but did not materialise at 125 kbit/s.

`tec` stayed 0 throughout, confirming the config (A) two-wire link is electrically clean.

### Combined load: one shared output budget

Both inputs converge on a single TX W5500, and it is the ceiling. Running both at once
(T5, Ethernet spread over 7 sockets + CAN at 1400 f/s) peaked at **29 329 packets/s** —
essentially the ~30 000 that Ethernet reaches alone. The two channels are not additive.

The proof is comparing the same UDP rate with and without CAN:

| UDP rate | Eth loss alone | Eth loss + 1400 f/s CAN |
|---|---|---|
| 24 000 | 0.006 % | 0.098 % |
| 28 000 | 0.001 % | **0.268 %** |

**Priority does not protect against loss.** With UDP on 9217 (FIFO 0, outranking CAN's
FIFO 1) and only 500 f/s of CAN, Ethernet lost **18.2 %** while CAN lost **0.000 %** — the
*higher-priority* channel lost everything. Packets are therefore dropped at RX W5500
ingestion, upstream of the arbiter; the arbiter only orders what already got in. This is
consistent with T1 and T3 landing on the same knee despite different priorities.

**CAN's cost depends strongly on how UDP is spread**, and this needs confirming:

| Configuration | CAN rate | UDP capacity lost | Ratio |
|---|---|---|---|
| UDP spread over 7 sockets | 1400 f/s | ~670 pps | ~0.5 : 1 |
| UDP on one socket (9217) | 500 f/s | ~4000 pps | **~8 : 1** |

A 16x difference is surprising enough to repeat before relying on it. Single-socket
delivery drops from ~21 000 to a hard ~18 000 pps plateau once any CAN traffic is present.

`ALMOSTFULL` never asserted in any combined run, even at 18 % loss — further evidence it is
not a usable congestion signal on this bitstream.

### Two findings worth acting on

**Overload disables the board permanently.** At ~32 kpps the RX W5500 socket closes and does
not reopen; the board answers nothing, even a 10 pps trickle, until it is power-cycled. The
margin between "fully working" (30k, 0.002 % loss) and "dead until someone walks over to it"
is about 7 %. For an interlock system, a traffic burst that silently disables the input path
is a safety-relevant failure, not just a throughput limit.

**Loads well below the knee corrupt data.** INTERLOCK assertions were observed at 16 000 and
20 000 pps — rates with < 0.05 % loss. Test values are clamped to 1–89 °C inside a 0–90 °C
band, so no legitimate packet can trip an interlock; a payload must have been mangled in
transit. This matches the FIFO wrap bug (`g_DEPTH` exceeds what `c_ADDR_WIDTH` can address
in `metric_packet_fifo.vhd` and `priority_fifo.vhd`, so overflow corrupts instead of
backpressuring). A corrupted value can assert a **global interlock** from garbage.

## Reading the results

The *shape* of the failure identifies the bottleneck, which is why every step records
alerts and per-port counts rather than a single loss number:

| Signature | Bottleneck |
|---|---|
| Smooth proportional loss, no alerts, `spread` gaps | RX SPI read rate — the FPGA cannot drain the W5500 fast enough |
| Loss **plus** `ALMOSTFULL` | Downstream/TX drain limit; the priority FIFOs are backing up |
| One port goes permanently silent, others fine | W5500 RX socket closed — the documented 2 KB-buffer bug (`README.md:72`) |
| Loss with **junk payloads or impossible seq values** | FIFO wrap corruption (`g_DEPTH` exceeds `c_ADDR_WIDTH` in `metric_packet_fifo.vhd` and `priority_fifo.vhd`, so overflow corrupts instead of backpressuring) |
| `host_drops > 0` / `valid=0` | **Not the FPGA.** The harness lost them; re-run that step |
| CAN gaps with Nucleo `failed > 0` | The 125 kbit/s bus saturated — not the FPGA |
| CAN gaps with Nucleo `failed == 0` | The FPGA dropped them: `can_metric_adapter`'s `overrun`, left unconnected at `top.vhd:487` |
| Nucleo boot line says `rx wire (PB8): ... OPEN!` | **Not the FPGA.** The `can_tx → PB8` return wire is not connected. Frames still reach the FPGA, so metrics keep flowing and only the ACK path is dead — see below |

### Sanity-checking the numbers

The RX and TX W5500s are **two independent chips on two independent SPI masters**
(`spi_busy_0` / `spi_busy_1` in `top.vhd`), so their costs run in parallel, not in series.
At 15 MHz SPI and 533 ns/byte that is ~21 µs per packet on the RX chip and ~27 µs on the
TX chip — an SPI-bound ceiling around **37–48 kpps**, with the TX chip binding first. Real
throughput will be well below that once FSM state overhead and socket round-robin polling
are counted, but figures in the **10–20 kpps** range are plausible, not suspicious.

### The board can wedge — and does

Overloading the RX W5500 closes its socket **permanently**; it does not reopen on its own.
This has been reproduced: a 32 kpps step killed it, after which every later step reported a
tidy `100.000%` loss against a dead board and the ramp happily produced a knee from
meaningless data.

The harness now guards against this. After any step losing more than 50 %, it probes the
board at 10 pps; if that fails it raises `BoardWedged`, stops immediately, and tells you the
rate that killed it. Rows recorded before the wedge are kept — they are valid.

**Recovery is a power-cycle or reset.** SW3 only clears the interlock latch, not a closed
socket.

Two related traps the harness now reports rather than believes:

- **Non-monotonic loss.** If a step passes at a rate above one that failed, loss is not
  rising with rate, so there is no clean "knee" to find. The knee is capped below the first
  failure and the anomaly is printed. (Observed for real: 8000 pps lost 0.242 % while
  16000 pps lost 0.000 %.)
- **An INTERLOCK during a ramp means corruption.** Test values are clamped well inside each
  device's critical band, so an interlock can only fire if a payload was mangled in transit
  — the FIFO wrap bug. Treat `ILK > 0` as a data-integrity finding, not a nuisance.

For CAN, a 4-byte standard frame at 125 kbit/s is 79 bits plus up to 13 stuff bits — a
**bus** ceiling of roughly 1350–1580 frames/s. If the FPGA keeps up all the way there, the
correct conclusion is *"CAN is bus-limited, not FPGA-limited"*. That is a real result, not
a failed test.

---

## Files

```
host/
  scis.py            packet format, pacer, receiver, drop guard, ramp + knee finder
  nucleo.py          serial control of the Nucleo (RATE / STOP / STAT / PING)
  test_eth_ramp.py   T0-T3
  test_can_ramp.py   T4
  test_combined.py   T5
  results/           CSV output
firmware/
  platformio.ini     BUS_WIRING=BUS_TWO_WIRE -- must match the bitstream
  src/main.cpp       CAN load generator
```

### Firmware notes

The CAN setup is lifted from the working `can_publisher` firmware (125 kbit/s, 75 % sample
point — matching the FPGA's `can_node`). Three things differ, all for throughput:

- all **three** bxCAN TX mailboxes are in play; `can_publisher` sends one at a time
- the send is **non-blocking** — no free mailbox increments `failed` instead of busy-waiting
  20 ms and silently distorting the offered rate
- no `delay()`, no period clamp, no sweep, no ACK probe

`BUS_TWO_WIRE` selects `CAN_MODE_LOOPBACK` with retransmission **off**, so a lost frame is
dropped rather than retried. That keeps the offered rate exactly what was asked for, which
is what a ramp needs.

**Board note.** This was an H743ZI (FDCAN) and is now an F446RE (bxCAN): three fixed TX
mailboxes instead of a TX FIFO in message RAM, and bit timing off APB1 at 45 MHz, so
20 tq/bit × prescaler 18 rather than the H7's 16 tq/bit. Same 125 kbit/s, same 75 % sample
point, same frames on the wire. `TransmitFifoPriority` is **on**: every frame here carries
CAN ID 2, and under ID priority the tie is broken by mailbox number, which would let the
sequence numbers leave the board out of order.

The stock F446RE clock tree runs off the internal HSI RC (~1 %), where the H743ZI variant
used the ST-LINK's crystal-derived 8 MHz MCO. Classic CAN at these settings tolerates about
0.77 % total error between the two nodes and the FPGA end has a crystal, so if a ramp shows
stuff/form/CRC errors instead of clean saturation, suspect the oscillator before the FPGA —
`can_publisher`'s `-DUSE_HSE_MCO_CLOCK` block moves the PLL onto the MCO.

---

## Gotchas

- **Payloads must be exactly 9 bytes and start with `'V'`** (`udp_packet_adapter.vhd:133-142`).
  Anything else is discarded — but only *after* the FPGA has paid the full SPI cost to read
  it, which is itself a worthwhile experiment.
- **`INTERLOCK` is also 9 bytes.** The receiver checks for the alert strings before parsing,
  and `unpack()` rejects them anyway since `'INT' != 'V01'`.
- **The pacer must yield the GIL.** A pure-Python busy-wait starves the receiver threads in
  the same process; packets then pile up in the socket buffer and get dropped. That was
  measured, not theorised — it cost 55 % loss at 500 pps before `time.sleep(0)` was added to
  `Pacer.wait()`. If you rewrite the pacer, keep the yield.
- **If the interlock latches**, press SW3 on the board. Values are kept in-band precisely so
  this should not happen; if it does, something is wrong with the sequence encoding.
