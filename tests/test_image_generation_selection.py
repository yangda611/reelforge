from __future__ import annotations


def test_image_generation_selection_normalizes_legacy_values_to_visible_newapi_options():
    from novelvideo.config import (
        image_generation_selection_options,
        normalize_image_generation_selection,
    )

    options = image_generation_selection_options()

    assert normalize_image_generation_selection("huimeng_image2_official") == "newapi_gpt_image2"
    assert normalize_image_generation_selection("huimeng_gpt_image2") == "newapi_gpt_image2"
    assert normalize_image_generation_selection("huimeng_nanobanana2") == "newapi_nanobanana2"
    assert normalize_image_generation_selection("openrouter_nanobanana2") == "newapi_nanobanana2"
    assert normalize_image_generation_selection("unknown") in options


def test_character_image_selection_normalizes_legacy_values_to_visible_newapi_options():
    from novelvideo.config import (
        character_image_selection_options,
        normalize_character_image_selection,
    )

    options = character_image_selection_options()

    assert normalize_character_image_selection("huimeng_gpt_image2") == "newapi_gpt_image2"
    assert normalize_character_image_selection("huimeng_nanobanana2") == "newapi_nanobanana2"
    assert normalize_character_image_selection("seedream") in options


def test_custom_ce_image_models_replace_and_resolve_selection_options(
    monkeypatch, tmp_path
):
    import novelvideo.config as config
    from novelvideo.model_gateway_settings import (
        MODE_CUSTOM,
        save_newapi_media_model_mappings,
        save_newapi_provider_channels,
        set_direct_multi_channel_mode,
        set_model_gateway_mode,
    )

    state_dir = str(tmp_path / "state")
    monkeypatch.setenv("NOVELVIDEO_STATE_DIR", state_dir)
    monkeypatch.setattr(config, "STATE_DIR", state_dir)
    monkeypatch.delenv("ST_CONTROL_PLANE_DSN", raising=False)

    set_model_gateway_mode(MODE_CUSTOM)
    set_direct_multi_channel_mode(True)
    save_newapi_provider_channels(
        [
            {
                "id": "openai-custom",
                "provider": "openai",
                "baseUrl": "https://relay.example.test/v1",
                "upstreamKey": "sk-relay-test",
            }
        ]
    )
    save_newapi_media_model_mappings(
        {
            "gpt-image-2.5-sunburst-Gpt02": {
                "provider": "openai",
                "channelId": "openai-custom",
                "upstreamModel": "gpt-image-2.5-upstream",
                "mediaType": "image",
                "label": "gpt-image-2.5-sunburst-Gpt02",
                "enabled": True,
                "sortOrder": 10,
            },
            "gpt-image-2.6-sunburst-Gpt02": {
                "provider": "openai",
                "channelId": "openai-custom",
                "upstreamModel": "gpt-image-2.6-upstream",
                "mediaType": "image",
                "label": "gpt-image-2.6-sunburst-Gpt02",
                "enabled": True,
                "sortOrder": 20,
            },
            "NTT-720p": {
                "provider": "openai",
                "channelId": "openai-custom",
                "upstreamModel": "NTT-720p",
                "mediaType": "video",
                "enabled": True,
            },
        },
        replace_catalog=True,
    )

    options = config.image_generation_selection_options()
    assert list(options.keys()) == [
        "gpt-image-2.5-sunburst-Gpt02",
        "gpt-image-2.6-sunburst-Gpt02",
    ]
    assert config.character_image_selection_options() == options
    assert (
        config.normalize_image_generation_selection("newapi_gpt_image2")
        == "gpt-image-2.5-sunburst-Gpt02"
    )
    assert (
        config.normalize_character_image_selection("newapi_nanobanana2")
        == "gpt-image-2.5-sunburst-Gpt02"
    )
    assert (
        config.normalize_image_generation_selection("gpt-image-2.6-sunburst-Gpt02")
        == "gpt-image-2.6-sunburst-Gpt02"
    )
    assert config.is_registered_image_generation_selection(
        "gpt-image-2.6-sunburst-Gpt02"
    )

    resolved = config.get_grid_generation_config(
        selection_override="gpt-image-2.6-sunburst-Gpt02"
    )
    assert resolved["provider"] == "newapi"
    assert resolved["base_url"] == "https://relay.example.test/v1"
    assert resolved["api_key"] == "sk-relay-test"
    assert resolved["model"] == "gpt-image-2.6-upstream"

    entry = config.resolve_image_generation_selection_entry(
        "gpt-image-2.6-sunburst-Gpt02"
    )
    assert entry["upstream_model"] == "gpt-image-2.6-upstream"
