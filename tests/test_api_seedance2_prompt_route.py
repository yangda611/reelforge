from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from novelvideo.project_context import ProjectContext


pytestmark = pytest.mark.m09


@dataclass
class DummySqliteStore:
    beats: list[dict]
    updates: list[dict] = field(default_factory=list)
    characters: list[dict] = field(default_factory=list)
    sketch_colors: dict[str, str] = field(default_factory=dict)
    source_text: str = ""

    async def get_script_as_dict(self, episode: int):
        return {"episode": episode, "beats": [dict(beat) for beat in self.beats]}

    async def load_working_content(self, episode: int):
        return self.source_text

    def get_all_characters(self):
        return list(self.characters)

    def get_sketch_colors(self, episode_num: int):
        return dict(self.sketch_colors)

    async def update_beat_asset(self, episode_number: int, beat_number: int, **updates):
        self.updates.append(
            {"episode": episode_number, "beat": beat_number, "updates": updates}
        )
        for beat in self.beats:
            if int(beat.get("beat_number") or 0) == beat_number:
                beat.update(updates)
                return True
        return False


class DummyCogneeStore:
    pass


class DummyUsageMeter:
    def __init__(self):
        self.reserve_calls: list[dict] = []
        self.confirm_calls: list[tuple[str, dict | None]] = []
        self.refund_calls: list[tuple[str, dict | None]] = []
        self.interrupted_calls: list[tuple[str, dict | None]] = []
        self.contexts: list[dict] = []
        self.clear_count = 0

    async def reserve_feature_start_credits(self, **kwargs):
        self.reserve_calls.append(kwargs)
        return {"id": "seedance2-prompt-reservation", "cost": 9}

    async def settle_feature_credit_reservation(
        self,
        reservation_id: str,
        *,
        action: str,
        metadata=None,
    ):
        target = self.confirm_calls if action == "confirm" else self.refund_calls
        target.append((reservation_id, metadata))

    async def settle_cancelled_feature_credit_reservation(
        self, reservation_id: str, *, metadata=None
    ):
        self.interrupted_calls.append((reservation_id, metadata))

    def set_llm_usage_context(
        self,
        user_id: str,
        project_id: str = "",
        resource_kind: str = "",
        billing_metadata: dict | None = None,
    ):
        self.contexts.append(
            {
                "user_id": user_id,
                "project_id": project_id,
                "resource_kind": resource_kind,
                "billing_metadata": billing_metadata or {},
            }
        )

    def clear_llm_usage_context(self):
        self.clear_count += 1


def _client(
    monkeypatch,
    tmp_path,
    beats: list[dict],
    *,
    ctx=None,
    usage_meter=None,
    source_text="",
):
    from novelvideo.api.routes import scripts
    from novelvideo.api.deps import ProjectResolution

    sqlite_store = DummySqliteStore(beats, source_text=source_text)

    async def _make_sqlite_store(username: str, project: str):
        return sqlite_store

    async def _make_cognee_store(username: str, project: str):
        return DummyCogneeStore()

    async def fake_resolve_project_scope(project, user, *, required_role="viewer"):
        return ProjectResolution(
            ctx=ctx,
            username="admin",
            project_name="demo",
            project_dir=tmp_path,
            output_dir=str(tmp_path),
            state_dir=str(tmp_path / "state"),
            runtime_dir=str(tmp_path / "runtime"),
        )

    monkeypatch.setattr(scripts, "resolve_project_scope", fake_resolve_project_scope)
    monkeypatch.setattr(scripts, "make_sqlite_store", _make_sqlite_store)
    monkeypatch.setattr(
        scripts,
        "make_sqlite_store_for_context",
        lambda _ctx: _make_sqlite_store("admin", "demo"),
    )
    monkeypatch.setattr(scripts, "make_cognee_store", _make_cognee_store)
    if usage_meter is not None:
        monkeypatch.setattr(scripts, "get_usage_meter", lambda: usage_meter)

    app = FastAPI()
    app.include_router(scripts.router)
    app.dependency_overrides[scripts.get_api_user] = lambda: {"username": "admin"}
    return TestClient(app), sqlite_store


