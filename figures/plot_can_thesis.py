#!/usr/bin/env python3
"""The two CAN figures for the thesis chapter, from host/campaign/results-canfocus.

Same house style as the floorplan and campaign figures (style.py). The colour
adjacencies these two panels actually place together were run through
cvd_check.py: blue/orange 32.2, verm/orange 12.4, blue/purple 12.1,
purple/verm 16.2, and every data colour against the #555555 reference ink
clears 15.0 -- all pass on both the normal-vision floor and the CVD floor.

    ./plot_can_thesis.py [outdir]      # default: the thesis bilder/hauptteil
"""
import collections
import csv
import os
import statistics as st
import sys

import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from style import CB, INK, INK2, axes_style, legend, save, title  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "..", "host", "campaign", "results-canfocus", "steps.csv")
DEFAULT_OUT = "/home/misha/research/bilder/hauptteil"

# Longest a standard 11-bit-ID, 4-data-byte frame can be on a 125 kbit/s bus,
# averaged over the payloads this generator sends: 85.85 bits including stuff
# bits and the 3-bit intermission -> 1456 frames/s. Derived in can_bus_limit().
BUS_LIMIT_FPS = 1456.0

REF = INK2                       # reference lines: clears every data colour

# 6.30 in is the thesis text width (455.24 pt), so \includegraphics[width=\textwidth]
# neither shrinks nor stretches the figure and 7 pt type stays 7 pt on the page.
FIGSIZE = (6.30, 2.45)


def load(path):
    with open(path) as fh:
        return list(csv.DictReader(fh))


def by_rung(rows, key):
    """Group repeats of one rung, keyed by the commanded rate."""
    out = collections.defaultdict(list)
    for r in rows:
        out[int(r["requested_pps"])].append(float(r[key]) if r[key] else 0.0)
    return out


