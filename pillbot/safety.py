"""
pillbot.safety.py - the deterministic dose safety gate (medicine-AGNOSTIC).

The inventory is open: a magazine can hold ANY medicine. So dose limits are NOT
a hardcoded catalog - they are RESOLVED per medicine, in priority order:

  1. FDA-derived limits, from the LOCAL openFDA cache (pillbot.drugdata). Pulled
     when the medicine is loaded; cached so the gate never makes a live call.
  2. Curated, clinician-reviewed OVERRIDES (MED_SAFETY below) - optional, and
     they may only make dosing MORE restrictive, never looser.
  3. If neither yields a numeric daily limit -> the medicine is UNRESOLVED and
     the gate FAILS CLOSED (refuses) rather than dispensing an uncapped drug.

evaluate() returns a per-dose DECISION CERTIFICATE (rules version + every rule,
with provenance of the limit) - the artifact the tamper-evident ledger records.
The gate is pure/testable; callers pass recent confirmed doses + the time, and
optionally the FDA cache. The AI can never reach or override this.

NOTE on interactions: openFDA OTC labels do not provide a clean machine-readable
drug-drug interaction matrix, so cross-medicine conflict rules come ONLY from
the curated overrides (a known gap for arbitrary meds - would need DrugBank/a
clinician). FDA "do not use" / warning TEXT is surfaced for information.
"""
from __future__ import annotations

import datetime
from dataclasses import asdict, dataclass
from typing import Dict, List, Optional, Tuple

from pillbot import drugdata as _drugdata

# Curated, clinician-reviewed OVERRIDES (optional, tighten-only). These are NOT
# the catalog - any medicine works via the FDA cache. Placeholder values; must
# be reviewed/signed off by a clinician before real use.
MED_SAFETY: Dict[str, Dict] = {
    "paracetamol 500 mg": {"max_per_day": 8, "min_interval_h": 4, "conflicts_with": [], "conflict_window_h": 0},
    "ibuprofen 200 mg":   {"max_per_day": 6, "min_interval_h": 4, "conflicts_with": [], "conflict_window_h": 0},
    "antacid chewable":   {"max_per_day": 8, "min_interval_h": 2, "conflicts_with": ["ibuprofen 200 mg"], "conflict_window_h": 2},
}

# Bump when the resolution logic or overrides change - recorded in every certificate.
RULES_VERSION = "2026-06-20.2"


@dataclass
class RuleEval:
    rule: str
    passed: bool
    detail: str


@dataclass
class SafetyDecision:
    allowed: bool
    medicine: str
    rules_version: str
    evaluations: List[RuleEval]
    message: str
    ts: str

    def as_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "medicine": self.medicine,
            "rules_version": self.rules_version,
            "message": self.message,
            "ts": self.ts,
            "evaluations": [asdict(e) for e in self.evaluations],
        }


@dataclass
class MedicineProfile:
    medicine: str
    max_per_day: Optional[int]
    min_interval_h: Optional[float]
    conflicts_with: List[str]
    conflict_window_h: float
    sources: List[str]
    review_status: str

    @property
    def resolved(self) -> bool:
        # A medicine is only dispensable if we have a numeric daily cap.
        return self.max_per_day is not None


def _norm(medicine: Optional[str]) -> str:
    return (medicine or "").strip().lower()


def _min_opt(a, b):
    vals = [x for x in (a, b) if x is not None]
    return min(vals) if vals else None


def _max_opt(a, b):
    vals = [x for x in (a, b) if x is not None]
    return max(vals) if vals else None


def _curated_override(medicine: str, overrides: Dict[str, Dict]) -> Optional[Dict]:
    nm = _norm(medicine)
    for name, rules in overrides.items():
        if _norm(name) == nm:
            return rules
    return None


def _safe_get(cache, medicine):
    try:
        return cache.get(medicine)
    except Exception:
        return None