def _project_ctx(tmp_path: Path) -> ProjectContext:
    return ProjectContext(
        project_id="proj_123",
        project_name="demo",
        owner_type="user",
        owner_id="user_owner",
        owner_username="admin",
        requester_user_id="user_editor",
        requester_username="admin",
        requester_principals=(("user", "user_editor"),),
        effective_role="editor",
        home_node_id="node_a",
        output_dir=tmp_path,
        state_dir=tmp_path / "state",
        runtime_dir=tmp_path / "runtime",
        is_home_node=True,
    )


def _write_test_image(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (8, 8), "white").save(path)


def test_generate_seedance2_prompt_updates_config_json(monkeypatch, tmp_path):
    from novelvideo.seedance2_i2v import panel_service

    saved_json = json.dumps(
        {
            "mode": "first_frame",
            "duration": 5,
            "resolution": "720p",
            "ratio": "9:16",
            "final_prompt": "optimized seedance2 prompt",
            "prompt_guidance": "more camera motion",
            "prompt_source": "generated",
        }
    )
    seen = {}

    async def _generate_seedance2_prompt_for_panel(**kwargs):
        seen.update(kwargs)
        beat = kwargs["beat"]
        beat["seedance2_config_json"] = saved_json
        await kwargs["store"].update_beat_asset(
            episode_number=kwargs["episode"],
            beat_number=int(beat["beat_number"]),
            seedance2_config_json=saved_json,
        )
        return saved_json

    monkeypatch.setattr(
        panel_service,
        "generate_seedance2_prompt_for_panel",
        _generate_seedance2_prompt_for_panel,
    )
    client, store = _client(
        monkeypatch,
        tmp_path,
        [
            {
                "beat_number": 1,
                "seedance2_config_json": json.dumps(
                    {
                        "mode": "first_frame",
                        "duration": 5,
                        "resolution": "720p",
                        "ratio": "9:16",
                        "final_prompt": "current seedance2 prompt",
                    }
                ),
            },
            {"beat_number": 2},
        ],
        source_text="INT. SUBWAY STATION - NIGHT\nJi-won raises her phone.",
    )

    response = client.post(
        "/projects/demo/episodes/1/beats/1/seedance2-prompt/generate",
        json={
            "manual_prompt_reference": "current seedance2 prompt",
            "prompt_guidance": "more camera motion",
            "prompt_guidance_template_keys": ["camera"],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["data"]["final_prompt"] == "optimized seedance2 prompt"
    assert payload["data"]["seedance2_config_json"] == saved_json
    assert payload["data"]["beat"]["seedance2_config_json"] == saved_json
    assert seen["manual_prompt_reference"] == "current seedance2 prompt"
    assert seen["prompt_guidance"] == "more camera motion"
    assert seen.get("prompt_guidance_template_keys") == ["camera"]
    assert seen["language"] == "en"
    assert seen["prev_beat"] is None
    assert seen["next_beat"]["beat_number"] == 2
    assert store.updates == [
        {
            "episode": 1,
            "beat": 1,
            "updates": {"seedance2_config_json": saved_json},
        }
    ]


def test_generate_seedance2_prompt_passes_prev_and_next_beats(monkeypatch, tmp_path):
    from novelvideo.seedance2_i2v import panel_service

    saved_json = json.dumps({"mode": "first_frame", "final_prompt": "ok"})
    seen = {}

    async def _generate_seedance2_prompt_for_panel(**kwargs):
        seen.update(kwargs)
        return saved_json

    monkeypatch.setattr(
        panel_service,
        "generate_seedance2_prompt_for_panel",
        _generate_seedance2_prompt_for_panel,
    )
    client, _store = _client(
        monkeypatch,
        tmp_path,
        [
            {"beat_number": 1, "visual_description": "Beat 1"},
            {"beat_number": 2, "visual_description": "Beat 2"},
            {"beat_number": 3, "visual_description": "Beat 3"},
        ],
    )

    response = client.post(
        "/projects/demo/episodes/1/beats/2/seedance2-prompt/generate",
        json={},
    )

    assert response.status_code == 200
    assert seen["prev_beat"]["beat_number"] == 1
    assert seen["next_beat"]["beat_number"] == 3


def test_generate_seedance2_prompt_reserves_feature_credit_and_confirms(
    monkeypatch,
    tmp_path,
):
    from novelvideo.seedance2_i2v import panel_service

    saved_json = json.dumps(
        {
            "mode": "first_frame",
            "duration": 5,
            "resolution": "720p",
            "ratio": "9:16",
            "final_prompt": "optimized seedance2 prompt",
            "prompt_source": "generated",
        }
    )

    async def _generate_seedance2_prompt_for_panel(**kwargs):
        beat = kwargs["beat"]
        beat["seedance2_config_json"] = saved_json
        await kwargs["store"].update_beat_asset(
            episode_number=kwargs["episode"],
            beat_number=int(beat["beat_number"]),
            seedance2_config_json=saved_json,
        )
        return saved_json

    monkeypatch.setattr(
        panel_service,
        "generate_seedance2_prompt_for_panel",
        _generate_seedance2_prompt_for_panel,
    )
    usage_meter = DummyUsageMeter()
    ctx = _project_ctx(tmp_path)
    client, _store = _client(
        monkeypatch,
        tmp_path,
        [
            {
                "beat_number": 1,
                "seedance2_config_json": json.dumps(
                    {
                        "mode": "first_frame",
                        "duration": 5,
                        "resolution": "720p",
                        "ratio": "9:16",
                    }
                ),
            }
        ],
        ctx=ctx,
        usage_meter=usage_meter,
    )

    response = client.post(
        "/projects/demo/episodes/1/beats/1/seedance2-prompt/generate",
        json={"prompt_guidance": "more camera motion"},
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert usage_meter.reserve_calls[0]["feature_key"] == "mainline.seedance2_prompt"
    assert usage_meter.reserve_calls[0]["task_type"] == "seedance2_prompt"
    assert usage_meter.reserve_calls[0]["resource_kind"] == "script"
    assert usage_meter.reserve_calls[0]["require_price_rule"] is True
    assert usage_meter.reserve_calls[0]["require_positive_cost"] is True
    assert usage_meter.contexts[0]["billing_metadata"][
        "model_call_credit_policy"
    ] == "feature_included"
    assert usage_meter.contexts[0]["billing_metadata"][
        "feature_credit_reservation_id"
    ] == "seedance2-prompt-reservation"
    assert usage_meter.confirm_calls[0][0] == "seedance2-prompt-reservation"
    assert usage_meter.refund_calls == []
    assert usage_meter.clear_count == 1


def test_generate_seedance2_prompt_uses_evidence_settlement_on_failure(
    monkeypatch,
    tmp_path,
):
    from novelvideo.seedance2_i2v import panel_service

    async def _generate_seedance2_prompt_for_panel(**kwargs):
        raise ValueError("seedance2 prompt invalid")

    monkeypatch.setattr(
        panel_service,
        "generate_seedance2_prompt_for_panel",
        _generate_seedance2_prompt_for_panel,
    )
    usage_meter = DummyUsageMeter()
    client, _store = _client(
        monkeypatch,
        tmp_path,
        [
            {
                "beat_number": 1,
                "seedance2_config_json": json.dumps(
                    {
                        "mode": "first_frame",
                        "duration": 5,
                        "resolution": "720p",
                        "ratio": "9:16",
                    }
                ),
            }
        ],
        ctx=_project_ctx(tmp_path),
        usage_meter=usage_meter,
    )

    response = client.post(
        "/projects/demo/episodes/1/beats/1/seedance2-prompt/generate",
        json={"prompt_guidance": "more camera motion"},
    )

    assert response.status_code == 200
    assert response.json() == {"ok": False, "error": "seedance2 prompt invalid"}
    assert usage_meter.confirm_calls == []
    assert usage_meter.refund_calls == []
    assert usage_meter.interrupted_calls[0][0] == "seedance2-prompt-reservation"
    assert usage_meter.clear_count == 1


def test_generate_seedance2_prompt_requires_next_beat_for_first_last_mode(
    monkeypatch, tmp_path
):
    client, store = _client(
        monkeypatch,
        tmp_path,
        [
            {
                "beat_number": 1,
                "seedance2_config_json": json.dumps(
                    {
                        "mode": "first_last_frame",
                        "final_prompt": "current seedance2 prompt",
                    }
                ),
            }
        ],
        source_text="INT. SUBWAY STATION - NIGHT\nJi-won raises her phone.",
    )

    response = client.post(
        "/projects/demo/episodes/1/beats/1/seedance2-prompt/generate",
        json={"manual_prompt_reference": "current seedance2 prompt"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "ok": False,
        "error": "这是最后一个 Beat，无法使用首尾帧模式",
    }
    assert store.updates == []


async def test_generate_seedance2_prompt_s9_inherits_continuity_bible_and_neighbor_shots(
    monkeypatch, tmp_path
):
    from types import SimpleNamespace
    from novelvideo import s9_storyboard
    from novelvideo.s9_storyboard import (
        S9EpisodeContinuityBible,
        S9EpisodePreviewMeta,
        S9SceneLightingAnchor,
        S9SegmentAssetSlot,
        S9SegmentMeta,
        S9SubShot,
    )
    from novelvideo.seedance2_i2v import panel_service
    from novelvideo.seedance2_i2v.models import (
        Seedance2I2VMode,
        Seedance2VideoConfig,
        dump_seedance2_config,
        parse_seedance2_config,
    )

    bible = S9EpisodeContinuityBible(
        scene_lighting_rules=[
            S9SceneLightingAnchor(
                scene_id="客厅",
                time_of_day="夜",
                key_lighting="台灯暖金侧光搭配窗外冷青霓虹反光",
            )
        ],
        prop_state_chain=["铜钥匙：大段1落桌面，大段2握入掌心"],
    )
    ep_meta = S9EpisodePreviewMeta(continuity_bible=bible)
    full_source = "\n".join(
        [
            "第1行：雨点急促敲打着老旧木窗棂，窗外整条长街路灯在夜幕雨雾里泛起昏黄光斑与水渍倒影。",
            "第2行：远处街头转角处偶尔驶过一辆黑色轿车，轮胎碾过积水路面溅起细碎水花很快消失夜色中。",
            "第3行：陈默推门走进安静的玄关，抖落肩头雨水并将一枚沉甸甸的铜钥匙轻轻放在玄关柜角处。",
            "第4行：他缓慢脱下湿透的深灰风衣外套顺手搭在玄关木椅椅背上，抬眼扫视着空无一人的客厅。",
            "第5行：他摘下帽檐坐进沙发，拿起床头档案。",
            "第6行：对方突然在电话里报出暗号。",
            "第7行：整个客厅陷入短暂而凝重的沉默，墙壁挂钟秒针走字声和窗外雨声此刻格外清晰刺耳。",
            "第8行：陈默缓缓合上那份泛黄的纸质档案，眉头深锁地将视线缓缓转向几米开外的玄关方向。",
            "第9行：他毫不犹豫伸手抓起柜角那枚铜钥匙，猛然起身披上风衣大步冲向门口准备趁夜离开。",
            "第10行：沉重的防盗房门在他的身后发出一声闷响重重合上，走廊声控灯光随即亮起又渐渐暗去。",
        ]
    )

    prev_shots = [
        S9SubShot(shot_number=1, planned_duration_seconds=4, start_seconds=0, end_seconds=4, content="推门进入", scene_id="客厅"),
        S9SubShot(shot_number=2, planned_duration_seconds=4, start_seconds=4, end_seconds=8, content="前段末镜：钥匙放稳玄关桌", scene_id="客厅"),
    ]
    cur_shots = [
        S9SubShot(shot_number=1, planned_duration_seconds=5, start_seconds=0, end_seconds=5, content="坐进沙发翻开档案", scene_id="客厅"),
    ]
    next_shots = [
        S9SubShot(shot_number=1, planned_duration_seconds=5, start_seconds=0, end_seconds=5, content="后段首镜：抓钥匙起身奔出", scene_id="客厅"),
    ]

    def _s9_beat(num: int, shots: list[S9SubShot], excerpt: str = "") -> dict:
        meta = S9SegmentMeta(
            segment_number=num,
            segment_title=f"大段{num}",
            planned_duration_seconds=5,
            source_excerpt=excerpt,
            segment_asset_slots=[
                S9SegmentAssetSlot(slot=1, media_type="image", asset_kind="scene", name="客厅", asset_id="客厅")
            ],
        )
        return {
            "beat_number": num,
            "segment_title": f"大段{num}",
            "time_of_day": "夜",
            "scene_ref": {"scene_id": "客厅", "variant_id": ""},
            "visual_description": f"大段{num}描述",
            "s9_subshots_json": json.dumps([s.model_dump(mode="json") for s in shots], ensure_ascii=False),
            "s9_segment_meta_json": json.dumps(meta.model_dump(mode="json"), ensure_ascii=False),
            "seedance2_config_json": dump_seedance2_config(
                Seedance2VideoConfig(
                    mode=Seedance2I2VMode.MULTIMODAL_REFERENCE,
                    prompt_style="s9_segment",
                    prompt_source="s9_script",
                )
            ),
        }

    beats = [
        _s9_beat(1, prev_shots),
        _s9_beat(2, cur_shots, excerpt="第5行：他摘下帽檐坐进沙发，拿起床头档案。\n第6行：对方突然在电话里报出暗号。"),
        _s9_beat(3, next_shots),
    ]

    class _S9Store(DummySqliteStore):
        def get_episode(self, episode_num: int):
            return SimpleNamespace(
                beat_source_text=full_source,
                raw_content=full_source,
                s9_preview_meta_json=ep_meta.model_dump_json(ensure_ascii=False),
            )

        def get_all_scenes(self):
            return [SimpleNamespace(id="客厅", name="客厅")]

        def get_all_props(self):
            return []

    captured: dict = {}

    async def _fake_stage2(**kwargs):
        captured.update(kwargs)
        return "【全局光影画风】\n暖金侧光。\n\n【分镜详情】\n分镜1 [0s-5s]：翻看档案。"

    monkeypatch.setattr(s9_storyboard, "run_s9_stage2_segment_prompt", _fake_stage2)
    monkeypatch.setattr(
        panel_service,
        "build_seedance2_project_assets",
        lambda *_a, **_kw: [],
    )

    store = _S9Store(beats=beats, source_text=full_source)
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    saved_json = await panel_service.generate_seedance2_prompt_for_panel(
        store=store,
        state_dir=state_dir,
        episode=1,
        beat=beats[1],
        project_dir=tmp_path,
        prev_beat=beats[0],
        next_beat=beats[2],
    )

    assert "台灯暖金侧光搭配窗外冷青霓虹反光" in captured["continuity_anchor"]
    assert "前段末镜：钥匙放稳玄关桌" == captured["prev_segment"].shots[-1].content
    assert "后段首镜：抓钥匙起身奔出" == captured["next_segment"].shots[0].content
    assert "第5行：他摘下帽檐" in captured["source_text"]
    assert "第1行：雨点急促敲打" not in captured["source_text"]
    assert "第10行：沉重的防盗房门" not in captured["source_text"]
    assert parse_seedance2_config(saved_json).final_prompt.startswith("【全局光影画风】")
