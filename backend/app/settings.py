"""Runtime configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _integer(name: str, default: int, minimum: int = 0) -> int:
    """Read a bounded integer environment variable."""
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return value if value >= minimum else default


@dataclass(frozen=True)
class Settings:
    """Application settings sourced from the process environment."""

    data_dir: Path
    initial_password: str
    bot_api_key: str
    public_base_url: str
    max_active_matches: int
    ban_seconds: int
    pick_seconds: int
    refresh_interval_seconds: int
    cookie_secure: bool

    @property
    def database_path(self) -> Path:
        """Return the SQLite persistence path."""
        return self.data_dir / "banpick.sqlite3"


def load_settings() -> Settings:
    """Load settings with safe local-development defaults."""
    data_dir = Path(os.getenv("BANPICK_DATA_DIR", "./data"))
    return Settings(
        data_dir=data_dir,
        initial_password=os.getenv("BANPICK_ADMIN_INITIAL_PASSWORD", "change-me"),
        bot_api_key=os.getenv("BANPICK_BOT_API_KEY", "change-me"),
        public_base_url=os.getenv("BANPICK_PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/"),
        max_active_matches=_integer("BANPICK_MAX_ACTIVE_MATCHES", 1, 1),
        ban_seconds=_integer("BANPICK_BAN_SECONDS", 30, 1),
        pick_seconds=_integer("BANPICK_PICK_SECONDS", 30, 1),
        refresh_interval_seconds=_integer("BANPICK_CHAMPION_REFRESH_INTERVAL_SECONDS", 0),
        cookie_secure=os.getenv("BANPICK_COOKIE_SECURE", "false").casefold() == "true",
    )
