"""
devclient.py — BLE client for the MCS Glove DEBUG interface (developer tool).

8-byte debug protocol on the Debug characteristic:
  write [opcode, target, reg, value, 0,0,0,0]  ->  read back 8 bytes:
  [opcode, target, reg, value_lo, value_hi, ok, _, _]

Opcodes:
  0x01 read  DRV register   (target=channel 0..7, reg)
  0x02 write DRV register   (target=channel, reg, value)
  0x03 run mode + GO        (target=channel, value=mode; returns STATUS)
  0x04 read MAX17055 word   (reg=gauge register; lo/hi = 16-bit word)

Owns a background asyncio loop so the GUI calls plain blocking methods.
A lock serialises every write+read round trip, so battery polling and motor
commands from different threads can't interleave on the shared result buffer.
"""

from __future__ import annotations
import asyncio
import threading
from concurrent.futures import Future
from typing import Optional, Callable

from bleak import BleakScanner, BleakClient

DEBUG_UUID = "25b63a19-e70e-4040-bd91-85623b961384"

DIS_UUIDS = {
    "manufacturer": "00002a29-0000-1000-8000-00805f9b34fb",
    "model":        "00002a24-0000-1000-8000-00805f9b34fb",
    "hw_rev":       "00002a27-0000-1000-8000-00805f9b34fb",
    "fw_rev":       "00002a26-0000-1000-8000-00805f9b34fb",
    "serial":       "00002a25-0000-1000-8000-00805f9b34fb",
}


class DebugClient:
    def __init__(self):
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        self._client: Optional[BleakClient] = None
        self._lock = threading.Lock()          # serialise debug round trips
        self.on_status: Optional[Callable[[str], None]] = None

    def _run_loop(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _submit(self, coro) -> Future:
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def _status(self, msg):
        (self.on_status or print)(msg)

    # ---- connection ----
    def connect(self, name="MCS Glove", timeout=10.0):
        return self._submit(self._connect(name, timeout)).result()

    async def _connect(self, name, timeout):
        self._status("Scanning...")
        dev = await BleakScanner.find_device_by_name(name, timeout=timeout)
        if dev is None:
            raise RuntimeError(f"Device '{name}' not found")
        self._status(f"Connecting to {name}...")
        self._client = BleakClient(dev)
        await self._client.connect()
        self._status("Connected.")

    def read_device_info(self):
        return self._submit(self._read_device_info()).result()

    async def _read_device_info(self):
        info = {"address": self._client.address if self._client else None}
        for key, uuid in DIS_UUIDS.items():
            try:
                raw = await self._client.read_gatt_char(uuid)
                info[key] = bytes(raw).decode("utf-8", "ignore").strip("\x00").strip()
            except Exception:
                info[key] = None
        return info

    @property
    def connected(self):
        return self._client is not None and self._client.is_connected

    def disconnect(self):
        if self._client:
            try:
                self._submit(self._client.disconnect()).result()
            finally:
                self._client = None

    def close(self):
        try:
            self.disconnect()
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)

    # ---- low-level round trip ----
    async def _cmd(self, payload, wait):
        if not self.connected:
            raise RuntimeError("Not connected")
        await self._client.write_gatt_char(DEBUG_UUID, bytes(payload), response=True)
        await asyncio.sleep(wait)
        r = await self._client.read_gatt_char(DEBUG_UUID)
        return list(r)

    def _round_trip(self, payload, wait):
        with self._lock:                       # atomic write+read
            return self._submit(self._cmd(payload, wait)).result()

    # ---- public ops (blocking) ----
    def read_reg(self, ch, reg):
        r = self._round_trip([0x01, ch, reg, 0, 0, 0, 0, 0], 0.06)
        return {"value": r[3], "ok": bool(r[5])}

    def write_reg(self, ch, reg, val):
        r = self._round_trip([0x02, ch, reg, val, 0, 0, 0, 0], 0.06)
        return {"value": r[3], "ok": bool(r[5])}

    def run_mode(self, ch, mode, wait=2.5):
        r = self._round_trip([0x03, ch, 0x00, mode, 0, 0, 0, 0], wait)
        return {"status": r[3], "timed_out": bool(r[4]), "ok": bool(r[5])}

    def read_gauge(self, reg, wait=0.08):
        r = self._round_trip([0x04, 0, reg, 0, 0, 0, 0, 0], wait)
        return {"word": r[3] | (r[4] << 8), "ok": bool(r[5])}
    
    def stop_priority(self, ch):
        """Fire a standby write WITHOUT waiting for the command lock — used by
        the emergency stop so it can't get stuck behind queued reads."""
        try:
            self._submit(self._cmd([0x02, ch, 0x01, 0x40, 0, 0, 0, 0], 0.0)).result(timeout=2)
        except Exception:
            pass