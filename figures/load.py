"""Load campaign and simulation results into plain dicts/lists.

No pandas: the CSVs are tens of rows and the plots need explicit control over
which rows are admissible. Two filters matter and are applied everywhere:

  valid == 0            our own socket queue overflowed; the row says nothing
                        about the board and must not appear in a figure
  over_wire_limit == 1  the offered rate exceeds what a 100 Mbit link can carry
                        (148 810 pps for a 9-byte payload), so the discard
                        happened in the switch. Plotted, but never used for a
                        sustained-rate claim, and marked in the figure.
"""

import csv
import json
import os

RESULTS = "/home/misha/thrpt_test/host/campaign/results"
SIM = "/home/misha/thrpt_test/sim/results/sim"

NUM = {"requested_pps", "achieved_pps", "delivered_pps", "sent", "received",
       "unique", "loss_pct", "dupes", "datagrams", "batch_mean", "batch_max",
       "ragged", "junk", "almostfull", "interlock", "can_sent", "can_failed",
       "can_unique", "can_loss_pct", "can_tec", "host_drops", "longest_gap",
       "over_wire_limit", "elapsed", "valid"}


def _num(row):
    out = dict(row)
    for k in NUM:
        v = row.get(k, "")
        if v == "" or v is None:
            out[k] = None
        else:
            try:
                out[k] = float(v)
            except ValueError:
                out[k] = None
    return out


def arms():
    """Every campaign arm on disk, newest batch first, keyed by tag."""
    out = {}
    if not os.path.isdir(RESULTS):
        return out
    for tag in sorted(os.listdir(RESULTS)):
        d = os.path.join(RESULTS, tag)
        steps = os.path.join(d, "steps.csv")
        if not os.path.isfile(steps):
            continue
        with open(steps) as fh:
            rows = [_num(r) for r in csv.DictReader(fh)]
        meta, summary = {}, {}
        for name, dest in (("meta.json", "meta"), ("summary.json", "summary")):
            p = os.path.join(d, name)
            if os.path.isfile(p):
                with open(p) as fh:
                    try:
                        val = json.load(fh)
                    except Exception:
                        val = {}
                if dest == "meta":
                    meta = val
                else:
                    summary = val
        out[tag] = {
            "tag": tag, "rows": rows, "meta": meta, "summary": summary,
            "batch": meta.get("hdl_generics", {}).get("TX_BATCH_MAX_PACKETS"),
            "md5": meta.get("bitstream", {}).get("md5"),
        }
    return out


def series(rows, test, valid_only=True, in_wire=False):
    out = [r for r in rows if r["test"] == test]
    if valid_only:
        out = [r for r in out if r["valid"] == 1]
    if in_wire:
        out = [r for r in out if r["over_wire_limit"] == 0]
    return sorted(out, key=lambda r: r["requested_pps"] or 0)


def knee(rows, test, limit=0.1):
    """Highest in-wire, valid offered rate whose loss stays under `limit` %."""
    good = [r for r in series(rows, test, valid_only=True, in_wire=True)
            if r["loss_pct"] is not None and r["loss_pct"] <= limit]
    if not good:
        return None
    return max(good, key=lambda r: r["achieved_pps"] or 0)


def peak_delivered(rows, test):
    s = series(rows, test, valid_only=True, in_wire=True)
    if not s:
        return None
    return max(s, key=lambda r: r["delivered_pps"] or 0)


def sim_rows(name="backpressure.csv"):
    p = os.path.join(SIM, name)
    if not os.path.isfile(p):
        return []
    with open(p) as fh:
        rows = list(csv.DictReader(fh))
    for r in rows:
        for k in ("batch", "wire_ns", "drain", "sockets", "fed_sockets", "txbuf",
                  "hot_every", "dg_socket0", "dg_socket1",
                  "packets_fed", "packets_offered", "datagrams", "records",
                  "min_free_b", "fsr_reads", "fsr_longest_run", "rsr_reads",
                  "spi_txns", "quiet_max_clks", "sockets_served", "livelock"):
            try:
                r[k] = int(r[k])
            except (ValueError, KeyError, TypeError):
                r[k] = None
        try:
            r["fsr_span_us"] = float(r["fsr_span_us"])
        except (ValueError, KeyError, TypeError):
            r["fsr_span_us"] = None
    return rows


def repeat_rows(tag="batch16"):
    """The repeatability run for one arm, grouped by offered rate."""
    import statistics
    path = os.path.join(RESULTS, tag, "repeat.csv")
    if not os.path.isfile(path):
        return []
    with open(path) as fh:
        rows = list(csv.DictReader(fh))
    by = {}
    for r in rows:
        try:
            rate = int(r["rate"])
            dp = float(r["delivered_pps"])
        except (ValueError, KeyError):
            continue
        if r.get("valid") == "0":
            continue
        by.setdefault(rate, []).append(dp)
    out = []
    for rate in sorted(by):
        v = by[rate]
        out.append({"rate": rate, "n": len(v), "median": statistics.median(v),
                    "lo": min(v), "hi": max(v),
                    "spread_pct": (100.0 * (max(v) - min(v)) / max(v)) if max(v) else 0.0})
    return out
