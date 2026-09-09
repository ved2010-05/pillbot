"""
pillbot.runtime.py - assembles the magazine-based dispensing + safety engine
into one object the app drives.

It owns: the pill sensor, the magazine registry (+ its persistent store), the
actuator, and the DispenserService. The caller supplies the user's recent
confirmed doses (from the dispense log) and records the outcome; the runtime
owns inventory, physical verification, and the safety gate.

dispense(user, medicine, recent_doses):
    safety gate (dose limits + inventory)  ->  actuate servo  ->  sensor-verify
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Callable, List, Optional

import pillbot.clock
from pillbot.dispenser import DispenserService, make_sim_actuator
from pillbot.magazines import InsertOutcome, Magazine, MagazineRegistry, VirtualPillSensor
from pillbot.safety import evaluate


@dataclass
class DispenseDecision:
    dispensed: bool
    medicine: str
    message: str
    bay: Optional[int] = None
    remaining: Optional[int] = None
    blocked_by: Optional[str] = None   # "policy" | "hardware" | None
    request_id: Optional[str] = None
    certificate: Optional[dict] = None


class PillbotRuntime:
    def __init__(self, store=None, sensor: Optional[VirtualPillSensor] = None,
                 clock: Optional[Callable] = None, actuator: Optional[Callable] = None,
                 ledger=None, fda_cache=None):
        self.clock = clock or pillbot.clock.now
        self.sensor = sensor or VirtualPillSensor()
        self.registry = MagazineRegistry(self.sensor, store, clock=self.clock)
        self.ledger = ledger
        self.fda_cache = fda_cache   # openFDA drug-data cache for the safety gate
        # default actuator is the simulator (servo -> sensor); real hardware
        # plugs a different actuator in here later.
        actuate = actuator or make_sim_actuator(self.sensor, self.registry)
        self.service = DispenserService(self.registry, actuate)

    # --- hot-plug passthroughs -------------------------------------------
    def insert_magazine(self, tag_id: str, bay: int) -> InsertOutcome:
        return self.registry.on_insert(tag_id, bay)

    def assign(self, tag_id: str, medicine: str, **kw) -> Magazine:
        return self.registry.assign_medicine(tag_id, medicine, **kw)

    def remove_magazine(self, bay: int):
        return self.registry.on_remove(bay)

    def inventory(self) -> List[Magazine]:
        return self.registry.inventory()

    # For the simulator: set what the sensor will measure for a tag (in real
    # hardware the physical sensor provides this).
    def sim_load(self, tag_id: str, count: int) -> None:
        self.sensor.load(tag_id, count)

    # --- the gated, verified, audited dispense ----------------------------
    def dispense(self, user: str, medicine: str, recent_doses: List[dict],
                 request_id: Optional[str] = None) -> DispenseDecision:
        rid = request_id or uuid.uuid4().hex

        # Idempotency: a replay of an already-processed request returns the
        # recorded outcome WITHOUT actuating again - closes the crash-window
        # between actuation and logging that could otherwise double-dispense.
        if self.ledger is not None:
            prior = self.ledger.get(rid)
            if prior is not None:
                p = prior.payload
                return DispenseDecision(
                    bool(p.get("dispensed")), medicine,
                    "(replay) request already processed",
                    bay=p.get("bay"), remaining=p.get("remaining"),
                    blocked_by=p.get("blocked_by"), request_id=rid,
                    certificate=p.get("certificate"))

        now = self.clock()
        cert = evaluate(medicine, recent_doses, now, registry=self.registry,
                        fda_cache=self.fda_cache)

        if not cert.allowed:
            decision = DispenseDecision(False, medicine, cert.message,
                                        blocked_by="policy", request_id=rid,
                                        certificate=cert.as_dict())
        else:
            out = self.service.dispense(medicine)
            decision = DispenseDecision(out.ok, medicine, out.message, bay=out.bay,
                                        remaining=out.remaining,
                                        blocked_by=None if out.ok else "hardware",
                                        request_id=rid, certificate=cert.as_dict())

        # Record every decision (dispensed OR blocked) in the tamper-evident,
        # idempotent ledger: binds intent -> safety certificate -> outcome.
        if self.ledger is not None:
            self.ledger.append(rid, {
                "user": user,
                "medicine": medicine,
                "dispensed": decision.dispensed,
                "bay": decision.bay,
                "remaining": decision.remaining,
                "blocked_by": decision.blocked_by,
                "certificate": cert.as_dict(),
            })
        return decision
