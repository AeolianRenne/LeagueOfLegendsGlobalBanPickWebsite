"""Tests for tolerant OP.GG source normalization."""

from app.hero_sync import Hero, apply_opgg_roles, normalize_mcp


def test_merges_champion_metadata_and_lane_stats() -> None:
    """Lane metadata augments the same hero rather than creating a duplicate."""
    heroes = normalize_mcp(
        {"data": {"champions": [{"id": "Aatrox", "name": "亚托克斯", "title": "暗裔剑魔", "image": "https://example.test/a.png"}]}},
        {"data": {"TOP": {"champions": [{"id": "Aatrox", "name": "亚托克斯", "position": "TOP", "win_rate": 51.2, "pick_rate": 8.1}]}}},
    )
    assert len(heroes) == 1
    assert heroes[0].name == "亚托克斯"
    assert heroes[0].roles == ["TOP"]
    assert heroes[0].win_rate == 51.2


def test_public_position_pages_override_static_role_heuristics() -> None:
    hero = Hero("Aatrox", "Aatrox", "亚托克斯", "暗裔剑魔", "https://example.test/a.png", ["JUNGLE"])

    heroes = apply_opgg_roles([hero], {"TOP": "Ranking table Aatrox", "JUNGLE": "Ranking table Lee Sin"})

    assert heroes[0].roles == ["TOP"]


def test_identical_position_pages_do_not_assign_every_lane() -> None:
    """A public page that ignores its position filter falls back to static roles."""
    hero = Hero("Aatrox", "Aatrox", "亚托克斯", "暗裔剑魔", "https://example.test/a.png", ["TOP"])

    heroes = apply_opgg_roles([hero], {role: "Aatrox" for role in ("TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY")})

    assert heroes[0].roles == ["TOP"]


def test_mcp_champions_without_positions_are_not_treated_as_lane_data() -> None:
    heroes = normalize_mcp(
        {"data": {"champions": [{"id": "Aatrox", "name": "亚托克斯", "title": "暗裔剑魔"}]}},
        {"data": {"champions": []}},
    )

    assert heroes[0].roles == []
