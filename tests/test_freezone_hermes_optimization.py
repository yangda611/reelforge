from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from novelvideo.chat import hermes_sdk, runtime_event_evidence
from novelvideo.chat.runtime_port import ChatBackendEvent
from novelvideo.freezone.agent_workflows import catalog

_FREEZONE_PLUGIN_PATH = Path(__file__).resolve().parents[1] / ".hermes" / "plugins" / "freezone" / "__init__.py"


def _load_freezone_plugin():
    sentinel = object()
    previous = {name: sys.modules.get(name, sentinel) for name in ("tools", "tools.registry")}
    tools_module = types.ModuleType("tools")
    registry_module = types.ModuleType("tools.registry")
    registry_module.tool_error = lambda value: json.dumps(value, ensure_ascii=False)
    registry_module.tool_result = lambda value: json.dumps(value, ensure_ascii=False)
    sys.modules["tools"] = tools_module
    sys.modules["tools.registry"] = registry_module
    try:
        spec = importlib.util.spec_from_file_location("freezone_plugin_test_opt", _FREEZONE_PLUGIN_PATH)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        for name, value in previous.items():
            if value is sentinel:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


def _thread(tmp_path: Path) -> hermes_sdk.HermesSdkThread:
    return hermes_sdk.HermesSdkThread(
        cli_path=tmp_path / "hermes",
        cwd=tmp_path,
        env={},
        model=None,
        username="local",
        session_id="session-opt",
    )


def test_unwrap_hermes_tool_call_handles_single_batch_and_string_arguments() -> None:
    name, args = hermes_sdk._unwrap_hermes_tool_call(
        "tool_call",
        {
            "name": "freezone_organize_canvas",
            "arguments": json.dumps({"layout_mode": "auto", "categories": [{"label": "角色", "node_ids": ["n1"]}]}),
        },
    )
    assert name == "freezone_organize_canvas"
    assert args == {"layout_mode": "auto", "categories": [{"label": "角色", "node_ids": ["n1"]}]}

    single_batch_name, single_batch_args = hermes_sdk._unwrap_hermes_tool_call(
        "tool_call",
        {
            "calls": [
                {"name": "freezone_layout_nodes", "arguments": {"mode": "auto", "node_ids": []}},
            ],
        },
    )
    assert single_batch_name == "freezone_layout_nodes"
    assert single_batch_args == {"mode": "auto", "node_ids": []}

    multi_names = hermes_sdk._batch_tool_call_names(
        "tool_call",
        {
            "calls": [
                {"name": "freezone_get_canvas_ontology", "arguments": {}},
                {"name": "freezone_organize_canvas", "arguments": {"layout_mode": "auto"}},
            ],
        },
    )
    assert multi_names == ("freezone_get_canvas_ontology", "freezone_organize_canvas")


def test_translate_notification_unwraps_tool_call_and_update(tmp_path: Path) -> None:
    thread = _thread(tmp_path)

    started = thread._translate_notification(
        {
            "method": "session/update",
            "params": {
                "update": {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "call-wrap-1",
                    "title": "tool_call",
                    "rawInput": {
                        "name": "freezone_organize_canvas",
                        "arguments": {"layout_mode": "auto"},
                    },
                }
            },
        },
        "turn-1",
    )
    assert started is not None
    assert started.type == "tool_started"
    assert started.name == "freezone_organize_canvas"
    assert started.input == {"layout_mode": "auto"}
    assert thread._tool_names_by_call_id["call-wrap-1"] == "freezone_organize_canvas"

    finished = thread._translate_notification(
        {
            "method": "session/update",
            "params": {
                "update": {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "call-wrap-1",
                    "status": "completed",
                    "rawOutput": {"ok": True, "command_count": 1},
                }
            },
        },
        "turn-1",
    )
    assert finished is not None
    assert finished.type == "tool_updated"
    assert finished.name == "freezone_organize_canvas"
    assert finished.output == {"ok": True, "command_count": 1}


def test_turn_tool_call_guard_ignores_discovery_and_tracks_unwrapped_tool_call() -> None:
    guard = hermes_sdk._TurnToolCallGuard()
    for index in range(25):
        stop = guard.observe(
            ChatBackendEvent(
                type="tool_started",
                thread_id="t1",
                turn_id="turn1",
                name="tool_search",
                input={"query": f"freezone {index}"},
            )
        )
        assert stop is None
    assert guard.total == 0

    stop = guard.observe(
        ChatBackendEvent(
            type="tool_started",
            thread_id="t1",
            turn_id="turn1",
            name="tool_call",
            input={"name": "freezone_organize_canvas", "arguments": {"layout_mode": "auto"}},
        )
    )
    assert stop is None
    assert guard.total == 1


