# -*- coding: utf-8 -*-
"""
MarsV3 PillBot — Production-grade autonomous pill dispenser
============================================================

SAFETY ARCHITECTURE
───────────────────
  The AI is a convenience layer only.  Every dispense — whether triggered
  by AI, scheduler, or admin — must pass through the deterministic safety
  gate in pillbot.safety FIRST (medicine-keyed dose limits + magazine
  inventory), applied via PillbotRuntime.dispense().  It is pure Python and
  is the only authority that may block a dispense.

SERIAL PROTOCOL
───────────────
  Host  → Arduino : "<slot>\n"            e.g. "2\n"
  Arduino → Host  : "OK:<slot>\n"         success acknowledgment
                  | "ERR:<slot>:<msg>\n"  hardware fault

  dispense() retries up to DISPENSE_RETRIES times and blocks until an ACK
  or timeout.  A dispense is only logged as successful if hardware confirmed.

USER IDENTIFICATION
───────────────────
  Users authenticate with a 4-digit PIN stored (SHA-256 hashed) in SQLite.
  The session is locked until a known user is identified.

SCHEDULER PERSISTENCE
─────────────────────
  Fired events are stored in SQLite so a restart never double-fires.

ENV VARS (all optional, defaults shown)
────────────────────────────────────────
  OPENROUTER_API_KEY   — required for AI
  OPENROUTER_MODEL     — default: qwen/qwen-2.5-coder-32b-instruct:free
  STEIN_BASE_URL       — if set, data is also synced to Stein (optional)
  SERIAL_PORT          — default: COM3 (Windows) / /dev/ttyUSB0 (Linux)
  SERIAL_BAUD          — default: 9600
  SERIAL_TIMEOUT       — read timeout seconds (default: 3)
  DISPENSE_RETRIES     — default: 3
  PILLBOT_DB           — default: pillbot.db
  PILLBOT_LOG          — default: pillbot.log
  PILLBOT_SCHEDULER    — set "0" to disable
  ADMIN_PIN            — 4-digit PIN for admin/override mode (default: 0000)

Run:
  pip install requests pyserial pyttsx3
  python pillbot.py
"""

# ─────────────────────────────────────────────────────────────
# Imports
# ─────────────────────────────────────────────────────────────
import os
import re
import sys
import json
import time
import queue
import signal
import hashlib
import logging
import sqlite3
import datetime
import threading
import traceback
import uuid
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, List, Optional, Tuple

import requests
import pyttsx3

from pillbot.config import Settings
import pillbot.clock
from pillbot.clock import now as clock_now
from pillbot.hardware import make_backend
from pillbot.runtime import PillbotRuntime
from pillbot.store import SqliteMagazineStore
from pillbot import drugdata
from pillbot.ledger import HashChainedLedger
import pillbot.selfcare


# ─────────────────────────────────────────────────────────────
# Logging — rotating file + console
# ─────────────────────────────────────────────────────────────
LOG_FILE = os.getenv("PILLBOT_LOG", "pillbot.log")

_fmt = logging.Formatter(
    "%(asctime)s  %(levelname)-8s  [%(threadName)s]  %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
_file_h = RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=5)
_file_h.setFormatter(_fmt)
_cons_h = logging.StreamHandler()
_cons_h.setFormatter(_fmt)

log = logging.getLogger("pillbot")
log.setLevel(logging.DEBUG)
log.addHandler(_file_h)
log.addHandler(_cons_h)


# ─────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────
settings = Settings.from_env()

OPENROUTER_API_KEY: str = settings.openrouter_api_key
OPENROUTER_MODEL: str   = settings.openrouter_model
OPENROUTER_URL: str     = settings.openrouter_url

STEIN_BASE: str         = settings.stein_base
STEIN_HEADERS           = {"Content-Type": "application/json"}

SERIAL_PORT: str        = settings.serial_port
SERIAL_BAUD: int        = settings.serial_baud
SERIAL_TIMEOUT: float   = settings.serial_timeout
DISPENSE_RETRIES: int   = settings.dispense_retries

DB_PATH: str            = settings.db_path
ENABLE_SCHEDULER: bool  = settings.enable_scheduler
REQUEST_TIMEOUT: int    = settings.request_timeout

ADMIN_PIN: str          = settings.admin_pin

for _warning in settings.validate():
    log.warning("CONFIG: %s", _warning)


# ─────────────────────────────────────────────────────────────
# Medication identity, inventory, and dose-safety rules now live in modules:
#   - what's loaded / pill counts  -> pillbot.magazines (live MagazineRegistry)
#   - per-medicine dose limits      -> pillbot.safety.MED_SAFETY (medicine-keyed)
# The old fixed slot map (SLOT_TO_MED / MED_TO_SLOT / slot-keyed MED_SAFETY) has
# been removed: the dispenser is now expandable and magazine-based.
# ─────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────
# Session
# ─────────────────────────────────────────────────────────────
SESSION: Dict[str, Any] = {"active_user": None, "admin": False}
_pending_lock = threading.Lock()
PENDING_CONFIRM: Dict[str, Any] = {"type": None, "data": {}}


def _pin_hash(pin: str) -> str:
    return hashlib.sha256(pin.strip().encode()).hexdigest()


# ─────────────────────────────────────────────────────────────
# SQLite — local primary store
# ─────────────────────────────────────────────────────────────
def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    with _db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT NOT NULL,
                age        TEXT,
                gender     TEXT,
                notes      TEXT,
                pin_hash   TEXT,
                created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now'))
            );

            CREATE TABLE IF NOT EXISTS reminders (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                reminder_user  TEXT NOT NULL,
                pill           TEXT NOT NULL,
                slot           INTEGER NOT NULL,
                time           TEXT NOT NULL,
                regularity     TEXT DEFAULT 'daily',
                enable         INTEGER DEFAULT 1,
                created_at     TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now'))
            );

            CREATE TABLE IF NOT EXISTS dispense_log (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                user     TEXT,
                pill     TEXT NOT NULL,
                slot     INTEGER NOT NULL,
                reason   TEXT,
                hw_ack   INTEGER DEFAULT 0,
                ts       TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now'))
            );

            -- Persistent scheduler state: prevents double-fire after restart
            CREATE TABLE IF NOT EXISTS scheduler_fired (
                fire_key  TEXT PRIMARY KEY,
                fired_at  TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now'))
            );

            -- Catch-all structured event log (every significant event)
            CREATE TABLE IF NOT EXISTS event_log (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL,
                detail   TEXT,
                ts       TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now'))
            );

            -- Persistent key/value for the monotonic clock guard (rollback defence)
            CREATE TABLE IF NOT EXISTS clock_state (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)
    log.info("SQLite DB ready at %s", DB_PATH)


