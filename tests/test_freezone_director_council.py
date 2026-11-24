from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from importlib import import_module
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI


ROLES = {"lead", "continuity", "performance", "staging", "camera", "prompt"}
SHOTS = [
    {
        "shot_id": "1", "segment": 1, "duration_ms": 2500,
        "camera": "Locked medium shot", "action": "She stops turning the key",
        "dialogue": ["Wait."], "sound": "Key clicks", "entry": "Key in left hand",
        "exit": "Left hand still on key",
    },
    {
        "shot_id": "2", "segment": 1, "duration_ms": 1500,
        "camera": "Close shot from the same side", "action": "She lowers her gaze",
        "dialogue": [], "sound": "Room tone", "entry": "Left hand still on key",
        "exit": "Eyes on key",
    },
]


class ScriptedProvider:
    def __init__(self, *, shots=None, omit_review=False, issue=None):
        self.shots = SHOTS if shots is None else shots
        self.omit_review = omit_review
        self.issue = issue
        self.started = {}
        self.calls = []
        self.batches = {
            "brief": {"lead", "continuity"},
            "design": {"performance", "staging", "camera"},
            "review": ROLES,
        }
        self.barriers = {phase: asyncio.Event() for phase in self.batches}

    async def __call__(self, role, phase, data, output_type):
        self.calls.append((role, phase, data))
        if phase in self.batches:
            self.started.setdefault(phase, set()).add(role)
            if self.started[phase] == self.batches[phase]:
                self.barriers[phase].set()
            await asyncio.wait_for(self.barriers[phase].wait(), timeout=2)
        if phase == "merge":
            assert self.barriers["design"].is_set()
            answer = {"title": "The lock", "decision": "Hold on the hesitation", "shots": self.shots}
        elif phase == "compile":
            assert data["preview"]["shots"][0]["duration_ms"] == 2500
            answer = {
                "prompts": [
                    {"kind": "video", "target": 1, "text": "SHOT 1. Wait.\nSHOT 2. Lower gaze."},
                    {"kind": "first_frame", "target": 1, "text": "Medium shot; key in left hand."},
                    {"kind": "first_frame", "target": 2, "text": "Close shot; hand still on key."},
                ],
            }
        elif phase == "repair":
            assert data["findings"][0]["shot_id"] == "1"
            answer = {"prompts": [
                {"kind": "video", "target": 1, "text": "SHOT 1. Left hand hesitates. Wait.\nSHOT 2. Lower gaze."},
                {"kind": "first_frame", "target": 1, "text": "Medium shot; left hand rests on key."},
            ]}
        elif phase in {"review", "recheck"}:
            shot_ids = ["1"] if self.omit_review and role == "continuity" else ["1", "2"]
            if phase == "review" and self.issue and role == "continuity":
                answer = {"checks": [
                    {"shot_id": "1", "status": self.issue, "note": "Show the left hand."},
                    {"shot_id": "2", "status": "pass", "note": "Fits the source"},
                ]}
                return output_type.model_validate(answer)
            if phase == "recheck":
                shot_ids = data["shot_ids"]
            answer = {"checks": [{"shot_id": i, "status": "pass", "note": "Fits the source"} for i in shot_ids]}
        else:
            answer = {"notes": "Approved scope only.", "open_questions": []}
        return output_type.model_validate(answer)


def council():
    return import_module("novelvideo.freezone.director_council")


def brief(api, **overrides):
    return api.SceneBrief(
        request="Plan this scene",
        source='She turns the key and says "Wait."',
        locked_dialogue=["Wait."],
        segment_max_ms=6000,
        deliverables=["video", "first_frame"],
        **overrides,
    )


async def test_preview_runs_the_independent_roles_in_parallel_and_stops_before_prompts():
    api = council()
    provider = ScriptedProvider()
    result = await api.plan_director_council(brief(api), run=provider)

    assert result.storyboard.total_ms == 4000
    assert [(row.start_ms, row.duration_ms) for row in result.storyboard.rows] == [(0, 2500), (2500, 1500)]
    assert "Wait." in result.storyboard.rows[0].focus
    assert "| 分镜号 | 时间 | 镜长 | 画面重点 |" in result.storyboard.markdown
    assert "| 1 | 0–2.5 s | 2.5 s | Locked medium shot" in result.storyboard.markdown
    assert result.storyboard.rows[1].focus.startswith("Close shot")
    assert {role for role, _, _ in provider.calls} == ROLES
    assert not {"compile", "review"} & {phase for _, phase, _ in provider.calls}


