"""
pillbot.clock.py - injectable time source.

The two most safety-critical functions (the dose safety gate and the
scheduler) must not call datetime.now()/time.monotonic() directly, or they
become non-deterministic and untestable. Routing all time through a Clock
lets tests freeze/advance time, and gives us a single seam to later harden
against wall-clock rollback using the monotonic source.
"""
from __future__ import annotations

import datetime
import threading
import time


class Clock:
    """Abstract time source."""

    def now(self) -> datetime.datetime:
        raise NotImplementedError

    def monotonic(self) -> float:
        raise NotImplementedError


class SystemClock(Clock):
    """Real wall-clock + monotonic source (production default)."""

    def now(self) -> datetime.datetime:
        return datetime.datetime.now()

    def monotonic(self) -> float:
        return time.monotonic()


class ManualClock(Clock):
    """Test double: time only moves when you tell it to."""

    def __init__(self, start: datetime.datetime, mono: float = 0.0):
        self._now = start
        self._mono = mono
        self._lock = threading.Lock()

    def set(self, when: datetime.datetime) -> None:
        with self._lock:
            self._now = when

    def advance(self, **delta) -> None:
        """advance(hours=4, minutes=30, ...) - moves wall + monotonic clocks together."""
        step = datetime.timedelta(**delta)
        with self._lock:
            self._now = self._now + step
            self._mono += step.total_seconds()

    def now(self) -> datetime.datetime:
        with self._lock:
            return self._now

    def monotonic(self) -> float:
        with self._lock:
            return self._mono


class MonotonicGuard(Clock):
    """Wraps a base clock with a persistent, non-decreasing high-water-mark, so
    the safety window cannot be reset by rolling the system clock BACKWARD (or by
    a Pi with no RTC booting at epoch). now() returns max(base.now(), high-water);
    a backward jump is clamped forward and reported via on_rollback. The HWM is
    seeded once from persistent storage at construction, then kept in memory and
    written back through save_hwm."""

    def __init__(self, base: Clock, load_hwm, save_hwm, on_rollback=None):
        self._base = base
        self._save = save_hwm
        self._on_rollback = on_rollback
        try:
            self._hwm = load_hwm()
        except Exception:
            self._hwm = None

    def now(self) -> datetime.datetime:
        base = self._base.now()
        if self._hwm is not None and base < self._hwm:
            if self._on_rollback is not None:
                try:
                    self._on_rollback(base, self._hwm)
                except Exception:
                    pass
            effective = self._hwm
        else:
            effective = base
        if self._hwm is None or effective > self._hwm:
            self._hwm = effective
            try:
                self._save(effective)
            except Exception:
                pass
        return effective

    def monotonic(self) -> float:
        return self._base.monotonic()


_clock: Clock = SystemClock()


def get_clock() -> Clock:
    return _clock


def set_clock(clock: Clock) -> None:
    """Swap the global clock. Tests use this; production never calls it."""
    global _clock
    _clock = clock


def now() -> datetime.datetime:
    return _clock.now()


def monotonic() -> float:
    return _clock.monotonic()
