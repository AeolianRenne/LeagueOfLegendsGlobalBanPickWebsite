"""Administrator and capability-token authentication helpers."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

from .database import Database, now


def token_hash(value: str) -> str:
    """Hash a bearer token before persistence."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def password_hash(password: str, salt: str) -> str:
    """Derive a password verifier using PBKDF2."""
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 600_000).hex()


def seed_admin(database: Database, password: str) -> None:
    """Create the initial administrator only if none exists."""
    with database.connection() as connection:
        exists = connection.execute("SELECT 1 FROM admins LIMIT 1").fetchone()
        if exists:
            return
        salt = secrets.token_hex(16)
        connection.execute(
            "INSERT INTO admins(username, password_hash, salt, created_at) VALUES (?, ?, ?, ?)",
            ("admin", password_hash(password, salt), salt, now()),
        )


def authenticate_admin(database: Database, password: str) -> bool:
    """Validate the single seeded administrator password."""
    with database.connection() as connection:
        row = connection.execute("SELECT password_hash, salt FROM admins WHERE username = 'admin'").fetchone()
    return bool(row) and hmac.compare_digest(row["password_hash"], password_hash(password, row["salt"]))


def create_session(database: Database) -> str:
    """Create a 12-hour administrator session."""
    token = secrets.token_urlsafe(32)
    expiry = (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat()
    with database.connection() as connection:
        connection.execute(
            "INSERT INTO sessions(token_hash, username, expires_at, created_at) VALUES (?, 'admin', ?, ?)",
            (token_hash(token), expiry, now()),
        )
    return token


def session_valid(database: Database, token: str | None) -> bool:
    """Return whether a session cookie grants current administrator access."""
    if not token:
        return False
    with database.connection() as connection:
        row = connection.execute("SELECT expires_at FROM sessions WHERE token_hash = ?", (token_hash(token),)).fetchone()
    return bool(row) and datetime.fromisoformat(row["expires_at"]) > datetime.now(timezone.utc)


def delete_session(database: Database, token: str | None) -> None:
    """Delete a session when a user logs out."""
    if token:
        with database.connection() as connection:
            connection.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash(token),))
