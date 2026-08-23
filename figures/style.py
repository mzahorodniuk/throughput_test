"""Shared figure style: the same one the repo's floorplan figures use.

Copied deliberately rather than re-chosen. figures/README.md in the SCIS repo is
explicit that a colour means the same thing across the GateMate and Artix
figures, so these plots have to sit in the same paper without a reader having to
relearn the palette. cvd_check.py computes the pair separations for the specific
adjacencies used here rather than trusting "Okabe-Ito is safe".
"""

import matplotlib as mpl

mpl.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

# hardware/../figures/plot_floorplan_device.py:216-224, byte for byte
mpl.rcParams.update({
    "font.family": "sans-serif",
    "font.sans-serif": ["DejaVu Sans"],
    "font.size": 7,
    "axes.linewidth": 0.5,
    "pdf.fonttype": 42,      # TrueType, not Type3 -- required by IEEE/ACM
    "ps.fonttype": 42,
    "savefig.bbox": "tight",
    "savefig.pad_inches": 0.02,
})

# figures/plot_floorplan_impl.py:105-107
CB = {"blue": "#0072B2", "orange": "#E69F00", "verm": "#D55E00",
      "green": "#009E73", "sky": "#56B4E9", "purple": "#CC79A7",
      "yellow": "#F0E442", "gray": "#7F7F7F", "black": "#111111"}

INK = "#111111"
INK2 = "#555555"
MUTED = "#8A8A8A"
GRID = "#E4E4E4"

# One colour and one marker per configuration, fixed: the marker is the second
# channel, so identity never rests on hue alone (green/purple separate by only
# 8.2 dE under protanopia -- passing, but not by much).
SERIES = {
    "spread": dict(color=CB["blue"],   marker="o", label="8 sockets (spread)"),
    "p9217":  dict(color=CB["orange"], marker="s", label="1 socket, 9217 (FIFO 0)"),
    "p9219":  dict(color=CB["green"],  marker="^", label="1 socket, 9219 (FIFO 3)"),
    "can":    dict(color=CB["purple"], marker="D", label="CAN"),
}

WIRE_LIMIT = 148810      # 9-byte payload, 84 B on the wire, 100 Mbit/s


def axes_style(ax, grid_axis="both"):
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(INK2)
    ax.grid(True, axis=grid_axis, color=GRID, lw=0.5, zorder=0)
    ax.set_axisbelow(True)
    ax.tick_params(colors=INK2, width=0.5, length=2.5, labelsize=6.4)
    for lbl in ax.get_xticklabels() + ax.get_yticklabels():
        lbl.set_color(INK)
    return ax


def legend(ax, **kw):
    opts = dict(frameon=False, fontsize=6.2, handletextpad=0.4,
                labelspacing=0.42, borderpad=0, columnspacing=1.1)
    opts.update(kw)
    lg = ax.legend(**opts)
    for t in lg.get_texts():
        t.set_color(INK)
    return lg


def title(ax, text):
    ax.set_title(text, fontsize=7.5, pad=4, color=INK, loc="left")


def save(fig, stem, outdir, web=True):
    """PDF and 600 dpi PNG for the paper, plus a 150 dpi PNG for the web page.

    The 600 dpi PNGs are 300-400 kB each; embedding seven of them in an HTML
    page as data URIs is several megabytes of base64 for no visible gain on a
    screen. The _web variant exists only for that.
    """
    import os
    os.makedirs(outdir, exist_ok=True)
    p = os.path.join(outdir, stem)
    fig.savefig(f"{p}.pdf")
    fig.savefig(f"{p}.png", dpi=600)
    if web:
        fig.savefig(f"{p}_web.png", dpi=150)
    plt.close(fig)
    return f"{p}.pdf"


def kfmt(v, _pos=None):
    """Axis labels in k, because every rate here is tens of thousands."""
    if v == 0:
        return "0"
    if abs(v) >= 1000:
        s = f"{v/1000:g}"
        return f"{s}k"
    return f"{v:g}"
