from __future__ import annotations

from novelvideo.generators.episode_optimizer import (
    BRIDGMAN_CHARACTER_PALETTE,
    EpisodeOptimizer,
    PROP_MARKER_PALETTE,
)


def _rgb_distance(a: str, b: str) -> float:
    ar, ag, ab = (int(a[index:index + 2], 16) for index in (1, 3, 5))
    br, bg, bb = (int(b[index:index + 2], 16) for index in (1, 3, 5))
    return ((ar - br) ** 2 + (ag - bg) ** 2 + (ab - bb) ** 2) ** 0.5


def test_prop_marker_palette_stays_visually_separated_from_character_palette():
    prop_hexes = [hex_code for hex_code, _name in PROP_MARKER_PALETTE]
    character_hexes = [hex_code for hex_code, _name in BRIDGMAN_CHARACTER_PALETTE]

    assert "#E65100" not in prop_hexes
    assert "#4A148C" not in prop_hexes
    assert "#6D4C41" in prop_hexes
    assert "#7B1FA2" in prop_hexes
    assert min(
        _rgb_distance(prop_hex, character_hex)
        for prop_hex in prop_hexes
        for character_hex in character_hexes
    ) >= 80
    assert min(
        _rgb_distance(left, right)
        for index, left in enumerate(prop_hexes)
        for right in prop_hexes[index + 1:]
    ) >= 55


def test_assign_sketch_colors_preserves_existing_when_identity_set_changes():
    existing = {
        "Hero_A": "#FF00FF FLUORESCENT MAGENTA",
        "Hero_Removed": "#00FFFF FLUORESCENT CYAN",
    }
    beats = [
        {"visual_description": "{{Hero_B}} enters"},
        {"visual_description": "{{Hero_A}} stays"},
    ]

    colors = EpisodeOptimizer.assign_sketch_colors(
        characters=[],
        episode_beats=beats,
        existing_colors=existing,
    )

    assert colors["Hero_A"] == "#FF00FF FLUORESCENT MAGENTA"
    assert colors["Hero_Removed"] == "#00FFFF FLUORESCENT CYAN"
    assert "Hero_B" in colors
    assert colors["Hero_B"] not in set(existing.values())
