"""
pillbot/drugdata.py - authoritative drug knowledge from the FDA (openFDA).

For each medicine in the inventory we pull its OFFICIAL FDA label (the openFDA
drug/label endpoint, https://api.fda.gov) and extract the authoritative usage
info: the max dose per 24h, the dosing interval, the "do not use" / "ask a
doctor" warnings, the purpose, and the active ingredient - each kept with a
CITATION (source URL + retrieval time) so we can prove where it came from.

SAFETY DESIGN (this is a medication device, so the rules are deliberate):
  1. openFDA returns LABEL TEXT, not clean numbers. We extract candidate limits
     with conservative patterns and ALWAYS keep the matched source snippet.
     Extracted limits are flagged FDA_DERIVED_UNREVIEWED until a clinician
     signs off - the API informs, it does not replace clinical judgement.
  2. The data is CACHED locally (DrugDataCache) so the deterministic safety
     gate never depends on a live network call - a network failure must never
     break safety.
  3. When composed into the gate, an FDA-derived limit may only make dosing
     MORE restrictive than the hard-coded ceiling, never looser
     (restrictive_max_per_day / restrictive_min_interval below).
"""
from __future__ import annotations

import datetime
import json
import os
import re
from dataclasses import asdict, dataclass
from typing import Callable, Dict, List, Optional

import requests

OPENFDA_URL = "https://api.fda.gov/drug/label.json"
REVIEW_UNREVIEWED = "FDA_DERIVED_UNREVIEWED"

# Inventory medicine name -> the FDA active-ingredient search term. (paracetamol
# is the international name; US/FDA labels use "acetaminophen".)
ACTIVE_INGREDIENT: Dict[str, str] = {
    "paracetamol": "acetaminophen",
    "acetaminophen": "acetaminophen",
    "ibuprofen": "ibuprofen",
    "antacid": "calcium carbonate",   # common chewable-antacid active ingredient
    "aspirin": "aspirin",
}

_UNIT = (r"(?:tablets?|caplets?|capsules?|pills?|softgels?|gelcaps?|geltabs?|"
         r"gel\s?caps?|chewables?|lozenges?|packets?|doses?)")
# "per day" phrasing varies a lot: "24 hours", "24-hour period", "a day", "daily".
_PERDAY = r"(?:24[\s-]*hours?|a day|per\s*day|daily)"
_MAXDAY_PATTERNS = [
    r"do not (?:take|use|exceed)\s+(?:more than\s+)?(\d+)\s+" + _UNIT + r"\b[^.]*?" + _PERDAY,
    r"more than (\d+)\s+" + _UNIT + r"\b[^.]*?" + _PERDAY,
    r"(?:maximum|max(?:imum)?)\b[^.]*?(\d+)\s+" + _UNIT + r"\b[^.]*?" + _PERDAY,
]
_INTERVAL_PATTERNS = [
    r"every\s+(\d+)\s*(?:to|-|–)?\s*\d*\s*hours",
]


@dataclass
class DrugInfo:
    medicine: str
    active_ingredient: Optional[str] = None
    purpose: Optional[str] = None
    max_per_day: Optional[int] = None
    min_interval_h: Optional[float] = None
    max_per_day_source: Optional[str] = None   # the matched FDA-text snippet
    interval_source: Optional[str] = None
    do_not_use: Optional[str] = None
    ask_doctor: Optional[str] = None
    when_using: Optional[str] = None
    stop_use: Optional[str] = None
    warnings: Optional[str] = None
    drug_interactions: Optional[str] = None
    dosage_text: Optional[str] = None
    source_url: Optional[str] = None
    retrieved_at: Optional[str] = None
    review_status: str = REVIEW_UNREVIEWED
    fetch_ok: bool = False
    note: str = ""

    def as_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "DrugInfo":
        fields = cls.__dataclass_fields__
        return cls(**{k: v for k, v in d.items() if k in fields})


def active_ingredient_for(medicine: str) -> str:
    m = (medicine or "").lower()
    for keyword, ingredient in ACTIVE_INGREDIENT.items():
        if keyword in m:
            return ingredient
    parts = m.split()
    return parts[0] if parts else m


def _first(label: dict, key: str, limit: int = 1500) -> Optional[str]:
    v = label.get(key)
    if isinstance(v, list) and v:
        text = " ".join(str(x) for x in v).strip()
    elif isinstance(v, str):
        text = v.strip()
    else:
        return None
    return (text[:limit] + "...") if len(text) > limit else text


