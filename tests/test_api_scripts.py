from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

pytestmark = pytest.mark.m03


class _ScriptEpisodeStore:
    def __init__(self, identity_ids: list[str]):
        self.episode = SimpleNamespace(identity_ids=identity_ids)
        self.get_episode_calls: list[int] = []

    def get_episode(self, episode_num: int):
        self.get_episode_calls.append(episode_num)
        return self.episode


def _script_client(monkeypatch, tmp_path, identity_ids: list[str]):
    from novelvideo.api.routes import scripts
    from novelvideo.api.deps import ProjectResolution

    store = _ScriptEpisodeStore(identity_ids)

    async def fake_make_sqlite_store(username: str, project: str):
        assert username == "alice"
        assert project == "demo"
        return store

    async def fake_resolve_project_scope(project, user, *, required_role="viewer"):
        assert project == "demo"
        assert user == {"username": "alice"}
        return ProjectResolution(
            ctx=None,
            username="alice",
            project_name="demo",
            project_dir=tmp_path,
            output_dir=str(tmp_path),
            state_dir=str(tmp_path / "state"),
            runtime_dir=str(tmp_path / "runtime"),
        )

    monkeypatch.setattr(scripts, "resolve_project_scope", fake_resolve_project_scope)
    monkeypatch.setattr(scripts, "make_sqlite_store", fake_make_sqlite_store)

    app = FastAPI()
    app.include_router(scripts.router, prefix="/api/v1")
    app.dependency_overrides[scripts.get_api_user] = lambda: {"username": "alice"}

    return TestClient(app), store


def test_script_generate_requires_identity_plan_before_side_effects(monkeypatch, tmp_path):
    client, store = _script_client(monkeypatch, tmp_path, [])

    response = client.post("/api/v1/projects/demo/episodes/2/script/generate", json={})

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["code"] == "identity_plan_required"
    assert body["error"]
    assert store.get_episode_calls == [2]


def test_script_generate_starts_script_writer_when_identity_plan_exists(
    monkeypatch, tmp_path
):
    client, store = _script_client(monkeypatch, tmp_path, ["秦_幼年"])

    response = client.post("/api/v1/projects/demo/episodes/2/script/generate", json={})

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert "project context" in body["error"]
    assert store.get_episode_calls == [2]


def test_pipeline_script_step_uses_script_writer_task_type():
    from novelvideo.api.routes.pipeline import _STEP_MAP

    assert _STEP_MAP["script"][0] == "script_writer"


def test_batch_generate_seedance2_prompts_enqueues_missing_only_and_skips_complete(
    monkeypatch, tmp_path
):
    import json
    from novelvideo.api.deps import ProjectResolution
    from novelvideo.api.routes import scripts
    from novelvideo.project_context import ProjectContext
    from novelvideo.seedance2_i2v.models import Seedance2VideoConfig, dump_seedance2_config

    beats = [
        {
            "beat_number": 1,
            "seedance2_config_json": dump_seedance2_config(
                Seedance2VideoConfig(final_prompt="已有提示词")
            ),
        },
        {
            "beat_number": 2,
            "seedance2_config_json": dump_seedance2_config(
                Seedance2VideoConfig(final_prompt="")
            ),
        },
    ]

    class _BatchStore:
        async def get_script_as_dict(self, episode: int):
            return {"episode": episode, "beats": [dict(b) for b in beats]}

    enqueued_calls: list[dict] = []

    class _DummyTaskBackend:
        async def enqueue_project_task(self, ctx, **kwargs):
            enqueued_calls.append(kwargs)
            return SimpleNamespace(
                task_state=SimpleNamespace(task_id="batch-task-1"),
                backend="local",
                queue="default",
            )

    ctx = ProjectContext(
        project_id="proj_demo",
        project_name="demo",
        owner_type="user",
        owner_id="u1",
        owner_username="alice",
        requester_user_id="u1",
        requester_username="alice",
        requester_principals=(),
        effective_role="editor",
        home_node_id="node1",
        output_dir=tmp_path,
        state_dir=tmp_path / "state",
        runtime_dir=tmp_path / "runtime",
        is_home_node=True,
    )

    monkeypatch.setattr(
        scripts,
        "resolve_project_scope",
        lambda project, user, *, required_role="viewer": _async_val(
            ProjectResolution(
                ctx=ctx,
                username="alice",
                project_name="demo",
                project_dir=tmp_path,
                output_dir=str(tmp_path),
                state_dir=str(tmp_path / "state"),
                runtime_dir=str(tmp_path / "runtime"),
            )
        ),
    )
    async def _async_store(_ctx):
        return _BatchStore()

    monkeypatch.setattr(scripts, "make_sqlite_store_for_context", _async_store)
    monkeypatch.setattr(scripts, "get_task_backend", lambda: _DummyTaskBackend())

    app = FastAPI()
    app.include_router(scripts.router, prefix="/api/v1")
    app.dependency_overrides[scripts.get_api_user] = lambda: {"username": "alice"}
    client = TestClient(app)

    res_missing = client.post(
        "/api/v1/projects/demo/episodes/1/seedance2-prompts/batch-generate",
        json={"only_missing": True, "s9_tail_anchor": False},
    )
    assert res_missing.status_code == 200
    body_missing = res_missing.json()
    assert body_missing["ok"] is True
    assert body_missing["task_type"] == "seedance2_prompt_batch"
    assert body_missing["target_beats"] == [2]
    assert enqueued_calls[0]["payload"]["beat_numbers"] == [2]

    # When only_missing=True and all beats already have prompts, skip queuing.
    beats[1]["seedance2_config_json"] = json.dumps({"final_prompt": "已补全"})
    res_skip = client.post(
        "/api/v1/projects/demo/episodes/1/seedance2-prompts/batch-generate",
        json={"only_missing": True},
    )
    assert res_skip.status_code == 200
    assert res_skip.json()["skipped"] is True
    assert res_skip.json()["target_beats"] == []


