"""Serial control of the Nucleo CAN load generator.

The firmware speaks one command per line at 115200 on /dev/ttyACM0 (see
firmware/src/main.cpp). Keeping it here means the CAN and combined tests drive
the board through one small object instead of open-coding serial handling twice.
"""

import time

try:
    import serial
except ImportError:                                    # pragma: no cover
    raise SystemExit(
        "pyserial is required for the CAN tests:\n"
        "    pip3 install pyserial"
    )

DEFAULT_PORT = "/dev/ttyACM0"
BAUD = 115200


class Nucleo:
    """Thin wrapper over the firmware's RATE / STOP / STAT / PING protocol."""

    def __init__(self, port=DEFAULT_PORT, baud=BAUD, timeout=2.0):
        self.ser = serial.Serial(port, baud, timeout=timeout)
        # The board resets when the port opens; give the banner time to land.
        time.sleep(2.0)
        self.ser.reset_input_buffer()

    def _cmd(self, text, expect_prefix=None):
        self.ser.write((text + "\n").encode())
        self.ser.flush()
        deadline = time.time() + 3.0
        while time.time() < deadline:
            line = self.ser.readline().decode(errors="replace").strip()
            if not line or line.startswith("#"):       # banner / comment lines
                continue
            if expect_prefix is None or line.startswith(expect_prefix):
                return line
            if line.startswith("ERR"):
                raise RuntimeError(f"Nucleo rejected {text!r}: {line}")
        raise TimeoutError(f"no reply to {text!r}")

    def ping(self):
        return self._cmd("PING", "PONG")

    def start(self, fps):
        """Begin sending at `fps` frames/s; resets the firmware's counters."""
        return self._cmd(f"RATE {int(fps)}", "OK RATE")

    def stop(self):
        return self._cmd("STOP", "OK STOP")

    def stat(self):
        """Return {'sent': n, 'failed': n, 'tec': n, 'rec': n}.

        `failed` is frames that never reached the bus because the TX FIFO was
        full -- our limit, not the FPGA's. Loss with failed == 0 is the FPGA.
        """
        line = self._cmd("STAT", "STAT")
        out = {}
        for token in line.split()[1:]:
            key, _, value = token.partition("=")
            out[key] = int(value)
        return out

    def close(self):
        try:
            self.stop()
        except Exception:
            pass
        self.ser.close()
