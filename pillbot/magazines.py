"""
pillbot.magazines.py - modular, hot-pluggable, tagged pill magazines.

The dispenser is expandable: each bay holds a MAGAZINE (a cartridge of one
medicine with its own servo). A magazine carries a blank, reusable TAG (a
unique id). The first time a tag is seen the caregiver assigns a medicine to
it; the system remembers that tag -> medicine mapping forever after.

A PILL SENSOR is the source of truth for COUNT. The same sensor that measures
the load on insertion also verifies every dispense:

    before=14, actuate servo, after=13  -> exactly one pill left (verified)
    before=14,               after=14   -> jam (nothing dispensed)
    before=14,               after=12   -> double-dispense
    before=5,                after=14   -> refill detected

So one sensor yields inventory + refill detection + tamper detection +
per-dose physical verification. Dose-counting is gated on PHYSICAL PROOF, not
on a servo ack.

Magazines may be 'refillable' (count can rise; a refill re-confirms identity)
or 'prefilled' (sealed, with lot + expiry). This module is pure logic with an
injected sensor/store/clock, so the whole flow is testable with no hardware.
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Dict, List, Optional, Protocol, Tuple


class MagState(str, Enum):
    PENDING_ASSIGN = "pending_assign"  # unknown tag: needs a medicine assigned
    READY = "ready"
    EMPTY = "empty"
    EXPIRED = "expired"


@dataclass
class Magazine:
    tag_id: str
    medicine: Optional[str] = None
    kind: str = "refillable"          # "refillable" | "prefilled"
    capacity: int = 0
    remaining: int = 0
    lot: str = ""
    expiry: Optional[str] = None      # ISO date "YYYY-MM-DD"
    bay: Optional[int] = None
    state: MagState = MagState.PENDING_ASSIGN


@dataclass
class InsertOutcome:
    magazine: Magazine
    needs_assignment: bool            # unknown tag -> ask which medicine
    needs_reconfirm: bool             # refill detected -> confirm identity still correct
    prompt: str                       # what the bot should say to the caregiver
    discrepancy: Optional[str] = None


# --- sensor + store abstractions (real hardware/db plug in later) ----------
class PillSensor(Protocol):
    def count(self, tag_id: str) -> int: ...


class VirtualPillSensor:
    """Simulated sensor: you set the measured pill count per tag. dispense_one()
    models the servo successfully ejecting a pill (count -1)."""

    def __init__(self) -> None:
        self._counts: Dict[str, int] = {}

    def load(self, tag_id: str, n: int) -> None:
        self._counts[tag_id] = n

    def dispense_one(self, tag_id: str) -> None:
        self._counts[tag_id] = max(0, self._counts.get(tag_id, 0) - 1)

    def count(self, tag_id: str) -> int:
        return self._counts.get(tag_id, 0)


class MagazineStore(Protocol):
    def load(self, tag_id: str) -> Optional[dict]: ...
    def save(self, tag_id: str, data: dict) -> None: ...


class InMemoryStore:
    def __init__(self) -> None:
        self._d: Dict[str, dict] = {}

    def load(self, tag_id: str) -> Optional[dict]:
        v = self._d.get(tag_id)
        return dict(v) if v is not None else None

    def save(self, tag_id: str, data: dict) -> None:
        self._d[tag_id] = dict(data)


_PERSIST_KEYS = ("medicine", "kind", "capacity", "remaining", "lot", "expiry")


class MagazineRegistry:
    """Tracks which magazines are currently inserted, remembers tag->medicine
    assignments, and keeps live per-magazine inventory from the sensor."""

    def __init__(self, sensor: PillSensor, store: Optional[MagazineStore] = None,
                 clock: Optional[Callable[[], datetime.datetime]] = None):
        self.sensor = sensor
        self.store = store or InMemoryStore()
        self._clock = clock or (lambda: datetime.datetime.now())
        self.by_tag: Dict[str, Magazine] = {}   # currently-inserted magazines
        self.by_bay: Dict[int, str] = {}        # bay -> tag

    # --- hot-plug ---------------------------------------------------------
    def on_insert(self, tag_id: str, bay: int) -> InsertOutcome:
        sensed = self.sensor.count(tag_id)
        saved = self.store.load(tag_id)

        if saved and saved.get("medicine"):
            prev = int(saved.get("remaining", 0))
            mag = Magazine(
                tag_id=tag_id, bay=bay,
                medicine=saved.get("medicine"),
                kind=saved.get("kind", "refillable"),
                capacity=max(int(saved.get("capacity", 0)), sensed),
                remaining=sensed,
                lot=saved.get("lot", ""),
                expiry=saved.get("expiry"),
            )
            discrepancy = None
            needs_reconfirm = False
            if sensed > prev:
                discrepancy = f"count rose {prev}->{sensed} (refill detected)"
                # A refilled reusable magazine could hold a different medicine -
                # the blank tag can't prove identity, so re-confirm it.
                needs_reconfirm = mag.kind == "refillable"
            elif sensed < prev:
                discrepancy = f"count fell {prev}->{sensed} since last seen"
            mag.state = self._derive_state(mag)
            self._attach(mag, bay)
            self._persist(mag)
            return InsertOutcome(
                mag, needs_assignment=False, needs_reconfirm=needs_reconfirm,
                prompt=self._reinsert_prompt(mag, needs_reconfirm),
                discrepancy=discrepancy,
            )

        # Unknown tag -> first-use assignment handshake.
        mag = Magazine(tag_id=tag_id, bay=bay, remaining=sensed,
                       capacity=sensed, state=MagState.PENDING_ASSIGN)
        self._attach(mag, bay)
        return InsertOutcome(
            mag, needs_assignment=True, needs_reconfirm=False,
            prompt=f"New magazine in bay {bay}: I count {sensed} pills. "
                   f"Which medicine is this?",
        )

    def assign_medicine(self, tag_id: str, medicine: str, kind: str = "refillable",
                        lot: str = "", expiry: Optional[str] = None) -> Magazine:
        """Caregiver confirms the medicine for a pending or refilled magazine."""
        mag = self.by_tag.get(tag_id)
        if mag is None:
            raise KeyError(f"No inserted magazine with tag {tag_id!r}")
        mag.medicine = medicine
        mag.kind = kind
        mag.lot = lot
        mag.expiry = expiry
        mag.remaining = self.sensor.count(tag_id)
        mag.capacity = max(mag.capacity, mag.remaining)
        mag.state = self._derive_state(mag)
        self._persist(mag)
        return mag

    def on_remove(self, bay: int) -> Optional[Magazine]:
        tag = self.by_bay.pop(bay, None)
        if tag is None:
            return None
        mag = self.by_tag.pop(tag, None)
        if mag is not None:
            self._persist(mag)  # checkpoint last-known remaining
            mag.bay = None
        return mag

    # --- dispense + verification -----------------------------------------
    def magazine_for(self, medicine: str) -> Optional[Magazine]:
        """Pick the best ready magazine for a medicine (expandable: there may
        be several). Use soonest-expiry first, then lowest remaining."""
        candidates = [m for m in self.by_tag.values()
                      if m.medicine == medicine and m.state == MagState.READY and m.remaining > 0]
        if not candidates:
            return None
        candidates.sort(key=lambda m: (m.expiry or "9999-12-31", m.remaining))
        return candidates[0]

    def can_dispense(self, medicine: str) -> Tuple[bool, str]:
        """An inventory-level gate the safety check can consult."""
        any_mag = [m for m in self.by_tag.values() if m.medicine == medicine]
        if not any_mag:
            return False, f"No magazine loaded for {medicine}."
        if self.magazine_for(medicine) is not None:
            return True, "ok"
        if any(self._expired(m) for m in any_mag):
            return False, f"{medicine} magazine is expired."
        return False, f"{medicine} is out of stock."

    def record_dispense(self, tag_id: str) -> Tuple[bool, str]:
        """Call AFTER the servo actuates. Uses the sensor to confirm exactly one
        pill physically left this magazine. Returns (verified, message)."""
        mag = self.by_tag.get(tag_id)
        if mag is None:
            return False, f"Magazine {tag_id!r} not present."
        before = mag.remaining
        after = self.sensor.count(tag_id)
        mag.remaining = after
        mag.state = self._derive_state(mag)
        self._persist(mag)
        delta = before - after
        if delta == 1:
            return True, f"verified: 1 pill dispensed ({after} left)"
        if delta <= 0:
            return False, f"NO pill detected leaving (before={before}, after={after}) - possible jam"
        return False, f"{delta} pills left the magazine (expected 1) - possible double-dispense"

    # --- inventory views --------------------------------------------------
    def inventory(self) -> List[Magazine]:
        return list(self.by_tag.values())

    def total_remaining(self, medicine: str) -> int:
        return sum(m.remaining for m in self.by_tag.values() if m.medicine == medicine)

    def loaded_medicines(self) -> List[str]:
        return sorted({m.medicine for m in self.by_tag.values() if m.medicine})

    # --- internals --------------------------------------------------------
    def _attach(self, mag: Magazine, bay: int) -> None:
        # If something was already in this bay, drop it from the live view.
        old_tag = self.by_bay.get(bay)
        if old_tag and old_tag != mag.tag_id:
            self.by_tag.pop(old_tag, None)
        self.by_bay[bay] = mag.tag_id
        self.by_tag[mag.tag_id] = mag

    def _persist(self, mag: Magazine) -> None:
        self.store.save(mag.tag_id, {k: getattr(mag, k) for k in _PERSIST_KEYS})

    def _today(self) -> datetime.date:
        return self._clock().date()

    def _expired(self, mag: Magazine) -> bool:
        if not mag.expiry:
            return False
        try:
            return datetime.date.fromisoformat(mag.expiry) < self._today()
        except ValueError:
            return False

    def _derive_state(self, mag: Magazine) -> MagState:
        if mag.medicine is None:
            return MagState.PENDING_ASSIGN
        if mag.remaining <= 0:
            return MagState.EMPTY
        if self._expired(mag):
            return MagState.EXPIRED
        return MagState.READY

    def _reinsert_prompt(self, mag: Magazine, needs_reconfirm: bool) -> str:
        if needs_reconfirm:
            return (f"Bay {mag.bay} refilled: now {mag.remaining} pills. "
                    f"Still {mag.medicine}? Confirm or reassign.")
        if mag.state == MagState.EMPTY:
            return f"Bay {mag.bay}: {mag.medicine} is EMPTY - please refill or replace."
        if mag.state == MagState.EXPIRED:
            return f"Bay {mag.bay}: {mag.medicine} is EXPIRED (lot {mag.lot or '?'})."
        return f"Bay {mag.bay}: {mag.medicine} recognized, {mag.remaining} pills."
