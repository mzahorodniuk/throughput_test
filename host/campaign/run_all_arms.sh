#!/usr/bin/env bash
# Re-measure every batch arm from its saved bitstream, with one harness version.
#
# Order is descending so the arm already on the board (the one just built) runs
# first without a reflash. Each arm reflashes from bitstreams/batch<N>.bit and
# puts the HDL generic back in step, so meta.json records the batch size that
# actually produced the numbers.
set -uo pipefail
cd "$(dirname "$0")"
for N in "$@"; do
  echo "############ batch $N ############"
  cur=$(grep -oP 'TX_BATCH_MAX_PACKETS\s*:\s*integer\s*:=\s*\K\d+' \
        /home/misha/FPGA-centric-SCIS/hardware/hdl/w5500/w5500_state_machine.vhd | head -1)
  want_md5=$(md5sum "bitstreams/batch$N.bit" 2>/dev/null | cut -d' ' -f1)
  have_md5=$(md5sum /home/misha/FPGA-centric-SCIS/hardware/fpga/ccgm1a1/bitstream.bit | cut -d' ' -f1)
  if [ "$cur" != "$N" ] || [ "$want_md5" != "$have_md5" ]; then
    ./flash_saved.sh "$N" || { echo "flash failed for $N"; continue; }
  else
    echo "batch $N already on the board (md5 $have_md5)"
  fi
  python3 -u run_campaign.py --tag "batch$N" --suite all \
      --duration 3 --can-duration 5 --can-rate 1400 > "run_batch$N.log" 2>&1
  grep -A12 "=== knees" "run_batch$N.log" || tail -5 "run_batch$N.log"
done
echo "############ all arms done ############"