async def test_gateway_role_runner_loads_yd_and_returns_typed_output_without_tools(monkeypatch):
    from pydantic_ai.messages import ModelResponse, SystemPromptPart, TextPart, UserPromptPart
    from pydantic_ai.models.function import FunctionModel

    from novelvideo import config

    api = council()
    captured = []

    def respond(messages, info):
        assert not info.function_tools and not info.output_tools
        parts = [part for message in messages for part in message.parts]
        system = " ".join(part.content for part in parts if isinstance(part, SystemPromptPart))
        assert "表演指导" in system and "YD" in system and "## FT1 " in system
        user = next(part.content for part in parts if isinstance(part, UserPromptPart))
        assert json.loads(user) == {"scene": {"source": "She hesitates."}}
        return ModelResponse(parts=[TextPart('{"notes":"Hold the gaze.","open_questions":[]}')])

    def gateway(name, default, **kwargs):
        captured.append((name, kwargs))
        return FunctionModel(respond)

    monkeypatch.setattr(config, "get_newapi_text_pydantic_model", gateway)
    note = await api.gateway_role_runner(
        "performance", "design", {"scene": {"source": "She hesitates."}}, api.DepartmentNote,
    )
    assert note.notes == "Hold the gaze."
    assert captured[0][0] == "FREEZONE_TEXT_WRITER_MODEL"
    assert captured[0][1]["capability"] == "freezone.text.generate"


async def test_compile_reviews_every_shot_and_keeps_image_and_video_prompts_separate():
    api = council()
    provider = ScriptedProvider()
    scene = brief(api)
    preview = await api.plan_director_council(scene, run=provider)
    result = await api.compile_director_council(scene, preview, run=provider)

    assert result.status == "ready"
    assert {(item.role, item.shot_id) for item in result.review} == {
        (role, shot_id) for role in ROLES for shot_id in ("1", "2")
    }
    assert [(item.kind, item.target) for item in result.prompts] == [
        ("video", 1), ("first_frame", 1), ("first_frame", 2)
    ]


async def test_incomplete_review_is_not_a_ready_prompt():
    api = council()
    provider = ScriptedProvider(omit_review=True)
    scene = brief(api)
    preview = await api.plan_director_council(scene, run=provider)
    with pytest.raises(api.DirectorCouncilError, match="coverage"):
        await api.compile_director_council(scene, preview, run=provider)


async def test_repair_is_bounded_to_affected_prompts_and_rechecks_changed_segment():
    api = council()
    provider = ScriptedProvider(issue="revise")
    scene = brief(api)
    preview = await api.plan_director_council(scene, run=provider)
    result = await api.compile_director_council(scene, preview, run=provider)

    assert result.status == "ready"
    assert result.revision == "v002"
    assert len(result.review) == 12
    assert result.prompts[2].text == "Close shot; hand still on key."
    assert {role for role, phase, _ in provider.calls if phase == "recheck"} == ROLES
    assert sum(phase == "repair" for _, phase, _ in provider.calls) == 1


async def test_blocker_does_not_autorepair_or_expose_executable_prompts():
    api = council()
    provider = ScriptedProvider(issue="block")
    scene = brief(api)
    preview = await api.plan_director_council(scene, run=provider)
    result = await api.compile_director_council(scene, preview, run=provider)

    assert result.status == "needs_revision"
    assert result.prompts == []
    assert not any(phase == "repair" for _, phase, _ in provider.calls)


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("dialogue", ["Let's go."], "dialogue"),
        ("duration_ms", 5000, "limit"),
        ("segment", 2, "segment"),
    ],
)
async def test_preview_rejects_storyboard_drift(field, value, error):
    api = council()
    provider = ScriptedProvider(shots=[{**SHOTS[0], field: value}, SHOTS[1]])
    with pytest.raises(api.DirectorCouncilError, match=error):
        await api.plan_director_council(brief(api), run=provider)


