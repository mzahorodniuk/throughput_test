"""Provenance for one campaign arm.

Every number this campaign produces has to be attributable to a specific
bitstream. The archived `bitstream.bit.batch16` / `.batch1fix` files in the repo
show what happens otherwise: nothing in the tree records what they were built
from, so they cannot back a claim. This records the md5 of the file that was
actually flashed, the batching generics read out of the HDL that built it, and
the host-side settings that bound the measurement.
"""

import hashlib
import json
import os
import re
import subprocess
import time

REPO = "/home/misha/FPGA-centric-SCIS"
FSM = os.path.join(REPO, "hardware/hdl/w5500/w5500_state_machine.vhd")
BIT = os.path.join(REPO, "hardware/fpga/ccgm1a1/bitstream.bit")

GENERICS = ["TX_BATCH_MAX_PACKETS", "TX_FLUSH_IDLE_CLKS", "TX_MAX_OPEN_CLKS",
            "WATCHDOG_CLKS", "minimum_free_tx_buffer_memory"]


def _sh(cmd, cwd=None):
    try:
        return subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True,
                              text=True, timeout=20).stdout.strip()
    except Exception:
        return ""


def hdl_generics(path=FSM):
    """Read the batching generics out of the HDL, as built."""
    out = {}
    try:
        with open(path) as fh:
            text = fh.read()
    except OSError:
        return out
    for g in GENERICS:
        m = re.search(rf"^\s*(?:constant\s+)?{g}\s*:\s*(?:integer|unsigned\([^)]*\))\s*:?=\s*"
                      rf"(?:to_unsigned\()?(\d+)", text, re.M)
        if m:
            out[g] = int(m.group(1))
    return out


def md5(path):
    try:
        h = hashlib.md5()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _read(path):
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return None


def collect(tag, nic="enp2s0", extra=None):
    m = {
        "tag": tag,
        "collected_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "bitstream": {
            "path": BIT,
            "md5": md5(BIT),
            "size": os.path.getsize(BIT) if os.path.exists(BIT) else None,
            "mtime": (time.strftime("%Y-%m-%dT%H:%M:%S",
                                    time.localtime(os.path.getmtime(BIT)))
                      if os.path.exists(BIT) else None),
        },
        "hdl_generics": hdl_generics(),
        "git": {
            "head": _sh("git rev-parse --short HEAD", REPO),
            "branch": _sh("git rev-parse --abbrev-ref HEAD", REPO),
            "dirty_files": len([l for l in _sh("git status --porcelain", REPO)
                                .splitlines() if l.strip()]),
        },
        "host": {
            "nic": nic,
            "nic_speed_mbit": _read(f"/sys/class/net/{nic}/speed"),
            "nic_duplex": _read(f"/sys/class/net/{nic}/duplex"),
            "rmem_max": _read("/proc/sys/net/core/rmem_max"),
            "netdev_max_backlog": _read("/proc/sys/net/core/netdev_max_backlog"),
            "ncpu": os.cpu_count(),
            "kernel": _sh("uname -r"),
        },
    }
    if extra:
        m.update(extra)
    return m


def batch_size():
    return hdl_generics().get("TX_BATCH_MAX_PACKETS")


def write(path, m):
    with open(path, "w") as fh:
        json.dump(m, fh, indent=2, sort_keys=True)
    return path


if __name__ == "__main__":
    import sys
    print(json.dumps(collect(sys.argv[1] if len(sys.argv) > 1 else "adhoc"),
                     indent=2, sort_keys=True))