def _log_event(category: str, detail: Any) -> None:
    if not isinstance(detail, str):
        try:
            detail = json.dumps(detail, default=str)
        except Exception:
            detail = str(detail)
    try:
        with _db() as conn:
            conn.execute(
                "INSERT INTO event_log (category, detail) VALUES (?,?)",
                (category, detail),
            )
        log.debug("[EVENT:%s] %s", category, (detail or "")[:300])
    except Exception:
        log.exception("_log_event failed")


# ── Monotonic clock-rollback guard persistence ────────────────────────────
def _load_clock_hwm() -> Optional[datetime.datetime]:
    try:
        with _db() as conn:
            row = conn.execute(
                "SELECT value FROM clock_state WHERE key='hwm'"
            ).fetchone()
        return datetime.datetime.fromisoformat(row["value"]) if row else None
    except Exception:
        return None


def _save_clock_hwm(dt: datetime.datetime) -> None:
    try:
        with _db() as conn:
            conn.execute(
                "INSERT INTO clock_state (key, value) VALUES ('hwm', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (dt.isoformat(timespec="seconds"),),
            )
    except Exception:
        log.exception("_save_clock_hwm failed")


def _on_clock_rollback(base: datetime.datetime, hwm: datetime.datetime) -> None:
    log.warning("Clock rollback detected: wall=%s < high-water=%s. Clamping "
                "forward so the safety window cannot be reset.", base, hwm)
    _log_event("clock_rollback", {"wall": base.isoformat(), "hwm": hwm.isoformat()})


# ─────────────────────────────────────────────────────────────
# Dose-safety enforcement now lives in pillbot.safety (medicine-keyed) and is
# applied via the magazine runtime — see pillbot.safety.safety_gate and
# PillbotRuntime.dispense(). The AI still cannot bypass it.
# ─────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────
# Dispenser hardware — via the swappable backend (real serial or simulator)
# ─────────────────────────────────────────────────────────────
_backend = make_backend(settings)

# Magazine-based dispensing engine. Uses a simulated sensor/actuator for now
# (so it runs hardware-free); a real serial-backed sensor + actuator plug in
# here later. Tag assignments persist in the same DB (separate "magazines" table).
# openFDA drug-knowledge cache: populated when a medicine is assigned to a
# magazine; the deterministic safety gate reads it OFFLINE (no live call there).
_drug_cache = drugdata.DrugDataCache(DB_PATH + ".drugdata.json")
_runtime = PillbotRuntime(
    store=SqliteMagazineStore(DB_PATH),
    clock=clock_now,
    ledger=HashChainedLedger(DB_PATH, clock=clock_now),
    fda_cache=_drug_cache,
)


def _ensure_drug_data(medicine: str):
    """Ensure FDA label data is cached for a medicine (fetched on assign so the
    gate can use it offline). Returns the DrugInfo or None."""
    info = _drug_cache.get(medicine)
    if info is not None:
        return info
    try:
        return drugdata.refresh(_drug_cache, medicine,
                                api_key=os.getenv("PILLBOT_OPENFDA_KEY") or None)
    except Exception as exc:
        log.warning("openFDA fetch failed for %s: %s", medicine, exc)
        return None


def _speak_drug_summary(medicine: str, info) -> None:
    if info is None or not getattr(info, "fetch_ok", False):
        speak(f"(No FDA data for {medicine} - it can only be dispensed if a "
              f"clinician-reviewed limit is set.)")
        return
    bits = []
    if info.max_per_day:
        bits.append(f"max {info.max_per_day}/day")
    if info.min_interval_h:
        bits.append(f"every {info.min_interval_h:g}h")
    limits = ", ".join(bits) if bits else "no numeric limit parsed (label text only)"
    speak(f"FDA label for {info.active_ingredient or medicine}: {limits}.")


def dispense(slot: int, med: str, user: str = "(unknown)") -> bool:
    """
    Dispense one tablet from `slot` via the active backend (real serial or the
    simulator). Returns True ONLY if the hardware confirmed with an OK ack.
    The retry/reconnect/protocol logic lives in pillbot.hardware.SerialBackend.
    """
    _log_event("dispense_attempt", {"slot": slot, "med": med, "user": user})

    result = _backend.dispense_slot(int(slot))
    _log_event("dispense_ack", {"slot": slot, "ack": result.ack,
                                "ok": result.ok, "attempts": result.attempts})

    if result.ok:
        log.info("Dispensed slot=%d (%s) in %d attempt(s)", slot, med, result.attempts)
        return True

    if result.error_code and result.error_code != "no_ack":
        log.error("Hardware fault on slot %d: %s", slot, result.error_code)
        _log_event("dispense_hw_error", {"slot": slot, "ack": result.ack,
                                         "error": result.error_code})
    else:
        log.error("Dispense FAILED after %d attempt(s) — slot=%d", result.attempts, slot)
        _log_event("dispense_failed", {"slot": slot, "med": med,
                                       "attempts": result.attempts})
    return False


# ─────────────────────────────────────────────────────────────
# TTS  (bounded queue — drops audio on overflow, never blocks logic)
# ─────────────────────────────────────────────────────────────
_TTS_QUEUE: queue.Queue = queue.Queue(maxsize=30)