def resolve_profile(medicine: str, fda_cache=None,
                    overrides: Optional[Dict[str, Dict]] = None) -> MedicineProfile:
    """Resolve a medicine's effective dose profile from the FDA cache + curated
    overrides. Composition is restrictive: the stricter daily cap and the longer
    interval win, so an override can only tighten the FDA-derived limit."""
    overrides = MED_SAFETY if overrides is None else overrides

    fda_max = fda_min = None
    sources: List[str] = []
    status = "UNRESOLVED"
    if fda_cache is not None:
        try:
            info = fda_cache.get(medicine)
        except Exception:
            info = None
        if info is not None:
            fda_max = getattr(info, "max_per_day", None)
            fda_min = getattr(info, "min_interval_h", None)
            url = getattr(info, "source_url", None)
            if url:
                sources.append(f"openFDA: {url}")
            status = getattr(info, "review_status", "FDA_DERIVED_UNREVIEWED")

    ov = _curated_override(medicine, overrides)
    ov_max = ov_min = None
    conflicts: List[str] = []
    cw = 0.0
    if ov is not None:
        ov_max = ov.get("max_per_day")
        ov_min = ov.get("min_interval_h")
        conflicts = list(ov.get("conflicts_with", []))
        cw = float(ov.get("conflict_window_h", 0))
        sources.append("curated clinician-reviewed override")
        status = "CURATED" if status == "UNRESOLVED" else status + "+CURATED"

    return MedicineProfile(
        medicine=medicine,
        max_per_day=_min_opt(fda_max, ov_max),      # stricter daily cap wins
        min_interval_h=_max_opt(fda_min, ov_min),   # longer interval wins
        conflicts_with=conflicts,
        conflict_window_h=cw,
        sources=sources,
        review_status=status,
    )


