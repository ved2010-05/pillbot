r"""
pillbot.hardware.py - dispenser hardware-abstraction layer (the keystone).

dispense() used to be the one function welded to physical hardware, so on a
dev box the whole app was stuck in "degraded" mode and nothing downstream of
it could be tested. Here we put a DispenserBackend interface in front of it:

  - SerialBackend   drives a pyserial-compatible transport using the wire
                    protocol:  host -> "<slot>\n" ; device -> "OK:<slot>\n"
                    on success or "ERR:<slot>:<msg>\n" on fault. It retries
                    and reconnects. This is the real production path.
  - VirtualArduino  is a byte-level fake of the firmware (fault-injectable)
                    that lets the REAL SerialBackend code run with no board.
  - SimulatedBackend is just a SerialBackend driving a VirtualArduino, so
                    "sim mode" exercises the real protocol/retry code rather
                    than bypassing it.

Selection is via Settings.backend ("serial" | "sim" | "auto").
"""
from __future__ import annotations

import logging
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable, Dict, Optional

# pyserial is optional at import time so the app loads even where it is absent.
try:
    import serial as _pyserial
    from serial import SerialException
except Exception:  # pragma: no cover - only where pyserial is not installed
    _pyserial = None

    class SerialException(Exception):
        pass


log = logging.getLogger("pillbot.hardware")


@dataclass
class DispenseResult:
    ok: bool
    slot: int
    ack: str = ""
    error_code: str = ""
    attempts: int = 0
    latency_s: float = 0.0


def ack_is_success(ack: str, slot: int) -> bool:
    """EXACT match. Fixes the old bug where 'OK:1'.startswith('OK:1') also
    matched 'OK:10', 'OK:11', ... once slots exceeded 9."""
    return ack.strip() == f"OK:{slot}"


def parse_error_code(ack: str) -> str:
    """'ERR:<slot>:<msg>' -> '<msg>'."""
    parts = ack.strip().split(":", 2)
    return parts[2] if len(parts) >= 3 else "unknown"


class DispenserBackend(ABC):
    @abstractmethod
    def dispense_slot(self, slot: int) -> DispenseResult:
        ...

    def health(self) -> str:
        return "unknown"

    def close(self) -> None:
        pass


class VirtualArduino:
    r"""In-memory, byte-level emulation of the dispenser firmware. Exposes
    just enough of the pyserial surface (write/readline/reset_input_buffer/
    is_open/close) for SerialBackend to drive it unchanged.

    Inject faults per slot ('*' applies to all slots):
        'no_ack'     -> emit nothing (read times out)
        'jam'        -> ERR:<slot>:jam
        'empty'      -> ERR:<slot>:empty
        'garbage'    -> a non-conforming line
        'disconnect' -> raise SerialException on write (models a dropped link)
    """

    def __init__(self, slots: int = 3, faults: Optional[Dict[str, str]] = None):
        self.slots = slots
        self.faults: Dict[str, str] = dict(faults or {})
        self.is_open = True
        self.dispensed: Dict[int, int] = {}
        self._in = b""
        self._out = b""

    # --- pyserial-ish surface --------------------------------------------
    def write(self, data: bytes) -> int:
        if not self.is_open:
            raise SerialException("write to a closed virtual device")
        self._in += data
        while b"\n" in self._in:
            line, self._in = self._in.split(b"\n", 1)
            self._process(line.decode(errors="replace").strip())
        return len(data)

    def readline(self) -> bytes:
        if b"\n" in self._out:
            line, self._out = self._out.split(b"\n", 1)
            return line + b"\n"
        return b""  # nothing queued -> emulates a read timeout

    def reset_input_buffer(self) -> None:
        self._out = b""

    def close(self) -> None:
        self.is_open = False

    # --- firmware behaviour ----------------------------------------------
    def _fault_for(self, cmd: str) -> Optional[str]:
        return self.faults.get(cmd) or self.faults.get("*")

    def _process(self, cmd: str) -> None:
        fault = self._fault_for(cmd)
        if fault == "no_ack":
            return
        if fault == "garbage":
            self._out += b"NOISE\n"
            return
        if fault == "jam":
            self._out += f"ERR:{cmd}:jam\n".encode()
            return
        if fault == "empty":
            self._out += f"ERR:{cmd}:empty\n".encode()
            return
        if fault == "disconnect":
            self.is_open = False
            raise SerialException("virtual device disconnected")
        try:
            slot = int(cmd)
        except ValueError:
            self._out += f"ERR:{cmd}:badcmd\n".encode()
            return
        if slot < 1 or slot > self.slots:
            self._out += f"ERR:{cmd}:noslot\n".encode()
            return
        self.dispensed[slot] = self.dispensed.get(slot, 0) + 1
        self._out += f"OK:{slot}\n".encode()


