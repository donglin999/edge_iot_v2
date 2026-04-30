#!/usr/bin/env python3
"""Realistic Modbus TCP mock server.

Behaves like a small industrial RTU/PLC: full MBAP-on-TCP framing, function
codes 0x01/0x02/0x03/0x04, slave id echoed back. Each register address is
mapped to a *physical signal generator* so the values evolve smoothly over
time the way real instruments do — useful for E2E acquisition tests because
charts look meaningful and threshold alarms can be reproducibly triggered.

Address map (matches the backend point configuration; function code 3):

    0       出口温度    int16,   raw / 10   ⇒ 77.0–83.0 ℃    (coefficient 0.1)
    1       环境湿度    int16,   raw / 10   ⇒ 50.0–60.0 %    (coefficient 0.1)
    2..3    出口压力    float32 ABCD,       ⇒ 145.0–155.0 kPa (coefficient 1.0)
    4..5    电机转速    int32 ABCD,         ⇒ 1470–1530 rpm   (coefficient 1.0)
    6       振动        int16,   raw / 100  ⇒ 1.70–2.30 mm/s (coefficient 0.01)
    7       流量        int16,   raw / 10   ⇒ 115.0–125.0    (coefficient 0.1)
    8       电流        int16,   raw / 10   ⇒ 14.0–16.0 A    (coefficient 0.1)
    9       电压        int16,   raw / 10   ⇒ 218.0–222.0 V  (coefficient 0.1)
    10      运行状态    int16,              ⇒ 0=停机 / 1=运行 / 2=故障
    100+    其他        deterministic but unmapped — start_addr*7 + cycle

Run:
    python /mock/modbus_realistic.py
or via docker compose's mock-modbus container.
"""
from __future__ import annotations

import logging
import math
import random
import socket
import struct
import time
from socketserver import BaseRequestHandler, TCPServer, ThreadingMixIn

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("modbus-mock")


# ---------------------------------------------------------------------------
# Signal generators
# ---------------------------------------------------------------------------


_T0 = time.time()


def _tnow() -> float:
    return time.time() - _T0


def _smooth(base: float, amp: float, period_s: float, noise: float = 0.0) -> float:
    """Slow sine drift around `base` plus optional gaussian noise.

    Phase is anchored to wall-clock epoch time (not to process start), so
    restarting the mock container does not reset the sine waveform — the
    curve stays continuous across restarts. Noise is intentionally still
    drawn fresh each call (independent samples) so the trace doesn't look
    artificially periodic.
    """
    t = time.time()
    phase = 2.0 * math.pi * t / period_s
    return base + amp * math.sin(phase) + random.gauss(0, noise)


# Each generator returns the *engineering* value; the per-address encoder
# below converts it into the right number of uint16 words (1 for int16 points,
# 2 for the float32 / int32 points pressure & rpm).

def _temperature_raw() -> int:
    # 80.0 ℃ ± 3.0 with 5-min period; coefficient 0.1 ⇒ raw ≈ 800 ± 30
    return int(round(_smooth(800.0, 30.0, 300.0, 3.0)))


def _humidity_raw() -> int:
    # 55.0 % ± 5.0 with 10-min period; coefficient 0.1 ⇒ raw ≈ 550 ± 50
    return int(round(_smooth(550.0, 50.0, 600.0, 5.0)))


def _pressure_kpa() -> float:
    # 150.0 kPa ± 5.0 with 2-min period; transmitted as float32 (coefficient 1)
    return _smooth(150.0, 5.0, 120.0, 0.5)


def _rpm() -> int:
    # 1500 rpm ± 30 with 1-min period; transmitted as int32 (coefficient 1)
    return int(round(_smooth(1500.0, 30.0, 60.0, 5.0)))


def _vibration_raw() -> int:
    # 2.00 mm/s ± 0.30 with 30-s period; coefficient 0.01 ⇒ raw ≈ 200 ± 30
    base = _smooth(200.0, 30.0, 30.0, 5.0)
    # Occasional fault spike (≈ 6 mm/s = raw 600) to feed alarm pipeline
    if random.random() < 0.005:
        base += random.uniform(300.0, 500.0)
    return max(0, int(round(base)))


