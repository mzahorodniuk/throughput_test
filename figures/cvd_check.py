"""Colour-vision check for the palette used by the campaign figures.

The repo standardises on Okabe-Ito so a colour means the same thing here as in
the GateMate/Artix floorplan figures. That is a published colourblind-safe
qualitative palette, but "published as safe" is not "safe for the subset and the
adjacency this figure uses" -- two slots fine in isolation can still be the two
lines a reader has to separate. So the pairs the figures actually place together
get computed rather than assumed.

Method: sRGB -> linear -> LMS, the Vienot/Brettel reduced model for protanopia
and deuteranopia (together ~99 % of colour-vision deficiency), back to sRGB,
then dE in OKLab x100 -- the units the dataviz validator reports. >= 8 passes,
6-8 is legal only with a second channel (marker shape or a direct label), < 6
fails; normal-vision separation must clear 15.
"""

import itertools

import numpy as np

CB = {"blue": "#0072B2", "orange": "#E69F00", "verm": "#D55E00",
      "green": "#009E73", "sky": "#56B4E9", "purple": "#CC79A7",
      "yellow": "#F0E442", "gray": "#7F7F7F", "black": "#111111"}


def hex2rgb(h):
    h = h.lstrip("#")
    return np.array([int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4)])


def srgb_to_linear(c):
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def linear_to_srgb(c):
    c = np.clip(c, 0, 1)
    return np.where(c <= 0.0031308, c * 12.92, 1.055 * c ** (1 / 2.4) - 0.055)


RGB2LMS = np.array([[0.31399022, 0.63951294, 0.04649755],
                    [0.15537241, 0.75789446, 0.08670142],
                    [0.01775239, 0.10944209, 0.87256922]])
LMS2RGB = np.linalg.inv(RGB2LMS)
PROTAN = np.array([[0.0, 1.05118294, -0.05116099], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]])
DEUTAN = np.array([[1.0, 0.0, 0.0], [0.95172417, 0.0, 0.04827583], [0.0, 0.0, 1.0]])
M1 = np.array([[0.4122214708, 0.5363325363, 0.0514459929],
               [0.2119034982, 0.6806995451, 0.1073969566],
               [0.0883024619, 0.2817188376, 0.6299787005]])
M2 = np.array([[0.2104542553, 0.7936177850, -0.0040720468],
               [1.9779984951, -2.4285922050, 0.4505937099],
               [0.0259040371, 0.7827717662, -0.8086757660]])


def simulate(hexcol, kind):
    lms = RGB2LMS @ srgb_to_linear(hex2rgb(hexcol))
    m = {"protan": PROTAN, "deutan": DEUTAN, "normal": np.eye(3)}[kind]
    return linear_to_srgb(LMS2RGB @ (m @ lms))


def to_oklab(rgb):
    return M2 @ np.cbrt(M1 @ srgb_to_linear(np.asarray(rgb, dtype=float)))


def dE(a, b, kind):
    return float(np.linalg.norm(to_oklab(simulate(a, kind))
                                - to_oklab(simulate(b, kind))) * 100)


def check(pairs, label):
    print(f"\n=== {label} ===")
    worst, bad = 1e9, []
    for n1, n2 in pairs:
        a, b = CB[n1], CB[n2]
        dn, dp, dd = dE(a, b, "normal"), dE(a, b, "protan"), dE(a, b, "deutan")
        cvd = min(dp, dd)
        worst = min(worst, cvd)
        flag = "PASS" if cvd >= 8 else ("MARK-REQD" if cvd >= 6 else "FAIL")
        if dn < 15:
            flag = "FAIL(normal)"
        if flag != "PASS":
            bad.append((n1, n2, flag))
        print(f"  {n1:<7}/{n2:<7} normal={dn:6.1f}  protan={dp:6.1f}  "
              f"deutan={dd:6.1f}  min_cvd={cvd:6.1f}  {flag}")
    print(f"  -> worst CVD separation {worst:.1f}")
    return bad


if __name__ == "__main__":
    ramp = [("blue", "orange"), ("orange", "green"), ("blue", "green")]
    sweep = list(itertools.combinations(["blue", "orange", "green", "purple"], 2))
    live = [("verm", "sky"), ("verm", "gray"), ("sky", "gray")]
    bad = check(ramp, "ramp panels: eth spread / p9217 / p9219")
    bad += check(sweep, "batch sweep: all pairs (grouped bars touch)")
    bad += check(live, "livelock panel")
    print()
    if bad:
        print("PAIRS NEEDING A SECOND CHANNEL:")
        for n1, n2, f in bad:
            print(f"  {n1}/{n2}: {f}")
        print("-> MARK-REQD pairs are legal only with marker shape or a direct "
              "label; the plots apply both.")
    else:
        print("every pair clears dE>=8 under both protanopia and deuteranopia")