def _tts_worker() -> None:
    while True:
        text = _TTS_QUEUE.get()
        if text is None:
            break
        try:
            engine = pyttsx3.init()
            engine.setProperty("rate", 160)
            engine.say(str(text))
            engine.runAndWait()
            engine.stop()
            del engine
        except Exception as exc:
            log.warning("TTS error: %s", exc)
        finally:
            _TTS_QUEUE.task_done()


threading.Thread(target=_tts_worker, daemon=True, name="TTS").start()


def speak(text: str) -> None:
    print(f"[PillBot] {text}")
    log.info("[SPEAK] %s", text)
    _log_event("speak", text)
    try:
        _TTS_QUEUE.put_nowait(text)
    except queue.Full:
        log.warning("TTS queue full — audio skipped: %s", text[:80])


# ─────────────────────────────────────────────────────────────
# User CRUD
# ─────────────────────────────────────────────────────────────
def get_all_users() -> List[Dict]:
    with _db() as conn:
        rows = conn.execute("SELECT * FROM users").fetchall()
    result = [dict(r) for r in rows]
    _log_event("db_read", {"table": "users", "count": len(result)})
    return result


def get_user_by_name(name: str) -> Optional[Dict]:
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE lower(name)=?", (name.strip().lower(),)
        ).fetchone()
    result = dict(row) if row else None
    _log_event("db_read", {"table": "users", "query_name": name, "found": result is not None})
    return result


def add_user(
    name: str,
    age: Any = None,
    gender: str = None,
    notes: str = "",
    pin: str = "0000",
) -> Optional[Dict]:
    _log_event("add_user", {"name": name, "age": age, "gender": gender})
    with _db() as conn:
        conn.execute(
            "INSERT INTO users (name, age, gender, notes, pin_hash) VALUES (?,?,?,?,?)",
            (name, str(age) if age is not None else None, gender, notes, _pin_hash(pin)),
        )
    user = get_user_by_name(name)
    SESSION["active_user"] = user
    _stein_post("users", [user])
    return user


def update_user(row_id: int, updates: Dict) -> bool:
    allowed = {"name", "age", "gender", "notes"}
    clean = {k: v for k, v in updates.items() if k in allowed}
    if not clean:
        return False
    set_clause = ", ".join(f"{k}=?" for k in clean)
    values = list(clean.values()) + [row_id]
    _log_event("update_user", {"id": row_id, "fields": list(clean.keys())})
    with _db() as conn:
        conn.execute(f"UPDATE users SET {set_clause} WHERE id=?", values)
    return True


def authenticate_user(name: str, pin: str) -> Optional[Dict]:
    user = get_user_by_name(name)
    if not user:
        _log_event("auth_fail", {"name": name, "reason": "unknown_user"})
        return None
    if user.get("pin_hash") != _pin_hash(pin):
        _log_event("auth_fail", {"name": name, "reason": "wrong_pin"})
        return None
    _log_event("auth_ok", {"name": name})
    return user


# ─────────────────────────────────────────────────────────────
# Reminder CRUD
# ─────────────────────────────────────────────────────────────
def get_all_reminders() -> List[Dict]:
    with _db() as conn:
        rows = conn.execute("SELECT * FROM reminders").fetchall()
    result = [dict(r) for r in rows]
    _log_event("db_read", {"table": "reminders", "count": len(result)})
    return result


def add_reminder(
    reminder_user: str,
    pill: str,
    slot: int,
    time_hhmm: str,
    regularity: str = "daily",
    enable: bool = True,
) -> bool:
    _log_event("add_reminder", {
        "user": reminder_user, "pill": pill, "slot": slot,
        "time": time_hhmm, "regularity": regularity,
    })
    with _db() as conn:
        conn.execute(
            "INSERT INTO reminders (reminder_user,pill,slot,time,regularity,enable) VALUES (?,?,?,?,?,?)",
            (reminder_user, pill, slot, time_hhmm, regularity, 1 if enable else 0),
        )
    _stein_post("reminders", [{
        "reminder_user": reminder_user, "pill": pill, "slot": slot,
        "time": time_hhmm, "regularity": regularity, "enable": enable,
    }])
    return True


def remove_reminder_by_id(row_id: int) -> bool:
    _log_event("remove_reminder", {"id": row_id})
    with _db() as conn:
        cur = conn.execute("DELETE FROM reminders WHERE id=?", (row_id,))
    return cur.rowcount > 0


def remove_reminder_by_fields(user: str, pill: str, time_hhmm: str) -> bool:
    _log_event("remove_reminder", {"user": user, "pill": pill, "time": time_hhmm})
    with _db() as conn:
        cur = conn.execute(
            "DELETE FROM reminders WHERE lower(reminder_user)=? AND lower(pill)=? AND time=?",
            (user.strip().lower(), pill.strip().lower(), time_hhmm),
        )
    return cur.rowcount > 0


# ─────────────────────────────────────────────────────────────
# Dispense log
# ─────────────────────────────────────────────────────────────
def add_dispense_log(user: str, pill: str, slot: int, reason: str, hw_ack: bool) -> None:
    _log_event("dispense_log", {
        "user": user, "pill": pill, "slot": slot,
        "reason": reason, "hw_ack": hw_ack,
    })
    ts = clock_now().isoformat(timespec="seconds")
    with _db() as conn:
        conn.execute(
            "INSERT INTO dispense_log (user,pill,slot,reason,hw_ack,ts) VALUES (?,?,?,?,?,?)",
            (user, pill, slot, reason, 1 if hw_ack else 0, ts),
        )
    if hw_ack:
        _stein_post("log", [{
            "user": user, "pill": pill, "slot": slot,
            "reason": reason, "ts": ts,
        }])


def get_logs_last24h(user_name: str) -> List[Dict]:
    cutoff = (clock_now() - datetime.timedelta(hours=24)).isoformat()
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM dispense_log WHERE user=? AND ts>=?",
            (user_name, cutoff),
        ).fetchall()
    result = [dict(r) for r in rows]
    _log_event("db_read", {"table": "dispense_log", "user": user_name, "count": len(result)})
    return result


