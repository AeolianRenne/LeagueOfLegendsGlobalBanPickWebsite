"""SQLite persistence layer for the BanPick service."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


def now() -> str:
    """Return an ISO-8601 UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()


class Database:
    """Small transaction-oriented SQLite repository."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def initialize(self) -> None:
        """Create tables and seed application settings."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS admins (
                    username TEXT PRIMARY KEY,
                    password_hash TEXT NOT NULL,
                    salt TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY,
                    username TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS catalogues (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    summary_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS heroes (
                    catalogue_id INTEGER NOT NULL REFERENCES catalogues(id),
                    hero_id TEXT NOT NULL,
                    slug TEXT NOT NULL,
                    name TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    icon_url TEXT NOT NULL DEFAULT '',
                    roles_json TEXT NOT NULL,
                    win_rate REAL,
                    pick_rate REAL,
                    ban_rate REAL,
                    PRIMARY KEY (catalogue_id, hero_id)
                );
                CREATE TABLE IF NOT EXISTS hero_role_overrides (
                    hero_id TEXT PRIMARY KEY,
                    roles_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS series (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL UNIQUE,
                    best_of INTEGER NOT NULL CHECK (best_of IN (1, 3, 5)),
                    global_draft INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    catalogue_id INTEGER NOT NULL REFERENCES catalogues(id),
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    ended_at TEXT,
                    status_before_archive TEXT
                );
                CREATE TABLE IF NOT EXISTS access_links (
                    series_id INTEGER NOT NULL REFERENCES series(id),
                    role TEXT NOT NULL CHECK (role IN ('blue', 'red', 'spectator')),
                    token_hash TEXT NOT NULL UNIQUE,
                    token_value TEXT,
                    PRIMARY KEY (series_id, role)
                );
                CREATE TABLE IF NOT EXISTS games (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    series_id INTEGER NOT NULL REFERENCES series(id),
                    game_number INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    blue_ready INTEGER NOT NULL DEFAULT 0,
                    red_ready INTEGER NOT NULL DEFAULT 0,
                    phase_index INTEGER NOT NULL DEFAULT 0,
                    deadline_at TEXT,
                    timeout_team TEXT CHECK (timeout_team IN ('blue', 'red')),
                    blue_preselect TEXT,
                    red_preselect TEXT,
                    UNIQUE(series_id, game_number)
                );
                CREATE TABLE IF NOT EXISTS draft_actions (
                    game_id INTEGER NOT NULL REFERENCES games(id),
                    phase_index INTEGER NOT NULL,
                    action_kind TEXT NOT NULL CHECK (action_kind IN ('ban', 'pick')),
                    team TEXT NOT NULL CHECK (team IN ('blue', 'red')),
                    hero_id TEXT,
                    PRIMARY KEY (game_id, phase_index)
                );
                CREATE INDEX IF NOT EXISTS idx_heroes_catalogue ON heroes(catalogue_id);
                CREATE INDEX IF NOT EXISTS idx_games_series ON games(series_id, game_number);
                """
            )
            game_columns = {row["name"] for row in connection.execute("PRAGMA table_info(games)")}
            if "timeout_team" not in game_columns:
                connection.execute("ALTER TABLE games ADD COLUMN timeout_team TEXT CHECK (timeout_team IN ('blue', 'red'))")
            series_columns = {row["name"] for row in connection.execute("PRAGMA table_info(series)")}
            if "status_before_archive" not in series_columns:
                connection.execute("ALTER TABLE series ADD COLUMN status_before_archive TEXT")
            access_link_columns = {row["name"] for row in connection.execute("PRAGMA table_info(access_links)")}
            if "token_value" not in access_link_columns:
                connection.execute("ALTER TABLE access_links ADD COLUMN token_value TEXT")

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """Yield a transaction-safe connection."""
        connection = sqlite3.connect(self.path, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def get_setting(self, key: str, default: Any = None) -> Any:
        """Read one JSON setting."""
        with self.connection() as connection:
            row = connection.execute("SELECT value_json FROM settings WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value_json"]) if row else default

    def set_setting(self, key: str, value: Any) -> None:
        """Store one JSON setting."""
        with self.connection() as connection:
            connection.execute(
                """INSERT INTO settings(key, value_json, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, updated_at = excluded.updated_at""",
                (key, json.dumps(value), now()),
            )
