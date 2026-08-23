#!/usr/bin/env bash
# Re-flash an already-built bitstream from bitstreams/ and put the HDL generic
# back in step with it.
#
# The generic has to be set too, not just the bitstream: meta.py records the
# batch size by reading w5500_state_machine.vhd, so a bitstream flashed without
# the matching source edit would be filed under the wrong batch size -- exactly
# the provenance failure the loose bitstream.bit.batch16 / .batch1fix files in
# the repo already suffer from.
set -euo pipefail
N="${1:?usage: flash_saved.sh <batch>}"
FPGA=/home/misha/FPGA-centric-SCIS/hardware/fpga/ccgm1a1
FSM=/home/misha/FPGA-centric-SCIS/hardware/hdl/w5500/w5500_state_machine.vhd
KEEP=/home/misha/thrpt_test/host/campaign/bitstreams
SRC="$KEEP/batch$N.bit"
test -f "$SRC" || { echo "no saved bitstream $SRC"; exit 1; }

sed -i -E "s/(TX_BATCH_MAX_PACKETS\s*:\s*integer\s*:=\s*)[0-9]+/\1$N/" "$FSM"
cp "$SRC" "$FPGA/bitstream.bit"
test -f "$KEEP/batch$N.implementation.txt" && \
  cp "$KEEP/batch$N.implementation.txt" "$FPGA/implementation.txt"
echo "flashing batch$N  md5 $(md5sum "$SRC" | cut -d' ' -f1)"
cd "$FPGA"
source /opt/oss-cad-suite/environment
make upload_spi >/tmp/flash_b$N.log 2>&1 || { tail -20 /tmp/flash_b$N.log; exit 1; }
sleep 4
for i in $(seq 1 10); do
  ping -c1 -W1 192.168.2.100 >/dev/null 2>&1 && ping -c1 -W1 192.168.2.101 >/dev/null 2>&1 \
    && { echo "both W5500s answer after ${i}s"; exit 0; }
  sleep 1
done
echo "WARNING: board did not answer ping"