def _flow_raw() -> int:
    # 120.0 m³/h ± 5.0 with 3-min period; coefficient 0.1 ⇒ raw ≈ 1200 ± 50
    return max(0, int(round(_smooth(1200.0, 50.0, 180.0, 8.0))))


def _current_raw() -> int:
    # 15.0 A ± 1.0 with 1.5-min period; coefficient 0.1 ⇒ raw ≈ 150 ± 10
    return max(0, int(round(_smooth(150.0, 10.0, 90.0, 2.0))))


def _voltage_raw() -> int:
    # 220.0 V ± 2.0 with 1-min period; coefficient 0.1 ⇒ raw ≈ 2200 ± 20
    return int(round(_smooth(2200.0, 20.0, 60.0, 3.0)))


def _device_state() -> int:
    # ~98% running, occasional stop or alarm so transitions are observable.
    r = random.random()
    if r < 0.01:
        return 0    # stopped
    if r < 0.02:
        return 2    # alarm/fault
    return 1        # running


# ---------------------------------------------------------------------------
# Register encoding
# ---------------------------------------------------------------------------


def _u16(value: int) -> int:
    """Clamp to unsigned 16-bit."""
    return max(0, min(0xFFFF, int(value)))


def _f32_be_words(value: float) -> tuple[int, int]:
    """IEEE-754 big-endian: 2 registers, ABCD byte order."""
    packed = struct.pack(">f", float(value))
    hi = struct.unpack(">H", packed[0:2])[0]
    lo = struct.unpack(">H", packed[2:4])[0]
    return (hi, lo)


def _i32_be_words(value: int) -> tuple[int, int]:
    """Signed 32-bit big-endian: 2 registers, ABCD byte order."""
    packed = struct.pack(">i", int(value))
    hi = struct.unpack(">H", packed[0:2])[0]
    lo = struct.unpack(">H", packed[2:4])[0]
    return (hi, lo)


def _build_register_window(start: int, qty: int) -> list[int]:
    """Compose a contiguous block of `qty` uint16 registers starting at `start`.

    Pressure (addr 2..3) is encoded as a single float32 ABCD across two
    registers; rpm (addr 4..5) as a single int32 ABCD. Both samples are taken
    once per request so the high/low words are mathematically consistent
    even when the backend reads the whole window in one shot.
    """
    pressure_words = _f32_be_words(_pressure_kpa())
    rpm_words = _i32_be_words(_rpm())

    out: list[int] = []
    for i in range(qty):
        addr = start + i
        if addr == 0:
            out.append(_u16(_temperature_raw()))
        elif addr == 1:
            out.append(_u16(_humidity_raw()))
        elif addr == 2:
            out.append(pressure_words[0])      # float32 hi
        elif addr == 3:
            out.append(pressure_words[1])      # float32 lo
        elif addr == 4:
            out.append(rpm_words[0])           # int32 hi
        elif addr == 5:
            out.append(rpm_words[1])           # int32 lo
        elif addr == 6:
            out.append(_u16(_vibration_raw()))
        elif addr == 7:
            out.append(_u16(_flow_raw()))
        elif addr == 8:
            out.append(_u16(_current_raw()))
        elif addr == 9:
            out.append(_u16(_voltage_raw()))
        elif addr == 10:
            out.append(_u16(_device_state()))
        else:
            # Deterministic-ish filler for any other address.
            out.append(_u16((addr * 7 + int(_tnow())) & 0xFFFF))
    return out


def _build_coil_window(start: int, qty: int) -> list[int]:
    """Coils as packed bits — toggle in a slow waveform per address."""
    bits = []
    for i in range(qty):
        addr = start + i
        # Each coil is "on" for half of a 30s cycle, offset by addr.
        phase = (_tnow() + addr * 1.7) % 30.0
        bits.append(1 if phase < 15.0 else 0)
    # Pack into bytes, LSB-first within each byte (Modbus convention).
    out_bytes = []
    for i in range(0, len(bits), 8):
        chunk = bits[i : i + 8]
        byte = 0
        for j, bit in enumerate(chunk):
            byte |= (bit & 1) << j
        out_bytes.append(byte)
    return out_bytes


# ---------------------------------------------------------------------------
# Modbus TCP framing
# ---------------------------------------------------------------------------