class SerialBackend(DispenserBackend):
    """Production dispense path: protocol + retries + reconnect over any
    pyserial-compatible transport produced by `transport_factory`."""

    def __init__(self, transport_factory: Callable[[], object], retries: int = 3,
                 reconnect_delay: float = 1.0, label: str = "serial"):
        self._factory = transport_factory
        self._retries = max(1, retries)
        self._reconnect_delay = reconnect_delay
        self._label = label
        self._transport: Optional[object] = None
        self._lock = threading.Lock()

    def _ensure(self) -> Optional[object]:
        with self._lock:
            t = self._transport
            if t is not None and getattr(t, "is_open", False):
                return t
            try:
                self._transport = self._factory()
            except SerialException as exc:
                log.error("Backend %s: open failed: %s", self._label, exc)
                self._transport = None
            return self._transport

    def _reset(self) -> None:
        with self._lock:
            try:
                if self._transport is not None:
                    self._transport.close()
            except Exception:
                pass
            self._transport = None

    def dispense_slot(self, slot: int) -> DispenseResult:
        start = time.monotonic()
        for attempt in range(1, self._retries + 1):
            t = self._ensure()
            if t is None:
                time.sleep(self._reconnect_delay)
                continue
            try:
                # NOTE: if write/readline raise, the `with` releases the lock
                # before the except below runs, so _reset() does not deadlock.
                with self._lock:
                    t.reset_input_buffer()
                    t.write(f"{slot}\n".encode())
                    ack = t.readline().decode(errors="replace").strip()
            except SerialException as exc:
                log.error("Backend %s: serial error attempt %d: %s", self._label, attempt, exc)
                self._reset()
                continue

            if ack_is_success(ack, slot):
                return DispenseResult(True, slot, ack=ack, attempts=attempt,
                                      latency_s=time.monotonic() - start)
            if ack.startswith("ERR:"):
                # Hardware reported a definite fault - do not retry.
                return DispenseResult(False, slot, ack=ack,
                                      error_code=parse_error_code(ack),
                                      attempts=attempt, latency_s=time.monotonic() - start)
            log.warning("Backend %s: unexpected ack %r (attempt %d)", self._label, ack, attempt)

        return DispenseResult(False, slot, error_code="no_ack",
                              attempts=self._retries, latency_s=time.monotonic() - start)

    def health(self) -> str:
        t = self._ensure()
        return "ok" if (t is not None and getattr(t, "is_open", False)) else "unavailable"

    def close(self) -> None:
        self._reset()


def SimulatedBackend(slots: int = 3, faults: Optional[Dict[str, str]] = None,
                     retries: int = 3) -> SerialBackend:
    """A SerialBackend whose transport is a shared VirtualArduino, so the real
    protocol/retry code runs with zero hardware. The device is reachable via
    `backend.device` for tests and inventory inspection."""
    device = VirtualArduino(slots=slots, faults=faults)
    backend = SerialBackend(lambda: device, retries=retries, reconnect_delay=0.0, label="sim")
    backend.device = device  # type: ignore[attr-defined]
    return backend


def _real_serial_factory(port: str, baud: int, timeout: float) -> Callable[[], object]:
    def _open() -> object:
        if _pyserial is None:
            raise SerialException("pyserial is not installed")
        return _pyserial.Serial(port, baud, timeout=timeout)
    return _open


def make_backend(settings) -> DispenserBackend:
    """Build the backend selected by settings.backend ('serial'|'sim'|'auto')."""
    mode = getattr(settings, "backend", "auto")
    if mode == "sim":
        log.info("Dispenser backend: SIMULATOR (%d slots)", settings.sim_slots)
        return SimulatedBackend(slots=settings.sim_slots, retries=settings.dispense_retries)

    factory = _real_serial_factory(settings.serial_port, settings.serial_baud, settings.serial_timeout)
    serial_backend = SerialBackend(factory, retries=settings.dispense_retries, label="serial")

    if mode == "serial":
        log.info("Dispenser backend: SERIAL (%s)", settings.serial_port)
        return serial_backend

    # auto: use the real port if it opens right now, else fall back to the simulator.
    if serial_backend.health() == "ok":
        log.info("Dispenser backend: SERIAL auto-detected (%s)", settings.serial_port)
        return serial_backend
    log.warning("Dispenser backend: no serial on %s - falling back to SIMULATOR.", settings.serial_port)
    return SimulatedBackend(slots=settings.sim_slots, retries=settings.dispense_retries)