def fig_ladder(rows, outdir):
    """CAN on its own: what gets through, and where the rest goes."""
    can = [r for r in rows if r["arm"] == "can"]
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=FIGSIZE)

    # -- (a) delivered and carried against what the source offered -----------
    off = by_rung(can, "attempted_fps")
    dv = by_rung(can, "delivered_fps")
    cr = by_rung(can, "carried_fps")
    rungs = sorted(off)
    x = [st.median(off[k]) for k in rungs]

    lo, hi = 0, 1750
    ax1.plot([lo, hi], [lo, hi], ls=":", lw=0.9, color=REF, zorder=1)
    ax1.axhline(BUS_LIMIT_FPS, ls="--", lw=0.8, color=REF, zorder=1)
    ax1.annotate(f"most a 125 kbit/s bus can\ncarry with these frames:\n"
                 f"{BUS_LIMIT_FPS:,.0f} frames/s (calculated)",
                 xy=(40, BUS_LIMIT_FPS - 45), fontsize=6.0, color=REF,
                 va="top", linespacing=1.35)
    ax1.annotate("1:1 — everything\noffered arrives", xy=(700, 560),
                 fontsize=6.0, color=REF, ha="left", va="top",
                 linespacing=1.35)

    # blue sits under purple and is drawn wider, so the stretch where the two
    # coincide reads as one line with a halo rather than as a missing series.
    for k, xi in zip(rungs, x):
        ax1.plot([xi] * len(cr[k]), cr[k], marker="o", ms=3.4, ls="none",
                 mfc="none", mec=CB["blue"], mew=0.7, zorder=3)
        ax1.plot([xi] * len(dv[k]), dv[k], marker="D", ms=2.2, ls="none",
                 color=CB["purple"], zorder=4)
    ax1.plot(x, [st.median(cr[k]) for k in rungs], lw=2.2, color=CB["blue"],
             marker="o", ms=4.6, mfc="white", mew=1.0, zorder=3,
             label="reached the bus")
    ax1.plot(x, [st.median(dv[k]) for k in rungs], lw=1.0, color=CB["purple"],
             marker="D", ms=2.6, zorder=4, label="delivered to the host")

    ax1.set_xlim(lo, hi)
    ax1.set_ylim(0, hi)
    ax1.set_xlabel("offered at the source (frames/s)")
    ax1.set_ylabel("frames/s")
    title(ax1, "(a) CAN alone: delivered against offered")
    axes_style(ax1)
    legend(ax1, loc="lower right", bbox_to_anchor=(1.0, 0.02))

    # -- (b) the two ways a frame goes missing -------------------------------
    # Both shares are taken over the same denominator -- everything the source
    # tried to send -- so the two curves add up.
    att = {k: [] for k in rungs}
    lost = {k: [] for k in rungs}
    for r in can:
        k = int(r["requested_pps"])
        tried = int(r["can_sent"]) + int(r["can_failed"])
        att[k].append(100.0 * int(r["can_failed"]) / tried)
        lost[k].append(100.0 * int(r["lost"]) / tried)

    for k, xi in zip(rungs, x):
        ax2.plot([xi] * len(att[k]), att[k], marker="s", ms=2.4, ls="none",
                 color=CB["orange"], zorder=3)
        ax2.plot([xi] * len(lost[k]), lost[k], marker="o", ms=2.4, ls="none",
                 color=CB["verm"], zorder=4)
    ax2.plot(x, [st.median(att[k]) for k in rungs], lw=1.0, color=CB["orange"],
             marker="s", ms=3.4, zorder=3,
             label="never reached the bus\n(source mailboxes full)")
    ax2.plot(x, [st.median(lost[k]) for k in rungs], lw=1.2, color=CB["verm"],
             marker="o", ms=3.4, zorder=4,
             label="reached the bus, never arrived")

    clean = max(st.median(off[k]) for k in rungs
                if max(lost[k]) == 0 and max(att[k]) == 0)
    ax2.axvline(clean, ls="--", lw=0.8, color=REF, zorder=1)
    ax2.annotate("last fully clean step", xy=(clean - 22, 1.0), fontsize=6.0,
                 color=REF, rotation=90, ha="center", va="bottom")

    ax2.set_xlim(1150, 1700)
    ax2.set_ylim(-0.9, 21.0)
    ax2.set_xlabel("offered at the source (frames/s)")
    ax2.set_ylabel("% of everything the source tried to send")
    title(ax2, "(b) where the missing frames go")
    axes_style(ax2)
    legend(ax2, loc="upper left", bbox_to_anchor=(0.02, 1.0))

    fig.tight_layout(pad=0.4, w_pad=1.6)
    return save(fig, "can_throughput_ladder", outdir, web=False)


