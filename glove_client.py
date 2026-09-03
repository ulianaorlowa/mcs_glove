"""
High-level motor / battery API for the user app.
Wraps DebugClient with finger-level commands.
All public methods are BLOCKING (call from worker threads, not GUI thread).
"""
import time
from typing import Optional, Callable

from devclient import DebugClient
from models import Finger, FINGER_CHANNEL

NUM_CHANNELS = 8   

# DRV2605L registers
REG_STATUS   = 0x00
REG_MODE     = 0x01
REG_RTP      = 0x02
REG_LIBRARY  = 0x03
REG_WAVE1    = 0x04
REG_WAVE2    = 0x05
REG_GO       = 0x0C

MODE_INTERNAL = 0x00
MODE_RTP      = 0x05
MODE_DIAG     = 0x06
MODE_STANDBY  = 0x40

DEFAULT_LIBRARY = 2          # Library B — same as devtool
EFFECT_MIN = 1               # ROM library effect numbers, per DRV2605L datasheet
EFFECT_MAX = 123        

# MAX17055 fuel gauge registers
GAUGE_VCELL   = 0x09
GAUGE_REPSOC  = 0x06
GAUGE_CURRENT = 0x0A
GAUGE_TEMP    = 0x08

RSENSE_MOHM = 10.0


class GloveClient:
    """
    User-level glove API.  Call connect() once, then motor_*/battery_*
    from worker threads.  The underlying DebugClient serialises BLE traffic.
    """

    def __init__(self):
        self.dbg = DebugClient()
        self.device_info: dict = {}

    # ── connection ────────────────────────────────────────

    @property
    def connected(self) -> bool:
        return self.dbg.connected

    def connect(self, name="MCS Glove", timeout=10.0):
        """Blocking scan + connect.  Raises on failure."""
        self.dbg.connect(name=name, timeout=timeout)
        self.device_info = self.dbg.read_device_info()

    def disconnect(self):
        self.dbg.disconnect()

    def close(self):
        self.dbg.close()

    # ── motor: vibration (RTP) ────────────────────────────

    def vibration_on(self, finger: Finger, intensity_pct: int = 60):
        """Start continuous vibration.  intensity_pct 0-100."""
        ch = FINGER_CHANNEL[finger]
        target = max(0, min(255, intensity_pct * 255 // 100))
        self.dbg.write_reg(ch, REG_MODE, MODE_RTP)
        self.dbg.read_reg(ch, REG_STATUS)          # clear stale latches
        self.dbg.write_reg(ch, REG_RTP, target)

    def vibration_off(self, finger: Finger):
        ch = FINGER_CHANNEL[finger]
        self.dbg.write_reg(ch, REG_RTP, 0)
        self.dbg.write_reg(ch, REG_MODE, MODE_STANDBY)

    def all_off(self):
        """Emergency/normal stop — every hardware channel"""
        for ch in range(NUM_CHANNELS):
            self.dbg.stop_priority(ch)

    # ── motor: tick (ROM library effect) ──────────────────

    def tick(self, finger: Finger, effect_id: int = 1):
        """Play a single ROM library effect, blocks ~0.5 s."""
        if not EFFECT_MIN <= effect_id <= EFFECT_MAX:
            raise ValueError(
                f"effect_id must be {EFFECT_MIN}..{EFFECT_MAX}, got {effect_id}"
            )
        ch = FINGER_CHANNEL[finger]
        self.dbg.write_reg(ch, REG_MODE, MODE_INTERNAL)
        self.dbg.write_reg(ch, REG_LIBRARY, DEFAULT_LIBRARY)
        self.dbg.read_reg(ch, REG_STATUS)
        self.dbg.write_reg(ch, REG_WAVE1, effect_id)
        self.dbg.write_reg(ch, REG_WAVE2, 0x00)
        self.dbg.write_reg(ch, REG_GO, 0x01)
        time.sleep(0.5)
        self.dbg.write_reg(ch, REG_MODE, MODE_STANDBY)

    # ── diagnostics ───────────────────────────────────────

    def diagnose_finger(self, finger: Finger) -> dict:
        """
        Run DRV2605L diagnostics mode on one finger.
        Returns dict with keys: ok (bool), detail (str), status_raw (int).
        Also does a quick vibration burst so the user can physically confirm.
        """
        ch = FINGER_CHANNEL[finger]

        # 1) DRV hardware diagnostics
        r = self.dbg.run_mode(ch, MODE_DIAG, wait=2.0)
        if not r["ok"]:
            return {"ok": False, "detail": "I²C communication failed", "status_raw": -1}

        status = r["status"]
        diag_fail = bool((status >> 3) & 1)
        overtemp  = bool((status >> 1) & 1)
        oc_detect = bool(status & 1)

        problems = []
        if diag_fail:  problems.append("calibration error")
        if overtemp:   problems.append("overtemperature")
        if oc_detect:  problems.append("overcurrent / no motor")

        # 2) short vibration burst so user can feel it
        if not problems:
            self.dbg.write_reg(ch, REG_MODE, MODE_RTP)
            self.dbg.write_reg(ch, REG_RTP, 0x50)      # ~30% intensity
            time.sleep(0.8)
            self.dbg.write_reg(ch, REG_RTP, 0)
        self.dbg.write_reg(ch, REG_MODE, MODE_STANDBY)

        return {
            "ok": len(problems) == 0,
            "detail": ", ".join(problems) if problems else "OK",
            "status_raw": status,
        }

    # ── battery ───────────────────────────────────────────

    def battery_info(self) -> dict:
        """Read key battery metrics.  Returns dict with mV, pct, mA, temp_c."""
        def _read(reg):
            r = self.dbg.read_gauge(reg)
            return r["word"] if r["ok"] else 0

        vcell = _read(GAUGE_VCELL)
        soc   = _read(GAUGE_REPSOC)
        cur   = _read(GAUGE_CURRENT)
        temp  = _read(GAUGE_TEMP)

        def s16(v):
            return v - 65536 if v >= 32768 else v

        return {
            "voltage_mv": int(vcell * 5 / 64),
            "percent":    soc >> 8,
            "current_ma": round(s16(cur) * 1.5625 / RSENSE_MOHM, 1),
            "temp_c":     round(s16(temp) / 256, 1),
        }