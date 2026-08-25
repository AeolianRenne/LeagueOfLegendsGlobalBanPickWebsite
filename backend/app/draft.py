"""Server-authoritative draft state machine."""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from .auth import token_hash
from .database import Database, now
from .settings import Settings


PHASES: tuple[tuple[str, str], ...] = (
    ("ban", "blue"), ("ban", "red"), ("ban", "blue"), ("ban", "red"), ("ban", "blue"), ("ban", "red"),
    ("pick", "blue"), ("pick", "red"), ("pick", "red"), ("pick", "blue"), ("pick", "blue"), ("pick", "red"),
    ("ban", "blue"), ("ban", "red"), ("ban", "blue"), ("ban", "red"),
    ("pick", "red"), ("pick", "blue"), ("pick", "blue"), ("pick", "red"),
)
ACTIVE_SERIES_STATUSES = ("waiting_ready", "drafting", "paused", "awaiting_next")


class DraftError(ValueError):
    """A user-visible draft workflow violation."""


class DraftService:
    """Persist and validate series, games, and sequential selections."""

    def __init__(self, database: Database, settings: Settings) -> None:
        self.database = database
        self.settings = settings

    def create_series(self, best_of: int, global_draft: bool, created_by: str) -> dict[str, Any]:
        """Create a series and its reusable capability URLs."""
        if best_of not in {1, 3, 5}:
            raise DraftError("赛制只能是 BO1、BO3 或 BO5。")
        with self.database.connection() as connection:
            active = connection.execute(
                f"SELECT COUNT(*) AS count FROM series WHERE status IN ({','.join('?' for _ in ACTIVE_SERIES_STATUSES)})",
                ACTIVE_SERIES_STATUSES,
            ).fetchone()["count"]
            setting = connection.execute("SELECT value_json FROM settings WHERE key = 'max_active_matches'").fetchone()
            limit = __import__("json").loads(setting["value_json"]) if setting else self.settings.max_active_matches
            if active >= int(limit):
                raise DraftError("当前活跃赛事已达到实例上限。")
            catalogue = connection.execute("SELECT id FROM catalogues ORDER BY id DESC LIMIT 1").fetchone()
            if not catalogue:
                raise DraftError("尚无英雄资料，请先在管理后台同步英雄。")
            code = secrets.token_urlsafe(6).replace("-", "").replace("_", "").upper()[:8]
            while connection.execute("SELECT 1 FROM series WHERE code = ?", (code,)).fetchone():
                code = secrets.token_urlsafe(6).replace("-", "").replace("_", "").upper()[:8]
            series_id = connection.execute(
                """INSERT INTO series(code, best_of, global_draft, status, catalogue_id, created_by, created_at)
                VALUES (?, ?, ?, 'waiting_ready', ?, ?, ?)""",
                (code, best_of, int(global_draft), catalogue["id"], created_by, now()),
            ).lastrowid
            connection.execute(
                "INSERT INTO games(series_id, game_number, status) VALUES (?, 1, 'waiting_ready')",
                (series_id,),
            )
            tokens: dict[str, str] = {}
            for role in ("blue", "red", "spectator"):
                token = secrets.token_urlsafe(32)
                tokens[role] = token
                connection.execute(
                    "INSERT INTO access_links(series_id, role, token_hash, token_value) VALUES (?, ?, ?, ?)",
                    (series_id, role, token_hash(token), token),
                )
        return self._links(code, tokens) | {"code": code, "best_of": best_of, "global_draft": global_draft}

    def _links(self, code: str, tokens: dict[str, str]) -> dict[str, str]:
        return {role: f"{self.settings.public_base_url}/room/{code}/{token}" for role, token in tokens.items()}

    def role_for_token(self, code: str, token: str) -> str | None:
        """Resolve a room capability token to its role."""
        with self.database.connection() as connection:
            row = connection.execute(
                """SELECT access_links.role FROM access_links JOIN series ON series.id = access_links.series_id
                WHERE series.code = ? AND access_links.token_hash = ?""",
                (code, token_hash(token)),
            ).fetchone()
        return row["role"] if row else None

    def set_ready(self, code: str, role: str) -> dict[str, Any]:
        """Mark one captain ready and begin once both teams are ready."""
        if role not in {"blue", "red"}:
            raise DraftError("观战链接不能确认准备。")
        with self.database.connection() as connection:
            series, game = self._current(connection, code)
            if series["status"] == "ended":
                raise DraftError("赛事已归档，恢复后才能操作。")
            if game["status"] != "waiting_ready":
                raise DraftError("当前对局不在准备阶段。")
            connection.execute(f"UPDATE games SET {role}_ready = 1 WHERE id = ?", (game["id"],))
            game = connection.execute("SELECT * FROM games WHERE id = ?", (game["id"],)).fetchone()
            if game["blue_ready"] and game["red_ready"]:
                self._start_phase(connection, game["id"], 0)
                connection.execute("UPDATE series SET status = 'drafting' WHERE id = ?", (series["id"],))
        return self.state(code)

    def preselect(self, code: str, role: str, hero_id: str) -> dict[str, Any]:
        """Store the active team's provisional champion selection."""
        with self.database.connection() as connection:
            series, game = self._current(connection, code)
            if series["status"] == "ended":
                raise DraftError("赛事已归档，恢复后才能操作。")
            self._advance_expired(connection, series, game)
            game = connection.execute("SELECT * FROM games WHERE id = ?", (game["id"],)).fetchone()
            if role not in {"blue", "red"} or game["status"] not in {"drafting", "paused"}:
                raise DraftError("当前不能预选英雄。")
            kind, team = PHASES[game["phase_index"]]
            if team != role:
                raise DraftError("现在不是本方操作回合。")
            self._validate_hero(connection, series, game, hero_id)
            connection.execute(f"UPDATE games SET {role}_preselect = ? WHERE id = ?", (hero_id, game["id"]))
        return self.state(code)

    def act(self, code: str, role: str, hero_id: str | None) -> dict[str, Any]:
        """Confirm a ban, pick, or empty ban for the current phase."""
        with self.database.connection() as connection:
            series, game = self._current(connection, code)
            if series["status"] == "ended":
                raise DraftError("赛事已归档，恢复后才能操作。")
            self._advance_expired(connection, series, game)
            game = connection.execute("SELECT * FROM games WHERE id = ?", (game["id"],)).fetchone()
            if role not in {"blue", "red"}:
                raise DraftError("观战链接不能操作 BP。")
            if game["status"] not in {"drafting", "paused"}:
                raise DraftError("当前对局不在 BP 阶段。")
            kind, team = PHASES[game["phase_index"]]
            if team != role:
                raise DraftError("现在不是本方操作回合。")
            if kind == "pick" and not hero_id:
                raise DraftError("选择阶段必须确认英雄。")
            if hero_id:
                self._validate_hero(connection, series, game, hero_id)
            connection.execute(
                "INSERT INTO draft_actions(game_id, phase_index, action_kind, team, hero_id) VALUES (?, ?, ?, ?, ?)",
                (game["id"], game["phase_index"], kind, team, hero_id),
            )
            connection.execute("UPDATE games SET blue_preselect = NULL, red_preselect = NULL, status = 'drafting' WHERE id = ?", (game["id"],))
            self._next_phase(connection, series, game)
        return self.state(code)

    def advance_game(self, code: str) -> dict[str, Any]:
        """Create the next game after a completed game."""
        with self.database.connection() as connection:
            series = connection.execute("SELECT * FROM series WHERE code = ?", (code,)).fetchone()
            if not series:
                raise DraftError("赛事不存在。")
            if series["status"] != "awaiting_next":
                raise DraftError("当前赛事不能开始下一局。")
            previous = connection.execute("SELECT MAX(game_number) AS number FROM games WHERE series_id = ?", (series["id"],)).fetchone()["number"]
            if previous >= series["best_of"]:
                raise DraftError("已达到系列赛局数上限。")
            connection.execute("INSERT INTO games(series_id, game_number, status) VALUES (?, ?, 'waiting_ready')", (series["id"], previous + 1))
            connection.execute("UPDATE series SET status = 'waiting_ready' WHERE id = ?", (series["id"],))
        return self.state(code)

    def end_series(self, code: str) -> dict[str, Any]:
        """Archive a series while retaining enough state to resume it later."""
        with self.database.connection() as connection:
            series = connection.execute("SELECT * FROM series WHERE code = ?", (code,)).fetchone()
            if not series:
                raise DraftError("赛事不存在。")
            if series["status"] == "ended":
                raise DraftError("赛事已经归档。")
            connection.execute(
                "UPDATE series SET status = 'ended', ended_at = ?, status_before_archive = ? WHERE id = ?",
                (now(), series["status"], series["id"]),
            )
            # Do not leave a stale countdown visible through an archived room.
            # `restore_series` assigns a fresh phase deadline when appropriate.
            connection.execute(
                "UPDATE games SET deadline_at = NULL WHERE series_id = ? AND game_number = (SELECT MAX(game_number) FROM games WHERE series_id = ?)",
                (series["id"], series["id"]),
            )
        return self.state(code)

    def restore_series(self, code: str) -> dict[str, Any]:
        """Restore a deliberately archived series at its prior draft progress."""
        with self.database.connection() as connection:
            series, game = self._current(connection, code)
            if series["status"] != "ended":
                raise DraftError("当前赛事未归档。")
            status = series["status_before_archive"]
            if not status:
                raise DraftError("旧赛事未保存可恢复进度，请重新创建赛事。")
            connection.execute("UPDATE series SET status = ?, ended_at = NULL, status_before_archive = NULL WHERE id = ?", (status, series["id"]))
            if status == "drafting" and game["status"] == "drafting":
                self._start_phase(connection, game["id"], game["phase_index"])
        return self.state(code)

    def management_series(self) -> list[dict[str, Any]]:
        """List recent series and their stored administrator capability URLs."""
        with self.database.connection() as connection:
            rows = connection.execute("SELECT id, code, best_of, global_draft, status, created_at FROM series ORDER BY id DESC LIMIT 50").fetchall()
            result: list[dict[str, Any]] = []
            for row in rows:
                link_rows = connection.execute("SELECT role, token_value FROM access_links WHERE series_id = ?", (row["id"],)).fetchall()
                tokens = {link["role"]: link["token_value"] for link in link_rows}
                links = self._links(row["code"], tokens) if all(tokens.get(role) for role in ("blue", "red", "spectator")) else None
                result.append({**dict(row), "global_draft": bool(row["global_draft"]), "links": links, "links_reissuable": links is None})
        return result

    def reissue_links(self, code: str) -> dict[str, str]:
        """Explicitly replace unrecoverable legacy capability links with new ones."""
        with self.database.connection() as connection:
            series = connection.execute("SELECT id FROM series WHERE code = ?", (code,)).fetchone()
            if not series:
                raise DraftError("赛事不存在。")
            tokens = {role: secrets.token_urlsafe(32) for role in ("blue", "red", "spectator")}
            for role, token in tokens.items():
                connection.execute("UPDATE access_links SET token_hash = ?, token_value = ? WHERE series_id = ? AND role = ?", (token_hash(token), token, series["id"], role))
        return self._links(code, tokens)

    def state(self, code: str) -> dict[str, Any]:
        """Return a serializable, current draft snapshot."""
        with self.database.connection() as connection:
            series, game = self._current(connection, code)
            # An archive is a hard freeze: opening an old capability link must
            # never consume a deadline or mutate the stored draft progress.
            if series["status"] != "ended":
                self._advance_expired(connection, series, game)
            series, game = self._current(connection, code)
            actions = connection.execute("SELECT * FROM draft_actions WHERE game_id = ? ORDER BY phase_index", (game["id"],)).fetchall()
            heroes = connection.execute("SELECT * FROM heroes WHERE catalogue_id = ? ORDER BY name", (series["catalogue_id"],)).fetchall()
            previous_picks = self._previous_pick_ids(connection, series, game)
        action_data = [dict(row) for row in actions]
        used = {row["hero_id"] for row in actions if row["hero_id"]}
        current = None
        if series["status"] != "ended" and game["status"] in {"drafting", "paused"} and game["phase_index"] < len(PHASES):
            kind, team = PHASES[game["phase_index"]]
            current = {"kind": kind, "team": team, "phase_index": game["phase_index"]}
        return {
            "series": {"code": series["code"], "best_of": series["best_of"], "global_draft": bool(series["global_draft"]), "status": series["status"]},
            "game": {"number": game["game_number"], "status": game["status"], "blue_ready": bool(game["blue_ready"]), "red_ready": bool(game["red_ready"]), "deadline_at": game["deadline_at"], "timeout_team": game["timeout_team"], "blue_preselect": game["blue_preselect"], "red_preselect": game["red_preselect"]},
            "current": current,
            "actions": action_data,
            "heroes": [{**dict(row), "roles": __import__("json").loads(row["roles_json"])} for row in heroes],
            "used_hero_ids": sorted(used),
            "global_used_hero_ids": sorted(previous_picks),
        }

    def _current(self, connection: Any, code: str) -> tuple[Any, Any]:
        series = connection.execute("SELECT * FROM series WHERE code = ?", (code,)).fetchone()
        if not series:
            raise DraftError("赛事不存在。")
        game = connection.execute("SELECT * FROM games WHERE series_id = ? ORDER BY game_number DESC LIMIT 1", (series["id"],)).fetchone()
        if not game:
            raise DraftError("赛事没有可用对局。")
        return series, game

    def _start_phase(self, connection: Any, game_id: int, phase_index: int) -> None:
        kind, _ = PHASES[phase_index]
        seconds = self.settings.ban_seconds if kind == "ban" else self.settings.pick_seconds
        deadline = (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()
        connection.execute("UPDATE games SET status = 'drafting', phase_index = ?, deadline_at = ?, timeout_team = NULL WHERE id = ?", (phase_index, deadline, game_id))

    def _next_phase(self, connection: Any, series: Any, game: Any) -> None:
        next_index = game["phase_index"] + 1
        if next_index < len(PHASES):
            self._start_phase(connection, game["id"], next_index)
            return
        connection.execute("UPDATE games SET status = 'complete', deadline_at = NULL WHERE id = ?", (game["id"],))
        status = "complete" if game["game_number"] >= series["best_of"] else "awaiting_next"
        connection.execute("UPDATE series SET status = ? WHERE id = ?", (status, series["id"]))

    def _advance_expired(self, connection: Any, series: Any, game: Any) -> None:
        if game["status"] != "drafting" or not game["deadline_at"]:
            return
        if datetime.fromisoformat(game["deadline_at"]) > datetime.now(timezone.utc):
            return
        kind, team = PHASES[game["phase_index"]]
        selected = game[f"{team}_preselect"]
        if kind == "pick" and not selected:
            connection.execute("UPDATE games SET status = 'paused', deadline_at = NULL, timeout_team = ? WHERE id = ?", (team, game["id"]))
            return
        connection.execute(
            "INSERT INTO draft_actions(game_id, phase_index, action_kind, team, hero_id) VALUES (?, ?, ?, ?, ?)",
            (game["id"], game["phase_index"], kind, team, selected if kind == "pick" else None),
        )
        connection.execute("UPDATE games SET blue_preselect = NULL, red_preselect = NULL WHERE id = ?", (game["id"],))
        self._next_phase(connection, series, game)

    def _validate_hero(self, connection: Any, series: Any, game: Any, hero_id: str) -> None:
        exists = connection.execute("SELECT 1 FROM heroes WHERE catalogue_id = ? AND hero_id = ?", (series["catalogue_id"], hero_id)).fetchone()
        if not exists:
            raise DraftError("该英雄不在本场锁定的英雄资料中。")
        used = connection.execute("SELECT 1 FROM draft_actions WHERE game_id = ? AND hero_id = ?", (game["id"], hero_id)).fetchone()
        if used:
            raise DraftError("该英雄已在本局被禁用或选择。")
        if series["global_draft"] and hero_id in self._previous_pick_ids(connection, series, game):
            raise DraftError("该英雄已在本系列赛前局被选择。")

    def _previous_pick_ids(self, connection: Any, series: Any, game: Any) -> set[str]:
        if not series["global_draft"]:
            return set()
        rows = connection.execute(
            """SELECT draft_actions.hero_id FROM draft_actions JOIN games ON games.id = draft_actions.game_id
            WHERE games.series_id = ? AND games.game_number < ? AND draft_actions.action_kind = 'pick' AND draft_actions.hero_id IS NOT NULL""",
            (series["id"], game["game_number"]),
        ).fetchall()
        return {row["hero_id"] for row in rows}
