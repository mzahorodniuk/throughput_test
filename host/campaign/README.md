# Batching campaign harness

Measures the current SCIS design over Ethernet only, CAN only, and both at
once, at several values of `TX_BATCH_MAX_PACKETS`. Separate from the `host/`
scripts one level up because it replaces their measurement core -- see
"Why not scis.py" below.

## Running it

```bash
# one batch size: build from source, flash, keep the bitstream
./build_batch.sh 16              # 1..28, since N*9 must stay under 256

# re-flash a bitstream already built, and put the HDL generic back in step
./flash_saved.sh 16

# one arm (Ethernet, CAN, and both) against whatever is on the board
python3 -u run_campaign.py --tag batch16 --suite all
python3 -u run_campaign.py --tag batch16 --suite can,both   # subset

# every arm, sequentially, from the saved bitstreams
./run_all_arms.sh 16 4 1 28

# how repeatable is one step?
python3 repeatability.py --tag batch16 --reps 5
```

Output per arm, under `results/<tag>/`:

| file | contents |
|---|---|
| `steps.csv` | one row per ramp step, all suites |
| `meta.json` | md5 of the flashed bitstream, the batching generics read out of the HDL, host network and kernel settings |
| `summary.json` | the knee per configuration, plus any wedge rates and reflashes |
| `repeat.csv` | repeatability run, if one was done |

## Why not `scis.py`

Four harness limits, all of which bound the numbers in `../../README.md` rather
than the board:

1. **90 112 probes.** `scis.pack` hides the sequence number in one device's safe
   band, so `run_step` must stop there -- a 0.6 s window at 150 kpps. Here the
   probe key is `(port, identifier, seq)` across all eight wide-band IDs on all
   eight ports: 5.8 M probes. Values still stay inside 1..89 degC, so no test
   packet can trip the interlock.
2. **Sender and receivers shared a GIL.** A bare `sendto` loop reaches 381 k pps
   on this host, but `test_eth_ceiling.py` asked for 1.4 M and achieved 90 k at
   *zero* loss. Senders and receivers are separate processes here, forked after
   the sockets are bound so the source port still equals the destination port.
3. **`SO_RCVBUF` was a no-op.** It asks for 8 MiB; `net.core.rmem_max` here is
   212 992, so the kernel grants ~0.4 MiB. Raising it needs root, so the receive
   loop only copies bytes during the step and parses afterwards.
4. **Any unknown identifier counted as CAN.** At overload the transmit path emits
   mis-framed datagrams whose 9-byte slices decode to arbitrary identifiers.
   Filing those as CAN inflated `can_unique` past `can_sent` and produced
   *negative* CAN loss. CAN is matched on `CAN_IDENTIFIER` exactly; anything else
   unknown lands in a `foreign` column.

## Two limits that are not the board

- **148 810 pps.** A 9-byte UDP payload is an 84-byte Ethernet frame on the
  wire, so that is all a 100 Mbit link carries. `enp2s0` is 1 Gbit while both
  W5500s are 100 Mbit, so above that the *switch* discards. Those steps are run
  and plotted but flagged `over_wire_limit=1` and never used for a sustained
  rate.
- **`host_drops`.** The `/proc/net/udp` delta across each step. Non-zero means
  our own socket queue overflowed; the row is marked `valid=0` and excluded.

## The wedge

Overload closes the RX W5500 socket and it does not reopen; SW3 clears only the
interlock latch. Re-flashing resets the FPGA, which re-runs socket init, and that
does recover it -- verified. `run_campaign.py` reflashes automatically, records
the rate in `summary.json`, and stops that ladder rather than continuing to
produce rows from a dead board. Without this one wedge turns every later step
into a tidy 100 % loss and the ramp still yields a plausible-looking knee: see
`../results-excluded/`.

At `TX_BATCH_MAX_PACKETS = 1` the board wedges at 40 kpps. At 4, 16 and 28 it
runs to the wire limit without wedging.

## Provenance

`TX_BATCH_MAX_PACKETS` is a generic of `w5500_state_machine` with no entry in
either `generic map` in `top.vhd`, and `entity top` has no generic clause, so the
value in a bitstream is the entity default. yosys is elaborated with a bare
`-e top`, so there is no command-line override either: one rebuild per batch
size is the only way. `build_batch.sh` edits the default, builds, saves the
bitstream under `bitstreams/`, and flashes; `flash_saved.sh` re-flashes a saved
one *and* restores the matching source edit, so `meta.json` can never file a
bitstream under the wrong batch size.

A build takes about 115 s (synth + P&R + pack) on this machine.
