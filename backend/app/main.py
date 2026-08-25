"""FastAPI entry point for the standalone Global BanPick application."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .auth import authenticate_admin, create_session, delete_session, seed_admin, session_valid
from .database import Database
from .draft import DraftError, DraftService
from .hero_sync import HeroSynchronizer
from .settings import Settings, load_settings


class LoginRequest(BaseModel):
    password: str = Field(min_length=1, max_length=512)


class CreateSeriesRequest(BaseModel):
    best_of: int = 1
    global_draft: bool = True


class ExtendSeriesRequest(BaseModel):
    best_of: int = 1


class HeroRequest(BaseModel):
    hero_id: str = Field(min_length=1, max_length=128)


class HeroRolesRequest(BaseModel):
    roles: list[str] = Field(min_length=1, max_length=5)


class SettingsRequest(BaseModel):
    refresh_interval_seconds: int = Field(ge=0, le=2_592_000)
    max_active_matches: int = Field(ge=1, le=100)


class Connections:
    """Keep live room sockets grouped by series code."""

    def __init__(self) -> None:
        self.rooms: dict[str, set[WebSocket]] = {}

    async def connect(self, code: str, socket: WebSocket) -> None:
        await socket.accept()
        self.rooms.setdefault(code, set()).add(socket)

    def remove(self, code: str, socket: WebSocket) -> None:
        self.rooms.get(code, set()).discard(socket)

    async def broadcast(self, code: str, state: dict[str, Any]) -> None:
        dead: list[WebSocket] = []
        for socket in self.rooms.get(code, set()).copy():
            try:
                await socket.send_json(state)
            except Exception:
                dead.append(socket)
        for socket in dead:
            self.remove(code, socket)


settings = load_settings()
settings.data_dir.mkdir(parents=True, exist_ok=True)
database = Database(settings.database_path)
drafts = DraftService(database, settings)
synchronizer = HeroSynchronizer(database, settings.data_dir / "hero-images")
connections = Connections()


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Initialize persistence and keep countdowns advancing without clients."""
    database.initialize()
    seed_admin(database, settings.initial_password)
    if database.get_setting("refresh_interval_seconds") is None:
        database.set_setting("refresh_interval_seconds", settings.refresh_interval_seconds)
    if database.get_setting("max_active_matches") is None:
        database.set_setting("max_active_matches", settings.max_active_matches)
    task = asyncio.create_task(_tick())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="Global BanPick", version="0.1.0", lifespan=lifespan)
static_root = Path(__file__).resolve().parents[2] / "frontend-dist"
if static_root.exists():
    app.mount("/assets", StaticFiles(directory=static_root / "assets"), name="assets")
image_root = settings.data_dir / "hero-images"
image_root.mkdir(parents=True, exist_ok=True)
app.mount("/hero-images", StaticFiles(directory=image_root), name="heroes")


async def _tick() -> None:
    """Advance expired phases, push room changes, and run configured refreshes."""
    elapsed = 0
    while True:
        await asyncio.sleep(1)
        elapsed += 1
        with database.connection() as connection:
            rows = connection.execute("SELECT code FROM series WHERE status IN ('waiting_ready', 'drafting', 'paused', 'awaiting_next')").fetchall()
        for row in rows:
            try:
                await connections.broadcast(row["code"], drafts.state(row["code"]))
            except DraftError:
                continue
        interval = int(database.get_setting("refresh_interval_seconds", 0))
        if interval and elapsed >= interval:
            elapsed = 0
            await synchronizer.refresh()


def require_admin(banpick_session: str | None = Cookie(default=None)) -> None:
    """Reject management APIs without a valid session cookie."""
    if not session_valid(database, banpick_session):
        raise HTTPException(status_code=401, detail="管理员登录已失效。")


def require_bot(x_banpick_api_key: str | None = Header(default=None)) -> None:
    """Reject optional bot APIs without their shared secret."""
    if not x_banpick_api_key or x_banpick_api_key != settings.bot_api_key:
        raise HTTPException(status_code=401, detail="机器人 API 密钥无效。")


def room_role(code: str, token: str) -> str:
    """Resolve and validate a room capability role."""
    role = drafts.role_for_token(code, token)
    if not role:
        raise HTTPException(status_code=404, detail="赛事链接无效。")
    return role


async def publish(code: str) -> dict[str, Any]:
    """Build and broadcast a new authoritative room state."""
    state = drafts.state(code)
    await connections.broadcast(code, state)
    return state