def _exception(tid: int, uid: int, fc: int, ex: int) -> bytes:
    pdu = bytes([fc | 0x80, ex])
    return struct.pack(">HHH", tid, 0, len(pdu) + 1) + bytes([uid]) + pdu


def _wrap(tid: int, uid: int, pdu: bytes) -> bytes:
    return struct.pack(">HHH", tid, 0, len(pdu) + 1) + bytes([uid]) + pdu


class ModbusHandler(BaseRequestHandler):
    def handle(self):  # noqa: C901 — straight-line protocol parser is fine here
        peer = self.client_address
        log.info("conn open from %s:%s", *peer)
        try:
            while True:
                # Read MBAP header
                hdr = self._recv_exact(7)
                if hdr is None:
                    break
                tid, proto, length, uid = struct.unpack(">HHHB", hdr)
                if proto != 0:
                    log.warning("non-Modbus protocol id %d, dropping", proto)
                    return
                pdu = self._recv_exact(length - 1)
                if pdu is None or len(pdu) < 1:
                    break
                fc = pdu[0]

                if fc in (0x03, 0x04) and len(pdu) >= 5:
                    start, qty = struct.unpack(">HH", pdu[1:5])
                    if not (1 <= qty <= 125):
                        self.request.sendall(_exception(tid, uid, fc, 0x03))
                        continue
                    regs = _build_register_window(start, qty)
                    body = struct.pack("B", qty * 2) + b"".join(struct.pack(">H", r) for r in regs)
                    resp = _wrap(tid, uid, bytes([fc]) + body)
                    self.request.sendall(resp)
                    log.info("fc=%d start=%d qty=%d → first=%d", fc, start, qty, regs[0] if regs else -1)

                elif fc in (0x01, 0x02) and len(pdu) >= 5:
                    start, qty = struct.unpack(">HH", pdu[1:5])
                    if not (1 <= qty <= 2000):
                        self.request.sendall(_exception(tid, uid, fc, 0x03))
                        continue
                    coil_bytes = _build_coil_window(start, qty)
                    body = struct.pack("B", len(coil_bytes)) + bytes(coil_bytes)
                    resp = _wrap(tid, uid, bytes([fc]) + body)
                    self.request.sendall(resp)
                    log.info("fc=%d start=%d qty=%d (%d bytes)", fc, start, qty, len(coil_bytes))

                else:
                    log.warning("unsupported fc=%d", fc)
                    self.request.sendall(_exception(tid, uid, fc, 0x01))
        except (ConnectionResetError, BrokenPipeError):
            pass
        except Exception:  # noqa: BLE001
            log.exception("handler error")
        finally:
            try:
                self.request.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self.request.close()
            except OSError:
                pass
            log.info("conn closed from %s:%s", *peer)

    def _recv_exact(self, n: int) -> bytes | None:
        """Read exactly n bytes; return None on EOF."""
        buf = b""
        while len(buf) < n:
            chunk = self.request.recv(n - len(buf))
            if not chunk:
                return None
            buf += chunk
        return buf


class ThreadedTCPServer(ThreadingMixIn, TCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main(host: str = "0.0.0.0", port: int = 5020) -> None:
    log.info("Realistic Modbus TCP mock starting on %s:%d", host, port)
    log.info("address map:")
    log.info("  0       temperature  int16   raw/10  ℃   (~800 raw → 80.0 ℃)")
    log.info("  1       humidity     int16   raw/10  %   (~550 raw → 55.0 %)")
    log.info("  2..3    pressure     float32 ABCD       (~150.0 kPa)")
    log.info("  4..5    rpm          int32 ABCD          (~1500 rpm)")
    log.info("  6       vibration    int16   raw/100 mm/s (~200 raw → 2.00 mm/s)")
    log.info("  7       flow         int16   raw/10        (~1200 raw → 120.0)")
    log.info("  8       current      int16   raw/10  A   (~150 raw → 15.0 A)")
    log.info("  9       voltage      int16   raw/10  V   (~2200 raw → 220.0 V)")
    log.info("  10      state        int16            0=stop 1=run 2=alarm")
    server = ThreadedTCPServer((host, port), ModbusHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")
        server.shutdown()


if __name__ == "__main__":
    import os

    main(host=os.environ.get("MOCK_HOST", "0.0.0.0"), port=int(os.environ.get("MOCK_PORT", "5020")))
