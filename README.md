# PillBot

An AI-assisted **home medication organizer, reminder, and automatic dispenser**.

PillBot stores the medicines you already own — everyday OTC meds (paracetamol,
ibuprofen, antacid) and meds your doctor has told you to take — reminds you on
schedule, and dispenses them on request. It is **not** a prescriber, a diagnostic
tool, or a clinician.

> ⚠️ **Status: hardware-free core, not for real-patient use.** The dosing tables
> and the self-care knowledge base are **unsigned placeholders** that need
> licensed-clinician sign-off, and the product needs regulatory + legal review
> before any real deployment. See [COMPLIANCE.md](COMPLIANCE.md). This repo runs
> fully on a PC with a **simulated** dispenser — no Raspberry Pi / Arduino / servo
> / sensor required.

## Design principle: *AI proposes, a deterministic system disposes*

The AI is a convenience layer. Every dispense — scheduled, on-request, or
self-care — passes through deterministic, validated code that holds final
authority and the AI cannot override:

- **Dose-safety gate** (`pillbot.safety`) — medicine-keyed daily limits, minimum
  interval, and interaction windows. Emits a per-dose **decision certificate**.
- **Sensor-verified dispensing** (`pillbot.dispenser` + `pillbot.magazines`) — a
  dose is only counted when a sensor confirms exactly one pill physically left.
- **Tamper-evident ledger** (`pillbot.ledger`) — every decision is hash-chained
  and idempotent (a replay can't double-dispense).
- **Curated self-care** (`pillbot.selfcare`) — OTC suggestions for minor symptoms
  come from a validated table with contraindication + red-flag checks; the LLM
  only names the *symptom*, never the medicine.

## Package layout

| Module | Responsibility |
|---|---|
| `pillbot/app.py` | App: CLI loop, AI orchestration, scheduler, session/auth, wiring |
| `pillbot/config.py` | Typed settings + secrets (from env / `.env`) |
| `pillbot/clock.py` | Injectable clock + monotonic rollback guard |
| `pillbot/hardware.py` | Dispenser backend: real serial + virtual-Arduino simulator |
| `pillbot/magazines.py` | Expandable, hot-pluggable, tagged magazines + pill sensor |
| `pillbot/store.py` | SQLite persistence for magazine tag→medicine assignments |
| `pillbot/dispenser.py` | Magazine-aware dispensing service (pick → actuate → verify) |
| `pillbot/safety.py` | Deterministic dose-safety gate + decision certificate |
| `pillbot/runtime.py` | Assembles the engine (registry + service + safety + ledger) |
| `pillbot/ledger.py` | Tamper-evident, idempotent hash-chained dispense ledger |
| `pillbot/selfcare.py` | Curated OTC self-care recommender (red-flag + contraindication) |
| `pillbot/__main__.py` | `python -m pillbot` entry point |

## Run it

```bash
pip install -r requirements.txt        # or: pip install -e .
python -m pillbot                      # or, after install:  pillbot
```

It starts with a **simulated** dispenser by default (no hardware). Configure via
environment or a `.env` file (see [.env.example](.env.example)):

```
OPENROUTER_API_KEY=...        # required only for AI command interpretation
PILLBOT_BACKEND=auto          # serial | sim | auto (auto falls back to sim)
ADMIN_PIN=...                 # admin mode disabled unless set
```

### CLI quick reference

```
:new                      register a user (name + 4-digit PIN)
:insert <tag> <bay> <n>   hot-plug a magazine (sim: n pills)
:assign <tag> <medicine>  assign a medicine to a magazine on first use
:mags                     list loaded magazines + counts
:dispense <medicine>      dispense a medicine you name (gated + verified)
:symptom <how you feel>   curated OTC self-care suggestion for a minor symptom
:audit                    verify the tamper-evident dispense ledger
:logout / :quit
```

You can also just type naturally; the AI interprets it (needs `OPENROUTER_API_KEY`).

## Tests

```bash
python -m pytest tests -q
```

85 tests cover the safety gate, magazines + sensor verification, the ledger
(chain integrity + idempotency), the clock guard, reminder-time normalization,
the scheduler, and the self-care recommender — all hardware-free.

> **Note:** the test suite has not yet been migrated into this repository. The
> 85 tests exist and are being moved across from the original working copy; the
> package itself is complete and runs. Treat the count above as a claim about
> the original tree until `tests/` appears here.

## What's next

- Real-hardware path: extend the serial protocol with the sensor-count token so a
  physical Arduino gets the same closed-loop verification as the simulator.
- Caregiver remote portal, adherence/missed-dose analytics, voice input, web UI.
- Clinician sign-off on dosing + self-care tables; regulatory/legal review.

See [ROADMAP.md](ROADMAP.md) for the full plan and [COMPLIANCE.md](COMPLIANCE.md)
for the regulatory posture.
