# Excluded runs

## batch1-wedged-firstattempt

The first batch-1 arm, run before the harness could recover from a wedge. The
board's RX W5500 socket closed at 40 000 pps on the very first plan and did not
reopen, so every step after that -- the two single-socket plans, all of CAN, all
three combined plans -- reported a tidy 100 % loss against a dead board.

Kept because the wedge itself is a real observation (batch 1 wedges at 40 kpps
where batch 16 survives 148 kpps), but not usable for any rate: the rows after
the wedge measure a board that was not running.

`run_campaign.py` now reflashes and continues, and records the wedge rate in
`summary.json` instead of silently producing a ladder from a dead board.