@app.get("/health")
async def health() -> dict[str, str]:
    """Return a lightweight container health response."""
    return {"status": "ok"}


@app.post("/api/admin/login")
async def login(request: LoginRequest, response: Response) -> dict[str, bool]:
    """Create an administrator browser session."""
    if not authenticate_admin(database, request.password):
        raise HTTPException(status_code=401, detail="密码错误。")
    token = create_session(database)
    response.set_cookie("banpick_session", token, max_age=43_200, httponly=True, samesite="lax", secure=settings.cookie_secure)
    return {"ok": True}


@app.post("/api/admin/logout")
async def logout(response: Response, banpick_session: str | None = Cookie(default=None)) -> dict[str, bool]:
    """Invalidate the current browser session."""
    delete_session(database, banpick_session)
    response.delete_cookie("banpick_session")
    return {"ok": True}


@app.get("/api/admin/me")
async def me(_: None = Depends(require_admin)) -> dict[str, str]:
    """Confirm current administrator authentication."""
    return {"username": "admin"}


@app.get("/api/admin/settings")
async def admin_settings(_: None = Depends(require_admin)) -> dict[str, int]:
    """Return mutable instance settings."""
    return {"refresh_interval_seconds": int(database.get_setting("refresh_interval_seconds", 0)), "max_active_matches": int(database.get_setting("max_active_matches", settings.max_active_matches))}


@app.put("/api/admin/settings")
async def update_settings(request: SettingsRequest, _: None = Depends(require_admin)) -> dict[str, int]:
    """Persist mutable instance settings."""
    database.set_setting("refresh_interval_seconds", request.refresh_interval_seconds)
    database.set_setting("max_active_matches", request.max_active_matches)
    return await admin_settings(None)


@app.post("/api/admin/sync")
async def sync_heroes(_: None = Depends(require_admin)) -> dict[str, Any]:
    """Run an on-demand OP.GG catalogue refresh."""
    result = await synchronizer.refresh()
    if not result["ok"]:
        raise HTTPException(status_code=502, detail=result)
    return result


@app.get("/api/admin/heroes")
async def list_admin_heroes(_: None = Depends(require_admin)) -> list[dict[str, Any]]:
    """Return the latest heroes for manual lane classification."""
    return drafts.management_heroes()


@app.put("/api/admin/heroes/{hero_id}/roles")
async def update_admin_hero_roles(hero_id: str, request: HeroRolesRequest, _: None = Depends(require_admin)) -> dict[str, Any]:
    """Persist an administrator's lane selection for one hero."""
    try:
        return drafts.update_hero_roles(hero_id, request.roles)
    except DraftError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.post("/api/admin/series")
async def create_admin_series(request: CreateSeriesRequest, _: None = Depends(require_admin)) -> dict[str, Any]:
    """Create a series from the standalone management console."""
    try:
        return drafts.create_series(request.best_of, request.global_draft, "admin")
    except DraftError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.get("/api/admin/series")
async def list_series(_: None = Depends(require_admin)) -> list[dict[str, Any]]:
    """List recent series for management."""
    return drafts.management_series()


@app.post("/api/admin/series/{code}/next")
async def admin_next(code: str, _: None = Depends(require_admin)) -> dict[str, Any]:
    """Advance a series to the next game."""
    try:
        state = drafts.advance_game(code)
        await connections.broadcast(code, state)
        return state
    except DraftError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.post("/api/admin/series/{code}/format")
async def admin_extend_format(code: str, request: ExtendSeriesRequest, _: None = Depends(require_admin)) -> dict[str, Any]:
    """Increase an archived or completed series to a longer format."""
    try:
        state = drafts.extend_best_of(code, request.best_of)
        await connections.broadcast(code, state)
        return state
    except DraftError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.post("/api/admin/series/{code}/end")
async def admin_end(code: str, _: None = Depends(require_admin)) -> dict[str, Any]:
    """Archive a series and preserve its resumable progress."""
    try:
        state = drafts.end_series(code)
        await connections.broadcast(code, state)
        return state
    except DraftError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.post("/api/admin/series/{code}/restore")
async def admin_restore(code: str, _: None = Depends(require_admin)) -> dict[str, Any]:
    """Restore a previously archived series."""
    try:
        state = drafts.restore_series(code)
        await connections.broadcast(code, state)
        return state
    except DraftError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.delete("/api/admin/series/{code}")
