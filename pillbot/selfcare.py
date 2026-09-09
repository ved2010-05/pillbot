"""
pillbot.selfcare.py - the curated OTC self-care knowledge base + selector.

PillBot is a home medication ORGANIZER, reminder, and dispenser - NOT a
prescriber, diagnostic tool, or clinician. This module powers a deliberately
narrow "general-wellness information" feature: for MINOR, self-limiting adult
symptoms (mild headache, mild fever, body/muscle ache, mild heartburn/acidity,
mild indigestion) it SURFACES, from a CURATED, clinician-validated table, the
common over-the-counter option(s) PillBot already physically stocks
(paracetamol 500 mg, ibuprofen 200 mg, antacid chewable).

It exists to keep the product in the lighter "general-wellness / not medical
advice / not a medical device" regulatory lane, and it mirrors the whole
product's pattern: "AI proposes, a deterministic/validated system disposes."
The LLM may NEVER author a medicine, dose, or indication from free-form
reasoning - it may only trigger recommend(), which selects an entry that
already exists in the curated tables below. The user-facing wording is owned by
this deterministic layer, never spoken verbatim from the model.

WHAT THIS MODULE IS / ISN'T
  * PURE: no DB, no hardware, no network, no clock dependence. The caller passes
    the symptom, the user's free-text notes, and the list of currently
    available medicines (cross-checked elsewhere against
    pillbot.magazines.MagazineRegistry.can_dispense and the deterministic
    pillbot.safety.evaluate gate). This module never dispenses anything.
  * INFORMATION, not advice: every recommendation carries the disclaimer and is
    framed as a "common OTC option", never a directive to treat a named disease.

DECISION ORDER (fail-closed, red-flag first)
  1. RED-FLAG escalation - if user_notes (or the symptom text) matches any
     curated red-flag keyword, return an ESCALATION with NO medicine. This runs
     BEFORE any recommendation and cannot be overridden.
  2. RECOMMENDATION - otherwise, for a known in-scope symptom, build the
     candidate list from the curated map, then drop any candidate that is
     either (a) not in available_medicines, or (b) contraindicated by a
     keyword hit in user_notes. If any candidate survives, recommend it (with
     the disclaimer).
  3. NO SUITABLE OPTION - unknown/out-of-scope symptom, or every candidate
     filtered out -> a safe fallback that points to a pharmacist/doctor.

CONTRAINDICATION SCAN IS KEYWORD-BASED.
  The contraindication and red-flag checks scan the free-text user_notes for
  substrings. This is intentionally conservative but coarse: it cannot
  understand negation ("no ulcer") or structure. Passing STRUCTURED condition
  tags / NLP-extracted conditions instead of raw notes is a FUTURE IMPROVEMENT
  (and is also better for data-minimisation - raw notes should not be widened
  beyond the specific safety flags the check needs).

VERSIONED + UNSIGNED.
  Both the rules version (from pillbot.safety) and this module's
  KNOWLEDGE_BASE_VERSION are exposed so every recorded suggestion certificate
  can embed them. The knowledge base itself is a NON-BINDING PLACEHOLDER drafted
  by an AI assistant from a conservative pharmacist perspective; it has NOT been
  reviewed or signed off by a licensed clinician and MUST NOT govern any
  real-patient use until it is. See DISCLAIMER / KB_REVIEW_STATUS below.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Dict, List, Optional

from pillbot.safety import RULES_VERSION

# Bump whenever the curated tables below change; recorded in every certificate.
KNOWLEDGE_BASE_VERSION = "2026-06-17.1"

# The curated rule set is an AI-drafted placeholder, NOT clinician-signed.
# This MUST flip to a signed status before the feature is enabled for real users.
KB_REVIEW_STATUS = "UNSIGNED_PLACEHOLDER"

# Shown WITH every recommendation, before the user can confirm. This is the
# load-bearing "information, not advice" framing; the India DMR-Act-sensitive
# wording must be reviewed and approved by counsel before release.
DISCLAIMER = (
    "This is general wellness information, not medical advice, diagnosis, or a "
    "prescription. PillBot is not a doctor or pharmacist. This is a common "
    "over-the-counter option many people use for mild, short-term symptoms - it "
    "is not tailored to your medical condition. If you are unsure, pregnant or "
    "breastfeeding, taking other medicines, have a long-term condition (e.g. "
    "liver/kidney/heart disease, ulcers, asthma), or this is for a child, check "
    "with a pharmacist or doctor first. Read the label and package leaflet, do "
    "not exceed the stated dose, and do not combine products with the same "
    "active ingredient. If symptoms are severe, persist (e.g. fever >3 days or "
    "pain >a few days), or get worse, stop and see a doctor. In an emergency, "
    "call your local emergency number - do not wait for PillBot."
)

# Persistent product-wide notice (shown regardless of result kind).
PRODUCT_NOTICE = (
    "PillBot stores and dispenses medicines you already own; it does not assess "
    "whether a medicine is right for you."
)

# Plain-language message used for every red-flag / escalation case.
ESCALATION_MESSAGE = (
    "I can't suggest a medicine for this - please see a pharmacist or doctor "
    "(or call your local emergency number if this is urgent)."
)

# Safe fallback when nothing in scope can be offered.
NO_OPTION_MESSAGE = (
    "I don't have a self-care suggestion for this. For anything beyond a mild, "
    "short-term symptom, please ask a pharmacist or doctor."
)


# --- canonical medicine names (must match MED_SAFETY / loaded inventory) ----
PARACETAMOL = "paracetamol 500 mg"
IBUPROFEN = "ibuprofen 200 mg"
ANTACID = "antacid chewable"


class ResultKind(str, Enum):
    RECOMMENDATION = "recommendation"        # one or more stocked OTC options
    ESCALATION = "escalation"                # red flag -> see a doctor, no med
    NO_OPTION = "no_option"                  # unknown / all candidates filtered


@dataclass
class MedicineSuggestion:
    """A single curated OTC option for an in-scope symptom (information only)."""
    medicine: str
    note: str


@dataclass
class SelfCareResult:
    """The structured, deterministic output of recommend(). Exactly one
    ResultKind. Recommendations always carry the disclaimer and never name a
    medicine outside available_medicines. The AI surfaces FROM this result; it
    must never free-form a medicine, dose, or indication."""
    kind: ResultKind
    symptom: str
    suggestions: List[MedicineSuggestion]
    message: str
    disclaimer: str
    product_notice: str
    knowledge_base_version: str
    rules_version: str
    review_status: str
    # Audit trail: why each candidate was kept or dropped (for the ledger).
    triggered_rule: Optional[str] = None     # which red-flag rule fired, if any
    filtered: List[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        d = asdict(self)
        d["kind"] = self.kind.value
        return d


# --- CURATED symptom -> stocked OTC mapping (clinician-validated table) ------
# Keyed by canonical symptom; aliases route common phrasings to the same entry.
# UNSIGNED placeholder content - see KB_REVIEW_STATUS / DISCLAIMER.
SYMPTOM_RECOMMENDATIONS: Dict[str, List[MedicineSuggestion]] = {
    "mild headache": [
        MedicineSuggestion(
            PARACETAMOL,
            "First-line. Safer overall profile for routine self-care; preferred "
            "when GI, renal, cardiac, asthma, anticoagulant, or pregnancy "
            "concerns exist or are unknown.",
        ),
        MedicineSuggestion(
            IBUPROFEN,
            "Reasonable alternative if paracetamol is unsuitable/ineffective and "
            "no NSAID contraindication applies. Take with or after food.",
        ),
    ],
    "mild fever": [
        MedicineSuggestion(
            PARACETAMOL,
            "First-line antipyretic for adult self-care; preferred over "
            "ibuprofen, especially with dehydration risk or renal/cardiac "
            "concerns. Prioritise fluids and rest.",
        ),
        MedicineSuggestion(
            IBUPROFEN,
            "Alternative antipyretic if paracetamol is unsuitable/ineffective and "
            "no NSAID contraindication. Take with food; keep well hydrated.",
        ),
    ],
    "body/muscle ache": [
        MedicineSuggestion(
            PARACETAMOL,
            "First-line for general aches/myalgia in self-care due to safer "
            "profile.",
        ),
        MedicineSuggestion(
            IBUPROFEN,
            "Useful where an anti-inflammatory effect helps (e.g. minor "
            "strain/overexertion) if no NSAID contraindication. Take with food.",
        ),
    ],
    "heartburn/acidity": [
        MedicineSuggestion(
            ANTACID,
            "First-line for occasional heartburn/acid reflux/sour stomach. Rapid, "
            "short-acting symptomatic relief. Separate dosing in time from other "
            "medicines.",
        ),
    ],
    "indigestion": [
        MedicineSuggestion(
            ANTACID,
            "First-line for mild indigestion/dyspepsia with an acid component "
            "(fullness, mild upper-abdominal burning after eating). Paracetamol "
            "and ibuprofen do NOT treat indigestion, and ibuprofen can worsen it.",
        ),
    ],
}

# Common phrasings -> canonical symptom key. Kept conservative; anything not
# matched here falls through to NO_OPTION (fail-closed, never guessed).
SYMPTOM_ALIASES: Dict[str, str] = {
    "headache": "mild headache",
    "mild headache": "mild headache",
    "tension headache": "mild headache",
    "head ache": "mild headache",
    "fever": "mild fever",
    "mild fever": "mild fever",
    "temperature": "mild fever",
    "body ache": "body/muscle ache",
    "body aches": "body/muscle ache",
    "muscle ache": "body/muscle ache",
    "muscle aches": "body/muscle ache",
    "muscle pain": "body/muscle ache",
    "body/muscle ache": "body/muscle ache",
    "myalgia": "body/muscle ache",
    "aches": "body/muscle ache",
    "heartburn": "heartburn/acidity",
    "acidity": "heartburn/acidity",
    "acid reflux": "heartburn/acidity",
    "reflux": "heartburn/acidity",
    "sour stomach": "heartburn/acidity",
    "heartburn/acidity": "heartburn/acidity",
    "indigestion": "indigestion",
    "dyspepsia": "indigestion",
    "upset stomach": "indigestion",
}


# --- CURATED per-medicine contraindications (keyword -> block) ---------------
# Each entry: the canonical medicine, and the user_notes keyword fragments that
# block (severity "absolute") or deprioritise (severity "caution") it. The scan
# is a plain substring match over lowercased user_notes; both severities are
# treated as "do not surface this medicine" here (fail-safe), and the dropped
# candidate is recorded in SelfCareResult.filtered for the audit ledger.
# UNSIGNED placeholder content.
@dataclass
class Contraindication:
    medicine: str
    keywords: List[str]
    severity: str          # "absolute" | "caution"
    action: str


CONTRAINDICATIONS: List[Contraindication] = [
    Contraindication(
        IBUPROFEN,
        ["ulcer", "peptic ulcer", "gi bleed", "gastrointestinal bleed",
         "stomach bleed", "black stool", "tarry stool", "vomiting blood",
         "blood in vomit"],
        "absolute",
        "Block ibuprofen (GI ulcer/bleed risk). Prefer paracetamol; escalate if "
        "active bleeding is suspected.",
    ),
    Contraindication(
        IBUPROFEN,
        ["anticoagulant", "warfarin", "doac", "apixaban", "rivaroxaban",
         "clopidogrel", "blood thinner", "blood-thinner"],
        "absolute",
        "Block ibuprofen (additive bleeding risk). Offer paracetamol; advise "
        "pharmacist/clinician review.",
    ),
    Contraindication(
        IBUPROFEN,
        ["kidney disease", "renal", "chronic kidney", "ckd", "dehydrated",
         "dehydration"],
        "absolute",
        "Block ibuprofen (acute kidney injury risk). Use paracetamol; encourage "
        "fluids; advise clinician review.",
    ),
    Contraindication(
        IBUPROFEN,
        ["nsaid allergy", "ibuprofen allergy", "aspirin allergy",
         "nsaid-sensitive asthma", "aspirin-sensitive"],
        "absolute",
        "Block ibuprofen (severe bronchospasm/anaphylaxis risk). Use "
        "paracetamol; flag this true drug allergy to clinicians.",
    ),
    Contraindication(
        IBUPROFEN,
        ["third trimester", "28 weeks", "late pregnancy"],
        "absolute",
        "Block ibuprofen in late pregnancy (fetal harm risk). Recommend "
        "paracetamol and clinician guidance.",
    ),
    Contraindication(
        IBUPROFEN,
        ["heart failure", "uncontrolled hypertension", "cardiovascular disease",
         "heart disease", "pregnant", "pregnancy", "breastfeeding", "asthma",
         "ace inhibitor", "arb", "diuretic", "ssri", "snri", "corticosteroid",
         "steroid"],
        "caution",
        "Deprioritise ibuprofen. Prefer paracetamol; if genuinely needed, defer "
        "to clinician advice, lowest dose, shortest time, with food.",
    ),
    Contraindication(
        PARACETAMOL,
        ["liver disease", "hepatic", "cirrhosis", "paracetamol allergy",
         "acetaminophen allergy"],
        "absolute",
        "Block paracetamol (hepatic impairment / allergy). Do not self-treat; "
        "refer to clinician.",
    ),
    Contraindication(
        PARACETAMOL,
        ["heavy alcohol", "alcoholic", "malnutrition", "malnourished",
         "cold and flu", "cold/flu", "combination product",
         "paracetamol-containing"],
        "caution",
        "Deprioritise paracetamol / use the most conservative limit and warn "
        "about cumulative paracetamol from combination products. Advise "
        "pharmacist review.",
    ),
    Contraindication(
        ANTACID,
        ["black stool", "tarry stool", "vomiting blood", "blood in vomit",
         "chest pain"],
        "absolute",
        "Do not mask with antacid. Escalate to urgent medical assessment.",
    ),
    Contraindication(
        ANTACID,
        ["kidney disease", "chronic kidney", "ckd", "renal",
         "sodium-restricted", "low-sodium", "mineral-restricted"],
        "caution",
        "Deprioritise / limit antacid. Separate timing from other medicines; "
        "prefer clinician/pharmacist review for renal patients.",
    ),
]


# --- CURATED red-flag keyword list (refuse-and-escalate, runs FIRST) ---------
# Substring fragments scanned over the combined symptom + user_notes text. A hit
# means: surface NO medicine, tell the user to see a doctor / call emergency
# services, and record the triggering rule. UNSIGNED placeholder content.
@dataclass
class RedFlag:
    rule: str               # short rule id recorded in the ledger
    keywords: List[str]
    why: str


RED_FLAGS: List[RedFlag] = [
    RedFlag(
        "thunderclap_headache",
        ["worst-ever", "worst ever", "thunderclap", "sudden severe headache",
         "sudden, severe headache", "sudden severe"],
        "Possible subarachnoid haemorrhage / intracranial bleed - emergency.",
    ),
    RedFlag(
        "meningitis_signs",
        ["stiff neck", "neck stiffness", "non-blanching rash",
         "light sensitivity", "photophobia"],
        "Possible meningitis/encephalitis or meningococcal sepsis.",
    ),
    RedFlag(
        "neuro_or_head_injury",
        ["head injury", "confusion", "slurred speech", "weakness on one side",
         "one-sided weakness", "numbness", "vision loss", "loss of vision",
         "fainting", "fainted"],
        "Possible intracranial bleed or stroke; analgesics only mask it.",
    ),
    RedFlag(
        "cardiac_chest_pain",
        ["chest pain", "chest pressure", "radiating to arm", "pain in jaw",
         "jaw pain", "breathless", "shortness of breath",
         "difficulty breathing", "sweating with"],
        "Possible heart attack; never dismiss as heartburn or muscle ache.",
    ),
    RedFlag(
        "high_or_prolonged_fever",
        ["high fever", "very high fever", "fever for days",
         "fever lasting", "fever more than 3 days", "39 c", "102 f",
         "prolonged fever"],
        "Suggests significant infection needing diagnosis, not self-care.",
    ),
    RedFlag(
        "acute_abdomen",
        ["severe abdominal pain", "severe stomach pain", "rigid abdomen",
         "lower-right abdomen", "lower right abdomen", "abdominal rigidity"],
        "Possible appendicitis/perforation/obstruction - surgical emergency.",
    ),
    RedFlag(
        "gi_bleed",
        ["vomiting blood", "blood in vomit", "coffee-ground", "coffee ground",
         "black stool", "tarry stool", "black/tarry"],
        "Signs of gastrointestinal bleeding - and a hard stop for ibuprofen.",
    ),
    RedFlag(
        "anaphylaxis",
        ["anaphylaxis", "swelling of lips", "lip swelling", "tongue swelling",
         "facial swelling", "wheeze", "widespread rash", "allergic reaction"],
        "Possible anaphylaxis or severe drug reaction.",
    ),
    RedFlag(
        "dysphagia_alarm",
        ["difficulty swallowing", "pain on swallowing", "food sticking",
         "trouble swallowing", "unintentional weight loss", "weight loss"],
        "Alarm features suggesting a structural upper-GI problem.",
    ),
    RedFlag(
        "trauma_or_clot",
        ["fracture", "broken bone", "significant trauma", "swollen calf",
         "swollen red calf", "calf pain", "possible clot", "dvt"],
        "Needs imaging/assessment (fracture or DVT); analgesia would mask it.",
    ),
    RedFlag(
        "special_population",
        ["pregnant", "pregnancy", "breastfeeding", "immunocompromised",
         "child", "infant", "baby", "toddler", "my son", "my daughter",
         "years old", "year old", "for a kid"],
        "Higher-risk population; adult self-care defaults may not apply.",
    ),
    RedFlag(
        "uncontrolled_bleeding",
        ["uncontrolled bleeding", "won't stop bleeding", "heavy bleeding"],
        "Uncontrolled bleeding is an emergency.",
    ),
]


# --- helpers ----------------------------------------------------------------
def _norm(text: Optional[str]) -> str:
    return (text or "").strip().lower()


def _canonical_symptom(symptom: str) -> Optional[str]:
    """Map a free-text symptom to a curated key, or None if out of scope.

    Fail-closed: only an explicit alias/exact match resolves. We never guess a
    symptom into the minor/in-scope list."""
    nm = _norm(symptom)
    if not nm:
        return None
    if nm in SYMPTOM_RECOMMENDATIONS:
        return nm
    if nm in SYMPTOM_ALIASES:
        return SYMPTOM_ALIASES[nm]
    # Loose containment for phrasings like "I have a mild headache today".
    for alias, canon in SYMPTOM_ALIASES.items():
        if alias in nm:
            return canon
    return None


def _matched_red_flag(text: str) -> Optional[RedFlag]:
    """Return the first red-flag rule whose any keyword is a substring of text."""
    low = _norm(text)
    for flag in RED_FLAGS:
        for kw in flag.keywords:
            if kw in low:
                return flag
    return None


def _contraindication_for(medicine: str, notes_low: str) -> Optional[Contraindication]:
    """Return the first contraindication whose any keyword hits notes, else None.

    Keyword substring scan over free-text notes (see module docstring: structured
    condition tags / NLP are a future improvement)."""
    for c in CONTRAINDICATIONS:
        if c.medicine != medicine:
            continue
        for kw in c.keywords:
            if kw in notes_low:
                return c
    return None


def _available(medicine: str, available_medicines: List[str]) -> bool:
    avail = {_norm(m) for m in (available_medicines or [])}
    return _norm(medicine) in avail


def _escalation(symptom: str, rule: str) -> SelfCareResult:
    return SelfCareResult(
        kind=ResultKind.ESCALATION,
        symptom=symptom,
        suggestions=[],
        message=ESCALATION_MESSAGE,
        disclaimer=DISCLAIMER,
        product_notice=PRODUCT_NOTICE,
        knowledge_base_version=KNOWLEDGE_BASE_VERSION,
        rules_version=RULES_VERSION,
        review_status=KB_REVIEW_STATUS,
        triggered_rule=rule,
    )


def _no_option(symptom: str, filtered: Optional[List[str]] = None) -> SelfCareResult:
    return SelfCareResult(
        kind=ResultKind.NO_OPTION,
        symptom=symptom,
        suggestions=[],
        message=NO_OPTION_MESSAGE,
        disclaimer=DISCLAIMER,
        product_notice=PRODUCT_NOTICE,
        knowledge_base_version=KNOWLEDGE_BASE_VERSION,
        rules_version=RULES_VERSION,
        review_status=KB_REVIEW_STATUS,
        filtered=filtered or [],
    )


# --- the one public entry point ---------------------------------------------
def recommend(symptom: str, user_notes: str,
              available_medicines: List[str]) -> SelfCareResult:
    """Surface a curated OTC self-care option (information, not advice).

    Returns EXACTLY ONE SelfCareResult:
      (a) RECOMMENDATION - one or more stocked OTC options that are BOTH in
          available_medicines AND not contraindicated by a keyword hit in
          user_notes. Always carries the disclaimer. Never names a medicine
          outside available_medicines.
      (b) ESCALATION - a red-flag keyword fired (in the symptom or notes): no
          medicine; tell the user to see a doctor / call emergency services.
      (c) NO_OPTION - unknown/out-of-scope symptom, or every candidate filtered
          out (unavailable and/or contraindicated): safe fallback message.

    The red-flag check has PRIORITY and runs FIRST; it cannot be overridden.
    This function is the ONLY way the AI may obtain a medicine name for a
    symptom - it must never free-form one. The result still has to pass the
    deterministic pillbot.safety.evaluate gate and the user's explicit
    confirmation before anything is dispensed.

    Args:
        symptom: the user's symptom as classified by the caller.
        user_notes: the user's free-text notes/conditions to scan for red flags
            and contraindications (keyword-based; structured tags are a future
            improvement and would also minimise the data the check sees).
        available_medicines: canonical names PillBot currently has loaded in a
            READY magazine (cross-checked against MagazineRegistry.can_dispense /
            pillbot.safety upstream). Nothing outside this list is ever named.
    """
    notes_low = _norm(user_notes)
    symptom_low = _norm(symptom)

    # 1) RED-FLAG escalation FIRST - scan both the symptom and the notes.
    flag = _matched_red_flag(f"{symptom_low} {notes_low}")
    if flag is not None:
        return _escalation(symptom, flag.rule)

    # 2) Resolve the symptom against the curated, in-scope map (fail-closed).
    canon = _canonical_symptom(symptom)
    if canon is None:
        return _no_option(symptom)

    # 3) Build candidates, then drop unavailable + contraindicated ones.
    kept: List[MedicineSuggestion] = []
    filtered: List[str] = []
    for cand in SYMPTOM_RECOMMENDATIONS[canon]:
        if not _available(cand.medicine, available_medicines):
            filtered.append(f"{cand.medicine}: not available/loaded")
            continue
        hit = _contraindication_for(cand.medicine, notes_low)
        if hit is not None:
            filtered.append(
                f"{cand.medicine}: contraindicated ({hit.severity}) - {hit.action}"
            )
            continue
        kept.append(cand)

    if not kept:
        return _no_option(symptom, filtered=filtered)

    return SelfCareResult(
        kind=ResultKind.RECOMMENDATION,
        symptom=symptom,
        suggestions=kept,
        message=(
            f"A common over-the-counter option people use for {canon} is "
            f"{kept[0].medicine}. Read the label and take it only if it's right "
            f"for you."
        ),
        disclaimer=DISCLAIMER,
        product_notice=PRODUCT_NOTICE,
        knowledge_base_version=KNOWLEDGE_BASE_VERSION,
        rules_version=RULES_VERSION,
        review_status=KB_REVIEW_STATUS,
        filtered=filtered,
    )
