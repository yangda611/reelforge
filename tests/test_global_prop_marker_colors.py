from novelvideo.models import build_prop_menu


def test_build_prop_menu_preserves_episode_marker_color_only():
    menu = build_prop_menu(
        prop_menu=[
            {
                "prop_id": "纸箱",
                "marker_color": "#0D47A1 ROYAL BLUE",
            }
        ]
    )

    assert menu[0].marker_color == "#0D47A1 ROYAL BLUE"
    dumped = [item.model_dump() for item in build_prop_menu(prop_menu=menu)][0]
    assert dumped["marker_color"] == "#0D47A1 ROYAL BLUE"
    assert "asset_scope" not in dumped
    assert "is_global_asset" not in dumped
    assert "preserve_marker_color" not in dumped