@pytest.fixture
async def council_client(tmp_path, monkeypatch):
    from novelvideo.api import egress_binding
    from novelvideo.api.auth import get_api_user
    from novelvideo.api.routes import freezone as routes
    from novelvideo.freezone.paths import canvas_path

    actor = {"id": "alice"}

    async def resolve(project, user, **kwargs):
        ctx = SimpleNamespace(
            requester_user_id=user["id"], project_id=project,
            owner_type="user", state_dir=tmp_path,
        )
        return ctx, "alice", project, tmp_path, str(tmp_path)

    @asynccontextmanager
    async def scope(**kwargs):
        yield

    provider = ScriptedProvider()

    async def run_plan(scene, **kwargs):
        return await council().plan_director_council(scene, run=provider)

    async def run_compile(scene, preview, **kwargs):
        if callback := getattr(provider, "before_compile", None):
            callback()
        return await council().compile_director_council(scene, preview, run=provider)

    for canvas in ("first", "second"):
        path = canvas_path(tmp_path, canvas)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"nodes": [{
            "id": "script",
            "type": "textAnnotationNode",
            "data": {"displayName": "Script", "content": 'She says "Wait."'},
        }], "edges": [], "revision": 1}), encoding="utf-8")

    app = FastAPI()
    app.include_router(routes.router)

    async def authenticate():
        return actor.copy()

    app.dependency_overrides[get_api_user] = authenticate
    monkeypatch.setattr(routes, "_resolve_freezone_project", resolve)
    monkeypatch.setattr(egress_binding, "request_egress_scope", scope)
    monkeypatch.setattr(routes, "plan_director_council", run_plan, raising=False)
    monkeypatch.setattr(routes, "compile_director_council", run_compile, raising=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client, actor, provider, canvas_path(tmp_path, "first")


def council_url(canvas="first"):
    return f"/projects/p/freezone/canvases/{canvas}/director-council"


def preview_request(**overrides):
    return {
        "action": "preview", "request": "Plan", "node_ids": ["script"],
        "conditions_confirmed": True, "locked_dialogue": ["Wait."],
        "deliverables": ["video", "first_frame"], **overrides,
    }


async def test_http_requires_confirmation_and_uses_real_canvas_source(council_client):
    client, _, provider, canvas = council_client
    original = canvas.read_bytes()

    waiting = await client.post(council_url(), json=preview_request(conditions_confirmed=False))
    assert waiting.status_code == 200
    assert waiting.json()["status"] == "needs_confirmation"
    assert provider.calls == []

    planned = await client.post(council_url(), json=preview_request())
    assert planned.status_code == 200
    assert planned.json()["status"] == "preview_ready"
    assert provider.calls[0][2]["scene"]["assets"][0]["content"] == 'She says "Wait."'
    assert canvas.read_bytes() == original


async def test_only_same_actor_and_canvas_can_compile_an_unchanged_preview(council_client):
    client, actor, provider, canvas = council_client
    initial_canvas = canvas.read_bytes()
    preview_id = (await client.post(council_url(), json=preview_request())).json()["preview_id"]
    denied = {"action": "compile", "preview_id": preview_id}
    assert (await client.post(council_url(), json=denied)).status_code == 422

    compile_request = {**denied, "prompts_authorized": True}
    assert (await client.post(council_url("second"), json=compile_request)).status_code == 404
    actor["id"] = "bob"
    assert (await client.post(council_url(), json=compile_request)).status_code == 404
    actor["id"] = "alice"
    assert not any(phase == "compile" for _, phase, _ in provider.calls)

    compiled = await client.post(council_url(), json=compile_request)
    assert compiled.status_code == 200
    assert compiled.json()["status"] == "ready"
    assert len(compiled.json()["review"]) == 12
    assert canvas.read_bytes() == initial_canvas
    assert "revision" in compiled.json()
    await client.post(council_url(), json=compile_request)
    assert sum(phase == "compile" for _, phase, _ in provider.calls) == 1


async def test_http_rejects_missing_and_stale_source_nodes(council_client):
    client, _, provider, canvas = council_client
    missing = await client.post(council_url(), json=preview_request(node_ids=["made-up"]))
    assert missing.status_code == 409
    assert provider.calls == []
    preview_id = (await client.post(council_url(), json=preview_request())).json()["preview_id"]
    updated = json.loads(canvas.read_text(encoding="utf-8"))
    updated["nodes"][0]["data"]["content"] = "A different story."
    canvas.write_text(json.dumps(updated), encoding="utf-8")
    response = await client.post(
        council_url(),
        json={"action": "compile", "preview_id": preview_id, "prompts_authorized": True},
    )
    assert response.status_code == 409
    assert not any(phase == "compile" for _, phase, _ in provider.calls)


async def test_http_rejects_source_changed_during_compile(council_client):
    client, _, provider, canvas = council_client
    initial = canvas.read_bytes()
    planned = await client.post(council_url(), json=preview_request())

    def edit_while_compiling():
        updated = json.loads(initial)
        updated["nodes"][0]["data"]["content"] = "She changed her mind."
        canvas.write_text(json.dumps(updated), encoding="utf-8")

    provider.before_compile = edit_while_compiling
    request = {
        "action": "compile", "preview_id": planned.json()["preview_id"], "prompts_authorized": True,
    }
    assert (await client.post(council_url(), json=request)).status_code == 409
    canvas.write_bytes(initial)
    provider.before_compile = None
    assert (await client.post(council_url(), json=request)).status_code == 200
    assert sum(phase == "compile" for _, phase, _ in provider.calls) == 2


async def test_http_uses_persisted_script_result_instead_of_initial_prompt(council_client):
    client, _, provider, canvas = council_client
    updated = json.loads(canvas.read_bytes())
    updated["nodes"][0].update(type="scriptNode", data={
        "prompt": "An old idea",
        "scriptResult": {"title": "Final script", "rows": [{
            "shot_no": 1, "visual_description": "A hand on the key", "dialogue": "Wait.",
            "reference": "https://private.invalid/media",
        }]},
    })
    canvas.write_text(json.dumps(updated), encoding="utf-8")
    assert (await client.post(council_url(), json=preview_request())).status_code == 200
    content = provider.calls[0][2]["scene"]["assets"][0]["content"]
    assert all(item in content for item in ("Final script", "A hand on the key", "Wait."))
    assert "An old idea" not in content
    assert "private.invalid" not in content


async def test_http_does_not_plan_a_silently_truncated_source(council_client):
    client, _, provider, canvas = council_client
    updated = json.loads(canvas.read_bytes())
    updated["nodes"][0]["data"]["content"] = "story" * 5000
    canvas.write_text(json.dumps(updated), encoding="utf-8")
    response = await client.post(council_url(), json=preview_request())
    assert response.status_code == 409
    assert "24000" in response.json()["detail"]
    assert provider.calls == []
