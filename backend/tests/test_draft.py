"""Draft state-machine regression tests."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.database import Database, now
from app.draft import PHASES, DraftError, DraftService
from app.settings import Settings


def make_service(tmp_path: Path) -> DraftService:
    """Create a service with one catalogue of deterministic test heroes."""
    settings = Settings(tmp_path, "password", "key", "http://test", 2, 30, 30, 0, False)
    database = Database(settings.database_path)
    database.initialize()
    database.set_setting("max_active_matches", 2)
    with database.connection() as connection:
        catalogue_id = connection.execute("INSERT INTO catalogues(source, created_at, summary_json) VALUES ('test', ?, '{}')", (now(),)).lastrowid
        for index in range(30):
            connection.execute(
                """INSERT INTO heroes(catalogue_id, hero_id, slug, name, title, icon_url, roles_json)
                VALUES (?, ?, ?, ?, '', '', ?)""",
                (catalogue_id, f"hero-{index}", f"hero-{index}", f"Hero {index}", json.dumps(["TOP"])),
            )
    return DraftService(database, settings)


def test_standard_phase_order_and_global_pick_lock(tmp_path: Path) -> None:
    """Complete a game and reject a previous-game pick in global mode."""
    service = make_service(tmp_path)
    created = service.create_series(3, True, "test")
    code = created["code"]
    service.set_ready(code, "blue")
    service.set_ready(code, "red")
    pick_number = 0
    for kind, team in PHASES:
        if kind == "ban":
            service.act(code, team, None)
        else:
            hero_id = f"hero-{pick_number}"
            service.preselect(code, team, hero_id)
            service.act(code, team, hero_id)
            pick_number += 1
    state = service.state(code)
    assert state["series"]["status"] == "awaiting_next"
    assert len([item for item in state["actions"] if item["action_kind"] == "pick"]) == 10
    service.advance_game(code)
    service.set_ready(code, "blue")
    service.set_ready(code, "red")
    with pytest.raises(DraftError, match="前局"):
        service.preselect(code, "blue", "hero-0")


def test_ban_can_be_empty_but_pick_cannot(tmp_path: Path) -> None:
    """Keep standard draft semantics for empty confirmations."""
    service = make_service(tmp_path)
    code = service.create_series(1, False, "test")["code"]
    service.set_ready(code, "blue")
    service.set_ready(code, "red")
    service.act(code, "blue", None)
    with pytest.raises(DraftError, match="不是本方"):
        service.act(code, "blue", None)
    service.act(code, "red", None)
    service.act(code, "blue", None)
    service.act(code, "red", None)
    service.act(code, "blue", None)
    service.act(code, "red", None)
    with pytest.raises(DraftError, match="必须确认"):
        service.act(code, "blue", None)


def test_pick_timeout_records_the_timed_out_team(tmp_path: Path) -> None:
    """A pick without a preselection pauses and identifies the responsible side."""
    service = make_service(tmp_path)
    code = service.create_series(1, False, "test")["code"]
    service.set_ready(code, "blue")
    service.set_ready(code, "red")
    with service.database.connection() as connection:
        connection.execute(
            "UPDATE games SET phase_index = 6, status = 'drafting', deadline_at = ?",
            ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),),
        )

    state = service.state(code)

    assert state["game"]["status"] == "paused"
    assert state["game"]["timeout_team"] == "blue"