@pytest.mark.asyncio
async def test_stream_turn_extends_idle_deadline_for_wrapped_canvas_write_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    thread = _thread(tmp_path)
    recorded_timeouts: list[float] = []
    lines = iter(
        [
            (
                json.dumps(
                    {
                        "method": "session/update",
                        "params": {
                            "update": {
                                "sessionUpdate": "tool_call",
                                "toolCallId": "call-1",
                                "title": "tool_call",
                                "rawInput": {
                                    "calls": [
                                        {"name": "freezone_organize_canvas", "arguments": {"layout_mode": "auto"}},
                                    ]
                                },
                            }
                        },
                    }
                )
                + "\n"
            ).encode("utf-8"),
            (json.dumps({"id": 9, "result": {"stopReason": "end_turn"}}) + "\n").encode("utf-8"),
        ]
    )

    async def fake_prepare():
        return None

    async def fake_send(method: str, params: dict):
        if method == "session/prompt":
            return 9
        raise AssertionError(f"Unexpected request method: {method}")

    async def fake_readline():
        return next(lines)

    orig_wait_for = asyncio.wait_for

    async def spy_wait_for(awaitable, timeout=None):
        recorded_timeouts.append(float(timeout))
        return await orig_wait_for(awaitable, timeout=timeout)

    monkeypatch.setattr(hermes_sdk, "STREAM_IDLE_TIMEOUT", 10.0)
    monkeypatch.setattr(hermes_sdk, "STREAM_TOTAL_TIMEOUT", 2000.0)
    monkeypatch.setattr(hermes_sdk, "CANVAS_COMMAND_RESULT_TIMEOUT", 600.0)
    monkeypatch.setattr(thread, "_prepare", fake_prepare)
    monkeypatch.setattr(thread, "_send", fake_send)
    monkeypatch.setattr(asyncio, "wait_for", spy_wait_for)
    thread._session_id = "session-opt"
    thread._proc = SimpleNamespace(
        returncode=None,
        stdout=SimpleNamespace(readline=fake_readline),
    )

    events = [event async for event in thread._stream_turn("整理画布所有节点")]
    assert any(ev.type == "tool_started" and ev.name == "freezone_organize_canvas" for ev in events)
    assert events[-1].type == "complete"
    assert len(recorded_timeouts) >= 2
    assert recorded_timeouts[0] <= 15.0
    assert recorded_timeouts[1] > 400.0


def test_runtime_event_evidence_unwraps_hermes_tool_call() -> None:
    ev = SimpleNamespace(
        type="tool_started",
        name="tool_call",
        input={"name": "freezone_organize_canvas", "arguments": '{"layout_mode": "auto"}'},
    )
    assert runtime_event_evidence._codex_freezone_tool_name(ev) == "freezone_organize_canvas"
    assert runtime_event_evidence._codex_freezone_tool_arguments(ev) == {"layout_mode": "auto"}


def test_freezone_plugin_organize_layout_auto_and_group_client_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin = _load_freezone_plugin()
    monkeypatch.setattr(
        plugin,
        "_emit_canvas_commands",
        lambda project, canvas, commands, **_kwargs: json.dumps(
            {"ok": True, "project_id": project, "canvas_id": canvas, "commands": commands},
            ensure_ascii=False,
        ),
    )

    organized = json.loads(
        plugin._handle_organize_canvas(
            {
                "project_id": "proj-1",
                "canvas_id": "user_canvas",
                "categories": [{"label": "单节点分类", "node_ids": ["n1"]}],
                "ungroup_node_ids": ["n2"],
                "layout_mode": "auto",
            }
        )
    )
    assert organized["ok"] is True
    assert organized["commands"][0]["type"] == "organize_canvas"
    assert organized["commands"][0]["layout_mode"] == "auto"
    assert organized["commands"][0]["prune_empty_groups"] is True

    auto_layout = json.loads(
        plugin._handle_layout_nodes(
            {"project_id": "proj-1", "canvas_id": "user_canvas", "mode": "auto", "node_ids": []}
        )
    )
    assert auto_layout["ok"] is True
    assert auto_layout["commands"][0]["mode"] == "auto"

    grouped = json.loads(
        plugin._handle_group_nodes(
            {
                "project_id": "proj-1",
                "canvas_id": "user_canvas",
                "node_ids": ["n1", "n2"],
                "label": "主角组",
                "client_id": "grp-hero",
            }
        )
    )
    assert grouped["ok"] is True
    assert grouped["commands"][0]["client_id"] == "grp-hero"


