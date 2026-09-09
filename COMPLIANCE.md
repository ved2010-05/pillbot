# PillBot — Compliance & Intended Use

> ⚠️ **Not legal or clinical advice.** This document records an engineering-level
> compliance posture to guide development. It must be reviewed by qualified
> regulatory/IP counsel and a licensed clinician/pharmacist before any
> real-patient use, sale, marketing, or public demo. Regulations change — verify
> against current authoritative sources.

## Intended-use statement (load-bearing)

> PillBot's OTC self-care suggestion feature is a **general-wellness information
> aid** for a home medication organizer. For **minor, self-limiting symptoms**
> (mild headache, mild fever, body ache, mild heartburn/acidity) it surfaces,
> from a **curated, clinician-validated** knowledge base, the common
> over-the-counter option(s) PillBot **already physically stocks** so the user
> can choose to self-administer a medicine they already own. It does **not**
> diagnose, treat or cure disease, prescribe, or individualize a therapeutic
> regimen, and is **not** a substitute for a clinician or pharmacist. The AI only
> *proposes* from a fixed catalogue; a deterministic, clinician-signed-off safety
> gate and the user's own confirmation *dispose*.

This statement is the anchor for device classification — keep it consistent
across the product, marketing, and any filing. *(Wording, especially
India-DMR-Act-sensitive phrasing, requires counsel sign-off.)*

## How the design supports the lighter "general-wellness" lane

- Self-care suggestions come **only** from a curated, versioned table
  (`pillbot_selfcare`), never free-form LLM reasoning; the LLM only names the
  *symptom*.
- Suggestions are limited to medicines **stocked and loaded**; never names meds
  it can't dispense.
- **Red-flag escalation runs first** and cannot be overridden (severe/atypical
  presentations → "see a doctor", no medicine).
- **Contraindication checks** against the user's recorded conditions.
- Framed as **information, not advice**, with a disclaimer on every suggestion.
- **Human confirmation required**; nothing auto-dispenses.
- Every decision is recorded in a tamper-evident ledger with the safety
  certificate (accountability / auditability).

## Knowledge-base status

`pillbot_selfcare` and `pillbot_safety.MED_SAFETY` are flagged
`UNSIGNED_PLACEHOLDER` / placeholder values. **A licensed pharmacist/clinician
must review and sign off** the symptom→OTC map, contraindications, red-flags, and
dose limits before the feature is enabled for real users.

## Regulatory summary — general

| Area | Posture |
|---|---|
| Medical device / SaMD | An automated medication *dispenser* may be a regulated device (FDA / EU MDR); the safety software may be SaMD. **Determine classification with counsel first** — it shapes everything. |
| Privacy (HIPAA / GDPR) | Names, ages, health notes, and medication history are health data. **Sending identifiable data to a third-party LLM (OpenRouter) is the sharpest open issue** — minimize/avoid it, use on-device/self-hosted inference, or get a BAA/DPA. |
| Claims | No safety/efficacy/medical-benefit claims pre-clearance. Don't market the overdose-prevention gate as a therapeutic claim. |
| Liability | Dispensing medication carries real harm potential; a silent missed dose from a crash is the worst case (mitigated by the watchdog/missed-dose work on the roadmap). |

## Regulatory summary — India

| Area | Posture |
|---|---|
| Medical device | Likely regulated by **CDSCO** under the Medical Devices Rules, 2017 (all devices now regulated); classification (plausibly Class C/D) needs a regulatory opinion. |
| Data protection | **DPDP Act, 2023** governs health data; cross-border transfer to a foreign LLM needs care (consent, purpose limitation; verify current DPDP Rules). |
| Patents | **Patents Act §3(k)** — software "per se" is **not** patentable; claim the **apparatus/system** (the dispenser + sensor-verified closed loop + deterministic gate), not the algorithm. **§3(i)** — claim the device, not a treatment method. Coordinate with a patent attorney; the hardware-tied system claims are the defensible path. |
| Consumer / advertising | Consumer Protection Act, 2019 product-liability; Drugs & Magic Remedies (Objectionable Advertisements) Act, 1954 limits drug-related claims. |

## Required before any real-patient use / sale

1. Regulatory classification opinion (FDA and/or CDSCO / EU MDR).
2. Clinician/pharmacist sign-off on all dosing + self-care content.
3. Privacy review of the AI data flow (the LLM call); fix PHI exposure.
4. Patent strategy with counsel (apparatus/system claims; coordinate IN/US).
5. Product-liability counsel + insurance.

*Sources: internal `/legal:compliance-check` and `/legal:brief` (India) runs +
the 3-agent legal review. Orientation only — not a legal opinion.*
