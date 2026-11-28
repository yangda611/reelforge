"""Tests for deterministic two-stage mainline episode autopilot and batch video dispatch."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from novelvideo.project_context import ProjectContext
from novelvideo.services import episode_autopilot as autopilot_mod
from novelvideo.services.episode_autopilot import (
    ActiveAutopilotProjectIndex,
    AutopilotOptions,
    EpisodeAutopilotCoordinator,
    ProjectAutopilotStore,
    classify_autopilot_error,
)
from novelvideo.task_backend.limits import ProjectTaskLimitExceeded
from novelvideo.task_state import TaskState


def _make_ctx(root: Path, project_id: str, project_name: str = "demo") -> ProjectContext:
    state_dir = root / project_id / "state"
    output_dir = root / project_id / "output"
    runtime_dir = root / project_id / "runtime"
    state_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    return ProjectContext(
        project_id=project_id,
        project_name=project_name,
        owner_type="user",
        owner_id="user-alice",
        owner_username="alice",
        requester_user_id="user-alice",
        requester_username="alice",
        requester_principals=(("user", "user-alice"),),
        effective_role="owner",
        home_node_id="local-node",
        output_dir=output_dir,
        state_dir=state_dir,
        runtime_dir=runtime_dir,
        is_home_node=True,
    )


@dataclass
class _FakeEpisode:
    number: int
    title: str = ""
    identity_ids: list[str] = field(default_factory=list)
    scene_menu: list[dict[str, Any]] = field(default_factory=list)
    prop_menu: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class _FakeIdentity:
    identity_id: str
    identity_name: str


@dataclass
class _FakeCharacter:
    name: str
    face_prompt: str = "young detective"
    identities: list[_FakeIdentity] = field(default_factory=list)


@dataclass
class _FakeScene:
    name: str


@dataclass
class _FakeProp:
    name: str


class _FakeProjectSQLite:
    """In-memory project store mirroring SQLiteStore interface for deterministic tests."""

    def __init__(self, ctx: ProjectContext) -> None:
        self.ctx = ctx
        self.state_dir = ctx.state_dir
        self.characters: list[_FakeCharacter] = []
        self.scenes: list[_FakeScene] = []
        self.props: list[_FakeProp] = []
        self.episodes: dict[int, _FakeEpisode] = {}
        self.beats: dict[int, list[dict[str, Any]]] = {}

    def get_all_characters(self) -> list[_FakeCharacter]:
        return list(self.characters)

    def get_all_episodes(self) -> list[_FakeEpisode]:
        return [self.episodes[k] for k in sorted(self.episodes)]

    def get_all_scenes(self) -> list[_FakeScene]:
        return list(self.scenes)

    def get_all_props(self) -> list[_FakeProp]:
        return list(self.props)

    def get_episode(self, episode_num: int) -> _FakeEpisode | None:
        return self.episodes.get(int(episode_num))

    async def get_beats_as_dicts(self, episode_num: int) -> list[dict[str, Any]]:
        return [dict(b) for b in self.beats.get(int(episode_num), [])]

    async def update_beat_asset(
        self,
        *,
        episode_number: int,
        beat_number: int,
        seedance2_config_json: str | None = None,
        **kwargs: Any,
    ) -> None:
        for beat in self.beats.get(int(episode_number), []):
            if int(beat.get("beat_number") or 0) == int(beat_number):
                if seedance2_config_json is not None:
                    beat["seedance2_config_json"] = seedance2_config_json
                beat.update(kwargs)

    async def close(self) -> None:
        return None


class _FakeTaskInfra:
    """Simulated TaskManager + TaskBackend with deduplication and async execution."""

    def __init__(self) -> None:
        self.tasks: dict[tuple[str, str, int, int | None, str | None], TaskState] = {}
        self.counter = 0
        self.enqueued: list[tuple[str, str, int, int | None, str | None, dict[str, Any]]] = []
        self.cancelled_keys: list[tuple[str, str, int, int | None, str | None]] = []
        self.handlers: dict[str, Any] = {}
        self._bg: set[asyncio.Task] = set()

    def _key(
        self,
        ctx: ProjectContext,
        task_type: str,
        episode: int,
        beat_num: int | None = None,
        scope: str | None = None,
    ) -> tuple[str, str, int, int | None, str | None]:
        return (ctx.project_id, task_type, int(episode), beat_num, scope)

    def get_task_for_project(
        self,
        ctx: ProjectContext,
        task_type: str,
        episode: int,
        *,
        beat_num: int | None = None,
        scope: str | None = None,
    ) -> TaskState | None:
        return self.tasks.get(self._key(ctx, task_type, episode, beat_num, scope))

    def list_tasks_for_project(self, ctx: ProjectContext) -> list[TaskState]:
        return [t for (pid, *_), t in self.tasks.items() if pid == ctx.project_id]

    async def enqueue_project_task(
        self,
        ctx: ProjectContext,
        *,
        task_type: str,
        product_surface: str,
        queue_kind: str = "default",
        episode: int = 0,
        beat_num: int | None = None,
        scope: str | None = None,
        payload: dict[str, Any] | None = None,
    ):
        _ = (product_surface, queue_kind)
        key = self._key(ctx, task_type, episode, beat_num, scope)
        existing = self.tasks.get(key)
        if existing is not None and existing.status in {"submitting", "queued", "running"}:
            return SimpleNamespace(task_state=existing, backend="inline", queue=None)

        self.counter += 1
        state = TaskState(
            task_id=f"task-{self.counter}",
            task_type=task_type,
            username=ctx.owner_username,
            project=ctx.project_name,
            episode=int(episode),
            beat_num=beat_num,
            scope=scope,
            status="running",
            progress=0.1,
            project_id=ctx.project_id,
        )
        self.tasks[key] = state
        payload_copy = dict(payload or {})
        self.enqueued.append((ctx.project_id, task_type, int(episode), beat_num, scope, payload_copy))

        handler = self.handlers.get(task_type)

        async def _exec() -> None:
            try:
                if handler is not None:
                    res = handler(ctx, episode, beat_num, scope, payload_copy, state)
                    if asyncio.iscoroutine(res):
                        res = await res
                    if state.status == "cancelled":
                        return
                    if isinstance(res, dict) and res.get("__fail__"):
                        state.status = "failed"
                        state.error = str(res["__fail__"])
                        return
                    state.result = res if isinstance(res, dict) else {}
                else:
                    await asyncio.sleep(0.01)
                    if state.status == "cancelled":
                        return
                    state.result = {}
                state.status = "completed"
                state.progress = 1.0
            except Exception as exc:  # noqa: BLE001
                state.status = "failed"
                state.error = str(exc)

        bg_task = asyncio.create_task(_exec())
        self._bg.add(bg_task)
        bg_task.add_done_callback(self._bg.discard)
        return SimpleNamespace(task_state=state, backend="inline", queue=None)

    async def cancel_project_task(self, ctx: ProjectContext, task_state: TaskState, **_kwargs):
        key = self._key(
            ctx,
            task_state.task_type,
            int(task_state.episode or 0),
            task_state.beat_num,
            task_state.scope,
        )
        task_state.status = "cancelled"
        self.cancelled_keys.append(key)
        return {"ok": True, "status": "cancelled"}


async def _wait_for_status(
    coordinator: EpisodeAutopilotCoordinator,
    ctx: ProjectContext,
    episode: int,
    expected: set[str],
    *,
    timeout: float = 5.0,
):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        run = coordinator.get_run(ctx, episode)
        if run is not None and run.status in expected:
            return run
        await asyncio.sleep(0.01)
    run = coordinator.get_run(ctx, episode)
    raise AssertionError(
        f"Timed out waiting for ep={episode} status in {expected}; got {run.status if run else None} "
        f"(step={run.current_step if run else None}, err={run.last_error if run else None})"
    )


def test_error_classification_distinguishes_retryable_isolated_and_fatal() -> None:
    cat, retryable, fatal = classify_autopilot_error("HTTP 429 Too Many Requests")
    assert cat == "transient_upstream" and retryable is True and fatal is False

    cat, retryable, fatal = classify_autopilot_error("SensitiveContent detected in prompt")
    assert cat == "content_policy" and retryable is False and fatal is False

    cat, retryable, fatal = classify_autopilot_error("insufficient_credits: 积分不足")
    assert cat == "insufficient_credits" and retryable is False and fatal is True

    cat, retryable, fatal = classify_autopilot_error("HTTP 401 unauthorized")
    assert cat == "authentication" and retryable is False and fatal is True


@pytest.mark.asyncio
async def test_two_stage_semi_managed_lifecycle_pauses_at_asset_review_then_completes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    ctx = _make_ctx(tmp_path, "proj-alpha", "alpha")
    (ctx.output_dir / "novel.txt").write_text("第一章 惊雷", encoding="utf-8")
    sql = _FakeProjectSQLite(ctx)
    infra = _FakeTaskInfra()

    def _on_build_chars(_ctx, _ep, _b, _s, _p, _st):
        sql.characters.append(_FakeCharacter(name="林默", face_prompt="sharp eyes"))

    def _on_build_eps(_ctx, _ep, _b, _s, _p, _st):
        sql.episodes[1] = _FakeEpisode(number=1, title="第一集")

    def _on_build_scenes(_ctx, _ep, _b, _s, _p, _st):
        sql.scenes.append(_FakeScene(name="老宅客厅"))

    def _on_identity_planner(_ctx, ep, _b, _s, _p, _st):
        sql.characters[0].identities.append(
            _FakeIdentity(identity_id="林默_风衣", identity_name="风衣")
        )
        sql.episodes[ep].identity_ids = ["林默_风衣"]

    def _on_scene_planner(_ctx, ep, _b, _s, _p, _st):
        sql.episodes[ep].scene_menu = [{"scene_id": "老宅客厅", "variant_id": ""}]

    def _on_prop_planner(_ctx, ep, _b, _s, _p, _st):
        sql.props.append(_FakeProp(name="怀表"))
        sql.episodes[ep].prop_menu = [{"prop_id": "怀表", "name": "怀表"}]

    def _on_portrait(c, _ep, _b, _s, payload, _st):
        path = Path(c.output_dir) / "assets" / "characters" / payload["character_name"] / "portrait.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"png")

    def _on_identity_img(c, _ep, _b, _s, payload, _st):
        path = (
            Path(c.output_dir)
            / "assets"
            / "characters"
            / payload["character_name"]
            / "identities"
            / f"{payload['identity_name']}.png"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"png")

    def _on_scene_ref(c, _ep, _b, _s, payload, _st):
        path = Path(c.output_dir) / "assets" / "scenes" / payload["scene_name"] / "master.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"png")

    def _on_prop_ref(c, _ep, _b, _s, payload, _st):
        path = Path(c.output_dir) / "assets" / "props" / payload["prop_name"] / "reference_3view.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"png")

    def _on_script(_c, ep, _b, _s, _p, _st):
        sql.beats[ep] = [
            {"beat_number": 1, "visual_description": "雨夜入屋", "seedance2_config_json": '{"final_prompt": ""}'},
            {"beat_number": 2, "visual_description": "桌上怀表", "seedance2_config_json": '{"final_prompt": ""}'},
        ]

    def _on_prompt_batch(_c, ep, _b, _s, _p, _st):
        for beat in sql.beats[ep]:
            beat["seedance2_config_json"] = '{"final_prompt": "cinematic prompt"}'

    def _on_single_video(c, ep, beat_num, _s, _p, _st):
        vp = Path(c.output_dir) / "videos" / "beats" / f"ep{ep:03d}" / f"beat_{beat_num:02d}.mp4"
        vp.parent.mkdir(parents=True, exist_ok=True)
        vp.write_bytes(b"mp4")
        return {"video_path": str(vp)}

    def _on_compose(c, ep, _b, _s, _p, _st):
        fp = Path(c.output_dir) / "videos" / "episodes" / f"ep{ep:03d}_final.mp4"
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_bytes(b"final-mp4")
        return {"video_path": str(fp)}

    infra.handlers.update(
        {
            "build_characters": _on_build_chars,
            "build_episodes": _on_build_eps,
            "build_scenes": _on_build_scenes,
            "identity_planner": _on_identity_planner,
            "episode_scene_planner": _on_scene_planner,
            "episode_prop_planner": _on_prop_planner,
            "character_portrait": _on_portrait,
            "identity_image": _on_identity_img,
            "scene_reference_asset": _on_scene_ref,
            "prop_reference_asset": _on_prop_ref,
            "script_writer": _on_script,
            "seedance2_prompt_batch": _on_prompt_batch,
            "single_video": _on_single_video,
            "compose_episode": _on_compose,
        }
    )

    async def _fake_dispatch_video(*, ctx, episode_num, beat_num, body, **_kwargs):
        queued = await infra.enqueue_project_task(
            ctx,
            product_surface="mainline",
            task_type="single_video",
            queue_kind="video",
            episode=episode_num,
            beat_num=beat_num,
            payload={"video_backend": body.video_backend},
        )
        return {"ok": True, "task_id": queued.task_state.task_id}

    monkeypatch.setattr(autopilot_mod, "get_task_manager", lambda: infra)
    monkeypatch.setattr("novelvideo.ports.get_task_backend", lambda: infra)
    monkeypatch.setattr(
        EpisodeAutopilotCoordinator,
        "_open_project_sqlite",
        staticmethod(lambda _ctx: asyncio.sleep(0, result=sql)),
    )
    monkeypatch.setattr(
        "novelvideo.api.routes.generation.enqueue_single_beat_video_for_context",
        _fake_dispatch_video,
    )

    coordinator = EpisodeAutopilotCoordinator(
        index=ActiveAutopilotProjectIndex(tmp_path / "index.db"),
        poll_seconds=0.01,
        backpressure_seconds=0.01,
        retry_base_seconds=0.01,
    )

    # Phase 1: should stop cleanly at awaiting_asset_review
    await coordinator.start_episode(
        ctx,
        episode=1,
        options=AutopilotOptions(
            mode="two_stage",
            video_backend="huimeng_seedance-2.0",
        ),
    )
    reviewed = await _wait_for_status(coordinator, ctx, 1, {"awaiting_asset_review"})
    assert reviewed.phase == "review"
    assert reviewed.current_step == "asset_review"
    summary = reviewed.progress["asset_summary"]
    assert summary["portraits"] == {"ready": 1, "total": 1}
    assert summary["identities"] == {"ready": 1, "total": 1}
    assert summary["scenes"] == {"ready": 1, "total": 1}
    assert summary["props"] == {"ready": 1, "total": 1}
    # Script must NOT have run before human approval
    assert not any(item[1] == "script_writer" for item in infra.enqueued)

    # Phase 2: user approves -> script -> prompts -> videos -> compose -> completed
    await coordinator.continue_episode(ctx, episode=1)
    done = await _wait_for_status(coordinator, ctx, 1, {"completed"})
    assert done.phase == "done"
    assert done.current_step == "done"
    assert (ctx.output_dir / "videos" / "episodes" / "ep001_final.mp4").exists()

    enqueued_types = [item[1] for item in infra.enqueued]
    assert enqueued_types.index("character_portrait") < enqueued_types.index("identity_image")
    assert enqueued_types.index("script_writer") < enqueued_types.index("seedance2_prompt_batch")
    assert enqueued_types.index("seedance2_prompt_batch") < enqueued_types.index("single_video")
    assert enqueued_types.index("single_video") < enqueued_types.index("compose_episode")
    await coordinator.shutdown()


@pytest.mark.asyncio
async def test_project_and_episode_isolation_with_concurrent_execution_and_safe_cancel(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    ctx_a = _make_ctx(tmp_path, "proj-A", "alpha")
    ctx_b = _make_ctx(tmp_path, "proj-B", "beta")

    sql_by_project: dict[str, _FakeProjectSQLite] = {}
    for ctx in (ctx_a, ctx_b):
        (ctx.output_dir / "novel.txt").write_text("story", encoding="utf-8")
        sql = _FakeProjectSQLite(ctx)
        sql.characters = [_FakeCharacter(name="主角", face_prompt="hero")]
        sql.scenes = [_FakeScene(name="大厅")]
        sql.episodes = {
            1: _FakeEpisode(number=1, title="EP1", scene_menu=[{"scene_id": "大厅"}], prop_menu=[]),
            2: _FakeEpisode(number=2, title="EP2", scene_menu=[{"scene_id": "大厅"}], prop_menu=[]),
        }
        sql_by_project[ctx.project_id] = sql

    infra = _FakeTaskInfra()
    active_identity_by_project: dict[str, int] = {"proj-A": 0, "proj-B": 0}
    max_identity_by_project: dict[str, int] = {"proj-A": 0, "proj-B": 0}
    shared_portrait_started = asyncio.Event()
    ep1_video_started = asyncio.Event()
    release_ep1_video = asyncio.Event()

    async def _on_identity_planner(c, ep, _b, _s, _p, _st):
        pid = c.project_id
        active_identity_by_project[pid] += 1
        max_identity_by_project[pid] = max(
            max_identity_by_project[pid], active_identity_by_project[pid]
        )
        try:
            await asyncio.sleep(0.04)
            sql = sql_by_project[pid]
            if not sql.characters[0].identities:
                sql.characters[0].identities.append(
                    _FakeIdentity(identity_id="主角_常服", identity_name="常服")
                )
            sql.episodes[ep].identity_ids = ["主角_常服"]
        finally:
            active_identity_by_project[pid] -= 1

    async def _on_portrait(c, _ep, _b, _s, payload, _st):
        shared_portrait_started.set()
        await asyncio.sleep(0.03)
        p = Path(c.output_dir) / "assets" / "characters" / payload["character_name"] / "portrait.png"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"png")

    async def _on_identity_img(c, _ep, _b, _s, payload, _st):
        await asyncio.sleep(0.02)
        p = (
            Path(c.output_dir)
            / "assets"
            / "characters"
            / payload["character_name"]
            / "identities"
            / f"{payload['identity_name']}.png"
        )
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"png")

    async def _on_scene_ref(c, _ep, _b, _s, payload, _st):
        await asyncio.sleep(0.02)
        p = Path(c.output_dir) / "assets" / "scenes" / payload["scene_name"] / "master.png"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"png")

    async def _on_prop_planner(c, ep, _b, _s, _p, _st):
        sql_by_project[c.project_id].episodes[ep].prop_menu = []

    async def _on_single_video(c, ep, beat_num, _s, _p, st):
        if c.project_id == "proj-A" and ep == 1:
            ep1_video_started.set()
            await release_ep1_video.wait()
            if st.status == "cancelled":
                return {}
        vp = Path(c.output_dir) / "videos" / "beats" / f"ep{ep:03d}" / f"beat_{beat_num:02d}.mp4"
        vp.parent.mkdir(parents=True, exist_ok=True)
        vp.write_bytes(b"mp4")
        return {"video_path": str(vp)}

    async def _on_compose(c, ep, _b, _s, _p, _st):
        fp = Path(c.output_dir) / "videos" / "episodes" / f"ep{ep:03d}_final.mp4"
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_bytes(b"final")
        return {"video_path": str(fp)}

    infra.handlers.update(
        {
            "identity_planner": _on_identity_planner,
            "episode_prop_planner": _on_prop_planner,
            "character_portrait": _on_portrait,
            "identity_image": _on_identity_img,
            "scene_reference_asset": _on_scene_ref,
            "single_video": _on_single_video,
            "compose_episode": _on_compose,
        }
    )

    async def _fake_dispatch_video(*, ctx, episode_num, beat_num, **_kwargs):
        queued = await infra.enqueue_project_task(
            ctx,
            product_surface="mainline",
            task_type="single_video",
            queue_kind="video",
            episode=episode_num,
            beat_num=beat_num,
            payload={},
        )
        return {"ok": True, "task_id": queued.task_state.task_id}

    monkeypatch.setattr(autopilot_mod, "get_task_manager", lambda: infra)
    monkeypatch.setattr("novelvideo.ports.get_task_backend", lambda: infra)
    monkeypatch.setattr(
        EpisodeAutopilotCoordinator,
        "_open_project_sqlite",
        staticmethod(lambda c: asyncio.sleep(0, result=sql_by_project[c.project_id])),
    )
    monkeypatch.setattr(
        "novelvideo.api.routes.generation.enqueue_single_beat_video_for_context",
        _fake_dispatch_video,
    )

    coordinator = EpisodeAutopilotCoordinator(
        index=ActiveAutopilotProjectIndex(tmp_path / "index.db"),
        poll_seconds=0.01,
        backpressure_seconds=0.01,
        retry_base_seconds=0.01,
    )

    # Launch EP1+EP2 in Project A and EP1+EP2 in Project B simultaneously!
    await asyncio.gather(
        coordinator.batch_start(ctx_a, episodes=[1, 2]),
        coordinator.batch_start(ctx_b, episodes=[1, 2]),
    )

    # All 4 runs should independently reach awaiting_asset_review
    runs = await asyncio.gather(
        _wait_for_status(coordinator, ctx_a, 1, {"awaiting_asset_review"}),
        _wait_for_status(coordinator, ctx_a, 2, {"awaiting_asset_review"}),
        _wait_for_status(coordinator, ctx_b, 1, {"awaiting_asset_review"}),
        _wait_for_status(coordinator, ctx_b, 2, {"awaiting_asset_review"}),
    )
    assert all(r.status == "awaiting_asset_review" for r in runs)

    # Verify per-project DB physical isolation
    assert (ctx_a.state_dir / "autopilot_runs.db").exists()
    assert (ctx_b.state_dir / "autopilot_runs.db").exists()
    assert len(ProjectAutopilotStore(ctx_a.state_dir).list_project("proj-A")) == 2
    assert len(ProjectAutopilotStore(ctx_a.state_dir).list_project("proj-B")) == 0

    # Verify identity_planner was serialized within each project (no lost-update race)
    assert max_identity_by_project["proj-A"] == 1
    assert max_identity_by_project["proj-B"] == 1

    # Verify shared episode=0 portrait was deduplicated per project (1 per project, not 2)
    portraits_a = [e for e in infra.enqueued if e[0] == "proj-A" and e[1] == "character_portrait"]
    portraits_b = [e for e in infra.enqueued if e[0] == "proj-B" and e[1] == "character_portrait"]
    assert len(portraits_a) == 1
    assert len(portraits_b) == 1

    # Now test episode cancellation isolation during Phase 2:
    # Seed scripts for proj-A EP1 & EP2 and proj-B EP1
    for c in (ctx_a, ctx_b):
        for ep in (1, 2):
            sql_by_project[c.project_id].beats[ep] = [
                {"beat_number": 1, "visual_description": "shot", "seedance2_config_json": '{"final_prompt": "p"}'}
            ]
    # Also simulate an active shared episode=0 task on proj-A
    shared_task = TaskState(
        task_id="shared-task-keep-alive",
        task_type="scene_reference_asset",
        username=ctx_a.owner_username,
        project=ctx_a.project_name,
        episode=0,
        scope="shared_keep_alive",
        status="running",
        progress=0.5,
        project_id=ctx_a.project_id,
    )
    infra.tasks[infra._key(ctx_a, "scene_reference_asset", 0, None, "shared_keep_alive")] = shared_task

    await coordinator.continue_episode(ctx_a, episode=1)
    await coordinator.continue_episode(ctx_a, episode=2)
    await coordinator.continue_episode(ctx_b, episode=1)

    await asyncio.wait_for(ep1_video_started.wait(), timeout=2.0)
    await coordinator.cancel_episode(ctx_a, episode=1, cancel_episode_tasks=True)
    release_ep1_video.set()

    # proj-A EP1 is cancelled, while proj-A EP2 and proj-B EP1 complete unaffected!
    ep1_a = coordinator.get_run(ctx_a, 1)
    assert ep1_a is not None and ep1_a.status == "cancelled"
    ep2_a = await _wait_for_status(coordinator, ctx_a, 2, {"completed"})
    ep1_b = await _wait_for_status(coordinator, ctx_b, 1, {"completed"})
    assert ep2_a.status == "completed"
    assert ep1_b.status == "completed"

    # Shared episode=0 task must NOT have been cancelled by cancelling EP1!
    assert shared_task.status == "running"
    assert all(ep == 1 and pid == "proj-A" for (pid, _tt, ep, _b, _s) in infra.cancelled_keys)
    await coordinator.shutdown()


@pytest.mark.asyncio
async def test_backpressure_retry_and_beat_failure_isolation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    ctx = _make_ctx(tmp_path, "proj-retry", "retry")
    sql = _FakeProjectSQLite(ctx)
    sql.episodes[1] = _FakeEpisode(number=1, identity_ids=["A_1"])
    sql.beats[1] = [
        {"beat_number": 1, "visual_description": "b1", "seedance2_config_json": '{"final_prompt": "p1"}'},
        {"beat_number": 2, "visual_description": "b2", "seedance2_config_json": '{"final_prompt": "p2"}'},
        {"beat_number": 3, "visual_description": "b3", "seedance2_config_json": '{"final_prompt": "p3"}'},
    ]
    infra = _FakeTaskInfra()
    attempts: dict[int, int] = {1: 0, 2: 0, 3: 0}
    backpressure_hits = {"count": 0}

    def _on_single_video(c, ep, beat_num, _s, _p, _st):
        attempts[beat_num] += 1
        if beat_num == 1 and attempts[1] == 1:
            return {"__fail__": "HTTP 429 rate limit exceeded"}
        if beat_num == 2:
            return {"__fail__": "SensitiveContent blocked by content policy"}
        vp = Path(c.output_dir) / "videos" / "beats" / f"ep{ep:03d}" / f"beat_{beat_num:02d}.mp4"
        vp.parent.mkdir(parents=True, exist_ok=True)
        vp.write_bytes(b"mp4")
        return {"video_path": str(vp)}

    infra.handlers["single_video"] = _on_single_video

    async def _fake_dispatch_video(*, ctx, episode_num, beat_num, **_kwargs):
        if beat_num == 3 and backpressure_hits["count"] < 2:
            backpressure_hits["count"] += 1
            raise ProjectTaskLimitExceeded(ctx.project_id, "video", 10, 10)
        queued = await infra.enqueue_project_task(
            ctx,
            product_surface="mainline",
            task_type="single_video",
            queue_kind="video",
            episode=episode_num,
            beat_num=beat_num,
            payload={},
        )
        return {"ok": True, "task_id": queued.task_state.task_id}

    monkeypatch.setattr(autopilot_mod, "get_task_manager", lambda: infra)
    monkeypatch.setattr("novelvideo.ports.get_task_backend", lambda: infra)
    monkeypatch.setattr(
        EpisodeAutopilotCoordinator,
        "_open_project_sqlite",
        staticmethod(lambda _c: asyncio.sleep(0, result=sql)),
    )
    monkeypatch.setattr(
        "novelvideo.api.routes.generation.enqueue_single_beat_video_for_context",
        _fake_dispatch_video,
    )

    coordinator = EpisodeAutopilotCoordinator(
        index=ActiveAutopilotProjectIndex(tmp_path / "index.db"),
        poll_seconds=0.01,
        backpressure_seconds=0.01,
        retry_base_seconds=0.01,
    )
    await coordinator.start_episode(
        ctx,
        episode=1,
        options=AutopilotOptions(video_backend="huimeng_seedance_2_0", max_retries=2),
        start_phase="phase2",
    )

    paused = await _wait_for_status(coordinator, ctx, 1, {"paused"})
    # Beat 1 retried transient 429 and succeeded
    assert attempts[1] == 2
    assert (ctx.output_dir / "videos" / "beats" / "ep001" / "beat_01.mp4").exists()
    # Beat 2 hit content_policy -> did NOT retry -> isolated
    assert attempts[2] == 1
    # Beat 3 hit lane backpressure twice -> waited and succeeded!
    assert backpressure_hits["count"] == 2
    assert attempts[3] == 1
    assert (ctx.output_dir / "videos" / "beats" / "ep001" / "beat_03.mp4").exists()
    # Compose was NOT run because Beat 2 failed
    assert not any(e[1] == "compose_episode" for e in infra.enqueued)
    failed_items = paused.progress.get("failed_items") or []
    assert len(failed_items) == 1
    assert failed_items[0]["beat_number"] == 2
    assert failed_items[0]["category"] == "content_policy"
    await coordinator.shutdown()


@pytest.mark.asyncio
async def test_tail_anchor_chain_binds_last_frame_sequentially_across_beats(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    ctx = _make_ctx(tmp_path, "proj-chain", "chain")
    sql = _FakeProjectSQLite(ctx)
    sql.episodes[1] = _FakeEpisode(number=1, identity_ids=["A_1"])
    sql.beats[1] = [
        {"beat_number": 1, "visual_description": "b1", "seedance2_config_json": '{"final_prompt": "p1"}'},
        {"beat_number": 2, "visual_description": "b2", "seedance2_config_json": '{"final_prompt": "p2"}'},
        {"beat_number": 3, "visual_description": "b3", "seedance2_config_json": '{"final_prompt": "p3"}'},
    ]
    infra = _FakeTaskInfra()
    dispatch_log: list[tuple[int, bool | None, bool]] = []

    def _on_single_video(c, ep, beat_num, _s, payload, _st):
        vp = Path(c.output_dir) / "videos" / "beats" / f"ep{ep:03d}" / f"beat_{beat_num:02d}.mp4"
        vp.parent.mkdir(parents=True, exist_ok=True)
        vp.write_bytes(f"mp4-{beat_num}".encode())
        res: dict[str, Any] = {"video_path": str(vp)}
        if payload.get("return_last_frame"):
            lf = (
                Path(c.output_dir)
                / "videos"
                / "beats"
                / f"ep{ep:03d}"
                / "returned_last_frames"
                / f"beat_{beat_num:02d}.png"
            )
            lf.parent.mkdir(parents=True, exist_ok=True)
            lf.write_bytes(f"tail-from-{beat_num}".encode())
            res["last_frame_path"] = str(lf)
        return res

    def _on_compose(c, ep, _b, _s, _p, _st):
        fp = Path(c.output_dir) / "videos" / "episodes" / f"ep{ep:03d}_final.mp4"
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_bytes(b"final")
        return {"video_path": str(fp)}

    infra.handlers["single_video"] = _on_single_video
    infra.handlers["compose_episode"] = _on_compose

    async def _fake_dispatch_video(*, ctx, episode_num, beat_num, body, **_kwargs):
        frame_file = Path(ctx.output_dir) / "frames" / f"ep{episode_num:03d}" / f"beat_{beat_num:02d}.png"
        dispatch_log.append((beat_num, body.return_last_frame, frame_file.exists()))
        queued = await infra.enqueue_project_task(
            ctx,
            product_surface="mainline",
            task_type="single_video",
            queue_kind="video",
            episode=episode_num,
            beat_num=beat_num,
            payload={"return_last_frame": body.return_last_frame},
        )
        return {"ok": True, "task_id": queued.task_state.task_id}

    monkeypatch.setattr(autopilot_mod, "get_task_manager", lambda: infra)
    monkeypatch.setattr("novelvideo.ports.get_task_backend", lambda: infra)
    monkeypatch.setattr(
        EpisodeAutopilotCoordinator,
        "_open_project_sqlite",
        staticmethod(lambda _c: asyncio.sleep(0, result=sql)),
    )
    monkeypatch.setattr(
        "novelvideo.api.routes.generation.enqueue_single_beat_video_for_context",
        _fake_dispatch_video,
    )

    coordinator = EpisodeAutopilotCoordinator(
        index=ActiveAutopilotProjectIndex(tmp_path / "index.db"),
        poll_seconds=0.01,
        backpressure_seconds=0.01,
        retry_base_seconds=0.01,
    )
    await coordinator.start_episode(
        ctx,
        episode=1,
        options=AutopilotOptions(
            video_backend="huimeng_seedance_2_0",
            tail_anchor=True,
        ),
        start_phase="phase2",
    )
    done = await _wait_for_status(coordinator, ctx, 1, {"completed"})
    assert done.status == "completed"

    # Beat 1 & 2 requested return_last_frame=True; Beat 3 (last) requested False.
    # Beat 2 & 3 had their frames/ep001/beat_0N.png bound from the previous beat's tail frame!
    assert dispatch_log == [
        (1, True, False),
        (2, True, True),
        (3, False, True),
    ]
    import json as _json

    assert (ctx.output_dir / "frames" / "ep001" / "beat_02.png").read_bytes() == b"tail-from-1"
    assert (ctx.output_dir / "frames" / "ep001" / "beat_03.png").read_bytes() == b"tail-from-2"
    assert _json.loads(sql.beats[1][1]["seedance2_config_json"])["s9_include_first_frame"] is True
    assert _json.loads(sql.beats[1][2]["seedance2_config_json"])["s9_include_first_frame"] is True
    await coordinator.shutdown()


@pytest.mark.asyncio
async def test_autopilot_rest_api_routes_project_isolation_and_batch_start(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from novelvideo.api.auth import get_api_user
    from novelvideo.api.routes import autopilot as autopilot_routes

    ctx_a = _make_ctx(tmp_path, "proj-api-A", "api-a")
    ctx_b = _make_ctx(tmp_path, "proj-api-B", "api-b")
    ctx_map = {"proj-api-A": ctx_a, "proj-api-B": ctx_b}

    coordinator = EpisodeAutopilotCoordinator(
        index=ActiveAutopilotProjectIndex(tmp_path / "api-index.db"),
        poll_seconds=0.05,
        backpressure_seconds=0.05,
        retry_base_seconds=0.05,
    )
    # Prevent actual background task execution in pure route contract test
    monkeypatch.setattr(coordinator, "_ensure_worker", lambda _run: None)
    monkeypatch.setattr(autopilot_routes, "episode_autopilot_coordinator", coordinator)

    async def _fake_resolve(project: str, _user: dict, required_role: str = "viewer"):
        _ = required_role
        c = ctx_map[project]
        return SimpleNamespace(
            ctx=c,
            username=c.owner_username,
            project_name=c.project_name,
            output_dir=str(c.output_dir),
            state_dir=str(c.state_dir),
            project_dir=c.output_dir,
        )

    monkeypatch.setattr(autopilot_routes, "resolve_project_scope", _fake_resolve)

    app = FastAPI()
    app.include_router(autopilot_routes.router, prefix="/api/v1")
    app.dependency_overrides[get_api_user] = lambda: {"username": "alice", "id": "user-alice"}

    client = TestClient(app)

    # Batch-start EP1 & EP2 on proj-api-A
    resp = client.post(
        "/api/v1/projects/proj-api-A/autopilot/batch-start",
        json={"episodes": [1, 2], "mode": "two_stage", "tail_anchor": True},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["data"]["count"] == 2
    assert [r["episode"] for r in body["data"]["runs"]] == [1, 2]
    assert all(r["status"] == "phase1_running" for r in body["data"]["runs"])

    # proj-api-B has zero runs (project isolation!)
    resp_b = client.get("/api/v1/projects/proj-api-B/autopilot/runs")
    assert resp_b.status_code == 200
    assert resp_b.json()["data"]["runs"] == []

    # Pause EP1 on proj-api-A; EP2 stays phase1_running (episode isolation!)
    pause_res = client.post("/api/v1/projects/proj-api-A/autopilot/episodes/1/pause")
    assert pause_res.status_code == 200
    assert pause_res.json()["data"]["status"] == "paused"

    ep2_res = client.get("/api/v1/projects/proj-api-A/autopilot/episodes/2")
    assert ep2_res.status_code == 200
    assert ep2_res.json()["data"]["status"] == "phase1_running"

    # Continue EP1 resumes cleanly
    cont_res = client.post(
        "/api/v1/projects/proj-api-A/autopilot/episodes/1/continue",
        json={"tail_anchor": False},
    )
    assert cont_res.status_code == 200
    assert cont_res.json()["data"]["status"] == "phase1_running"
    assert cont_res.json()["data"]["options"]["tail_anchor"] is False

    # Cancel EP1 cleanly
    cancel_res = client.post(
        "/api/v1/projects/proj-api-A/autopilot/episodes/1/cancel",
        json={"cancel_episode_tasks": False},
    )
    assert cancel_res.status_code == 200
    assert cancel_res.json()["data"]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_batch_generate_episode_videos_parallel_and_tail_anchor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from novelvideo.api.auth import get_api_user
    from novelvideo.api.routes import generation as gen_routes

    ctx = _make_ctx(tmp_path, "proj-batch-vid", "batch-vid")
    sql = _FakeProjectSQLite(ctx)
    sql.beats[1] = [
        {"beat_number": 1, "visual_description": "b1"},
        {"beat_number": 2, "visual_description": "b2"},
        {"beat_number": 3, "visual_description": "b3"},
    ]
    # Beat 1 already exists -> should be skipped when only_missing=True
    existing_v1 = ctx.output_dir / "videos" / "beats" / "ep001" / "beat_01.mp4"
    existing_v1.parent.mkdir(parents=True, exist_ok=True)
    existing_v1.write_bytes(b"existing")

    dispatched: list[int] = []

    async def _fake_resolve(project: str, _user: dict, required_role: str = "editor"):
        _ = (project, required_role)
        return SimpleNamespace(
            ctx=ctx,
            username=ctx.owner_username,
            project_name=ctx.project_name,
            output_dir=str(ctx.output_dir),
            state_dir=str(ctx.state_dir),
            project_dir=ctx.output_dir,
        )

    async def _fake_dispatch(*, beat_num: int, **_kwargs):
        if beat_num == 3:
            raise ProjectTaskLimitExceeded(ctx.project_id, "video", 10, 10)
        dispatched.append(beat_num)
        return {"ok": True, "task_id": f"t-{beat_num}", "task_key": f"k-{beat_num}"}

    monkeypatch.setattr(gen_routes, "_resolve_generation_project", _fake_resolve)
    monkeypatch.setattr(
        gen_routes,
        "make_sqlite_store_for_context",
        lambda _c: asyncio.sleep(0, result=sql),
    )
    monkeypatch.setattr(gen_routes, "enqueue_single_beat_video_for_context", _fake_dispatch)

    app = FastAPI()
    app.include_router(gen_routes.router, prefix="/api/v1")
    app.dependency_overrides[get_api_user] = lambda: {"username": "alice", "id": "user-alice"}
    client = TestClient(app)

    # Parallel batch mode: Beat 1 skipped, Beat 2 enqueued, Beat 3 deferred cleanly on lane limit
    res = client.post(
        "/api/v1/projects/proj-batch-vid/episodes/1/videos/batch-generate",
        json={"only_missing": True, "video_backend": "huimeng_seedance-2.0"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["data"]["skipped_beats"] == [1]
    assert [e["beat_number"] for e in body["data"]["enqueued"]] == [2]
    assert body["data"]["deferred_beats"] == [3]
    assert dispatched == [2]

    # Tail-anchor chain mode: starts server-side autopilot chain for missing beats [2, 3]
    coordinator = EpisodeAutopilotCoordinator(
        index=ActiveAutopilotProjectIndex(tmp_path / "batch-index.db"),
    )
    monkeypatch.setattr(coordinator, "_ensure_worker", lambda _run: None)
    monkeypatch.setattr(autopilot_mod, "episode_autopilot_coordinator", coordinator)

    chain_res = client.post(
        "/api/v1/projects/proj-batch-vid/episodes/1/videos/batch-generate",
        json={
            "only_missing": True,
            "tail_anchor": True,
            "video_backend": "huimeng_seedance-2.0",
        },
    )
    assert chain_res.status_code == 200
    chain_body = chain_res.json()
    assert chain_body["ok"] is True
    assert chain_body["data"]["tail_anchor_chain"] is True
    assert chain_body["data"]["target_beats"] == [2, 3]
    assert chain_body["data"]["autopilot_run"]["status"] == "phase2_running"
    assert chain_body["data"]["autopilot_run"]["options"]["tail_anchor"] is True
    assert chain_body["data"]["autopilot_run"]["options"]["skip_compose"] is True


