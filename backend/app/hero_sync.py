"""OP.GG champion catalogue synchronization with a public-page fallback."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from bs4 import BeautifulSoup

from .database import Database, now

MCP_URL = "https://mcp-api.op.gg/mcp"
OPGG_CHAMPIONS_URL = "https://op.gg/lol/champions"
VALID_ROLES = {"TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"}
ROLE_ALIASES = {"TOP": "TOP", "JUNGLE": "JUNGLE", "MID": "MIDDLE", "MIDDLE": "MIDDLE", "BOTTOM": "BOTTOM", "ADC": "BOTTOM", "SUPPORT": "UTILITY", "UTILITY": "UTILITY"}


class SyncError(RuntimeError):
    """Raised when an upstream cannot produce a usable catalogue."""


@dataclass(frozen=True)
class Hero:
    """Normalized hero data consumed by the draft UI."""

    hero_id: str
    slug: str
    name: str
    title: str
    icon_url: str
    roles: list[str]
    win_rate: float | None = None
    pick_rate: float | None = None
    ban_rate: float | None = None


class OpggMcpProvider:
    """Read public champion metadata through OP.GG's Streamable HTTP MCP endpoint."""

    async def fetch(self) -> list[Hero]:
        """Fetch a merged champion and lane-meta response."""
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
            initialize = await client.post(MCP_URL, headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "global-banpick", "version": "1.0.0"}}})
            initialize.raise_for_status()
            session = initialize.headers.get("mcp-session-id")
            if session:
                headers["Mcp-Session-Id"] = session
            champions = await self._call(client, headers, 2, "lol_list_champions", {"desired_output_fields": ["data.champions[].{id,name,title,image,icon,slug}"]})
            lanes = await self._call(client, headers, 3, "lol_list_lane_meta_champions", {"desired_output_fields": ["data.*.champions[].{id,name,position,win_rate,pick_rate,ban_rate,image,icon}"]})
        heroes = normalize_mcp(champions, lanes)
        if not heroes:
            raise SyncError("OP.GG MCP 未返回可用英雄资料。")
        return heroes

    async def _call(self, client: httpx.AsyncClient, headers: dict[str, str], request_id: int, name: str, arguments: dict[str, Any]) -> Any:
        response = await client.post(MCP_URL, headers=headers, json={"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": {"name": name, "arguments": arguments}})
        response.raise_for_status()
        return _mcp_payload(response.text)


class OpggPublicPageProvider:
    """Fallback parser for publicly rendered OP.GG champion pages."""

    async def fetch(self) -> list[Hero]:
        """Extract structured page state without private endpoints or session headers."""
        async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers={"User-Agent": "GlobalBanPick/1.0 (+self-hosted draft tool)"}) as client:
            response = await client.get(OPGG_CHAMPIONS_URL)
            response.raise_for_status()
        heroes = normalize_public_html(response.text)
        if not heroes:
            raise SyncError("OP.GG 公开英雄页面未包含可用资料。")
        return heroes