# ─────────────────────────────────────────────────────────────
# Optional Stein remote sync (fire-and-forget background thread)
# ─────────────────────────────────────────────────────────────
def _stein_post(endpoint: str, payload: list) -> None:
    if not STEIN_BASE:
        return

    def _do() -> None:
        try:
            r = requests.post(
                f"{STEIN_BASE}/{endpoint}",
                json=payload,
                headers=STEIN_HEADERS,
                timeout=REQUEST_TIMEOUT,
            )
            r.raise_for_status()
            _log_event("stein_sync", {"endpoint": endpoint, "status": r.status_code})
        except requests.RequestException as exc:
            log.warning("Stein sync failed %s: %s", endpoint, exc)
            _log_event("stein_sync_error", {"endpoint": endpoint, "error": str(exc)})

    threading.Thread(target=_do, daemon=True, name="SteinSync").start()


# ─────────────────────────────────────────────────────────────
# AI (OpenRouter)
# ─────────────────────────────────────────────────────────────
AI_SYSTEM_PROMPT = """
You are PillBot, a voice-controlled assistant for an EXPANDABLE, magazine-based
pill dispenser. Each medicine sits in its own removable magazine; the set of
medicines currently loaded (and how many pills remain) is given to you in the
context's "inventory" field, and it can change at any time.

Rules:
- Only ever suggest medicines that appear in the provided "inventory". Never
  invent or recommend anything that is not currently loaded.
- Refer to medicines by NAME, never by slot or bay number.
- Use the user profile and 24h dispense log to inform PRN suggestions.
- You are NOT the safety authority. A deterministic code-level gate runs after
  every suggestion (dose limits + inventory). Do not reason about dosing limits
  yourself.
- The scheduler handles timed reminders independently; you may only add/remove them.
- If the user DESCRIBES a minor symptom (e.g. "I have a headache", "my stomach is acidic"),
  respond with "self_care" and just the symptom keyword. Do NOT name, choose, or compare
  medicines yourself — the device's validated self-care table decides, checks red flags and
  contraindications, and may refuse. Use "suggest_dispense" only when the user explicitly
  names a medicine they already want.
- Never dispense autonomously; the user must confirm.
- If a registered user's name is provided, do not ask for age/gender again.
- Ask for missing required fields via a "speak" action.

Respond ONLY with a single strict JSON object — no prose, no markdown fences.
Required field "action" must be one of:

  "speak"            -> {"action":"speak","text":"...","reason":"..."}
  "add_user"         -> {"action":"add_user","name":"","age":"","gender":"","notes":""}
  "update_user"      -> {"action":"update_user","row_id":<int|null>,"name":"",<fields>}
  "add_reminder"     -> {"action":"add_reminder","reminder_user":"","pill":"","time":"HH:MM","regularity":"daily"}
  "remove_reminder"  -> {"action":"remove_reminder","row_id":<int|null>,"reminder_user":"","pill":"","time":"HH:MM"}
  "suggest_dispense" -> {"action":"suggest_dispense","medication":"","reason":"..."}
  "self_care"        -> {"action":"self_care","symptom":"headache","reason":"..."}
  "noop"             -> {"action":"noop","reason":"..."}

Always include a "reason" field.
""".strip()


def _med_context(m) -> Dict[str, Any]:
    """Inventory entry enriched with the medicine's FDA knowledge, so the AI can
    reason with the label (purpose, limits, do-not-use) before proposing."""
    ctx: Dict[str, Any] = {"medicine": m.medicine, "remaining": m.remaining,
                           "state": m.state.value}
    info = _drug_cache.get(m.medicine)
    if info is not None and getattr(info, "fetch_ok", False):
        ctx["fda"] = {
            "purpose": (info.purpose or "")[:120],
            "max_per_day": info.max_per_day,
            "min_interval_h": info.min_interval_h,
            "do_not_use": (info.do_not_use or "")[:200],
            "source": info.source_url,
        }
    return ctx


def ask_ai(
    user_message: str,
    user_info: Dict,
    reminders: List[Dict],
    logs: List[Dict],
) -> Dict[str, Any]:
    ctx = {
        "inventory": [_med_context(m) for m in _runtime.inventory() if m.medicine],
        "user": user_info,
        "recent_dispense_log": logs,
        "reminders": reminders,
        "query": user_message,
    }
    payload = {
        "model": OPENROUTER_MODEL,
        "messages": [
            {"role": "system", "content": AI_SYSTEM_PROMPT},
            {"role": "user",   "content": json.dumps(ctx, default=str)},
        ],
        "temperature": 0.15,
    }
    _log_event("ai_request", {"model": OPENROUTER_MODEL, "user_message": user_message})
    log.debug("AI request → %s", user_message[:200])

    try:
        r = requests.post(
            OPENROUTER_URL,
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=30,
        )
        r.raise_for_status()
    except requests.RequestException as exc:
        log.error("AI request failed: %s", exc)
        _log_event("ai_error", {"error": str(exc)})
        return {"action": "noop", "reason": f"AI request failed: {exc}"}

    raw = ""
    try:
        raw = r.json()["choices"][0]["message"]["content"]
        _log_event("ai_response", {"raw": raw})
        log.debug("AI raw → %s", raw[:300])
    except (KeyError, IndexError, ValueError) as exc:
        log.error("AI response structure error: %s", exc)
        _log_event("ai_error", {"error": str(exc)})
        return {"action": "noop", "reason": "Malformed AI response"}

    # Direct parse
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # Balanced-brace extraction
    start = raw.find("{")
    if start != -1:
        depth = 0
        for i, ch in enumerate(raw[start:], start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            if depth == 0:
                try:
                    return json.loads(raw[start: i + 1])
                except json.JSONDecodeError:
                    break

    log.error("AI non-JSON response: %s", raw[:300])
    _log_event("ai_error", {"error": "non-JSON", "raw": raw[:300]})
    speak("Could not parse AI response. Please try again.")
    return {"action": "noop", "reason": "non-JSON AI response"}


# ─────────────────────────────────────────────────────────────
# Execute AI actions
# ─────────────────────────────────────────────────────────────
def _normalize_hhmm(text: str) -> Optional[str]:
    """Parse a user/AI-supplied time into canonical 24-hour 'HH:MM', or None if
    unparseable. Accepts '8:00', '8', '8am', '8 pm', '20:00', 'noon', 'midnight'.
    Fixes the bug where '8:00'/'noon' were stored raw and never matched the
    scheduler's zero-padded '%H:%M'."""
    s = (text or "").strip().lower()
    if not s:
        return None
    if s in ("noon", "midday"):
        return "12:00"
    if s == "midnight":
        return "00:00"
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", s)
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2) or 0)
    ampm = m.group(3)
    if ampm == "am" and hour == 12:
        hour = 0
    elif ampm == "pm" and hour != 12:
        hour += 12
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return f"{hour:02d}:{minute:02d}"


