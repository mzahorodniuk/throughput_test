# Superseded arms (before the CAN-attribution fix)

These three arms are correct for every Ethernet number and for the CAN-only
suite, but their **combined-arm CAN columns are not trustworthy**.

The receiver filed every identifier that was not one of the harness's own
`WIDE_BAND_IDS` as a CAN arrival. At overload the transmit path emits mis-framed
datagrams whose 9-byte slices decode to arbitrary identifiers, and those were
counted as CAN frames. The giveaway is in the tables: combined CAN "loss" of
**-14.8 %** on the batch-1 arm. Loss cannot be negative, so `can_unique` had been
inflated past `can_sent` -- the attribution was wrong, not the board generous.

`hiload.py` now counts a record as CAN only when its identifier is exactly
`CAN_IDENTIFIER` (0x0080 = CAN ID 2 << 6) and counts anything else in a new
`foreign` column. All four arms were then re-measured from the saved bitstreams
so the published set comes from one version of the harness.

Kept for comparison, not for citation.