class HeroSynchronizer:
    """Persist an immutable catalogue after successfully contacting an OP.GG provider."""

    def __init__(self, database: Database, image_dir: Path) -> None:
        self.database = database
        self.image_dir = image_dir

    async def refresh(self) -> dict[str, Any]:
        """Try MCP then page data and keep the existing catalogue on failure."""
        errors: list[str] = []
        for source, provider in (("opgg_mcp", OpggMcpProvider()), ("opgg_public_page", OpggPublicPageProvider())):
            try:
                heroes = await provider.fetch()
                await self._cache_avatars(heroes)
                catalogue_id = self._store(source, heroes)
                return {"ok": True, "source": source, "catalogue_id": catalogue_id, "hero_count": len(heroes), "errors": errors}
            except Exception as error:  # Upstreams are intentionally independent fallbacks.
                errors.append(f"{source}: {error}")
        with self.database.connection() as connection:
            existing = connection.execute("SELECT id FROM catalogues ORDER BY id DESC LIMIT 1").fetchone()
        return {"ok": False, "source": None, "catalogue_id": existing["id"] if existing else None, "hero_count": 0, "errors": errors}

    async def _cache_avatars(self, heroes: list[Hero]) -> None:
        self.image_dir.mkdir(parents=True, exist_ok=True)
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            tasks = [self._cache_avatar(client, hero) for hero in heroes if hero.icon_url]
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _cache_avatar(self, client: httpx.AsyncClient, hero: Hero) -> None:
        filename = re.sub(r"[^a-zA-Z0-9_-]", "_", hero.hero_id) + ".png"
        destination = self.image_dir / filename
        if destination.exists() and destination.stat().st_size > 100:
            return
        response = await client.get(hero.icon_url)
        if response.is_success and response.headers.get("content-type", "").startswith("image/"):
            destination.write_bytes(response.content)

    def _store(self, source: str, heroes: list[Hero]) -> int:
        summary = {"hero_count": len(heroes), "source": source}
        with self.database.connection() as connection:
            catalogue_id = connection.execute("INSERT INTO catalogues(source, created_at, summary_json) VALUES (?, ?, ?)", (source, now(), json.dumps(summary))).lastrowid
            for hero in heroes:
                cached = self.image_dir / (re.sub(r"[^a-zA-Z0-9_-]", "_", hero.hero_id) + ".png")
                icon_url = f"/hero-images/{cached.name}" if cached.exists() else hero.icon_url
                connection.execute(
                    """INSERT INTO heroes(catalogue_id, hero_id, slug, name, title, icon_url, roles_json, win_rate, pick_rate, ban_rate)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (catalogue_id, hero.hero_id, hero.slug, hero.name, hero.title, icon_url, json.dumps(hero.roles), hero.win_rate, hero.pick_rate, hero.ban_rate),
                )
        return int(catalogue_id)


def _mcp_payload(text: str) -> Any:
    """Decode a JSON-RPC or SSE MCP response into its tool result."""
    payloads = []
    for line in text.splitlines():
        if line.startswith("data:"):
            payloads.append(line.removeprefix("data:").strip())
    raw = payloads[-1] if payloads else text
    parsed = json.loads(raw)
    result = parsed.get("result", parsed)
    content = result.get("content", []) if isinstance(result, dict) else []
    texts = [item.get("text", "") for item in content if isinstance(item, dict) and item.get("type") == "text"]
    if texts:
        try:
            return json.loads(texts[-1])
        except json.JSONDecodeError:
            return {"text": texts[-1]}
    return result


def normalize_mcp(champions: Any, lanes: Any) -> list[Hero]:
    """Normalize varying MCP response shapes and merge lane statistics."""
    champion_items = _find_champion_items(champions)
    lane_items = _find_champion_items(lanes)
    merged: dict[str, dict[str, Any]] = {}
    for item in champion_items + lane_items:
        identifier = str(item.get("id") or item.get("champion_id") or item.get("key") or item.get("name") or "")
        if not identifier:
            continue
        existing = merged.setdefault(identifier, {"roles": []})
        for key, value in item.items():
            if value not in (None, "", [], {}):
                existing[key] = value
        position = item.get("position") or item.get("role") or item.get("lane")
        normalized = _role(position)
        if normalized and normalized not in existing["roles"]:
            existing["roles"].append(normalized)
    return [_hero(identifier, item) for identifier, item in merged.items() if item.get("name")]


def normalize_public_html(html: str) -> list[Hero]:
    """Extract hero-shaped objects embedded in public Next.js page state."""
    soup = BeautifulSoup(html, "html.parser")
    candidates: list[dict[str, Any]] = []
    for script in soup.find_all("script"):
        text = script.string or script.get_text()
        if not text or "champion" not in text.casefold():
            continue
        for object_text in re.findall(r"\{[^{}]{0,1600}(?:champion_id|championId|name)[^{}]{0,1600}\}", text, re.IGNORECASE):
            try:
                value = json.loads(object_text)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                candidates.append(value)
    merged: dict[str, dict[str, Any]] = {}
    for item in candidates:
        identifier = str(item.get("champion_id") or item.get("championId") or item.get("id") or item.get("name") or "")
        if identifier:
            merged.setdefault(identifier, item)
    return [_hero(identifier, item) for identifier, item in merged.items() if item.get("name")]


def _find_champion_items(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict) and (item.get("name") or item.get("champion_id"))]
    if isinstance(value, dict):
        result: list[dict[str, Any]] = []
        for key, item in value.items():
            if key in {"champions", "data", "results"}:
                result.extend(_find_champion_items(item))
            elif isinstance(item, dict):
                result.extend(_find_champion_items(item))
            elif isinstance(item, list):
                result.extend(_find_champion_items(item))
        return result
    return []


def _role(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return ROLE_ALIASES.get(value.upper())


def _hero(identifier: str, item: dict[str, Any]) -> Hero:
    image = item.get("image") or item.get("icon") or item.get("image_url") or item.get("imageUrl") or ""
    if isinstance(image, dict):
        image = image.get("url") or image.get("src") or image.get("image_url") or ""
    roles = [_role(item.get("position") or item.get("role") or item.get("lane"))]
    roles += [_role(role) for role in item.get("roles", []) if isinstance(item.get("roles"), list)]
    return Hero(
        hero_id=identifier,
        slug=str(item.get("slug") or item.get("key") or identifier),
        name=str(item.get("name")),
        title=str(item.get("title") or ""),
        icon_url=str(image),
        roles=sorted({role for role in roles if role in VALID_ROLES}),
        win_rate=_number(item.get("win_rate") or item.get("winRate")),
        pick_rate=_number(item.get("pick_rate") or item.get("pickRate")),
        ban_rate=_number(item.get("ban_rate") or item.get("banRate")),
    )


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