def execute_ai_action(ai_resp: Dict[str, Any]) -> None:
    action = ai_resp.get("action", "noop")
    reason = ai_resp.get("reason", "")
    log.info("AI action=%s  reason=%s", action, reason)
    _log_event("ai_action", ai_resp)

    if action == "noop":
        return

    if action == "speak":
        speak(ai_resp.get("text", ""))
        return

    if action == "add_user":
        name = ai_resp.get("name")
        if not name:
            speak("I need a name to add a user.")
            return
        speak(f"Creating account for {name}. Please enter a 4-digit PIN:")
        pin = input("PIN> ").strip()
        if not re.fullmatch(r"\d{4}", pin):
            speak("Invalid PIN — must be exactly 4 digits. User not added.")
            return
        add_user(
            name=name,
            age=ai_resp.get("age"),
            gender=ai_resp.get("gender"),
            notes=ai_resp.get("notes", ""),
            pin=pin,
        )
        speak(f"User {name} registered successfully.")
        return

    if action == "update_user":
        row_id = ai_resp.get("row_id")
        name   = ai_resp.get("name")
        if not row_id and name:
            u = get_user_by_name(name)
            row_id = u.get("id") if u else None
        if not row_id:
            speak("Could not find the user to update.")
            return
        updates = {k: v for k, v in ai_resp.items() if k in {"name", "age", "gender", "notes"}}
        ok = update_user(int(row_id), updates)
        speak("User updated." if ok else "Update failed.")
        return

    if action == "add_reminder":
        ru         = ai_resp.get("reminder_user") or ai_resp.get("reminder-user", "")
        pill       = ai_resp.get("pill", "")
        time_hhmm  = ai_resp.get("time", "")
        regularity = ai_resp.get("regularity", "daily")
        if not (ru and pill and time_hhmm):
            speak("Missing user, pill, or time for this reminder.")
            return
        normalized = _normalize_hhmm(time_hhmm)
        if normalized is None:
            speak(f"I couldn't understand the time '{time_hhmm}'. Use 24-hour HH:MM, e.g. 20:00.")
            return
        add_reminder(ru, pill, 0, normalized, regularity)  # slot vestigial (magazine-based)
        speak(f"Reminder added for {ru}: {pill} at {normalized}.")
        return

    if action == "remove_reminder":
        row_id = ai_resp.get("row_id")
        if row_id:
            ok = remove_reminder_by_id(int(row_id))
            speak("Reminder removed." if ok else "Reminder not found.")
            return
        ru        = ai_resp.get("reminder_user") or ai_resp.get("reminder-user", "")
        pill      = ai_resp.get("pill", "")
        time_hhmm = ai_resp.get("time", "")
        if not (ru and pill and time_hhmm):
            speak("Provide user, pill, and time to remove a reminder.")
            return
        ok = remove_reminder_by_fields(ru, pill, time_hhmm)
        speak("Reminder removed." if ok else "No matching reminder found.")
        return

    if action == "suggest_dispense":
        med    = ai_resp.get("medication") or ai_resp.get("pill", "")
        reason = ai_resp.get("reason", "PRN request")
        speak(f"AI suggests: {med}. Reason: {reason}")
        speak("Say 'yes' to confirm, anything else to cancel.")
        with _pending_lock:
            PENDING_CONFIRM["type"] = "dispense"
            PENDING_CONFIRM["data"] = {"med": med, "reason": reason}
        return

    if action == "self_care":
        symptom = ai_resp.get("symptom") or ai_resp.get("medication") or ""
        if not symptom:
            speak("Tell me the symptom and I'll check what's safe to suggest.")
            return
        _handle_symptom(symptom)
        return

    log.warning("Unrecognised AI action: %s", action)
    speak(f"Unknown action '{action}'.")


# ─────────────────────────────────────────────────────────────
# Confirmed dispense handler
# ─────────────────────────────────────────────────────────────
def _recent_confirmed_doses(user_name: str) -> List[Dict]:
    """The user's hardware-confirmed doses in the last 24h, shaped for the
    medicine-keyed safety gate: [{"medicine": str, "ts": datetime}]."""
    out: List[Dict] = []
    for r in get_logs_last24h(user_name):
        if not r.get("hw_ack"):
            continue
        try:
            out.append({"medicine": r.get("pill"),
                        "ts": datetime.datetime.fromisoformat(str(r.get("ts")))})
        except (TypeError, ValueError):
            continue
    return out


def _handle_symptom(symptom_text: str) -> None:
    """Surface a CURATED, deterministic OTC self-care option for a symptom.
    The AI may only reach here by naming a symptom; pillbot.selfcare (NOT the AI)
    chooses the medicine, runs red-flag + contraindication checks, and may refuse.
    Any resulting dispense still passes the dose-safety gate + user confirmation."""
    active = SESSION.get("active_user") or {}
    user_name = active.get("name", "(unknown)")
    notes = active.get("notes", "") or ""
    available = [m.medicine for m in _runtime.inventory()
                 if m.medicine and m.state.value == "ready"]

    result = pillbot.selfcare.recommend(symptom_text, notes, available)
    _log_event("self_care", {"user": user_name, "symptom": symptom_text,
                             "kind": result.kind.value,
                             "triggered_rule": result.triggered_rule})

    speak(result.message)
    if result.kind == pillbot.selfcare.ResultKind.RECOMMENDATION:
        speak(result.disclaimer)
        med = result.suggestions[0].medicine
        speak(f"Say 'yes' to dispense {med}, anything else to cancel.")
        with _pending_lock:
            PENDING_CONFIRM["type"] = "dispense"
            PENDING_CONFIRM["data"] = {"med": med, "reason": f"self-care: {symptom_text}"}
    elif result.kind == pillbot.selfcare.ResultKind.ESCALATION:
        speak(result.product_notice)


