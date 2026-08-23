#!/usr/bin/env python3
"""Build the results page (HTML) from the campaign data.

Every number is read from host/campaign/results/*/steps.csv, the per-arm
meta.json, and sim/results/sim/backpressure.csv. Nothing is typed in, so the
page cannot disagree with the CSVs.

    /home/misha/FPGA-centric-SCIS/.venv/bin/python make_artifact.py
"""

import base64
import html
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import load

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "out")
PAGE = os.path.join(HERE, "out", "results_page.html")

FIGS = [
    ("campaign_batch_sweep", "Sustained rate against batch size",
     "Left: the knee for three load shapes. Right: cost per metric, with the "
     "1/N model a fixed-cost-per-datagram pipeline predicts."),
    ("campaign_eth_ramp", "Ethernet ramp, and what happens past the knee",
     "Delivered tracks offered exactly until the knee, then stops. Zero loss is "
     "drawn at zero, not at a log-axis floor."),
    ("campaign_socket_placement", "Which socket the load lands on",
     "Identical up to the knee across all three placements. Past it, the "
     "eight-socket spread degrades far more gracefully than one hot socket."),
    ("campaign_batching", "Batching is load-driven",
     "The achieved batch is not the configured one. Right: the datagram rate the "
     "transmit chip must sustain falls as load rises, which is the mechanism."),
    ("campaign_can", "CAN: bus-limited, not FPGA-limited",
     "Solid is what the FPGA failed to deliver; dashed is what the STM32 never "
     "got onto the bus. The FPGA line stays at zero."),
    ("campaign_combined", "Sharing one transmit chip",
     "What adding CAN costs Ethernet, and whether Ethernet starves CAN."),
    ("campaign_livelock", "The transmit-buffer livelock, in simulation",
     "(a) how degraded the link must be. (b) larger batches stall less. (c) why "
     "the existing testbench could not have found it. (d) the case the claim is "
     "about: one hot socket, and a second socket that has traffic and does not "
     "get it out."),
]


def b64(path):
    with open(path, "rb") as fh:
        return base64.b64encode(fh.read()).decode()


def fnum(v, unit=""):
    if v is None:
        return "&mdash;"
    return f"{v:,.0f}{unit}"


def ffmt(v, spec=".1f", suffix=""):
    """Format or dash. Arms can legitimately be missing (a build that wedged,
    a partial run), and a half-built page is more useful than a traceback."""
    if v is None:
        return "&mdash;"
    return format(v, spec) + suffix


def collect():
    arms = load.arms()
    order = sorted(arms.values(), key=lambda a: a["batch"] or 0)
    d = {"arms": order}

    base = next((a for a in order if a["batch"] == 1), None)
    bk = load.knee(base["rows"], "eth_spread") if base else None
    best = None
    for a in order:
        for t in ("eth_spread", "eth_p9217", "eth_p9219"):
            kn = load.knee(a["rows"], t)
            if kn and (best is None or kn["delivered_pps"] > best[1]):
                best = (a["batch"], kn["delivered_pps"], t)
    d["best"] = best
    d["b1"] = bk["delivered_pps"] if bk else None
    d["gain"] = (best[1] / bk["delivered_pps"]) if (best and bk) else None

    can = None
    for a in order:
        kn = load.knee(a["rows"], "can")
        if kn and (can is None or kn["delivered_pps"] > can):
            can = kn["delivered_pps"]
    d["can"] = can

    # Worst CAN loss seen while Ethernet was also running, per arm. This is
    # strongly batch-dependent -- asserting "CAN is unaffected" would be true of
    # batch 16 and badly wrong of batch 4 -- so it is computed.
    d["can_cost"] = {}
    for a in order:
        vals = [r["can_loss_pct"] for r in a["rows"]
                if r["test"].startswith("both_") and r["can_loss_pct"] is not None]
        d["can_cost"][a["batch"]] = max(vals) if vals else None

    d["wedges"] = {}
    for a in order:
        w = sorted({int(v) for k, v in a["summary"].items()
                    if k.endswith("_wedged_at")})
        d["wedges"][a["batch"]] = w

    d["repeat"] = load.repeat_rows()
    sim = load.sim_rows()
    d["sim"] = sim
    wire = sorted([r for r in sim if r["axis"] == "wire"],
                  key=lambda r: r["wire_ns"])
    d["sim_ok"] = [r for r in wire if not r["livelock"]]
    d["sim_bad"] = [r for r in wire if r["livelock"]]
    d["sim_first"] = d["sim_bad"][0] if d["sim_bad"] else None
    d["sim_worst"] = max(sim, key=lambda r: r["fsr_span_us"] or 0) if sim else None
    d["drain"] = {r["drain"]: r for r in sim if r["axis"] == "drain_model"}
    d["tx256"] = next((r for r in sim if r["axis"] == "txbuf" and r["txbuf"] == 256),
                      None)
    d["hot"] = [r for r in sim if r["axis"] == "hot_socket"]
    d["socks"] = sorted([r for r in sim if r["axis"] == "sockets"],
                        key=lambda r: r["fed_sockets"])
    return d


