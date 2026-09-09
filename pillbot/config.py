"""
pillbot.config.py - one typed, validated configuration object.

Replaces ~a dozen scattered os.getenv() calls and, critically, removes the
hardcoded OpenRouter API key that used to live in source. Secrets now come
only from the environment (optionally via a local .env file). Nothing here
ever prints a secret - use redacted() for logging.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from typing import Dict, List


def load_dotenv(path: str = ".env") -> None:
    """Minimal .env loader (no third-party dependency). A real environment
    variable always wins over the file, so .env is dev convenience only."""
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val
    except OSError:
        pass


def _default_port() -> str:
    return "COM3" if sys.platform == "win32" else "/dev/ttyUSB0"


@dataclass
class Settings:
    openrouter_api_key: str = ""
    openrouter_model: str = "nvidia/nemotron-3-super-120b-a12b:free"
    openrouter_url: str = "https://openrouter.ai/api/v1/chat/completions"
    stein_base: str = ""
    backend: str = "auto"            # "serial" | "sim" | "auto"
    serial_port: str = field(default_factory=_default_port)
    serial_baud: int = 9600
    serial_timeout: float = 3.0
    dispense_retries: int = 3
    sim_slots: int = 3
    db_path: str = "pillbot.db"
    log_path: str = "pillbot.log"
    enable_scheduler: bool = True
    admin_pin: str = ""
    request_timeout: int = 20

    @classmethod
    def from_env(cls, dotenv: bool = True) -> "Settings":
        if dotenv:
            load_dotenv()
        g = os.getenv

        def _int(name: str, default: int) -> int:
            try:
                return int(g(name, str(default)))
            except (TypeError, ValueError):
                return default

        def _float(name: str, default: float) -> float:
            try:
                return float(g(name, str(default)))
            except (TypeError, ValueError):
                return default

        return cls(
            openrouter_api_key=g("OPENROUTER_API_KEY", ""),
            openrouter_model=g("OPENROUTER_MODEL", cls.openrouter_model),
            openrouter_url=g("OPENROUTER_URL", cls.openrouter_url),
            stein_base=g("STEIN_BASE_URL", ""),
            backend=g("PILLBOT_BACKEND", "auto").strip().lower(),
            serial_port=g("SERIAL_PORT", _default_port()),
            serial_baud=_int("SERIAL_BAUD", 9600),
            serial_timeout=_float("SERIAL_TIMEOUT", 3.0),
            dispense_retries=_int("DISPENSE_RETRIES", 3),
            sim_slots=_int("PILLBOT_SIM_SLOTS", 3),
            db_path=g("PILLBOT_DB", "pillbot.db"),
            log_path=g("PILLBOT_LOG", "pillbot.log"),
            enable_scheduler=g("PILLBOT_SCHEDULER", "1") == "1",
            admin_pin=g("ADMIN_PIN", ""),
            request_timeout=_int("PILLBOT_REQUEST_TIMEOUT", 20),
        )

    def validate(self) -> List[str]:
        """Return human-readable warnings. Never raises, never prints a secret."""
        warns: List[str] = []
        if not self.openrouter_api_key:
            warns.append("OPENROUTER_API_KEY not set - AI features will be unavailable.")
        elif not self.openrouter_api_key.startswith("sk-"):
            warns.append("OPENROUTER_API_KEY does not look like a valid key.")
        if self.backend not in ("serial", "sim", "auto"):
            warns.append(f"PILLBOT_BACKEND='{self.backend}' invalid; expected serial|sim|auto.")
        if not self.admin_pin:
            warns.append("ADMIN_PIN not set - admin mode is disabled.")
        elif self.admin_pin == "0000":
            warns.append("ADMIN_PIN is the insecure default '0000' - change it.")
        return warns

    def redacted(self) -> Dict[str, object]:
        """Config dict safe for logging - secrets are masked."""
        d: Dict[str, object] = {k: getattr(self, k) for k in self.__dataclass_fields__}
        for secret in ("openrouter_api_key", "admin_pin"):
            v = d.get(secret)
            d[secret] = (str(v)[:4] + "...redacted") if v else ""
        return d
