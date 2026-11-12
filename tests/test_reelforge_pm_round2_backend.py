"""Regression tests for the ReelForge round-2 PM review, backend half.

Covers P0-1 (gateway probe), P1-5 (assistant runtime status), P1-7
(gateway media catalog), P2-3 (no upstream OSS defaults) and P2-5
(terminal failure status text). P1-4 (Unicode project names) is covered in
tests/test_project_name_validation.py.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response

from novelvideo import config
from novelvideo.api.auth import get_api_user
from novelvideo.api.routes import assistant as assistant_routes
from novelvideo.api.routes import model_gateway
from novelvideo.model_gateway_settings import (
    classify_gateway_media_model,
    probe_model_gateway,
    save_official_newapi_key,
)
from novelvideo.project_context import ProjectContext
from novelvideo.shared import runtime_env
from novelvideo.task_state import TaskStateManager


def _isolate_settings_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(config, "STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(runtime_env, "load_project_dotenv", lambda override=False: None)
    monkeypatch.setenv("ST_EDITION", "ce")
    for key in (
        "ST_CONTROL_PLANE_DSN",
        "MODEL_GATEWAY_MODE",
        "MODEL_GATEWAY_RUNTIME_VERSION",
        "NEWAPI_API_KEY",
        "NEWAPI_BASE_URL",
    ):
        monkeypatch.delenv(key, raising=False)
    # This sandbox's no_proxy carries bracketed IPv6 entries that httpx's
    # proxy-pattern parser rejects at Client construction, which would mask
    # every respx-mocked probe/catalog test as an environment failure.
    # The probe logic itself is proxy-agnostic; use a plain no_proxy here.
    for key in (
        "http_proxy",
        "https_proxy",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "all_proxy",
        "ALL_PROXY",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("no_proxy", "localhost,127.0.0.1")
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1")


def _gateway_client() -> TestClient:
    app = FastAPI()
    app.include_router(model_gateway.router)
    return TestClient(app, raise_server_exceptions=False)


def _save_third_party_gateway() -> None:
    save_official_newapi_key(
        api_key="sk-test-gateway-key",
        base_url="https://gw.example.test",
        activate=True,
    )
    from novelvideo.model_gateway_settings import save_official_text_model

    save_official_text_model("gpt-6-astra")


def test_probe_unconfigured_when_no_gateway(monkeypatch, tmp_path):
    _isolate_settings_db(monkeypatch, tmp_path)
    client = _gateway_client()

    response = client.get("/model-gateway/probe")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["status"] == "unconfigured"
    assert data["model"] is None
    assert "未配置" in data["message"]


def test_probe_ok_with_valid_key(monkeypatch, tmp_path):
    _isolate_settings_db(monkeypatch, tmp_path)
    _save_third_party_gateway()
    client = _gateway_client()

    with respx.mock:
        respx.get("https://gw.example.test/v1/models").mock(
            return_value=Response(
                200,
                json={"data": [{"id": "gpt-6-astra"}, {"id": "gpt-image-2.5"}]},
            )
        )
        response = client.get("/model-gateway/probe")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["status"] == "ok"
    assert data["model"] == "gpt-6-astra"
    assert "正常" in data["message"]


def test_probe_auth_invalid_on_401(monkeypatch, tmp_path):
    _isolate_settings_db(monkeypatch, tmp_path)
    _save_third_party_gateway()
    client = _gateway_client()

    with respx.mock:
        respx.get("https://gw.example.test/v1/models").mock(
            return_value=Response(401, json={"error": {"message": "Invalid token"}})
        )
        response = client.get("/model-gateway/probe")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["status"] == "auth_invalid"
    assert data["model"] is None
    assert "失效" in data["message"]


def test_probe_unreachable_on_network_error(monkeypatch, tmp_path):
    _isolate_settings_db(monkeypatch, tmp_path)
    _save_third_party_gateway()
    client = _gateway_client()

    with respx.mock:
        respx.get("https://gw.example.test/v1/models").mock(
            side_effect=ConnectionError("boom")
        )
        response = client.get("/model-gateway/probe")

    assert response.status_code == 200
    assert response.json()["data"]["status"] == "unreachable"


def test_probe_model_gateway_never_raises():
    # Direct unit check: timeouts / bad payloads map to statuses, not raises.
    assert probe_model_gateway("", "")["status"] == "unconfigured"
    assert probe_model_gateway("https://127.0.0.1:1", "sk-x", timeout_seconds=0.1)[
        "status"
    ] == "unreachable"


def test_media_catalog_from_third_party_gateway(monkeypatch, tmp_path):
    _isolate_settings_db(monkeypatch, tmp_path)
    _save_third_party_gateway()
    client = _gateway_client()

    with respx.mock:
        respx.get("https://gw.example.test/v1/models").mock(
            return_value=Response(
                200,
                json={
                    "data": [
                        {"id": "gpt-6-astra"},
                        {"id": "gpt-image-2.5-flare"},
                        {"id": "seedance-2.0"},
                        {"id": "index-tts-2"},
                        {"id": "text-embedding-3-small"},
                    ]
                },
            )
        )
        response = client.get("/model-gateway/official/media-catalog")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["source"] == "gateway"
    assert data["catalogLabel"] == "当前网关模型（来自 gw.example.test）"
    assert data["fallback"] is False
    ids = {entry["id"]: entry["mediaType"] for entry in data["models"]}
    assert ids == {
        "gpt-image-2.5-flare": "image",
        "seedance-2.0": "video",
        "index-tts-2": "audio",
    }
    assert data["modelCount"] == 3


def test_media_catalog_falls_back_to_bundled_when_gateway_fails(
    monkeypatch, tmp_path
):
    _isolate_settings_db(monkeypatch, tmp_path)
    _save_third_party_gateway()
    client = _gateway_client()

    with respx.mock:
        respx.get("https://gw.example.test/v1/models").mock(
            return_value=Response(401, json={"error": {"message": "Invalid token"}})
        )
        response = client.get("/model-gateway/official/media-catalog")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["source"] == "bundled"
    assert data["catalogLabel"] == "官方中继内置目录（与当前网关无关）"
    assert data["fallback"] is True
    assert data["fallbackReason"]
    assert data["modelCount"] == 20


def test_media_catalog_bundled_when_unconfigured(monkeypatch, tmp_path):
    _isolate_settings_db(monkeypatch, tmp_path)
    client = _gateway_client()

    response = client.get("/model-gateway/official/media-catalog")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["source"] == "bundled"
    assert data["catalogLabel"] == "官方中继内置目录（与当前网关无关）"
    assert data["fallback"] is False
    assert data["modelCount"] == 20


def test_classify_gateway_media_model():
    assert classify_gateway_media_model("gpt-image-2.5") == "image"
    assert classify_gateway_media_model("LingShan-G2") == "image"
    assert classify_gateway_media_model("seedance-2.0") == "video"
    assert classify_gateway_media_model("index-tts-2") == "audio"
    assert classify_gateway_media_model("gpt-6-astra") is None
    assert classify_gateway_media_model("text-embedding-3-small") is None


def test_assistant_runtime_reports_missing_codex(monkeypatch):
    # No CODEX_BIN in this environment and no patched runtime: the probe
    # must report the runtime as missing, never as available.
    monkeypatch.delenv("CODEX_BIN", raising=False)
    monkeypatch.delenv("DRAMACLAW_CHAT_BACKEND", raising=False)
    monkeypatch.delenv("SUPERTALE_CHAT_BACKEND", raising=False)
    app = FastAPI()
    app.include_router(assistant_routes.router)
    app.dependency_overrides[get_api_user] = lambda: {
        "username": "local",
        "user_id": "local",
    }
    client = TestClient(app)

    response = client.get("/assistant/runtime")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["available"] is False
    assert data["runtime"] == "codex"
    assert data["missing"] is True
    assert data["code"] == "runtime_missing"
    assert "未检测到本地助手运行环境" in data["message"]
    assert "重启" not in data["message"]


def test_chat_error_text_states_runtime_missing_not_restart():
    from novelvideo.api.routes.chat import (
        _chat_error_code,
        _user_facing_chat_error,
    )
    from novelvideo.chat.codex_app_server import CodexRuntimeError

    exc = CodexRuntimeError("DRAMACLAW_CHAT_BACKEND=codex requested but unavailable")

    assert _chat_error_code(exc) == "runtime_missing"
    message = _user_facing_chat_error(exc)
    assert "未检测到本地助手运行环境" in message
    assert "重启" not in message


def test_media_relay_defaults_are_empty_on_fresh_install(monkeypatch, tmp_path):
    # P2-3: config defaults must not pre-fill the upstream author's bucket.
    # Verified in a clean subprocess so no in-process config reload can leak
    # into the rest of the suite.
    import subprocess
    import sys

    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"OSS_RELAY_ENDPOINT", "OSS_RELAY_BUCKET"}
    }
    env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent / "src")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from novelvideo import config; "
            "print(repr(config.OSS_RELAY_ENDPOINT)); "
            "print(repr(config.OSS_RELAY_BUCKET))",
        ],
        capture_output=True,
        text=True,
        env=env,
        # Run outside the repo so no local .env can mask the code default.
        cwd=tmp_path,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines() == ["''", "''"]


def _ctx(tmp_path: Path) -> ProjectContext:
    return ProjectContext(
        project_id="proj_pm2",
        project_name="demo",
        owner_type="user",
        owner_id="owner",
        owner_username="alice",
        requester_user_id="editor",
        requester_username="bob",
        requester_principals=(("user", "editor"),),
        effective_role="editor",
        home_node_id="node_a",
        output_dir=tmp_path / "output",
        state_dir=tmp_path / "state",
        runtime_dir=tmp_path / "runtime",
        is_home_node=True,
    )


def test_failed_task_status_text_uses_gateway_code(tmp_path):
    manager = TaskStateManager()
    ctx = _ctx(tmp_path)
    manager.create_task_for_project(ctx, "freezone_story_script", 0, scope="job_1")
    manager.update_progress_for_project(
        ctx,
        "freezone_story_script",
        0,
        scope="job_1",
        progress=0.1,
        current_task="开始生成故事脚本...",
    )

    manager.fail_task_for_project(
        ctx,
        "freezone_story_script",
        0,
        scope="job_1",
        error="status_code: 401, model_name: gpt-6-astra, body: {'message': 'Invalid token'}",
    )

    failed = manager.get_task_for_project(ctx, "freezone_story_script", 0, scope="job_1")
    assert failed is not None
    assert failed.status == "failed"
    assert failed.current_task == "生成失败：网关 401"
    assert failed.metadata is not None
    assert failed.metadata["gateway_status_code"] == 401
    assert failed.metadata["gateway_model"] == "gpt-6-astra"


def test_failed_task_status_text_without_gateway_code(tmp_path):
    manager = TaskStateManager()
    ctx = _ctx(tmp_path)
    manager.create_task_for_project(ctx, "freezone_story_script", 0, scope="job_2")
    manager.update_progress_for_project(
        ctx,
        "freezone_story_script",
        0,
        scope="job_2",
        progress=0.1,
        current_task="开始生成故事脚本...",
    )

    manager.fail_task_for_project(
        ctx,
        "freezone_story_script",
        0,
        scope="job_2",
        error="upstream exploded",
    )

    failed = manager.get_task_for_project(ctx, "freezone_story_script", 0, scope="job_2")
    assert failed is not None
    assert failed.current_task == "生成失败：upstream exploded"


def test_failed_task_keeps_explicit_current_task(tmp_path):
    manager = TaskStateManager()
    ctx = _ctx(tmp_path)
    manager.create_task_for_project(ctx, "freezone_story_script", 0, scope="job_3")

    manager.fail_task_for_project(
        ctx,
        "freezone_story_script",
        0,
        scope="job_3",
        error="status_code: 500, model_name: gpt-6-astra, body: {}",
        current_task="已取消：用户主动取消",
    )

    failed = manager.get_task_for_project(ctx, "freezone_story_script", 0, scope="job_3")
    assert failed is not None
    assert failed.current_task == "已取消：用户主动取消"
    assert failed.metadata["gateway_status_code"] == 500
