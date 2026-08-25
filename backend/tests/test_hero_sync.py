"""Tests for tolerant OP.GG source normalization."""

from app.hero_sync import normalize_mcp


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