def evaluate(medicine: str, recent_doses: List[dict], now: datetime.datetime,
             registry=None, fda_cache=None) -> SafetyDecision:
    """Run every rule and return a full decision certificate. recent_doses are
    confirmed doses within >= the last 24h: [{"medicine": str, "ts": datetime}].
    Limits are resolved per-medicine (FDA cache + curated overrides)."""
    ts = now.isoformat(timespec="seconds")
    evals: List[RuleEval] = []

    profile = resolve_profile(medicine, fda_cache=fda_cache)
    if not profile.resolved:  # fail-closed: no validated daily cap
        evals.append(RuleEval("dose_limit_known", False,
                              "no validated daily dose limit (FDA-derived or curated)"))
        return SafetyDecision(False, medicine, RULES_VERSION, evals,
                              f"Safety block: no validated dose limit for {medicine}. "
                              f"Load its FDA data or set a clinician-reviewed limit, "
                              f"or ask a pharmacist.", ts)

    evals.append(RuleEval(
        "dose_limit_source", True,
        f"max {profile.max_per_day}/24h"
        + (f", min interval {profile.min_interval_h}h" if profile.min_interval_h else "")
        + f" [{profile.review_status}] from " + (", ".join(profile.sources) or "profile")))

    nm = _norm(medicine)
    same = [d for d in recent_doses if _norm(d.get("medicine")) == nm]

    # 1) daily limit
    daily_ok = len(same) < profile.max_per_day
    evals.append(RuleEval("daily_limit", daily_ok,
                          f"{len(same)} of max {profile.max_per_day} in 24h"))
    if not daily_ok:
        return SafetyDecision(False, medicine, RULES_VERSION, evals,
                              f"Safety block: {medicine} - daily limit of "
                              f"{profile.max_per_day} reached.", ts)

    # 2) minimum interval (only if the profile has one)
    if profile.min_interval_h and same:
        last = max(d["ts"] for d in same)
        elapsed_h = (now - last).total_seconds() / 3600.0
        mi_ok = elapsed_h >= profile.min_interval_h
        evals.append(RuleEval("min_interval", mi_ok,
                              f"{elapsed_h:.2f}h since last (min {profile.min_interval_h}h)"))
        if not mi_ok:
            wait = int((profile.min_interval_h - elapsed_h) * 60)
            return SafetyDecision(False, medicine, RULES_VERSION, evals,
                                  f"Safety block: {medicine} - minimum "
                                  f"{profile.min_interval_h}h interval not met. "
                                  f"Wait ~{wait} more minute(s).", ts)
    else:
        evals.append(RuleEval("min_interval", True,
                              "no interval rule" if not profile.min_interval_h else "no prior dose in window"))

    # 3) conflicts (curated only - openFDA gives no clean interaction matrix)
    for conflict_med in profile.conflicts_with:
        cw = profile.conflict_window_h
        cutoff = now - datetime.timedelta(hours=cw)
        hit = any(_norm(d.get("medicine")) == _norm(conflict_med) and d["ts"] >= cutoff
                  for d in recent_doses)
        evals.append(RuleEval(f"conflict:{conflict_med}", not hit,
                              f"recent {conflict_med} within {cw}h: {'yes' if hit else 'no'}"))
        if hit:
            return SafetyDecision(False, medicine, RULES_VERSION, evals,
                                  f"Safety block: {medicine} conflicts with a recent "
                                  f"{conflict_med} dose (within {cw}h).", ts)

    # 3b) FDA-label drug-drug interactions vs other recently-taken meds. Open to
    #     ANY medicine - matched from the cached label text (no live call).
    if fda_cache is not None:
        others: Dict[str, str] = {}
        for d in recent_doses:
            om = d.get("medicine")
            if om and _norm(om) != nm:
                others.setdefault(_norm(om), om)
        if others:
            info_cur = _safe_get(fda_cache, medicine)
            ing_cur = _drugdata.active_ingredient_for(medicine)
            for oname in others.values():
                info_other = _safe_get(fda_cache, oname)
                warns = (_drugdata.interactions_between(info_cur, oname,
                                                        _drugdata.active_ingredient_for(oname))
                         + _drugdata.interactions_between(info_other, medicine, ing_cur))
                avoid = [w for w in warns if w.severity == "avoid"]
                if avoid:
                    w = avoid[0]
                    evals.append(RuleEval(f"fda_interaction:{oname}", False,
                                          f"{w.matched} in {w.field}: {w.snippet}"))
                    return SafetyDecision(False, medicine, RULES_VERSION, evals,
                                          f"Safety block: the FDA label advises against taking "
                                          f"{medicine} together with {oname} (taken recently). "
                                          f"Source: {w.source}", ts)
                caution = [w for w in warns if w.severity == "caution"]
                if caution:
                    w = caution[0]
                    evals.append(RuleEval(f"fda_interaction:{oname}", True,
                                          f"CAUTION: possible interaction with {oname} "
                                          f"({w.matched}) - {w.snippet}"))

    # 4) inventory (magazine loaded / not empty / not expired)
    if registry is not None:
        inv_ok, inv_msg = registry.can_dispense(medicine)
        evals.append(RuleEval("inventory", inv_ok, inv_msg))
        if not inv_ok:
            return SafetyDecision(False, medicine, RULES_VERSION, evals, inv_msg, ts)

    return SafetyDecision(True, medicine, RULES_VERSION, evals, "OK", ts)


# --- thin wrappers (back-compat with existing callers/tests) ---------------
def check_dose_limits(medicine: str, recent_doses: List[dict],
                      now: datetime.datetime) -> Tuple[bool, str]:
    d = evaluate(medicine, recent_doses, now, registry=None)
    return d.allowed, d.message


def safety_gate(medicine: str, recent_doses: List[dict], now: datetime.datetime,
                registry=None, fda_cache=None) -> Tuple[bool, str]:
    d = evaluate(medicine, recent_doses, now, registry=registry, fda_cache=fda_cache)
    return d.allowed, d.message