def perform_dispense(medicine: str, reason: str = "prn") -> None:
    """Integrated dispense: medicine-based, through the magazine runtime
    (deterministic safety gate -> inventory -> servo -> sensor verification)."""
    active = SESSION.get("active_user") or {}
    user_name = active.get("name", "(unknown)")
    recent = _recent_confirmed_doses(user_name)
    decision = _runtime.dispense(user_name, medicine, recent, request_id=uuid.uuid4().hex)

    # Only a sensor-confirmed dose is logged as taken (hw_ack).
    add_dispense_log(user=user_name, pill=medicine,
                     slot=decision.bay or 0, reason=reason, hw_ack=decision.dispensed)

    if decision.dispensed:
        speak(f"Dispensed {medicine} for {user_name}. "
              f"{decision.remaining} left in bay {decision.bay}.")
    elif decision.blocked_by == "policy":
        speak(f"Cannot dispense: {decision.message}")
    else:
        speak(f"Dispense problem: {decision.message}  Dose NOT counted.")


def handle_confirmed_dispense(data: Dict) -> None:
    med = data.get("med", "")
    reason = data.get("reason", "prn")
    if not med:
        speak("No medicine specified to dispense.")
        return
    perform_dispense(med, reason=reason)


def _handle_magazine_command(text: str) -> bool:
    """Hot-plug / manual magazine commands. Returns True if handled.
      :mags                          - list loaded magazines + counts
      :insert <tag> <bay> <count>    - insert a magazine (sim: <count> pills)
      :assign <tag> <medicine...>    - assign a medicine to a pending magazine
      :remove <bay>                  - remove the magazine in a bay
      :dispense <medicine...>        - dispense now (PRN) for the active user
    """
    parts = text.split()
    cmd = parts[0].lower()

    if cmd == ":mags":
        mags = _runtime.inventory()
        if not mags:
            speak("No magazines loaded.")
        for m in sorted(mags, key=lambda x: (x.bay or 0)):
            speak(f"Bay {m.bay}: {m.medicine or '(unassigned)'} - "
                  f"{m.remaining} pills ({m.state.value}) [tag {m.tag_id}]")
        return True

    if cmd == ":insert":
        if len(parts) < 4:
            speak("Usage: :insert <tag> <bay> <count>")
            return True
        try:
            tag, bay, count = parts[1], int(parts[2]), int(parts[3])
        except ValueError:
            speak("Usage: :insert <tag> <bay> <count>  (bay and count are numbers)")
            return True
        _runtime.sim_load(tag, count)
        speak(_runtime.insert_magazine(tag, bay).prompt)
        return True

    if cmd == ":assign":
        if len(parts) < 3:
            speak("Usage: :assign <tag> <medicine>")
            return True
        tag, medicine = parts[1], " ".join(parts[2:])
        try:
            mag = _runtime.assign(tag, medicine)
            speak(f"Bay {mag.bay}: assigned {mag.medicine}, "
                  f"{mag.remaining} pills ({mag.state.value}).")
            _speak_drug_summary(medicine, _ensure_drug_data(medicine))
        except KeyError:
            speak(f"No inserted magazine with tag {tag}.")
        return True

    if cmd == ":remove":
        if len(parts) < 2 or not parts[1].isdigit():
            speak("Usage: :remove <bay>")
            return True
        mag = _runtime.remove_magazine(int(parts[1]))
        speak(f"Removed {mag.medicine or 'magazine'} from bay {parts[1]}."
              if mag else f"Nothing in bay {parts[1]}.")
        return True

    if cmd == ":dispense":
        if len(parts) < 2:
            speak("Usage: :dispense <medicine>")
            return True
        perform_dispense(" ".join(parts[1:]), reason="prn")
        return True

    if cmd == ":symptom":
        if len(parts) < 2:
            speak("Usage: :symptom <how you feel>  (e.g. :symptom headache)")
            return True
        _handle_symptom(" ".join(parts[1:]))
        return True

    if cmd == ":drug":
        if len(parts) < 2:
            speak("Usage: :drug <medicine>  (shows the FDA label knowledge)")
            return True
        med = " ".join(parts[1:])
        info = _ensure_drug_data(med)
        _speak_drug_summary(med, info)
        if info is not None and getattr(info, "fetch_ok", False):
            if info.do_not_use:
                speak("Do not use: " + info.do_not_use[:220])
            speak("Source: " + (info.source_url or ""))
        return True

    if cmd == ":audit":
        led = getattr(_runtime, "ledger", None)
        if led is None:
            speak("No dispense ledger configured.")
            return True
        ok, bad = led.verify_chain()
        n = len(led.entries())
        speak(f"Dispense ledger: {n} entr{'y' if n == 1 else 'ies'}, "
              f"chain {'INTACT' if ok else f'BROKEN at seq {bad}'}.")
        return True

    return False


# ─────────────────────────────────────────────────────────────
# Scheduler
# ─────────────────────────────────────────────────────────────
def _scheduler_fired(key: str) -> bool:
    with _db() as conn:
        return conn.execute(
            "SELECT 1 FROM scheduler_fired WHERE fire_key=?", (key,)
        ).fetchone() is not None


def _scheduler_mark_fired(key: str) -> None:
    with _db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO scheduler_fired (fire_key) VALUES (?)", (key,)
        )


