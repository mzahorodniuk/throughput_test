#!/usr/bin/env bash
# Build, archive and flash one bitstream for a given TX_BATCH_MAX_PACKETS.
#
# TX_BATCH_MAX_PACKETS is a generic of w5500_state_machine with no generic map
# entry at either instance in top.vhd, and `entity top` has no generic clause at
# all -- so the value that reaches a bitstream is the entity default, and it can
# only be changed by editing it. yosys is elaborated with a bare `-e top` here,
# so there is no command-line override either. One rebuild per batch size is
# therefore not a choice, it is the only way.
#
# N * 9 must stay below minimum_free_tx_buffer_memory (256), so 28 is the
# largest legal value.
#
#   ./build_batch.sh 4          build, flash, leave the source edited
#   ./build_batch.sh 16 restore restore the file afterwards
set -euo pipefail

N="${1:?usage: build_batch.sh <batch> }"
FPGA=/home/misha/FPGA-centric-SCIS/hardware/fpga/ccgm1a1
FSM=/home/misha/FPGA-centric-SCIS/hardware/hdl/w5500/w5500_state_machine.vhd
KEEP=/home/misha/thrpt_test/host/campaign/bitstreams
mkdir -p "$KEEP"

if [ "$N" -lt 1 ] || [ "$N" -gt 28 ]; then
  echo "batch must be 1..28 (N*9 < 256)"; exit 1
fi

cp -n "$FSM" "$FSM.campaign-orig"

cur=$(grep -oP 'TX_BATCH_MAX_PACKETS\s*:\s*integer\s*:=\s*\K\d+' "$FSM" | head -1)
echo "== TX_BATCH_MAX_PACKETS: $cur -> $N =="
sed -i -E "s/(TX_BATCH_MAX_PACKETS\s*:\s*integer\s*:=\s*)[0-9]+/\1$N/" "$FSM"
grep -n "TX_BATCH_MAX_PACKETS : integer" "$FSM"

cd "$FPGA"
source /opt/oss-cad-suite/environment

t0=$(date +%s)
echo "== synth =="; make synth_w5500_and_dc >"/tmp/build_b$N.synth.log" 2>&1 \
  || { echo "SYNTH FAILED"; tail -25 "/tmp/build_b$N.synth.log"; exit 1; }
echo "== pnr ==";   make impl            >"/tmp/build_b$N.impl.log"  2>&1 \
  || { echo "PNR FAILED";   tail -25 "/tmp/build_b$N.impl.log";  exit 1; }
echo "== pack ==";  make gmpack          >"/tmp/build_b$N.pack.log"  2>&1 \
  || { echo "PACK FAILED";  tail -25 "/tmp/build_b$N.pack.log";  exit 1; }
t1=$(date +%s)

cp bitstream.bit       "$KEEP/batch$N.bit"
cp implementation.txt  "$KEEP/batch$N.implementation.txt"
echo "== built in $((t1-t0)) s, md5 $(md5sum bitstream.bit | cut -d' ' -f1) =="
grep -iE "Device utilisation|CPE|BRAM" "/tmp/build_b$N.impl.log" | head -12 || true

echo "== flash =="
make upload_spi >"/tmp/build_b$N.flash.log" 2>&1 \
  || { echo "FLASH FAILED"; tail -25 "/tmp/build_b$N.flash.log"; exit 1; }
sleep 3
echo "== board check =="
for i in 1 2 3 4 5 6 7 8 9 10; do
  if ping -c1 -W1 192.168.2.100 >/dev/null 2>&1 && ping -c1 -W1 192.168.2.101 >/dev/null 2>&1; then
    echo "both W5500s answer ping after ${i}s"; break
  fi
  sleep 1
done

if [ "${2:-}" = "restore" ]; then
  cp "$FSM.campaign-orig" "$FSM"; echo "== source restored =="
fi