def test_standard_planner_unit_limit_supports_16_units_and_rejects_25(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _build_plan(unit_count: int) -> dict:
        nodes = [
            {
                "id": f"shot-{i}",
                "node_type": "videoNode",
                "data": {"prompt": f"shot {i}"},
            }
            for i in range(unit_count)
        ]
        return {"nodes": nodes, "edges": []}

    stage = {"id": "video", "node_type": "videoNode", "role": "unit", "required": True}
    monkeypatch.setattr(catalog, "standard_skill_stages", lambda _skill_id: [stage])
    monkeypatch.setattr(catalog, "_node_template_stage", lambda _node, _stages, _counts: stage)
    monkeypatch.setattr(catalog, "skill_stage_blockers", lambda _skill_id, _nodes, _edges: [])

    assert catalog._MAX_STANDARD_PLANNER_UNITS == 24
    res_16 = catalog.template_isomorphism("short-drama-quick", _build_plan(16))
    assert res_16.get("reason") != "unit_count:16"
    assert res_16["isomorphic"] is True

    res_25 = catalog.template_isomorphism("short-drama-quick", _build_plan(25))
    assert res_25 == {"isomorphic": False, "reason": "unit_count:25"}


def test_hermes_freezone_harness_includes_screenplay_breakdown_and_zero_discovery_cheat_sheet() -> None:
    from novelvideo.chat import service as chat_service

    repo_root = Path(__file__).resolve().parents[1]
    skill_md = (repo_root / ".hermes/skills/freezone/SKILL.md").read_text(encoding="utf-8")
    breakdown_guide = (
        repo_root / ".hermes/skills/freezone/references/screenplay-breakdown-guide.md"
    ).read_text(encoding="utf-8")

    assert "freezone_organize_canvas" in skill_md
    assert "screenplay-breakdown-guide.md" in skill_md
    assert "严禁对其先跑 `tool_search`" in skill_md
    assert "<= 30" in breakdown_guide
    assert "零孤立资产自检" in breakdown_guide
    assert "allReference" in breakdown_guide
    assert "text-to-image-video" in chat_service._FREEZONE_CANVAS_ASSISTANT_INSTRUCTIONS
    assert "Do NOT call tool_search, tool_describe" in chat_service._FREEZONE_CANVAS_ASSISTANT_INSTRUCTIONS
    assert "durationSec<=30" in chat_service._FREEZONE_CANVAS_ASSISTANT_INSTRUCTIONS
    assert "media_input_for" in chat_service._FREEZONE_CANVAS_ASSISTANT_INSTRUCTIONS
    assert "<=18-node" in chat_service._FREEZONE_CANVAS_ASSISTANT_INSTRUCTIONS
    assert "media_input_for" in breakdown_guide
    assert "50+" in breakdown_guide


def test_begin_agent_product_generation_rejects_mismatched_catalog_skill_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin = _load_freezone_plugin()
    monkeypatch.setattr(plugin, "_workflow_draft_scope", lambda args: ("p1", "c1", None))
    monkeypatch.setattr(
        plugin,
        "get_workflow_skill",
        lambda args: {
            "ok": True,
            "skill": {"id": args.get("skill_id"), "version": "1.0.0"},
            "available_recipes": [
                {"id": "storyboard-shot-plan-text"},
                {"id": "general-image"},
                {"id": "storyboard-image-to-video-motion-prompt"},
            ],
        },
    )
    monkeypatch.setattr(
        plugin,
        "_request",
        lambda *_args, **_kwargs: pytest.fail("mismatched catalog version must not reserve an operation"),
    )

    rejected = json.loads(
        plugin._handle_begin_agent_product_generation(
            {
                "product_kind": "workflow_result",
                "generation_session_id": "s1",
                "skill_id": "imported-2a809a9f2a4b",
                "skill_version": "1",
                "normalized_inputs": {"user_goal": "expand act 2"},
            }
        )
    )
    assert rejected["ok"] is False
    assert rejected["error"] == "workflow_result_skill_version_mismatch"
    assert rejected["actual_skill_version"] == "1.0.0"
    assert rejected["expected_artifact_id"] == "imported-2a809a9f2a4b@1.0.0"
    assert "storyboard-image-to-video-motion-prompt" in rejected["available_recipe_ids"]