def _snippet(text: str, match: re.Match, pad: int = 70) -> str:
    s = max(0, match.start() - pad)
    e = min(len(text), match.end() + pad)
    return ("..." if s else "") + text[s:e].strip() + ("..." if e < len(text) else "")


def extract_limits(label: dict) -> dict:
    """Best-effort extraction of max-per-24h and dosing interval from the label
    text. Heuristic and FDA-text-dependent - always keep the source snippet."""
    text = " ".join(filter(None, [
        _first(label, "dosage_and_administration"),
        _first(label, "do_not_use"),
        _first(label, "warnings"),
    ])).lower()
    out: dict = {"max_per_day": None, "min_interval_h": None,
                 "max_per_day_source": None, "interval_source": None}
    for pat in _MAXDAY_PATTERNS:
        m = re.search(pat, text)
        if m:
            out["max_per_day"] = int(m.group(1))
            out["max_per_day_source"] = _snippet(text, m)
            break
    for pat in _INTERVAL_PATTERNS:
        m = re.search(pat, text)
        if m:
            out["min_interval_h"] = float(m.group(1))
            out["interval_source"] = _snippet(text, m)
            break
    return out


def _openfda_query(ingredient: str, product_type: Optional[str],
                   api_key: Optional[str], timeout: float) -> Optional[dict]:
    search = f'openfda.generic_name:"{ingredient}"'
    if product_type:
        search += f' AND openfda.product_type:"{product_type}"'
    params = {"search": search, "limit": 1}
    if api_key:
        params["api_key"] = api_key
    try:
        r = requests.get(OPENFDA_URL, params=params, timeout=timeout)
        if r.status_code == 404:   # openFDA returns 404 when nothing matches
            return None
        r.raise_for_status()
        results = (r.json() or {}).get("results") or []
        return results[0] if results else None
    except requests.RequestException:
        return None


def fetch_label(medicine: str, api_key: Optional[str] = None,
                timeout: float = 15.0) -> Optional[dict]:
    """Query openFDA for a medicine's label. Prefers the OTC 'Drug Facts' label
    (tablet-count dosing, right for a home device); falls back to any label
    (e.g. prescription-only meds). Returns None on no-match / network error."""
    ingredient = active_ingredient_for(medicine)
    return (_openfda_query(ingredient, "HUMAN OTC DRUG", api_key, timeout)
            or _openfda_query(ingredient, None, api_key, timeout))


def build_drug_info(medicine: str, api_key: Optional[str] = None,
                    clock: Optional[Callable[[], datetime.datetime]] = None) -> DrugInfo:
    now = (clock or datetime.datetime.now)()
    ingredient = active_ingredient_for(medicine)
    info = DrugInfo(
        medicine=medicine,
        active_ingredient=ingredient,
        source_url=f'{OPENFDA_URL}?search=openfda.generic_name:"{ingredient}"&limit=1',
        retrieved_at=now.isoformat(timespec="seconds"),
    )
    label = fetch_label(medicine, api_key=api_key)
    if label is None:
        info.note = "no openFDA label found / fetch failed (offline?)"
        return info

    info.fetch_ok = True
    info.purpose = _first(label, "purpose")
    info.dosage_text = _first(label, "dosage_and_administration")
    info.do_not_use = _first(label, "do_not_use")
    info.ask_doctor = _first(label, "ask_doctor") or _first(label, "ask_doctor_or_pharmacist")
    info.when_using = _first(label, "when_using")
    info.stop_use = _first(label, "stop_use")
    info.warnings = _first(label, "warnings")
    info.drug_interactions = _first(label, "drug_interactions")
    lim = extract_limits(label)
    info.max_per_day = lim["max_per_day"]
    info.max_per_day_source = lim["max_per_day_source"]
    info.min_interval_h = lim["min_interval_h"]
    info.interval_source = lim["interval_source"]
    return info


# --- conservative composition: FDA can only TIGHTEN, never loosen -----------
def restrictive_max_per_day(fda: Optional[int], curated_ceiling: int) -> int:
    if fda is None:
        return curated_ceiling
    return min(int(fda), curated_ceiling)


def restrictive_min_interval(fda: Optional[float], curated_min: float) -> float:
    if fda is None:
        return curated_min
    return max(float(fda), curated_min)


