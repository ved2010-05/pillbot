"""
pillbot.dispenser.py - magazine-aware dispensing service.

This is the orchestration that the app calls instead of the old slot-based
dispense(): given a MEDICINE, it picks a ready magazine, actuates that bay's
servo through an injected actuator, and then SENSOR-VERIFIES that exactly one
pill physically left before the dose is counted.

Separation of concerns:
  - inventory + physical verification   -> here (DispenserService + registry)
  - per-user dose limits (max/day, etc.) -> the safety gate, consulted BEFORE
                                            calling dispense()

The actuator is just a Callable[[bay], ActuateResult], so the real hardware
(SerialBackend driving the servo) and the simulator plug in identically.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, Optional, Tuple

from pillbot.magazines import MagazineRegistry, VirtualPillSensor


@dataclass
class ActuateResult:
    ok: bool
    message: str = ""


@dataclass
class DispenseOutcome:
    ok: bool
    medicine: str
    bay: Optional[int]
    tag_id: Optional[str]
    remaining: Optional[int]
    message: str


class DispenserService:
    def __init__(self, registry: MagazineRegistry, actuate: Callable[[int], ActuateResult]):
        self.registry = registry
        self._actuate = actuate

    def can_dispense(self, medicine: str) -> Tuple[bool, str]:
        return self.registry.can_dispense(medicine)

    def dispense(self, medicine: str) -> DispenseOutcome:
        ok, reason = self.registry.can_dispense(medicine)
        if not ok:
            return DispenseOutcome(False, medicine, None, None, None, reason)

        mag = self.registry.magazine_for(medicine)
        if mag is None:  # defensive; can_dispense already vetted this
            return DispenseOutcome(False, medicine, None, None, None, "no ready magazine")

        res = self._actuate(mag.bay)
        if not res.ok:
            return DispenseOutcome(False, medicine, mag.bay, mag.tag_id,
                                   mag.remaining, f"actuator fault: {res.message}")

        # Hardware claims success — now require independent sensor proof.
        verified, vmsg = self.registry.record_dispense(mag.tag_id)
        remaining = self.registry.by_tag[mag.tag_id].remaining
        return DispenseOutcome(verified, medicine, mag.bay, mag.tag_id, remaining, vmsg)


# --------------------------------------------------------------------------
# Simulator wiring: an actuator whose "servo" decrements the shared sensor,
# so the full dispense -> sensor-verify loop runs with no hardware. Faults:
#   'jam' -> servo reports OK but no pill drops (sensor unchanged; caught)
#   'err' -> servo reports a hardware fault
# --------------------------------------------------------------------------
def make_sim_actuator(sensor: VirtualPillSensor, registry: MagazineRegistry,
                      faults: Optional[Dict[int, str]] = None) -> Callable[[int], ActuateResult]:
    faults = dict(faults or {})

    def actuate(bay: int) -> ActuateResult:
        fault = faults.get(bay)
        if fault == "err":
            return ActuateResult(False, "ERR:hardware")
        if fault == "jam":
            return ActuateResult(True, "ok-but-no-drop")  # sensor will catch it
        tag = registry.by_bay.get(bay)
        if tag is not None:
            sensor.dispense_one(tag)  # a real pill physically leaves the magazine
        return ActuateResult(True, "ok")

    return actuate