def _scheduler_prune() -> None:
    cutoff = (clock_now() - datetime.timedelta(days=2)).isoformat()
    with _db() as conn:
        conn.execute("DELETE FROM scheduler_fired WHERE fired_at < ?", (cutoff,))


def _regularity_matches(regularity: str, now: datetime.datetime) -> bool:
    reg = (regularity or "daily").strip().lower()
    if reg == "daily":
        return True
    if reg == "weekdays":
        return now.weekday() < 5
    if reg == "weekends":
        return now.weekday() >= 5
    dow = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"][now.weekday()]
    return dow in [p.strip() for p in reg.split(",")]


def _scheduler_tick() -> None:
    now      = clock_now()
    hhmm_now = now.strftime("%H:%M")
    today    = now.date().isoformat()
    log.debug("Scheduler tick %s", hhmm_now)
    _log_event("scheduler_tick", {"time": hhmm_now})

    try:
        reminders = get_all_reminders()
    except Exception as exc:
        log.error("Scheduler: fetch reminders failed: %s", exc)
        return

    for r in reminders:
        try:
            if not r.get("enable"):
                continue
            if str(r.get("time", "")).strip() != hhmm_now:
                continue
            if not _regularity_matches(str(r.get("regularity", "daily")), now):
                continue

            key = f"{r['reminder_user']}|{r['pill']}|{hhmm_now}|{today}"
            if _scheduler_fired(key):
                continue

            user = str(r["reminder_user"])
            pill = str(r["pill"])

            # Dispense through the magazine runtime: deterministic safety gate
            # (dose limits + inventory) -> servo -> sensor verification.
            recent = _recent_confirmed_doses(user)
            # fire_key as request_id: idempotent even if a tick repeats after a crash
            decision = _runtime.dispense(user, pill, recent, request_id=key)
            _scheduler_mark_fired(key)
            add_dispense_log(user=user, pill=pill, slot=decision.bay or 0,
                             reason="scheduled", hw_ack=decision.dispensed)

            if decision.dispensed:
                speak(f"Scheduled dose dispensed: {pill} for {user} "
                      f"(bay {decision.bay}, {decision.remaining} left).")
            elif decision.blocked_by == "policy":
                speak(f"Scheduled dose blocked for {user}: {decision.message}")
            else:
                speak(f"Scheduled dispense FAILED for {user} ({pill}): {decision.message}")

        except Exception as exc:
            log.error("Scheduler entry error: %s — %s\n%s", r, exc, traceback.format_exc())
            _log_event("scheduler_error", {"reminder": dict(r), "error": str(exc)})

    _scheduler_prune()


def _scheduler_loop() -> None:
    speak("Scheduler started.")
    log.info("Scheduler thread running.")
    while True:
        t0 = time.monotonic()
        try:
            _scheduler_tick()
        except Exception as exc:
            log.critical("Scheduler loop crash: %s\n%s", exc, traceback.format_exc())
        elapsed = time.monotonic() - t0
        time.sleep(max(1.0, 60.0 - elapsed % 60.0))


# ─────────────────────────────────────────────────────────────
# Startup diagnostics
# ─────────────────────────────────────────────────────────────
def run_startup_diagnostics() -> bool:
    """
    Check DB, serial, and AI connectivity before entering the main loop.
    Returns True if fully operational; False if degraded.
    Degraded mode still allows reminder management and AI queries.
    """
    log.info("=== Startup diagnostics ===")
    _log_event("startup_diagnostics", "begin")
    ok = True

    # DB
    try:
        with _db() as conn:
            conn.execute("SELECT 1").fetchone()
        log.info("[DIAG] DB        : OK")
        _log_event("diag_db", "ok")
    except Exception as exc:
        log.critical("[DIAG] DB        : FAIL — %s", exc)
        _log_event("diag_db", f"FAIL: {exc}")
        ok = False   # DB failure is fatal

    # Dispenser backend (real serial or simulator)
    backend_health = _backend.health()
    if backend_health == "ok":
        log.info("[DIAG] Dispenser  : OK (backend=%s)", settings.backend)
        _log_event("diag_dispenser", {"health": "ok", "backend": settings.backend})
    else:
        log.warning("[DIAG] Dispenser  : %s (backend=%s) — dispense will not work.",
                    backend_health.upper(), settings.backend)
        _log_event("diag_dispenser", {"health": backend_health, "backend": settings.backend})

    # AI
    if OPENROUTER_API_KEY:
        try:
            r = requests.post(
                OPENROUTER_URL,
                headers={
                    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": OPENROUTER_MODEL,
                    "messages": [{"role": "user", "content": "ping"}],
                    "max_tokens": 5,
                },
                timeout=10,
            )
            r.raise_for_status()
            log.info("[DIAG] AI         : OK (model=%s)", OPENROUTER_MODEL)
            _log_event("diag_ai", "ok")
        except Exception as exc:
            log.warning("[DIAG] AI         : UNAVAILABLE — %s", exc)
            _log_event("diag_ai", f"unavailable: {exc}")
    else:
        log.warning("[DIAG] AI         : SKIP — no API key set.")
        _log_event("diag_ai", "skipped_no_key")

    status = "READY" if ok else "DEGRADED"
    log.info("=== Diagnostics complete — system %s ===", status)
    _log_event("startup_diagnostics", status.lower())
    return ok


# ─────────────────────────────────────────────────────────────
# Graceful shutdown
# ─────────────────────────────────────────────────────────────
def _cleanup() -> None:
    """Idempotent, QUIET resource cleanup. Safe to call from a signal handler,
    the main loop, or atexit — so it must never raise and never sys.exit().
    Deliberately does no logging: during interpreter shutdown (the atexit path)
    the logging streams may already be closed."""
    if getattr(_cleanup, "_done", False):
        return
    _cleanup._done = True
    try:
        _backend.close()
    except Exception:
        pass
    try:
        _TTS_QUEUE.put_nowait(None)
    except Exception:
        pass