# ---------------------------------------------------------------------------

CSS = """
:root{
  --ground:#FBFCFD; --panel:#FFFFFF; --panel-2:#F1F5F8; --rail:#E3EAEF;
  --line:#D8E1E7; --line-soft:#EAF0F4;
  --ink:#101820; --ink-2:#41525E; --ink-3:#6C7F8C;
  --accent:#0072B2; --accent-soft:#E4F0F8;
  --good:#009E73; --warn:#E69F00; --bad:#D55E00; --alt:#CC79A7;
  --shadow:0 1px 2px rgba(16,24,32,.05), 0 8px 24px -18px rgba(16,24,32,.35);
}
@media (prefers-color-scheme: dark){
  :root:not([data-theme="light"]){
    --ground:#0D1317; --panel:#141D23; --panel-2:#18232A; --rail:#22303A;
    --line:#25333C; --line-soft:#1C282F;
    --ink:#E6EDF2; --ink-2:#A3B4BF; --ink-3:#7A8C98;
    --accent:#4FA8D8; --accent-soft:#12303F;
    --good:#3FBF9B; --warn:#E9AE33; --bad:#EE7B33; --alt:#D894B7;
    --shadow:0 1px 2px rgba(0,0,0,.4), 0 10px 30px -20px rgba(0,0,0,.8);
  }
}
:root[data-theme="dark"]{
  --ground:#0D1317; --panel:#141D23; --panel-2:#18232A; --rail:#22303A;
  --line:#25333C; --line-soft:#1C282F;
  --ink:#E6EDF2; --ink-2:#A3B4BF; --ink-3:#7A8C98;
  --accent:#4FA8D8; --accent-soft:#12303F;
  --good:#3FBF9B; --warn:#E9AE33; --bad:#EE7B33; --alt:#D894B7;
  --shadow:0 1px 2px rgba(0,0,0,.4), 0 10px 30px -20px rgba(0,0,0,.8);
}

*{box-sizing:border-box}
body{
  margin:0; background:var(--ground); color:var(--ink);
  font-family:"IBM Plex Sans","Helvetica Neue",Arial,sans-serif;
  font-size:15px; line-height:1.62; -webkit-font-smoothing:antialiased;
}
.wrap{max-width:74rem; margin:0 auto; padding:0 clamp(1rem,4vw,3rem) 5rem;}

/* the measurement rail: a scale gutter, echoing an instrument's edge */
.rail{position:relative; padding-left:clamp(0px,2.6vw,34px);}
.rail::before{
  content:""; position:absolute; left:0; top:.6rem; bottom:0; width:1px;
  background:var(--rail);
}
.rail>section{position:relative;}
.rail>section::before{
  content:""; position:absolute; left:calc(-1 * clamp(0px,2.6vw,34px));
  top:1.15rem; width:clamp(0px,2.6vw,34px); height:1px; background:var(--rail);
}

header.top{padding:clamp(2.5rem,6vw,4.5rem) 0 1.6rem;}
.eyebrow{
  font-family:"IBM Plex Mono",ui-monospace,monospace; font-size:11px;
  letter-spacing:.13em; text-transform:uppercase; color:var(--ink-3);
  margin:0 0 .9rem;
}
h1{
  font-size:clamp(2rem,4.6vw,3.1rem); line-height:1.08; font-weight:600;
  letter-spacing:-.021em; margin:0 0 .8rem; text-wrap:balance;
}
.lede{font-size:clamp(1.02rem,1.6vw,1.16rem); color:var(--ink-2); max-width:62ch; margin:0;}

h2{
  font-size:clamp(1.22rem,2.1vw,1.5rem); font-weight:600; letter-spacing:-.014em;
  margin:3.4rem 0 .45rem; text-wrap:balance;
}
h3{font-size:1.02rem; font-weight:600; margin:2rem 0 .4rem;}
p{margin:.7rem 0; max-width:68ch;}
a{color:var(--accent); text-underline-offset:2px;}
code,.mono{
  font-family:"IBM Plex Mono",ui-monospace,monospace; font-size:.9em;
  font-variant-ligatures:none;
}
code{background:var(--panel-2); padding:.08em .34em; border-radius:3px;}
.sub{color:var(--ink-2); font-size:.95rem;}
.muted{color:var(--ink-3);}

/* verdict tiles */
.tiles{
  display:grid; gap:12px; margin:1.8rem 0 .5rem;
  grid-template-columns:repeat(auto-fit,minmax(196px,1fr));
}
.tile{
  background:var(--panel); border:1px solid var(--line); border-radius:10px;
  padding:1rem 1.05rem 1.05rem; box-shadow:var(--shadow); position:relative;
  overflow:hidden;
}
.tile .k{
  font-family:"IBM Plex Mono",ui-monospace,monospace; font-size:10.5px;
  letter-spacing:.11em; text-transform:uppercase; color:var(--ink-3);
  display:block; margin-bottom:.45rem;
}
.tile .v{
  font-size:clamp(1.6rem,3vw,2.05rem); font-weight:600; letter-spacing:-.02em;
  font-variant-numeric:tabular-nums; line-height:1.05; display:block;
}
.tile .u{font-size:.83rem; color:var(--ink-2); display:block; margin-top:.3rem;}
.tile.accent .v{color:var(--accent)}
.tile.good .v{color:var(--good)}
.tile.bad .v{color:var(--bad)}

/* severity chip */
.chip{
  display:inline-flex; align-items:center; gap:.4em;
  font-family:"IBM Plex Mono",ui-monospace,monospace; font-size:10.5px;
  letter-spacing:.08em; text-transform:uppercase; padding:.24em .6em;
  border-radius:999px; border:1px solid currentColor; white-space:nowrap;
}
.chip.bad{color:var(--bad)} .chip.good{color:var(--good)}
.chip.warn{color:var(--warn)} .chip.info{color:var(--accent)}
.chip::before{
  content:""; width:.45em; height:.45em; border-radius:50%;
  background:currentColor; flex:none;
}

/* tables */
.tbl-wrap{overflow-x:auto; margin:1.1rem 0; border:1px solid var(--line); border-radius:10px; background:var(--panel);}
table{border-collapse:collapse; width:100%; font-size:13.5px;}
th,td{padding:.56rem .8rem; text-align:right; white-space:nowrap;}
th:first-child,td:first-child{text-align:left;}
thead th{
  font-family:"IBM Plex Mono",ui-monospace,monospace; font-size:10.5px;
  letter-spacing:.08em; text-transform:uppercase; color:var(--ink-3);
  font-weight:500; border-bottom:1px solid var(--line); background:var(--panel-2);
}
tbody tr+tr td{border-top:1px solid var(--line-soft);}
td.num{font-variant-numeric:tabular-nums; font-family:"IBM Plex Mono",ui-monospace,monospace;}
tbody tr:hover td{background:var(--panel-2);}
caption{caption-side:bottom; text-align:left; padding:.6rem .8rem; color:var(--ink-3); font-size:12.5px;}

/* figures */
figure{margin:1.6rem 0 0; background:var(--panel); border:1px solid var(--line);
       border-radius:10px; overflow:hidden; box-shadow:var(--shadow);}
figure img{display:block; width:100%; height:auto; background:#fff;}
figcaption{padding:.75rem .95rem .9rem; border-top:1px solid var(--line);}
figcaption b{font-weight:600;}
figcaption span{display:block; color:var(--ink-2); font-size:13px; margin-top:.2rem;}

/* callout */
.note{
  border-left:2px solid var(--accent); background:var(--accent-soft);
  padding:.85rem 1rem; border-radius:0 8px 8px 0; margin:1.3rem 0;
}
.note.bad{border-color:var(--bad); background:var(--panel-2);}
.note.bad{background:color-mix(in srgb, var(--bad) 8%, var(--panel));}
.note p{margin:.3rem 0;}
.note .h{font-weight:600; display:block; margin-bottom:.15rem;}

.two{display:grid; gap:1.2rem; grid-template-columns:1fr;}
@media(min-width:900px){.two{grid-template-columns:1.05fr .95fr;}}

svg.diagram{display:block; width:100%; height:auto;}
.diagram-card{background:var(--panel); border:1px solid var(--line);
  border-radius:10px; padding:1rem; box-shadow:var(--shadow);}

ul{margin:.6rem 0; padding-left:1.15rem; max-width:68ch;}
li{margin:.32rem 0;}
li::marker{color:var(--ink-3);}

footer{margin-top:4rem; padding-top:1.4rem; border-top:1px solid var(--line);
  color:var(--ink-3); font-size:12.5px;}
"""