async def admin_delete_series(code: str, _: None = Depends(require_admin)) -> dict[str, bool]:
    """Permanently remove a terminal series and its unused hero snapshot."""
    try:
        drafts.delete_series(code)
        return {"ok": True}
    except DraftError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.post("/api/admin/series/{code}/links/reissue")
async def admin_reissue_links(code: str, _: None = Depends(require_admin)) -> dict[str, str]:
    """Explicitly generate replacement links for legacy series without stored tokens."""
    try:
        return drafts.reissue_links(code)
    except DraftError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.get("/api/room/{code}/{token}/state")
async def room_state(code: str, token: str) -> dict[str, Any]:
    """Return room state to any valid capability link."""
    role = room_role(code, token)
    try:
        return {**await publish(code), "access_role": role}
    except DraftError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@app.post("/api/room/{code}/{token}/ready")
async def room_ready(code: str, token: str) -> dict[str, Any]:
    """Mark a captain ready."""
    try:
        return await _ready(code, room_role(code, token))
    except DraftError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


async def _ready(code: str, role: str) -> dict[str, Any]:
    drafts.set_ready(code, role)
    return await publish(code)


@app.post("/api/room/{code}/{token}/preselect")
async def room_preselect(code: str, token: str, request: HeroRequest) -> dict[str, Any]:
    """Set a captain's provisional pick."""
    try:
        drafts.preselect(code, room_role(code, token), request.hero_id)
        return await publish(code)
    except DraftError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.post("/api/room/{code}/{token}/act")
async def room_act(code: str, token: str, request: HeroRequest | None = None) -> dict[str, Any]:
    """Confirm a current ban or pick. Empty request means an empty ban."""
    try:
        drafts.act(code, room_role(code, token), request.hero_id if request else None)
        return await publish(code)
    except DraftError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.websocket("/ws/room/{code}/{token}")
async def room_socket(socket: WebSocket, code: str, token: str) -> None:
    """Push authoritative updates to blue, red, and spectator pages."""
    if not drafts.role_for_token(code, token):
        await socket.close(code=4404)
        return
    await connections.connect(code, socket)
    try:
        await socket.send_json(drafts.state(code))
        while True:
            await socket.receive_text()
    except WebSocketDisconnect:
        connections.remove(code, socket)
    except Exception:
        connections.remove(code, socket)


@app.post("/api/internal/series")
async def bot_create_series(request: CreateSeriesRequest, _: None = Depends(require_bot)) -> dict[str, Any]:
    """Optional bot endpoint for creating a series and receiving capability links."""
    try:
        return drafts.create_series(request.best_of, request.global_draft, "bot")
    except DraftError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.get("/api/internal/series/{code}")
async def bot_state(code: str, _: None = Depends(require_bot)) -> dict[str, Any]:
    """Optional bot endpoint for a current series snapshot."""
    try:
        return drafts.state(code)
    except DraftError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@app.post("/api/internal/series/{code}/next")
async def bot_next(code: str, _: None = Depends(require_bot)) -> dict[str, Any]:
    """Optional bot endpoint to advance a series."""
    try:
        state = drafts.advance_game(code)
        await connections.broadcast(code, state)
        return state
    except DraftError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.post("/api/internal/series/{code}/format")
async def bot_extend_format(code: str, request: ExtendSeriesRequest, _: None = Depends(require_bot)) -> dict[str, Any]:
    """Optional bot endpoint to increase a series format."""
    try:
        state = drafts.extend_best_of(code, request.best_of)
        await connections.broadcast(code, state)
        return state
    except DraftError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.post("/api/internal/series/{code}/end")
async def bot_end(code: str, _: None = Depends(require_bot)) -> dict[str, Any]:
    """Optional bot endpoint to end a series."""
    try:
        state = drafts.end_series(code)
        await connections.broadcast(code, state)
        return state
    except DraftError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.post("/api/internal/sync")
async def bot_sync(_: None = Depends(require_bot)) -> dict[str, Any]:
    """Optional bot endpoint for an administrator-authorized manual refresh."""
    result = await synchronizer.refresh()
    if not result["ok"]:
        raise HTTPException(status_code=502, detail=result)
    return result


@app.get("/{path:path}")
async def frontend(path: str) -> Any:
    """Serve the SPA shell for direct links when built frontend assets exist."""
    index = static_root / "index.html"
    if index.exists():
        return FileResponse(index)
    raise HTTPException(status_code=404, detail="前端尚未构建。")
