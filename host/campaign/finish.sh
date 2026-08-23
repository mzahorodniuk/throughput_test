#!/usr/bin/env bash
# Post-processing, once every arm has been measured.
#   1. the simulation sweep (CPU only, board idle -- safe to run after)
#   2. repeatability on batch 16, which needs that bitstream back on the board
#   3. figures and the report
set -uo pipefail
cd "$(dirname "$0")"

echo "=== 1/4 simulation sweep (backpressure livelock) ==="
python3 /home/misha/thrpt_test/sim/sweep_backpressure.py \
     2>&1 | tail -35

echo
echo "=== 2/4 repeatability, batch 16 ==="
./flash_saved.sh 16 >/dev/null 2>&1 || echo "  (reflash failed)"
sleep 3
python3 -u repeatability.py --tag batch16 --reps 5 2>&1 | tail -12

echo
echo "=== 3/4 figures ==="
/home/misha/FPGA-centric-SCIS/.venv/bin/python /home/misha/thrpt_test/figures/plot_campaign.py

echo
echo "=== 4/4 report ==="
/home/misha/FPGA-centric-SCIS/.venv/bin/python /home/misha/thrpt_test/figures/make_report.py
echo "FINISH DONE"