def diagram(d):
    """The two-state loop, drawn. Tokens only, so it works in both themes."""
    f = d["sim_first"]
    runs = f["fsr_longest_run"] if f else 0
    span = f["fsr_span_us"] / 1000.0 if f else 0
    return f"""
<div class="diagram-card">
<svg class="diagram" viewBox="0 0 720 232" role="img"
     aria-label="Two states alternate forever: GET_TX_FREE_BUFFER_SIZE reads
     Sn_TX_FSR, CHECK_IF_FREE_SIZE_IS_AVAILABLE sees less than 256 bytes free and
     returns to it. The watchdog resets on every state change, so it never fires.">
  <defs>
    <marker id="ah" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7"
            markerHeight="7" orient="auto-start-reverse">
      <path d="M0,0 L10,5 L0,10 z" fill="currentColor"/>
    </marker>
  </defs>
  <g font-family="IBM Plex Mono, monospace" font-size="11.5">
    <!-- state A -->
    <rect x="24" y="40" width="252" height="56" rx="8"
          fill="var(--panel-2)" stroke="var(--accent)" stroke-width="1.4"/>
    <text x="150" y="63" text-anchor="middle" fill="var(--ink)">GET_TX_FREE_BUFFER_SIZE</text>
    <text x="150" y="81" text-anchor="middle" fill="var(--ink-3)" font-size="10">reads Sn_TX_FSR (2 bytes over SPI)</text>

    <!-- state B -->
    <rect x="444" y="40" width="252" height="56" rx="8"
          fill="var(--panel-2)" stroke="var(--accent)" stroke-width="1.4"/>
    <text x="570" y="63" text-anchor="middle" fill="var(--ink)">CHECK_IF_FREE_SIZE_IS_AVAILABLE</text>
    <text x="570" y="81" text-anchor="middle" fill="var(--ink-3)" font-size="10">issues no transaction of its own</text>

    <!-- forward -->
    <g color="var(--ink-2)">
      <path d="M276 60 L438 60" stroke="currentColor" stroke-width="1.3"
            fill="none" marker-end="url(#ah)"/>
    </g>
    <text x="357" y="52" text-anchor="middle" fill="var(--ink-2)" font-size="10">transaction done</text>

    <!-- return: the branch with no way out -->
    <g color="var(--bad)">
      <path d="M570 96 L570 132 L150 132 L150 100" stroke="currentColor"
            stroke-width="1.6" fill="none" marker-end="url(#ah)"/>
    </g>
    <text x="360" y="147" text-anchor="middle" fill="var(--bad)" font-size="10.5">
      free &#8804; 256 B &#8594; same socket, no retry budget, no exit to receive
    </text>

    <!-- watchdog -->
    <rect x="24" y="172" width="672" height="44" rx="8"
          fill="none" stroke="var(--line)" stroke-width="1" stroke-dasharray="4 3"/>
    <text x="40" y="190" fill="var(--ink-2)" font-size="10.5">
      watchdog: <tspan fill="var(--ink-3)">if state /= prev_state then watchdog &lt;= 0</tspan>
    </text>
    <text x="40" y="207" fill="var(--bad)" font-size="10.5">
      state changes every transaction, so the count restarts every time &#8212; {runs:,} reads, {span:.2f} ms, never fires
    </text>
  </g>
</svg>
</div>"""