def _shutdown(signum=None, frame=None) -> None:
    """Signal handler / explicit-quit path: announce (streams are alive here),
    clean up, then exit the process."""
    if not getattr(_cleanup, "_done", False):
        try:
            log.info("Shutdown (signal=%s).", signum)
            _log_event("shutdown", {"signal": signum})
            speak("PillBot shutting down. Goodbye.")
        except Exception:
            pass
    _cleanup()
    sys.exit(0)


# SIGINT (Ctrl-C) is available on all platforms.
# SIGTERM exists on POSIX only — guard it so Windows doesn't crash at import.
signal.signal(signal.SIGINT, _shutdown)
if hasattr(signal, "SIGTERM"):
    signal.signal(signal.SIGTERM, _shutdown)

# atexit covers the case where the process ends without a signal
# (e.g. the console window is closed on Windows). It runs cleanup only —
# never sys.exit() — so it cannot raise SystemExit during interpreter teardown.
import atexit
atexit.register(_cleanup)


# ─────────────────────────────────────────────────────────────
# User identification / session login
# ─────────────────────────────────────────────────────────────
def login_prompt() -> None:
    """
    Identify the current user before allowing any commands.
      :new    — guided registration
      :admin  — admin mode via ADMIN_PIN
      <name>  — existing user; prompts for PIN
    """
    speak("Please identify yourself.  Type your name, ':new' to register, or ':admin' for admin mode.")
    while True:
        name_input = input("Name> ").strip()
        _log_event("login_attempt", {"input": name_input})

        if name_input.lower() == ":admin":
            pin = input("Admin PIN> ").strip()
            if pin == ADMIN_PIN:
                SESSION["active_user"] = {"name": "admin", "id": None}
                SESSION["admin"] = True
                speak("Admin mode activated.")
                _log_event("auth_ok", {"name": "admin"})
                return
            speak("Wrong admin PIN.")
            _log_event("auth_fail", {"name": "admin", "reason": "wrong_pin"})
            continue

        if name_input.lower() == ":new":
            speak("Let's register you. What is your full name?")
            new_name = input("Name> ").strip()
            if not new_name:
                speak("Name cannot be empty.")
                continue
            speak(f"Hi {new_name}. Choose a 4-digit PIN:")
            pin = input("PIN> ").strip()
            if not re.fullmatch(r"\d{4}", pin):
                speak("Invalid PIN — must be exactly 4 digits.")
                continue
            speak("Age (press Enter to skip):")
            age = input("Age> ").strip() or None
            speak("Gender (press Enter to skip):")
            gender = input("Gender> ").strip() or None
            speak("Any medical notes (press Enter to skip):")
            notes = input("Notes> ").strip()
            add_user(name=new_name, age=age, gender=gender, notes=notes, pin=pin)
            speak(f"Registered! Welcome, {new_name}.")
            return

        # Existing user
        existing = get_user_by_name(name_input)
        if not existing:
            speak(f"No user named '{name_input}'. Type ':new' to register.")
            continue

        pin = input("PIN> ").strip()
        user = authenticate_user(name_input, pin)
        if user:
            SESSION["active_user"] = user
            SESSION["admin"] = False
            speak(f"Welcome back, {user['name']}.")
            return
        speak("Incorrect PIN. Try again.")


# ─────────────────────────────────────────────────────────────
# Main CLI loop
# ─────────────────────────────────────────────────────────────
def main() -> None:
    init_db()
    # Install the monotonic clock guard (needs the DB) so a backward system-clock
    # jump cannot reset the dose-safety window.
    pillbot.clock.set_clock(pillbot.clock.MonotonicGuard(
        pillbot.clock.SystemClock(), _load_clock_hwm, _save_clock_hwm,
        on_rollback=_on_clock_rollback))
    log.info("PillBot starting.")
    _log_event("startup", {"scheduler": ENABLE_SCHEDULER})

    run_startup_diagnostics()

    mags = _runtime.inventory()
    if mags:
        speak("PillBot ready. Loaded magazines:")
        for m in sorted(mags, key=lambda x: (x.bay or 0)):
            speak(f"  Bay {m.bay}: {m.medicine or '(unassigned)'} — {m.remaining} pills")
    else:
        speak("PillBot ready. No magazines loaded — use ':insert <tag> <bay> <count>' then ':assign'.")

    if ENABLE_SCHEDULER:
        threading.Thread(target=_scheduler_loop, daemon=True, name="Scheduler").start()

    login_prompt()
    speak("Type a command, ':logout' to switch user, or ':quit' to exit.")

    while True:
        try:
            text = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            _shutdown()

        if not text:
            continue

        log.info("USER INPUT [%s]: %s",
                 (SESSION.get("active_user") or {}).get("name", "?"), text)
        _log_event("user_input", {
            "user": (SESSION.get("active_user") or {}).get("name"),
            "text": text,
        })

        if text.lower() in (":q", ":quit", "quit", "exit"):
            _shutdown()

        if text.lower() == ":logout":
            SESSION["active_user"] = None
            SESSION["admin"] = False
            speak("Logged out.")
            login_prompt()
            continue

        # ── Magazine hot-plug / manual dispense commands ──────
        if text.startswith(":") and _handle_magazine_command(text):
            continue

        # ── Pending dispense confirmation ─────────────────────
        with _pending_lock:
            ptype = PENDING_CONFIRM.get("type")

        if ptype == "dispense":
            if text.strip().lower() in ("yes", "y", "ok", "confirm"):
                with _pending_lock:
                    data = PENDING_CONFIRM.pop("data", {})
                    PENDING_CONFIRM["type"] = None
                handle_confirmed_dispense(data)
            else:
                with _pending_lock:
                    PENDING_CONFIRM["type"] = None
                    PENDING_CONFIRM["data"] = {}
                speak("Dispense cancelled.")
            continue

        # ── Normal AI flow ────────────────────────────────────
        active    = SESSION.get("active_user") or {}
        reminders = get_all_reminders()
        logs      = get_logs_last24h(active.get("name", ""))
        ai_resp   = ask_ai(text, active, reminders, logs)
        execute_ai_action(ai_resp)


# ─────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    main()