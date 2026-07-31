"""Load configuration from environment / .env file.

All secrets live in .env (gitignored). This module loads, validates,
and exposes a Config object. It NEVER logs secret values.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"


@dataclass(frozen=True)
class Config:
    mailon_id: str
    mailon_pw: str
    totp_secret: str
    login_url: str
    headless: bool
    max_mails_per_run: int

    # Derived paths
    data_dir: Path
    mails_dir: Path
    attachments_dir: Path
    logs_dir: Path
    state_db_path: Path

    # Browser session name (isolated browser profile across runs)
    session_name: str = "mailon-sync"


def _require(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        raise RuntimeError(
            f"Missing required env var {name}. "
            f"Copy .env.example to .env and fill in values."
        )
    return val


def _as_bool(val: str | None, default: bool = False) -> bool:
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on", "y")


def load_totp_secret() -> str:
    """Load just the TOTP secret. Useful for the `totp` CLI command
    which doesn't need full login credentials."""
    if ENV_PATH.exists():
        load_dotenv(ENV_PATH, override=False)
    return _require("MAILON_TOTP_SECRET")


def load_config() -> Config:
    # Load .env if present (does not override real env)
    if ENV_PATH.exists():
        load_dotenv(ENV_PATH, override=False)

    data_dir = PROJECT_ROOT / "data"
    logs_dir = PROJECT_ROOT / "logs"
    mails_dir = data_dir / "mails"
    attachments_dir = data_dir / "attachments"

    for d in (data_dir, logs_dir, mails_dir, attachments_dir):
        d.mkdir(parents=True, exist_ok=True)

    return Config(
        mailon_id=_require("MAILON_ID"),
        mailon_pw=_require("MAILON_PW"),
        totp_secret=_require("MAILON_TOTP_SECRET"),
        login_url=os.environ.get("MAILON_LOGIN_URL", "https://mailon.kr/").strip(),
        headless=_as_bool(os.environ.get("HEADLESS"), default=True),
        max_mails_per_run=int(os.environ.get("MAX_MAILS_PER_RUN", "0") or "0"),
        data_dir=data_dir,
        mails_dir=mails_dir,
        attachments_dir=attachments_dir,
        logs_dir=logs_dir,
        state_db_path=data_dir / "state.db",
    )