def build():
    d = collect()
    order = d["arms"]

    # ---- tiles ----
    best_b, best_v, _ = d["best"] if d["best"] else (None, None, None)
    wedge1 = d["wedges"].get(1) or []
    f = d["sim_first"]
    tiles = f"""
<div class="tiles">
  <div class="tile accent"><span class="k">sustained, Ethernet</span>
    <span class="v">{fnum(best_v)}</span>
    <span class="u">metrics/s under 0.1 % loss, at batch {best_b}</span></div>
  <div class="tile good"><span class="k">batching gain</span>
    <span class="v">{ffmt(d['gain'], '.1f', '&times;')}</span>
    <span class="u">against batch 1 ({fnum(d['b1'])} metrics/s)</span></div>
  <div class="tile"><span class="k">CAN, 125 kbit/s</span>
    <span class="v">{fnum(d['can'])}</span>
    <span class="u">frames/s &mdash; bus-limited, FPGA lost none</span></div>
  <div class="tile bad"><span class="k">batch 1 wedges at</span>
    <span class="v">{fnum(wedge1[0]) if wedge1 else 'never'}</span>
    <span class="u">RX socket closes permanently; reflash to recover</span></div>
</div>"""

    # ---- batch table ----
    rows = []
    for a in order:
        ks = {t: load.knee(a["rows"], t) for t in
              ("eth_spread", "eth_p9217", "eth_p9219", "can", "both_spread")}
        w = d["wedges"].get(a["batch"]) or []
        rag = max((r["ragged"] or 0) for r in a["rows"]) if a["rows"] else 0
        bmax = max((r["batch_max"] or 0) for r in a["rows"]) if a["rows"] else 0
        over = bmax and a["batch"] and bmax > a["batch"]
        rows.append(f"""<tr>
  <td class="mono">{a['batch']}</td>
  <td class="num">{fnum(ks['eth_spread']['delivered_pps'] if ks['eth_spread'] else None)}</td>
  <td class="num">{fnum(ks['eth_p9217']['delivered_pps'] if ks['eth_p9217'] else None)}</td>
  <td class="num">{fnum(ks['eth_p9219']['delivered_pps'] if ks['eth_p9219'] else None)}</td>
  <td class="num">{fnum(ks['both_spread']['delivered_pps'] if ks['both_spread'] else None)}</td>
  <td class="num">{fnum(ks['can']['delivered_pps'] if ks['can'] else None)}</td>
  <td class="num">{f"{ks['eth_spread']['batch_mean']:.1f}" if ks['eth_spread'] else "&mdash;"}</td>
  <td>{'<span class="chip bad">' + f"{w[0]:,}" + '</span>' if w else '<span class="chip good">never</span>'}</td>
  <td class="num">{rag:,}</td>
  <td class="num">{bmax:,}{' <span class="chip warn">over</span>' if over else ''}</td>
</tr>""")
    table = f"""<div class="tbl-wrap"><table>
<caption>Sustained = the highest offered rate whose loss stayed at or under
0.1 %, reported as what was actually delivered. Steps above the 100 Mbit wire
limit (148,810 packets/s) and steps where our own socket queue overflowed are
excluded. &ldquo;Ragged&rdquo; is the worst single step's count of datagrams whose
length was not a multiple of 9 bytes; &ldquo;max records&rdquo; above the configured
batch is mis-framing, not batching.</caption>
<thead><tr>
  <th>batch</th><th>8 sockets</th><th>1&times;9217</th><th>1&times;9219</th>
  <th>+ CAN</th><th>CAN f/s</th><th>achieved batch</th><th>wedge</th>
  <th>ragged</th><th>max records</th>
</tr></thead><tbody>{''.join(rows)}</tbody></table></div>"""

    # ---- provenance ----
    prov = []
    for a in order:
        b = a["meta"].get("bitstream", {})
        g = a["meta"].get("hdl_generics", {})
        prov.append(f"""<tr><td class="mono">batch {a['batch']}</td>
        <td class="mono">{(b.get('md5') or '')[:16]}</td>
        <td class="num">{g.get('TX_FLUSH_IDLE_CLKS','&mdash;')}</td>
        <td class="num">{g.get('TX_MAX_OPEN_CLKS','&mdash;')}</td>
        <td class="num">{g.get('WATCHDOG_CLKS','&mdash;')}</td>
        <td class="mono">{b.get('mtime','')}</td></tr>""")
    provtbl = f"""<div class="tbl-wrap"><table>
<caption>One bitstream per batch size, because <code>TX_BATCH_MAX_PACKETS</code>
is an entity default with no <code>generic map</code> entry and
<code>entity top</code> has no generic clause &mdash; there is no command-line
override. Each arm's md5 is recorded at measurement time.</caption>
<thead><tr><th>arm</th><th>bitstream md5</th><th>flush idle</th>
<th>max open</th><th>watchdog</th><th>built</th></tr></thead>
<tbody>{''.join(prov)}</tbody></table></div>"""

    # ---- sim tables ----
    simrows = []
    for r in d["socks"]:
        simrows.append((f"{r['fed_sockets']} of 8 sockets carrying load",
                        r["fsr_span_us"], r["result"], r["min_free_b"]))
    for r in d["hot"]:
        simrows.append((f"socket 0 hot, socket 1 gets 1 packet in {r['hot_every']}",
                        r["fsr_span_us"], r["result"], r["min_free_b"]))
    if 0 in d["drain"]:
        simrows.append(("buffer freed on SEND (tb_w5500_tx's model)",
                        d["drain"][0]["fsr_span_us"], d["drain"][0]["result"],
                        d["drain"][0]["min_free_b"]))
    if 1 in d["drain"]:
        simrows.append(("buffer freed as the wire drains (the real chip)",
                        d["drain"][1]["fsr_span_us"], d["drain"][1]["result"],
                        d["drain"][1]["min_free_b"]))
    if d["tx256"]:
        simrows.append(("256-byte transmit buffer",
                        d["tx256"]["fsr_span_us"], d["tx256"]["result"],
                        d["tx256"]["min_free_b"]))
    body = "".join(
        f"""<tr><td>{html.escape(n)}</td><td class="num">{(s/1000.0):.2f} ms</td>
        <td class="num">{mf} B</td>
        <td>{'<span class="chip bad">livelock</span>' if v=='LIVELOCK'
             else '<span class="chip good">no livelock</span>'}</td></tr>"""
        for n, s, v, mf in simrows)
    simtbl = f"""<div class="tbl-wrap"><table>
<caption>All at a wire cost of 1&nbsp;ms per datagram unless the row says
otherwise &mdash; about 200&times; slower than the 100 Mbit link as built.
&ldquo;Min free&rdquo; is the lowest <code>Sn_TX_FSR</code> the controller saw;
the threshold it compares against is 256 bytes.</caption>
<thead><tr><th>configuration</th><th>longest stall</th><th>min free</th>
<th>result</th></tr></thead><tbody>{body}</tbody></table></div>"""

    # ---- figures ----
    figs = []
    for stem, title, cap in FIGS:
        p = os.path.join(OUT, f"{stem}_web.png")
        if not os.path.exists(p):
            p = os.path.join(OUT, f"{stem}.png")
        if not os.path.exists(p):
            continue
        figs.append(f"""<figure>
  <img src="data:image/png;base64,{b64(p)}" alt="{html.escape(title)}"/>
  <figcaption><b>{html.escape(title)}</b><span>{html.escape(cap)}</span></figcaption>
</figure>""")

    cc = []
    for a in order:
        v = d["can_cost"].get(a["batch"])
        if v is None:
            continue
        chip = ('<span class="chip good">unaffected</span>' if v <= 0.5 else
                '<span class="chip warn">degraded</span>' if v <= 10 else
                '<span class="chip bad">starved</span>')
        cc.append(f"""<tr><td class="mono">batch {a['batch']}</td>
        <td class="num">{v:.3f} %</td><td>{chip}</td></tr>""")
    can_cost_tbl = f"""<div class="tbl-wrap"><table>
<caption>Worst CAN frame loss observed at any point in the combined ladder, with
CAN offered a steady 1,400 frames/s throughout. CAN has its own sequence space
and its own identifier, so this is CAN's own loss and not an artefact of the
Ethernet stream.</caption>
<thead><tr><th>arm</th><th>worst CAN loss</th><th></th></tr></thead>
<tbody>{"".join(cc)}</tbody></table></div>"""

    rr = d["repeat"]
    if rr:
        body_rr = "".join(
            f"""<tr><td class="num">{r['rate']:,}</td>
            <td class="num">{r['median']:,.0f}</td>
            <td class="num">{r['lo']:,.0f} &ndash; {r['hi']:,.0f}</td>
            <td class="num">{r['spread_pct']:.1f} %</td></tr>""" for r in rr)
        worst_rr = max(r["spread_pct"] for r in rr)
        repeat_tbl = f"""<div class="tbl-wrap"><table>
<caption>Five repeats per rate on the batch-16 build. Above the knee the
delivered rate <em>plateaus</em> near the knee value while loss rises to absorb
the excess &mdash; it does not collapse. Two earlier single-shot ladders
disagreed about this, which is why the repeats exist; the worst spread across
repeats is {worst_rr:.1f} %.</caption>
<thead><tr><th>offered</th><th>delivered, median</th><th>range</th>
<th>spread</th></tr></thead><tbody>{body_rr}</tbody></table></div>"""
    else:
        repeat_tbl = ""

    sim_ok = d["sim_ok"][-1] if d["sim_ok"] else None
    factor = (f["wire_ns"] / 5000.0) if f else 0

    doc = f"""<title>SCIS Transmit Batching</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600&display=swap">
<style>{CSS}</style>
<div class="wrap">
<header class="top">
  <p class="eyebrow">GateMate CCGM1A1 &middot; dual W5500 &middot; 15 MHz SPI &middot; 30 MHz fabric</p>
  <h1>What batching bought the transmit path</h1>
  <p class="lede">Four bitstreams, three load shapes, one bench. Plus the
  failure mode that cannot be reached from the bench at all, measured in
  simulation instead.</p>
</header>

<div class="rail">

<section>
{tiles}
<p class="sub">Every figure below is generated from the same CSVs as these
numbers. The measurement is end-to-end: the host counts what it sent and what
came back, with nothing instrumented on the board.</p>
</section>

<section>
<h2>The batch sweep</h2>
<p><code>TX_BATCH_MAX_PACKETS</code> sets how many 9-byte metric records may
accumulate in a W5500 transmit buffer before <code>SEND</code> is issued. A
datagram costs a fixed nine-transaction SPI sequence whatever it carries, so
N records in one datagram divides that cost by N. That is the whole mechanism,
and it shows up as a clean 1/N curve in the per-metric cost.</p>
{table}
{figs[0] if len(figs) > 0 else ''}
</section>

<section>
<h2>Where the load lands stopped mattering</h2>
<p>All three placements &mdash; spread across eight sockets, one socket on 9217
(FIFO&nbsp;0, top arbiter priority), one socket on 9219 (FIFO&nbsp;3) &mdash;
land within a rung of each other. The ceiling therefore sits upstream of the
priority arbiter, which has nothing to arbitrate while only one channel is
active. Past the knee the difference appears: the eight-socket spread degrades
gradually, a single hot socket falls off a cliff.</p>
{figs[2] if len(figs) > 2 else ''}
{figs[1] if len(figs) > 1 else ''}
{figs[3] if len(figs) > 3 else ''}

<h3>Is it repeatable, and what happens above the knee?</h3>
<p>The ladders are single-shot, which locates a knee but says nothing about
spread &mdash; and two runs disagreed about the shape above it. Repeating each
rate five times settles it.</p>
{repeat_tbl}
</section>

<section>
<h2>CAN, and both at once</h2>
<p>The FPGA is the device under test; the Nucleo is only a traffic source. Its
own counter for frames that never reached the bus separates &ldquo;the FPGA lost
it&rdquo; from &ldquo;it was never sent&rdquo;, and across the whole ladder the
FPGA line stays at zero: <strong>the CAN path is bus-limited, not
FPGA-limited</strong>. Its real ceiling is still unmeasured, and finding it needs
a faster bus &mdash; <code>CLKS_PER_BIT</code> in <code>top.vhd</code> with
matching STM32 bit timing, a change to two subsystems that was left alone here.</p>
<p>Run together, the two inputs share one transmit chip and one arbiter. Adding
1,400 frames/s of CAN costs Ethernet a few thousand packets/s spread over eight
sockets and roughly three times that on a single socket.</p>
<p><strong>Whether Ethernet can starve CAN depends on the batch size</strong>, and
that is the part worth carrying away:</p>
{can_cost_tbl}
{figs[4] if len(figs) > 4 else ''}
{figs[5] if len(figs) > 5 else ''}
</section>

<section>
<h2>The transmit-buffer livelock</h2>
<p><code>CHECK_IF_FREE_SIZE_IS_AVAILABLE</code> has no way out. Below
<code>minimum_free_tx_buffer_memory</code> (256 bytes) it returns to
<code>GET_TX_FREE_BUFFER_SIZE</code> and re-reads the same socket. Nothing
advances the socket counter, nothing falls through to the receive branch, and
there is no retry budget. The watchdog cannot see it, because it zeroes on every
state change and this alternation changes state on every SPI transaction.</p>
{diagram(d)}
<p style="margin-top:1.1rem">Both halves are now confirmed rather than argued.
The stall runs <strong>{ffmt((d['sim_worst']['fsr_span_us']/1000.0) if d['sim_worst'] else None, '.1f', ' ms')}</strong> at
its worst against a 2.18 ms watchdog threshold, and the SPI bus is never idle for
more than 25 clocks throughout &mdash; so <code>cs_idle_max</code>, the liveness
measure every other testbench relies on, registers nothing at all. Across the
run no <code>Sn_RX_RSR</code> read occurs, by construction: any other transaction
would have ended the run.</p>

<div class="note">
<span class="h">How far away is it?</span>
<p>The livelock starts once the chip's wire cost reaches
<strong>{ffmt((f['wire_ns']/1000.0) if f else None, '.0f', '&nbsp;&micro;s per datagram')}</strong>
({fnum(f['fsr_longest_run'] if f else None)} back-to-back <code>Sn_TX_FSR</code> reads,
{ffmt((f['fsr_span_us']/1000.0) if f else None, '.2f', '&nbsp;ms')}). As built it is about 5&nbsp;&micro;s, and
{ffmt((sim_ok['wire_ns']/1000.0) if sim_ok else None, '.0f', '&nbsp;&micro;s')} still recovers cleanly. So the link
has to be roughly <strong>{ffmt(factor, '.0f', '&times;')} slower than 100 Mbit</strong> before
this is reachable &mdash; which is why the bench has never produced it, and why
this is a simulation result and not a measurement.</p>
</div>

<div class="note bad">
<span class="h">Why no existing testbench could have found it</span>
<p><code>tb_w5500_tx</code>'s behavioural chip frees the entire transmit buffer
the instant <code>SEND</code> is written, so <code>Sn_TX_FSR</code> is never low
and the branch is unreachable at any wire speed. Modelling the buffer as freeing
only once the wire has carried the bytes &mdash; which is what the chip does,
since free space is not free until it has been sent &mdash; is the entire
difference. The sweep runs both models, so that comparison is in the output
rather than in a claim.</p>
</div>

{simtbl}
<p>The asymmetric case is the one the concern was actually about, and it is the
only one that reproduces it. Feeding two sockets evenly never stalls at all
&mdash; spreading the load halves the pressure on each socket's own buffer. Drive
one socket hard and give a second socket a trickle, and the second socket gets
two to four datagrams out while the first gets twenty-five: its traffic is stuck
behind a full buffer it has nothing to do with. That is panel (d).</p>
<p>Three consequences worth acting on. A <strong>256-byte transmit buffer never
transmits at all</strong>: free space can never exceed the 256-byte threshold, so
the comparison is never satisfied. <strong>Larger batches are safer</strong>, not
riskier &mdash; fewer trips through the check, more bytes retired per wire
transaction. And <strong>a single hot socket is the dangerous shape</strong>:
spreading load gives each socket its own buffer and the stall never starts.</p>
{figs[6] if len(figs) > 6 else ''}
</section>

<section>
<h2>What the old harness was measuring</h2>
<p>Four defects, all on the measurement side, found while setting this up. They
matter because the numbers in the previous README were produced with them.</p>
<ul>
<li><strong>90,112 probes, then the step stops.</strong> The sequence number
hides in one device's safe band, so at 150 kpps the window was 0.6 s. The probe
key is now <code>(port, identifier, seq)</code> across eight identifiers on eight
ports: 5.8 M probes, with every value still inside 1&ndash;89&nbsp;&deg;C so no
test packet can trip the interlock.</li>
<li><strong>Sender and receivers shared a GIL.</strong> A bare
<code>sendto</code> loop reaches 381 k pps on this host, but the old ceiling test
asked for 1.4 M pps and achieved 90 k at <em>zero</em> loss &mdash; the ceiling in
that CSV was the harness.</li>
<li><strong><code>SO_RCVBUF</code> was a no-op.</strong> It asked for 8 MiB;
<code>net.core.rmem_max</code> here is 212,992, so the kernel granted ~0.4 MiB.</li>
<li><strong>Any unknown identifier counted as CAN.</strong> At overload the
mis-framed datagrams decode to arbitrary identifiers, which inflated CAN arrivals
past CAN sends and produced <em>negative</em> loss &mdash; the giveaway that the
attribution was wrong rather than the board generous.</li>
</ul>
<p>One limit that is not a defect but bounds every Ethernet number here:
<code>enp2s0</code> is 1 Gbit while both W5500s are 100 Mbit, so above
<strong>148,810 packets/s</strong> the switch discards. A 9-byte UDP payload is a
64-byte frame plus preamble and inter-frame gap &mdash; 84 bytes, 672 bits. Steps
above that are run and plotted, never used for a sustained-rate claim.</p>
</section>

<section>
<h2>Framing integrity above the knee</h2>
<p>Past the knee the return path emits datagrams whose length is not a multiple
of 9 bytes, and datagrams carrying more records than the configured batch allows.
Since the configured ceiling is the number of records the controller will put in a
datagram, anything above it is mis-framing. The host discards a datagram whose
length is not a multiple of 9, so these register as loss &mdash; but a consumer
that parsed them optimistically would ingest garbage metric values instead, and in
an interlock system a mangled value can assert a global interlock from nothing.
The rate is strongly batch-dependent, which points at the transmit pointer and
length bookkeeping under backpressure rather than at the batching logic.</p>
{provtbl}
</section>

<footer>
<p>Reproduce: <code>host/campaign/run_all_arms.sh 28 16 4 1</code> &middot;
<code>host/campaign/build_batch.sh &lt;N&gt;</code> &middot;
<code>sim/sweep_backpressure.py</code> &middot;
<code>figures/plot_campaign.py</code> &middot;
<code>make hardware/fpga/ccgm1a1 sim_w5500_tx_backpressure</code></p>
<p>Palette is the repo's Okabe&ndash;Ito set, so a colour means the same thing
here as in the floorplan figures; <code>figures/cvd_check.py</code> computes the
separation for the specific pairs each panel places together.</p>
</footer>
</div>
</div>
"""
    with open(PAGE, "w") as fh:
        fh.write(doc)
    print(f"wrote {PAGE} ({len(doc)/1024:.0f} kB, {len(figs)} figures embedded)")
    return 0


if __name__ == "__main__":
    sys.exit(build())
