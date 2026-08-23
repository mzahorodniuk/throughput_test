#!/usr/bin/env python3
"""Figures for the SCIS batching campaign.

    /home/misha/FPGA-centric-SCIS/.venv/bin/python plot_campaign.py

Every panel is one measure on one axis -- no twin axes anywhere, because the two
scales' alignment would be arbitrary and would invent a relationship that is not
in the data. Where two measures belong together they get two panels.

Rows the harness marked `valid=0` (our own socket queue overflowed) never
appear. Rows above the 100 Mbit wire limit are drawn, because what an
overloaded link does is part of the result, but they are shaded and excluded
from every sustained-rate number.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
from matplotlib.lines import Line2D
from matplotlib.ticker import FuncFormatter

import load
import style as st
from style import CB, INK, INK2, MUTED, SERIES, WIRE_LIMIT
import matplotlib.pyplot as plt

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")
W = 7.0

# Colour per batch arm. Fixed by batch size, so a colour keeps its meaning even
# if an arm is missing from a given figure.
BATCH_STYLE = {
    1:  dict(color=CB["verm"],   marker="v", label="batch 1 (no batching)"),
    4:  dict(color=CB["orange"], marker="s", label="batch 4"),
    8:  dict(color=CB["purple"], marker="D", label="batch 8"),
    16: dict(color=CB["blue"],   marker="o", label="batch 16 (default)"),
    28: dict(color=CB["green"],  marker="^", label="batch 28 (max legal)"),
}


def bstyle(b):
    return BATCH_STYLE.get(b, dict(color=CB["gray"], marker="x",
                                   label=f"batch {b}"))


def wire_band(ax, ymin=None):
    """Shade offered rates a 100 Mbit link cannot carry."""
    x0, x1 = ax.get_xlim()
    if x1 > WIRE_LIMIT:
        ax.axvspan(WIRE_LIMIT, x1, color=GREYBAND, zorder=0, lw=0)
    ax.axvline(WIRE_LIMIT, color=MUTED, lw=0.7, ls=(0, (3, 2)), zorder=1)
    ax.text(WIRE_LIMIT, ax.get_ylim()[1], " 100 Mbit\n wire limit",
            fontsize=5.8, color=INK2, va="top", ha="left")
    ax.set_xlim(x0, x1)


GREYBAND = "#F2F2F2"


def kax(ax):
    ax.xaxis.set_major_formatter(FuncFormatter(st.kfmt))
    ax.yaxis.set_major_formatter(FuncFormatter(st.kfmt))


# ---------------------------------------------------------------------------
def fig_eth_ramp(arms):
    """What batching buys, and what happens past the knee."""
    tags = sorted(arms.values(), key=lambda a: a["batch"] or 0)
    tags = [a for a in tags if load.series(a["rows"], "eth_spread")]
    if not tags:
        return None
    fig, axes = plt.subplots(1, 2, figsize=(W, 2.5))

    ax = axes[0]
    st.axes_style(ax)
    lim = 0
    for a in tags:
        s = load.series(a["rows"], "eth_spread")
        x = [r["achieved_pps"] for r in s]
        y = [r["delivered_pps"] for r in s]
        sy = bstyle(a["batch"])
        ax.plot(x, y, lw=1.4, ms=3.2, mew=0, zorder=3,
                color=sy["color"], marker=sy["marker"], label=sy["label"])
        lim = max(lim, max(x + y))
        k = load.knee(a["rows"], "eth_spread")
        if k:
            ax.plot([k["achieved_pps"]], [k["delivered_pps"]], marker="*",
                    ms=8, color=sy["color"], mec="white", mew=0.6, zorder=5)
        # A wedge is not a data point on a curve: the socket closed and stayed
        # closed, so the ladder stopped there. Saying so beats an unexplained
        # plunge to the axis.
        wr = a["summary"].get("eth_spread_wedged_at")
        if wr:
            wrow = [r for r in s if r["requested_pps"] == wr]
            if wrow:
                ax.plot([wrow[0]["achieved_pps"]], [wrow[0]["delivered_pps"]],
                        marker="X", ms=6, color=sy["color"], mec="white",
                        mew=0.6, zorder=6)
                ax.annotate("socket closed,\ndid not reopen",
                            (wrow[0]["achieved_pps"], wrow[0]["delivered_pps"]),
                            textcoords="offset points", xytext=(8, -2),
                            fontsize=5.8, color=sy["color"], va="top")
    ax.plot([0, lim], [0, lim], color=MUTED, lw=0.7, ls=":", zorder=1)
    ax.text(lim * 0.60, lim * 0.66, "no loss", fontsize=5.8, color=INK2,
            rotation=38, ha="center", va="center")
    ax.set_xlabel("offered load (packets/s)")
    ax.set_ylabel("delivered (packets/s)")
    kax(ax)
    st.title(ax, "(a) delivered vs offered, 8 sockets")
    wire_band(ax)
    # The two glyphs mean something, so they belong in the legend rather than in
    # a floating note that collides with the annotation next to the wedge.
    h, l = ax.get_legend_handles_labels()
    h += [Line2D([], [], ls="", marker="*", ms=7, color=CB["gray"],
                 label="sustained (last step under 0.1 % loss)"),
          Line2D([], [], ls="", marker="X", ms=5, color=CB["gray"],
                 label="RX socket closed permanently")]
    st.legend(ax, handles=h, loc="upper left")

    ax = axes[1]
    st.axes_style(ax)
    for a in tags:
        s = load.series(a["rows"], "eth_spread")
        x = [r["achieved_pps"] for r in s]
        # Plotted as measured. A clamped floor would draw a genuine 0.000 % step
        # at whatever the floor is, which reads as "a bit of loss" when the
        # honest statement is "none" -- symlog keeps zero at zero.
        y = [r["loss_pct"] for r in s]
        sy = bstyle(a["batch"])
        ax.plot(x, y, lw=1.4, ms=3.2, mew=0, zorder=3,
                color=sy["color"], marker=sy["marker"], label=sy["label"])
    ax.axhline(0.1, color=CB["verm"], lw=0.7, ls="--", zorder=2)
    ax.text(0.99, 0.1, "0.1 % loss budget ", fontsize=5.8, color=CB["verm"],
            va="bottom", ha="right", transform=ax.get_yaxis_transform())
    ax.set_yscale("symlog", linthresh=1e-3)
    ax.set_ylim(bottom=0)
    ax.set_xlabel("offered load (packets/s)")
    ax.set_ylabel("loss (%)")
    ax.xaxis.set_major_formatter(FuncFormatter(st.kfmt))
    st.title(ax, "(b) loss vs offered (0 is drawn at 0)")
    wire_band(ax)
    st.legend(ax, loc="upper left")
    fig.tight_layout(pad=0.4)
    return st.save(fig, "campaign_eth_ramp", OUT)


# ---------------------------------------------------------------------------
def fig_socket_placement(arms):
    """Does it still matter which socket the load lands on?"""
    ref = arms.get("batch16") or next(iter(arms.values()), None)
    if ref is None:
        return None
    fig, axes = plt.subplots(1, 2, figsize=(W, 2.5))
    ax = axes[0]
    st.axes_style(ax)
    lim = 0
    for key in ("spread", "p9217", "p9219"):
        s = load.series(ref["rows"], f"eth_{key}")
        if not s:
            continue
        x = [r["achieved_pps"] for r in s]
        y = [r["delivered_pps"] for r in s]
        sy = SERIES[key]
        ax.plot(x, y, lw=1.4, ms=3.2, mew=0, zorder=3, **sy)
        lim = max(lim, max(x + y))
    ax.plot([0, lim], [0, lim], color=MUTED, lw=0.7, ls=":", zorder=1)
    ax.set_xlabel("offered load (packets/s)")
    ax.set_ylabel("delivered (packets/s)")
    kax(ax)
    st.title(ax, f"(a) socket placement, batch {ref['batch']}")
    wire_band(ax)
    st.legend(ax, loc="upper left")

    ax = axes[1]
    st.axes_style(ax, grid_axis="y")
    keys, vals, cols = [], [], []
    for key in ("spread", "p9217", "p9219"):
        k = load.knee(ref["rows"], f"eth_{key}")
        if k:
            keys.append(SERIES[key]["label"].replace(" (", "\n("))
            vals.append(k["delivered_pps"])
            cols.append(SERIES[key]["color"])
    bars = ax.bar(range(len(vals)), vals, width=0.6, color=cols, zorder=3,
                  linewidth=0)
    for b, v in zip(bars, vals):
        ax.text(b.get_x() + b.get_width() / 2, v, f"{v/1000:.1f}k",
                ha="center", va="bottom", fontsize=6.4, color=INK)
    ax.set_xticks(range(len(keys)))
    ax.set_xticklabels(keys, fontsize=6.0)
    ax.set_ylabel("sustained (packets/s)")
    ax.yaxis.set_major_formatter(FuncFormatter(st.kfmt))
    ax.set_ylim(0, max(vals) * 1.22 if vals else 1)
    st.title(ax, "(b) sustained rate under 0.1 % loss")
    if len(vals) > 1:
        # Three near-equal bars are only worth drawing because "equal" is the
        # finding, so the spread is stated rather than left to the eye.
        spread = (max(vals) - min(vals)) / max(vals) * 100
        ax.annotate(f"spread between placements: {spread:.2f} %\n"
                    f"the ceiling is upstream of the arbiter",
                    (0.5, 0.97), xycoords="axes fraction", ha="center",
                    va="top", fontsize=6.0, color=INK2)
    fig.tight_layout(pad=0.4)
    return st.save(fig, "campaign_socket_placement", OUT)


# ---------------------------------------------------------------------------
def fig_batching_engages(arms):
    """Batching is load-driven: the achieved batch is not the configured one."""
    ref = arms.get("batch16") or next(iter(arms.values()), None)
    if ref is None:
        return None
    fig, axes = plt.subplots(1, 2, figsize=(W, 2.5))

    ax = axes[0]
    st.axes_style(ax)
    for key in ("spread", "p9217", "p9219"):
        s = load.series(ref["rows"], f"eth_{key}", in_wire=True)
        if not s:
            continue
        ax.plot([r["achieved_pps"] for r in s], [r["batch_mean"] for r in s],
                lw=1.4, ms=3.2, mew=0, zorder=3, **SERIES[key])
    if ref["batch"]:
        ax.axhline(ref["batch"], color=MUTED, lw=0.7, ls=(0, (3, 2)), zorder=2)
        ax.text(0.99, ref["batch"],
                f"TX_BATCH_MAX_PACKETS = {ref['batch']} ", fontsize=5.8,
                color=INK2, va="bottom", ha="right",
                transform=ax.get_yaxis_transform())
    ax.set_xlabel("offered load (packets/s)")
    ax.set_ylabel("metrics per datagram (mean)")
    ax.xaxis.set_major_formatter(FuncFormatter(st.kfmt))
    st.title(ax, "(a) achieved batch size")
    st.legend(ax, loc="upper left")

    ax = axes[1]
    st.axes_style(ax)
    for key in ("spread", "p9217", "p9219"):
        s = load.series(ref["rows"], f"eth_{key}", in_wire=True)
        if not s:
            continue
        el = [(r["datagrams"] / r["elapsed"]) if r["elapsed"] else 0 for r in s]
        ax.plot([r["achieved_pps"] for r in s], el, lw=1.4, ms=3.2, mew=0,
                zorder=3, **SERIES[key])
    ax.set_xlabel("offered load (packets/s)")
    ax.set_ylabel("datagrams/s on the return path")
    kax(ax)
    st.title(ax, "(b) datagram rate the TX chip must sustain")
    fig.tight_layout(pad=0.4)
    return st.save(fig, "campaign_batching", OUT)


# ---------------------------------------------------------------------------
def fig_batch_sweep(arms):
    """The headline: sustained rate against TX_BATCH_MAX_PACKETS."""
    have = sorted([a for a in arms.values() if a["batch"]],
                  key=lambda a: a["batch"])
    if len(have) < 2:
        return None
    fig, axes = plt.subplots(1, 2, figsize=(W, 2.6))

    ax = axes[0]
    st.axes_style(ax)
    groups = [("eth_spread", SERIES["spread"]),
              ("eth_p9217", SERIES["p9217"]),
              ("both_spread", dict(color=CB["purple"], marker="D",
                                   label="8 sockets + CAN 1400 f/s"))]
    for test, sy in groups:
        xs, ys = [], []
        for a in have:
            k = load.knee(a["rows"], test)
            if k:
                xs.append(a["batch"])
                ys.append(k["delivered_pps"])
        if xs:
            ax.plot(xs, ys, lw=1.4, ms=3.6, mew=0, zorder=3, **sy)
            # Only the reference series is labelled: three series x four batch
            # sizes puts two labels on top of each other at both ends.
            if test == "eth_spread":
                for x, y in zip(xs, ys):
                    ax.annotate(f"{y/1000:.0f}k", (x, y),
                                textcoords="offset points", xytext=(0, 6),
                                ha="center", fontsize=5.8, color=INK)
    ax.axhline(WIRE_LIMIT, color=MUTED, lw=0.7, ls=(0, (3, 2)), zorder=2)
    ax.text(ax.get_xlim()[0], WIRE_LIMIT, " 100 Mbit wire limit",
            fontsize=5.8, color=INK2, va="bottom")
    ax.set_xlabel("TX_BATCH_MAX_PACKETS")
    ax.set_ylabel("sustained (packets/s)")
    ax.set_xticks([a["batch"] for a in have])
    ax.yaxis.set_major_formatter(FuncFormatter(st.kfmt))
    ax.set_ylim(0, WIRE_LIMIT * 1.12)
    st.title(ax, "(a) sustained rate vs batch size")
    st.legend(ax, loc="lower right")

    # SPI cost model: one datagram costs a fixed number of SPI transactions
    # whatever it carries, so the per-metric cost falls as 1/N.
    ax = axes[1]
    st.axes_style(ax)
    xs, ys = [], []
    for a in have:
        k = load.knee(a["rows"], "eth_spread")
        if k:
            xs.append(a["batch"])
            ys.append(1e6 / k["delivered_pps"])
    if xs:
        n = np.array(xs, dtype=float)
        # A datagram costs a fixed SPI sequence whatever it carries, so the
        # per-metric cost should be t_fixed/N + t_per. Least squares on 1/N.
        A = np.vstack([1.0 / n, np.ones_like(n)]).T
        coef, *_ = np.linalg.lstsq(A, np.array(ys), rcond=None)
        grid = np.linspace(min(xs), max(xs), 200)
        pred = coef[0] / n + coef[1]
        resid = float(np.max(np.abs(pred - np.array(ys))))
        ax.plot(grid, coef[0] / grid + coef[1], lw=1.2, color=CB["orange"],
                zorder=2,
                label=f"{coef[0]:.1f} us/datagram / N + {coef[1]:.2f} us/metric")
        ax.plot(xs, ys, ls="", ms=4.4, mew=0, color=CB["blue"], marker="o",
                zorder=4, label="measured")
        ax.set_xticks(xs)
        ax.annotate(f"worst residual {resid:.2f} us", (0.98, 0.62),
                    xycoords="axes fraction", ha="right", fontsize=5.8,
                    color=INK2)
        ax.set_xlabel("TX_BATCH_MAX_PACKETS")
        ax.set_ylabel("cost per metric (us)")
        st.title(ax, "(b) per-metric cost, and the 1/N fit")
        st.legend(ax, loc="upper right")
    fig.tight_layout(pad=0.4)
    return st.save(fig, "campaign_batch_sweep", OUT)


# ---------------------------------------------------------------------------
def fig_can(arms):
    """CAN: is the limit the bus or the FPGA?

    The FPGA is the device under test; the Nucleo is only a traffic source. Its
    `failed` counter is frames that never reached the bus because its own TX
    mailboxes were full -- our limit, not the board's. Loss with failed == 0 is
    the only thing that indicts the FPGA, so the two are drawn together.
    """
    have = [a for a in arms.values() if load.series(a["rows"], "can")]
    if not have:
        return None
    have = sorted(have, key=lambda a: a["batch"] or 0)
    fig, axes = plt.subplots(1, 2, figsize=(W, 2.5))

    ax = axes[0]
    st.axes_style(ax)
    lim, best = 0, 0
    for a in have:
        s = load.series(a["rows"], "can")
        x = [r["requested_pps"] for r in s]
        y = [r["delivered_pps"] for r in s]
        sy = bstyle(a["batch"])
        ax.plot(x, y, lw=1.4, ms=3.2, mew=0, zorder=3,
                color=sy["color"], marker=sy["marker"], label=sy["label"])
        lim = max(lim, max(x + y))
        clean = [r["delivered_pps"] for r in s if not (r["can_failed"] or 0)]
        best = max(best, max(clean) if clean else 0)
    ax.plot([0, lim], [0, lim], color=MUTED, lw=0.7, ls=":", zorder=1)
    if best:
        ax.axhline(best, color=MUTED, lw=0.7, ls=(0, (3, 2)), zorder=2)
        ax.annotate(f"highest rate the 125 kbit/s bus carried\n"
                    f"with nothing dropped: {best:,.0f} frames/s",
                    (0.03, best), xycoords=ax.get_yaxis_transform(),
                    xytext=(4, -4), textcoords="offset points",
                    fontsize=5.8, color=INK2, va="top")
    ax.set_xlabel("offered (frames/s)")
    ax.set_ylabel("delivered (frames/s)")
    st.title(ax, "(a) CAN only, 125 kbit/s bus")
    st.legend(ax, loc="upper left")

    # Linear, not log: the story is "exactly zero until the source saturates,
    # then a jump". A log axis renders the zeros as a floor and invites the
    # reader to see structure in them.
    ax = axes[1]
    st.axes_style(ax)
    for a in have:
        s = load.series(a["rows"], "can")
        sy = bstyle(a["batch"])
        x = [r["requested_pps"] for r in s]
        ax.plot(x, [r["loss_pct"] or 0 for r in s], lw=1.4, ms=3.2, mew=0,
                zorder=3, color=sy["color"], marker=sy["marker"],
                label=sy["label"])
        ax.plot(x, [100.0 * (r["can_failed"] or 0) /
                    max(1.0, (r["can_sent"] or 0) + (r["can_failed"] or 0))
                    for r in s],
                lw=1.1, ls=(0, (4, 2)), color=sy["color"], zorder=2)
    ax.set_xlabel("offered (frames/s)")
    ax.set_ylabel("% of frames")
    st.title(ax, "(b) frames lost, and frames never sent")
    # One entry per arm plus one for what the dash means -- not one dashed
    # entry per arm, which is the same statement repeated.
    h, l = ax.get_legend_handles_labels()
    h += [Line2D([], [], color=CB["gray"], lw=1.4, label="not delivered (the FPGA)"),
          Line2D([], [], color=CB["gray"], lw=1.1, ls=(0, (4, 2)),
                 label="STM32 mailboxes full (not the FPGA)")]
    st.legend(ax, handles=h, loc="upper left")
    fig.tight_layout(pad=0.4)
    return st.save(fig, "campaign_can", OUT)


# ---------------------------------------------------------------------------
def fig_combined(arms):
    """What sharing the TX chip with CAN costs Ethernet, and vice versa."""
    ref = arms.get("batch16") or next(iter(arms.values()), None)
    if ref is None or not load.series(ref["rows"], "both_spread"):
        return None
    fig, axes = plt.subplots(1, 2, figsize=(W, 2.5))

    ax = axes[0]
    st.axes_style(ax, grid_axis="y")
    keys = ["spread", "p9217", "p9219"]
    alone, withcan, labels, cols = [], [], [], []
    for key in keys:
        a1 = load.knee(ref["rows"], f"eth_{key}")
        a2 = load.knee(ref["rows"], f"both_{key}")
        if a1 and a2:
            alone.append(a1["delivered_pps"])
            withcan.append(a2["delivered_pps"])
            labels.append(SERIES[key]["label"].replace(" (", "\n("))
            cols.append(SERIES[key]["color"])
    if alone:
        idx = np.arange(len(alone))
        wd = 0.34
        # Same hue per configuration (colour follows the entity); the hatch,
        # not a new hue, marks the with-CAN case.
        ax.bar(idx - wd / 2 - 0.01, alone, wd, color=cols, zorder=3, lw=0,
               label="Ethernet alone")
        ax.bar(idx + wd / 2 + 0.01, withcan, wd, color=cols, zorder=3, lw=0,
               hatch="////", edgecolor="white", label="with CAN 1400 frames/s")
        for i, (a, b) in enumerate(zip(alone, withcan)):
            # "-0.0k" reads as a rounding artefact; say what it means instead.
            lbl = "no cost" if abs(a - b) < 500 else f"-{(a-b)/1000:.1f}k"
            ax.annotate(lbl, (i, max(a, b)), textcoords="offset points",
                        xytext=(0, 4), ha="center", fontsize=6.0, color=INK)
        ax.set_xticks(idx)
        ax.set_xticklabels(labels, fontsize=6.0)
        ax.set_ylabel("sustained (packets/s)")
        ax.yaxis.set_major_formatter(FuncFormatter(st.kfmt))
        ax.set_ylim(0, max(alone) * 1.2)
        st.title(ax, f"(a) cost of adding CAN, batch {ref['batch']}")
        h = [Line2D([], [], marker="s", ls="", ms=4, mfc=CB["gray"], mec="none",
                    label="Ethernet alone"),
             Line2D([], [], marker="s", ls="", ms=4, mfc="white",
                    mec=CB["gray"], mew=0.8, label="with CAN 1400 frames/s")]
        st.legend(ax, handles=h, loc="lower right")

    # Linear, and plotted as measured. Once CAN is matched on its own
    # identifier these values are exact zeros for most of the ladder, and a log
    # axis would render a column of genuine zeros as a floor with visible
    # structure in it.
    ax = axes[1]
    st.axes_style(ax)
    worst = 0.0
    for key in keys:
        s = load.series(ref["rows"], f"both_{key}", in_wire=True)
        if not s:
            continue
        y = [r["can_loss_pct"] or 0 for r in s]
        worst = max(worst, max(y) if y else 0)
        ax.plot([r["achieved_pps"] for r in s], y,
                lw=1.4, ms=3.2, mew=0, zorder=3, **SERIES[key])
    # The comparison that gives the panel its meaning: how much Ethernet was
    # losing at the same time.
    s = load.series(ref["rows"], "both_spread", in_wire=True)
    if s:
        ax.plot([r["achieved_pps"] for r in s], [r["loss_pct"] for r in s],
                lw=1.1, ls=(0, (4, 2)), color=CB["gray"], zorder=2,
                label="Ethernet loss, 8 sockets (for scale)")
    ax.set_xlabel("Ethernet offered load (packets/s)")
    ax.set_ylabel("loss (%)")
    ax.set_ylim(-1, max(6.0, worst * 1.4))
    ax.xaxis.set_major_formatter(FuncFormatter(st.kfmt))
    ax.annotate("Ethernet is collapsing here", (0.98, 0.86),
                xycoords="axes fraction", ha="right", fontsize=5.8, color=INK2)
    st.title(ax, f"(b) CAN loss peaks at {worst:.3f} % whatever Ethernet does")
    st.legend(ax, loc="upper left")
    fig.tight_layout(pad=0.4)
    return st.save(fig, "campaign_combined", OUT)


# ---------------------------------------------------------------------------
def fig_livelock():
    """Simulation: the transmit-buffer livelock and how far away it is."""
    rows = load.sim_rows()
    if not rows:
        return None
    fig, axg = plt.subplots(2, 2, figsize=(W, 4.5))
    axes = [axg[0][0], axg[0][1], axg[1][0], axg[1][1]]

    # (a) how slow the wire has to get
    ax = axes[0]
    st.axes_style(ax)
    w = sorted([r for r in rows if r["axis"] == "wire"], key=lambda r: r["wire_ns"])
    x = [r["wire_ns"] / 1000.0 for r in w]
    y = [max(r["fsr_span_us"], 1e-2) for r in w]
    ok = [i for i, r in enumerate(w) if not r["livelock"]]
    bad = [i for i, r in enumerate(w) if r["livelock"]]
    ax.plot(x, y, lw=1.4, color=CB["sky"], zorder=3)
    ax.plot([x[i] for i in ok], [y[i] for i in ok], ls="", marker="o", ms=3.4,
            color=CB["sky"], zorder=4, label="serves other work")
    ax.plot([x[i] for i in bad], [y[i] for i in bad], ls="", marker="X", ms=4.6,
            color=CB["verm"], zorder=4, label="livelocked")
    wd = 65535 * (1 / 30e6) * 1e6          # WATCHDOG_CLKS at 30 MHz, in us
    ax.axhline(wd, color=CB["verm"], lw=0.8, ls="--", zorder=2)
    ax.text(x[0], wd * 1.15, " watchdog threshold (2.18 ms)", fontsize=5.6,
            color=CB["verm"])
    ax.axvline(5.0, color=MUTED, lw=0.7, ls=(0, (3, 2)), zorder=2)
    ax.annotate("100 Mbit\n(as built)", xy=(5.0, y[0]),
                xytext=(9, 14), textcoords="offset points", fontsize=5.6,
                color=INK2, ha="left", va="bottom")
    ax.set_xscale("log"); ax.set_yscale("log")
    ax.set_xlabel("chip wire time per datagram (us)")
    ax.set_ylabel("longest stall (us)")
    st.title(ax, "(a) how slow the link must get")
    st.legend(ax, loc="upper left")

    # (b) batch size changes the exposure
    ax = axes[1]
    st.axes_style(ax)
    b = sorted([r for r in rows if r["axis"] == "batch"], key=lambda r: r["batch"])
    if b:
        ax.plot([r["batch"] for r in b], [r["fsr_span_us"] / 1000.0 for r in b],
                lw=1.4, ms=3.6, mew=0, color=CB["blue"], marker="o", zorder=3)
        for r in b:
            ax.annotate(f"{r['min_free_b']} B free", (r["batch"],
                        r["fsr_span_us"] / 1000.0),
                        textcoords="offset points", xytext=(3, 3), fontsize=5.4,
                        color=INK2)
        ax.axhline(wd / 1000.0, color=CB["verm"], lw=0.8, ls="--", zorder=2)
        ax.set_xticks([r["batch"] for r in b])
        ax.set_xlim(min(r["batch"] for r in b) - 1.5,
                    max(r["batch"] for r in b) + 5.5)   # room for the last label
        ax.set_xlabel("TX_BATCH_MAX_PACKETS")
        ax.set_ylabel("longest stall (ms)")
        st.title(ax, "(b) bigger batches stall less")

    # (c) why no existing testbench found it
    ax = axes[2]
    st.axes_style(ax, grid_axis="y")
    dm = {r["drain"]: r for r in rows if r["axis"] == "drain_model"}
    tb = [r for r in rows if r["axis"] == "txbuf" and r["txbuf"] == 256]
    names, vals, cols, notes = [], [], [], []
    if 0 in dm:
        names.append("buffer freed\non SEND\n(tb_w5500_tx)")
        # A real zero cannot be drawn on a log axis; the bar sits on the floor
        # and the label says what the value actually is.
        vals.append(max(dm[0]["fsr_span_us"] / 1000.0, 1e-3))
        cols.append(CB["gray"]); notes.append("no stall (0)")
    if 1 in dm:
        names.append("freed as the\nwire drains\n(the chip)")
        vals.append(dm[1]["fsr_span_us"] / 1000.0)
        cols.append(CB["verm"]); notes.append("livelock")
    if tb:
        names.append("256 B buffer\n(free never\nexceeds 256)")
        vals.append(tb[0]["fsr_span_us"] / 1000.0)
        cols.append(CB["verm"]); notes.append("never sends")
    if vals:
        bars = ax.bar(range(len(vals)), vals, width=0.6, color=cols, lw=0,
                      zorder=3)
        for bb, v, nt in zip(bars, vals, notes):
            ax.text(bb.get_x() + bb.get_width() / 2, v, f"{nt}\n",
                    ha="center", va="bottom", fontsize=5.8, color=INK)
        ax.set_yscale("log")
        ax.set_ylim(5e-4, max(vals) * 12)          # headroom for the top label
        ax.set_xticks(range(len(names)))
        ax.set_xticklabels(names, fontsize=5.6)
        ax.set_ylabel("longest stall (ms)")
        ax.axhline(wd / 1000.0, color=CB["verm"], lw=0.8, ls="--", zorder=2)
        ax.text(0.01, wd / 1000.0, " watchdog", fontsize=5.6, color=CB["verm"],
                va="bottom", ha="left", transform=ax.get_yaxis_transform())
        st.title(ax, "(c) chip model decides the outcome")

    # (d) The case the claim is actually about: socket 0 driven into
    # backpressure while socket 1 has traffic waiting. Even feeding cannot show
    # it -- spreading halves the pressure on each socket's own buffer and the
    # stall never starts -- so this is the asymmetric run.
    ax = axes[3]
    st.axes_style(ax, grid_axis="y")
    hot = sorted([r for r in rows if r["axis"] == "hot_socket"],
                 key=lambda r: r["hot_every"] or 0)
    even = next((r for r in rows if r["axis"] == "sockets" and r["fed_sockets"] == 2),
                None)
    cfg = ([("even split\n(1 in 2)", even)] if even else []) + \
          [(f"socket 0 hot\n(1 in {r['hot_every']})", r) for r in hot]
    if cfg:
        idx = np.arange(len(cfg))
        wd = 0.36
        s0 = [(r["dg_socket0"] or 0) for _, r in cfg]
        s1 = [(r["dg_socket1"] or 0) for _, r in cfg]
        ax.bar(idx - wd / 2 - .01, s0, wd, color=CB["verm"], lw=0, zorder=3,
               label="socket 0 (the hot one)")
        ax.bar(idx + wd / 2 + .01, s1, wd, color=CB["sky"], lw=0, zorder=3,
               label="socket 1 (waiting behind it)")
        for i, (_, r) in enumerate(cfg):
            ax.annotate("livelock" if r["livelock"] else "no livelock",
                        (i, max(s0[i], s1[i])), textcoords="offset points",
                        xytext=(0, 4), ha="center", fontsize=5.8,
                        color=CB["verm"] if r["livelock"] else CB["green"])
        ax.set_xticks(idx)
        ax.set_xticklabels([n for n, _ in cfg], fontsize=5.8)
        ax.set_ylabel("datagrams sent in the run")
        ax.set_ylim(0, max(s0 + s1) * 1.3 if (s0 + s1) else 1)
        st.title(ax, "(d) one hot socket blocks a second that has traffic")
        st.legend(ax, loc="upper right")

    fig.tight_layout(pad=0.5)
    return st.save(fig, "campaign_livelock", OUT)


# ---------------------------------------------------------------------------
def main():
    arms = load.arms()
    if not arms:
        print("no campaign results found")
        return 1
    print("arms:", {t: a["batch"] for t, a in arms.items()})
    made = []
    for fn in (fig_eth_ramp, fig_socket_placement, fig_batching_engages,
               fig_batch_sweep, fig_can, fig_combined):
        try:
            p = fn(arms)
            if p:
                made.append(p)
        except Exception as e:
            print(f"  {fn.__name__}: {type(e).__name__}: {e}")
    try:
        p = fig_livelock()
        if p:
            made.append(p)
    except Exception as e:
        print(f"  fig_livelock: {type(e).__name__}: {e}")
    for p in made:
        print("  wrote", p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