def fig_shared(rows, outdir):
    """CAN and Ethernet on the same return chip, at matched offered rates."""
    both = [r for r in rows if r["arm"] == "both"]
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=FIGSIZE)

    rungs = sorted({int(r["requested_pps"]) for r in both} - {0})
    alone = {k: [] for k in rungs}
    withc = {k: [] for k in rungs}
    for r in both:
        k = int(r["requested_pps"])
        if k == 0:
            continue
        (withc if r["with_can"] == "1" else alone)[k].append(float(r["eth_loss_pct"]))

    # -- (a) does CAN cost the Ethernet path anything? -----------------------
    # Every repeat is drawn. Two repeats per rung is too few for a mean to hide
    # the scatter behind, and at 105k the scatter is the whole story.
    pos = list(range(len(rungs)))
    dx = 0.17
    for p, k in zip(pos, rungs):
        for a, b in zip(alone[k], withc[k]):          # one line per matched pair
            ax1.plot([p - dx, p + dx], [a, b], lw=0.7, color=REF, zorder=2)
        ax1.plot([p - dx] * len(alone[k]), alone[k], marker="o", ms=4.0,
                 ls="none", color=CB["blue"], zorder=4,
                 label="Ethernet alone" if p == 0 else None)
        ax1.plot([p + dx] * len(withc[k]), withc[k], marker="s", ms=3.6,
                 ls="none", color=CB["orange"], zorder=4,
                 label="with CAN at 1 428 frames/s" if p == 0 else None)
    ax1.set_xticks(pos)
    ax1.set_xticklabels([f"{k // 1000}k" for k in rungs])
    ax1.set_xlim(-0.5, len(rungs) - 0.5)
    ax1.set_xlabel("Ethernet offered (packets/s)")
    ax1.set_ylabel("Ethernet packet loss (%)")
    ax1.set_ylim(-0.012, 0.30)
    ax1.axhline(0.1, ls="--", lw=0.8, color=REF, zorder=1)
    ax1.annotate("0.1 % loss budget", xy=(-0.42, 0.105), fontsize=6.0,
                 color=REF, ha="left", va="bottom")
    d = [b - a for k in rungs for a, b in zip(alone[k], withc[k])]
    ax1.annotate(f"mean change when CAN is added:\n"
                 f"{st.mean(d):+.3f} pp (sd {st.stdev(d):.3f}, n = {len(d)})",
                 xy=(0.97, 0.72), xycoords="axes fraction", fontsize=6.0,
                 color=INK, ha="right", va="top", linespacing=1.35)
    title(ax1, "(a) what CAN costs the Ethernet path")
    axes_style(ax1, grid_axis="y")
    legend(ax1, loc="upper left", bbox_to_anchor=(0.0, 1.0))

    # -- (b) does Ethernet cost CAN anything? --------------------------------
    # Counted frames, not a percentage: these are single-digit numbers out of
    # ~6 400, and a percent axis makes six lost frames look like a trend.
    canrows = [r for r in both if r["with_can"] == "1"]
    xs = sorted({int(r["requested_pps"]) for r in canrows})
    per = {k: [int(r["lost"]) for r in canrows
               if int(r["requested_pps"]) == k] for k in xs}
    # Repeats are nudged apart on x: the counts are small integers, so two
    # repeats that both lost nothing would otherwise be one marker.
    for k in xs:
        n = len(per[k])
        for i, v in enumerate(per[k]):
            ax2.plot([k + (i - (n - 1) / 2) * 2600], [v], marker="D", ms=3.6,
                     ls="none", color=CB["purple"], zorder=4)

    ctl = [r for r in canrows if int(r["requested_pps"]) == 0]
    load = [r for r in canrows if int(r["requested_pps"]) > 0]
    csent = sum(int(r["can_sent"]) for r in ctl)
    cmiss = sum(int(r["lost"]) for r in ctl)
    lsent = sum(int(r["can_sent"]) for r in load)
    lmiss = sum(int(r["lost"]) for r in load)

    ax2.axvline(0, ls="--", lw=0.8, color=REF, zorder=1)
    ax2.annotate("no Ethernet", xy=(2600, 13.2), fontsize=6.0,
                 color=REF, va="top", linespacing=1.35)
    ax2.annotate(f"with Ethernet running: {lmiss} frames lost of {lsent:,} "
                 f"= {100 * lmiss / lsent:.3f} %\n"
                 f"with no Ethernet at all: {cmiss} of {csent:,} "
                 f"= {100 * cmiss / csent:.3f} %",
                 xy=(0.97, 0.97), xycoords="axes fraction", fontsize=6.0,
                 color=INK, ha="right", va="top", linespacing=1.4)
    ax2.set_xlabel("Ethernet offered (packets/s)")
    ax2.set_ylabel("CAN frames lost per 4.5 s step (of ~6 430)")
    ax2.set_ylim(-1.2, 20)
    ax2.set_xlim(-8000, 113000)
    ax2.set_yticks([0, 5, 10, 15, 20])
    ax2.xaxis.set_major_formatter(FuncFormatter(
        lambda v, p: "0" if v == 0 else f"{v / 1000:g}k"))
    title(ax2, "(b) what Ethernet costs the CAN path")
    axes_style(ax2)      # one series only -- the panel title names it

    fig.tight_layout(pad=0.4, w_pad=1.6)
    return save(fig, "can_shared_return_path", outdir, web=False)


def main():
    outdir = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_OUT
    rows = load(DATA)
    print(fig_ladder(rows, outdir))
    print(fig_shared(rows, outdir))


if __name__ == "__main__":
    main()