async def _async_val(val):
    return val


async def test_run_seedance2_prompt_batch_executes_concurrently_with_locked_store(
    monkeypatch, tmp_path
):
    from novelvideo import cognee as cognee_pkg
    from novelvideo.project_context import ProjectContext
    from novelvideo.seedance2_i2v import panel_service
    from novelvideo.seedance2_i2v.models import Seedance2VideoConfig, dump_seedance2_config
    from novelvideo.task_backend.runners import script as script_runner

    beats = [
        {"beat_number": 1, "visual_description": "A", "seedance2_config_json": dump_seedance2_config(Seedance2VideoConfig(final_prompt=""))},
        {"beat_number": 2, "visual_description": "B", "seedance2_config_json": dump_seedance2_config(Seedance2VideoConfig(final_prompt=""))},
    ]

    class _FakeCogneeStore:
        def __init__(self, *_a, **_kw):
            pass

        async def initialize(self):
            return None

        async def close(self):
            return None

        async def get_script_as_dict(self, episode: int):
            return {"episode": episode, "beats": [dict(b) for b in beats], "prop_menu": []}

        async def update_beat_asset(self, episode_number: int, beat_number: int, **updates):
            beats[beat_number - 1].update(updates)
            return True

    progress_updates: list[float] = []

    class _FakeManager:
        def update_progress_for_project(self, ctx, task_type, episode, *, progress=None, **_kw):
            if progress is not None:
                progress_updates.append(progress)

    seen_calls: list[int] = []

    async def _fake_gen_panel(**kwargs):
        bn = int(kwargs["beat"]["beat_number"])
        seen_calls.append(bn)
        saved = dump_seedance2_config(Seedance2VideoConfig(final_prompt=f"prompt-{bn}"))
        await kwargs["store"].update_beat_asset(episode_number=1, beat_number=bn, seedance2_config_json=saved)
        return saved

    monkeypatch.setattr(cognee_pkg, "CogneeStore", _FakeCogneeStore)
    monkeypatch.setattr(script_runner, "get_task_manager", lambda: _FakeManager())
    monkeypatch.setattr(panel_service, "generate_seedance2_prompt_for_panel", _fake_gen_panel)
    monkeypatch.setattr(
        "novelvideo.utils.source_language.detect_episode_asset_language",
        lambda *_a, **_kw: _async_val("zh"),
    )

    ctx = ProjectContext(
        project_id="p1",
        project_name="demo",
        owner_type="user",
        owner_id="u1",
        owner_username="alice",
        requester_user_id="u1",
        requester_username="alice",
        requester_principals=(),
        effective_role="editor",
        home_node_id="n1",
        output_dir=tmp_path,
        state_dir=tmp_path / "state",
        runtime_dir=tmp_path / "runtime",
        is_home_node=True,
    )
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)

    result = await script_runner._run_seedance2_prompt_batch_scoped(
        {"episode": 1, "payload": {"beat_numbers": [1, 2], "only_missing": True}},
        ctx,
    )

    assert sorted(seen_calls) == [1, 2]
    assert result["updated"] == 2
    assert result["beat_numbers"] == [1, 2]
    assert progress_updates[-1] == 1.0