# --- drug-drug interactions from FDA label text (open to ANY medicine) ------
# Small, general drug-class knowledge for matching (extensible; could later be
# sourced from RxClass/RxNorm). Lets us catch e.g. NSAID-on-NSAID even when a
# label names the class rather than the specific drug.
DRUG_CLASSES: Dict[str, List[str]] = {
    "ibuprofen": ["nsaid"], "naproxen": ["nsaid"], "diclofenac": ["nsaid"],
    "ketoprofen": ["nsaid"], "celecoxib": ["nsaid"], "aspirin": ["nsaid", "blood thinner"],
    "warfarin": ["blood thinner"], "apixaban": ["blood thinner"],
    "rivaroxaban": ["blood thinner"], "clopidogrel": ["blood thinner"], "heparin": ["blood thinner"],
}
CLASS_KEYWORDS: Dict[str, List[str]] = {
    "nsaid": ["nsaid", "nonsteroidal anti-inflammatory"],
    "blood thinner": ["blood thinner", "anticoagulant"],
}


@dataclass
class InteractionWarning:
    other_medicine: str
    severity: str        # "avoid" (block) | "caution" (warn)
    matched: str         # token or drug class that matched
    field: str           # which label section it was found in
    snippet: str
    source: Optional[str] = None


def _name_tokens(medicine: str, ingredient: Optional[str]) -> set:
    toks = set()
    if ingredient:
        toks.add(ingredient.lower())
    words = (medicine or "").lower().split()
    if words:
        toks.add(words[0])
    return {t for t in toks if len(t) >= 4}


def _classes_for(medicine: str, ingredient: Optional[str]) -> set:
    toks = _name_tokens(medicine, ingredient)
    classes: set = set()
    for ing, cls_list in DRUG_CLASSES.items():
        if ing in toks:
            classes.update(cls_list)
    return classes


def _blob(info, fields) -> str:
    return " ".join(filter(None, [getattr(info, f, None) for f in fields])).lower()


def _find_mention(blob: str, name_tokens: set, classes: set):
    for tok in name_tokens:
        m = re.search(r"\b" + re.escape(tok) + r"\b", blob)
        if m:
            return tok, m
    for cls in classes:
        for kw in CLASS_KEYWORDS.get(cls, [cls]):
            m = re.search(r"\b" + re.escape(kw) + r"\b", blob)
            if m:
                return f"class:{cls}", m
    return None


def interactions_between(info_a, med_b: str,
                         ingredient_b: Optional[str]) -> List[InteractionWarning]:
    """Does medicine A's FDA label text warn about medicine B (by name, active
    ingredient, or drug class)? 'do_not_use' text -> 'avoid'; warnings /
    drug_interactions / ask_doctor text -> 'caution'. Possibly-empty list."""
    if info_a is None:
        return []
    name_tokens = _name_tokens(med_b, ingredient_b)
    classes = _classes_for(med_b, ingredient_b)
    out: List[InteractionWarning] = []
    for severity, fields in (("avoid", ("do_not_use",)),
                             ("caution", ("drug_interactions", "warnings", "ask_doctor"))):
        blob = _blob(info_a, fields)
        if not blob:
            continue
        found = _find_mention(blob, name_tokens, classes)
        if found:
            matched, m = found
            out.append(InteractionWarning(
                other_medicine=med_b, severity=severity, matched=matched,
                field=",".join(fields), snippet=_snippet(blob, m),
                source=getattr(info_a, "source_url", None)))
    return out


# --- local cache so the safety path never needs the network -----------------
class DrugDataCache:
    def __init__(self, path: str):
        self.path = path
        self._data: Dict[str, dict] = {}
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
            except Exception:
                self._data = {}

    def get(self, medicine: str) -> Optional[DrugInfo]:
        d = self._data.get((medicine or "").lower())
        return DrugInfo.from_dict(d) if d else None

    def put(self, info: DrugInfo) -> None:
        self._data[info.medicine.lower()] = info.as_dict()
        self._save()

    def all(self) -> List[DrugInfo]:
        return [DrugInfo.from_dict(d) for d in self._data.values()]

    def _save(self) -> None:
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2)
        except OSError:
            pass


def refresh(cache: DrugDataCache, medicine: str, api_key: Optional[str] = None) -> DrugInfo:
    """Fetch a medicine's FDA info and cache it (only if the fetch succeeded, so
    a transient outage never overwrites good cached data)."""
    info = build_drug_info(medicine, api_key=api_key)
    if info.fetch_ok:
        cache.put(info)
    return info
