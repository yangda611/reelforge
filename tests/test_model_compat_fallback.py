"""Regression tests for model-agnostic structured output and 400 parameter fallback."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.profiles.openai import OpenAIJsonSchemaTransformer

import novelvideo.config as config
from novelvideo.egress_context import (
    BillingPrincipal,
    CredentialReference,
    TrustedEgressContext,
)
from novelvideo.model_gateway_runtime import model_gateway_request_scope
from novelvideo.structured_extraction import (
    ChunkCharacterOutput,
    _run_structured,
)


class _SimpleResult(BaseModel):
    value: str


def _tool_completion_response(model: str, tool_name: str, arguments: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-compat",
            "object": "chat.completion",
            "created": 1,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call-compat-1",
                                "type": "function",
                                "function": {
                                    "name": tool_name,
                                    "arguments": arguments,
                                },
                            }
                        ],
                    },
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
    )


def _sse_tool_response(model: str, tool_name: str, arguments: str) -> httpx.Response:
    chunks = [
        {
            "id": "chatcmpl-stream-1",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call-stream-1",
                                "type": "function",
                                "function": {"name": tool_name, "arguments": arguments},
                            }
                        ],
                    },
                    "finish_reason": None,
                }
            ],
        },
        {
            "id": "chatcmpl-stream-1",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
        },
    ]
    body = "".join(f"data: {json.dumps(c, ensure_ascii=False)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream; charset=utf-8"},
        content=body.encode("utf-8"),
    )


def _sse_text_response(model: str, content: str) -> httpx.Response:
    chunks = [
        {
            "id": "chatcmpl-stream-txt",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "content": content},
                    "finish_reason": None,
                }
            ],
        },
        {
            "id": "chatcmpl-stream-txt",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        },
    ]
    body = "".join(f"data: {json.dumps(c, ensure_ascii=False)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream; charset=utf-8"},
        content=body.encode("utf-8"),
    )


def _patch_mock_http(monkeypatch, handler):
    monkeypatch.setattr(
        config,
        "_newapi_text_http_client_factory",
        lambda *, timeout_seconds: lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            timeout=timeout_seconds,
        ),
    )


@pytest.mark.parametrize(
    "model_name",
    [
        "claude-sonnet-5-5",
        "anthropic/claude-sonnet-4-5",
        "deepseek-chat",
        "qwen-plus",
        "glm-5.3",
        "kimi-k2",
        "custom-relay-model",
    ],
)
def test_non_openai_non_gemini_models_disable_forced_tool_choice(model_name):
    profile = config._get_newapi_text_model_profile(model_name)
    assert profile is not None
    assert profile.openai_supports_tool_choice_required is False
    assert profile.openai_supports_strict_tool_definition is False
    assert profile.json_schema_transformer is OpenAIJsonSchemaTransformer


@pytest.mark.parametrize(
    "model_name",
    [
        "gpt-6-sol",
        "gpt-4o",
        "o3-mini",
        "DC-character-builder-LLM",
        "brainclaw",
    ],
)
def test_openai_and_opaque_alias_models_keep_default_profile(model_name):
    assert config._get_newapi_text_model_profile(model_name) is None


def test_gemini_profile_handles_bare_and_google_prefixed():
    p1 = config._get_newapi_text_model_profile("gemini-3.6-flash")
    p2 = config._get_newapi_text_model_profile("google/gemini-2.5-flash")
    assert p1 is not None and p2 is not None
    assert config._get_newapi_text_model_profile("gemini-3.1-flash-image-preview") is None


def test_claude_sends_tool_choice_auto_on_first_request(monkeypatch):
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        assert payload.get("tool_choice") == "auto"
        return _tool_completion_response(
            payload["model"], payload["tools"][0]["function"]["name"], '{"value":"ok"}'
        )

    _patch_mock_http(monkeypatch, handler)
    model = config._newapi_text_openai_model(
        "claude-sonnet-5-5",
        api_key="key",
        base_url="https://example.test/v1",
        timeout_seconds=12.0,
        profile=config._get_newapi_text_model_profile("claude-sonnet-5-5"),
    )
    res = asyncio.run(
        Agent(
            model,
            output_type=_SimpleResult,
            model_settings={"openai_reasoning_effort": "low"},
        ).run("test")
    )
    assert res.output == _SimpleResult(value="ok")
    assert len(requests) == 1


def test_400_fallback_relaxes_tool_choice_and_reasoning_for_opaque_alias(monkeypatch):
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        if payload.get("tool_choice") == "required" or "reasoning_effort" in payload:
            return httpx.Response(
                400,
                json={
                    "error": {
                        "message": "参数异常",
                        "type": "invalid_request_error",
                        "param": "",
                        "code": None,
                    }
                },
            )
        return _tool_completion_response(
            payload["model"], payload["tools"][0]["function"]["name"], '{"value":"ok"}'
        )

    _patch_mock_http(monkeypatch, handler)
    model = config._newapi_text_openai_model(
        "DC-character-builder-LLM",
        api_key="key",
        base_url="https://example.test/v1",
        timeout_seconds=12.0,
        profile=None,
    )
    res = asyncio.run(
        Agent(
            model,
            output_type=_SimpleResult,
            model_settings={"openai_reasoning_effort": "high"},
        ).run("test")
    )
    assert res.output == _SimpleResult(value="ok")
    assert len(requests) == 3
    assert requests[0]["tool_choice"] == "required" and requests[0]["reasoning_effort"] == "high"
    assert requests[1]["tool_choice"] == "auto" and requests[1]["reasoning_effort"] == "high"
    assert requests[2]["tool_choice"] == "auto" and "reasoning_effort" not in requests[2]


def test_400_fallback_skips_tool_relaxation_when_error_mentions_reasoning(monkeypatch):
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        if "reasoning_effort" in payload:
            return httpx.Response(
                400,
                json={"error": {"message": "Unsupported parameter: reasoning_effort"}},
            )
        return _tool_completion_response(
            payload["model"], payload["tools"][0]["function"]["name"], '{"value":"ok"}'
        )

    _patch_mock_http(monkeypatch, handler)
    model = config._newapi_text_openai_model(
        "gpt-4o",
        api_key="key",
        base_url="https://example.test/v1",
        timeout_seconds=12.0,
        profile=None,
    )
    res = asyncio.run(
        Agent(
            model,
            output_type=_SimpleResult,
            model_settings={"openai_reasoning_effort": "high"},
        ).run("test")
    )
    assert res.output == _SimpleResult(value="ok")
    assert len(requests) == 2
    assert "reasoning_effort" in requests[0]
    assert "reasoning_effort" not in requests[1]


def test_400_fallback_also_works_for_streaming_requests(monkeypatch):
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        assert payload.get("stream") is True
        if "reasoning_effort" in payload:
            return httpx.Response(
                400,
                json={"error": {"message": "参数异常", "type": "invalid_request_error"}},
            )
        return _sse_tool_response(
            payload["model"], payload["tools"][0]["function"]["name"], '{"value":"ok"}'
        )

    _patch_mock_http(monkeypatch, handler)
    model = config._newapi_text_openai_model(
        "claude-sonnet-5-5",
        api_key="key",
        base_url="https://example.test/v1",
        timeout_seconds=12.0,
        profile=config._get_newapi_text_model_profile("claude-sonnet-5-5"),
    )
    agent = Agent(
        model,
        output_type=_SimpleResult,
        model_settings={"openai_reasoning_effort": "low"},
    )
    res = asyncio.run(_run_structured(agent, "林默走了进来。"))
    assert res == _SimpleResult(value="ok")
    assert len(requests) == 2
    assert requests[0]["tool_choice"] == "auto" and requests[0]["reasoning_effort"] == "low"
    assert requests[1]["tool_choice"] == "auto" and "reasoning_effort" not in requests[1]


def test_run_structured_falls_back_to_prompted_output_when_tools_rejected(monkeypatch):
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        if "tools" in payload:
            return httpx.Response(
                400,
                json={"error": {"message": "tools are not supported for this model"}},
            )
        return _sse_text_response(
            payload["model"],
            '{"characters":[{"name":"林默","evidence":[{"quote":"林默走了进来。"}]}]}',
        )

    _patch_mock_http(monkeypatch, handler)
    model = config._newapi_text_openai_model(
        "legacy-text-model",
        api_key="key",
        base_url="https://example.test/v1",
        timeout_seconds=12.0,
        profile=config._get_newapi_text_model_profile("legacy-text-model"),
    )
    agent = Agent(model, output_type=ChunkCharacterOutput)
    res = asyncio.run(_run_structured(agent, "林默走了进来。"))
    assert len(res.characters) == 1
    assert res.characters[0].name == "林默"
    assert any("tools" not in req for req in requests)


def test_organization_context_never_retries_on_400(monkeypatch):
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return httpx.Response(400, json={"error": {"message": "参数异常"}})

    _patch_mock_http(monkeypatch, handler)
    model = config._newapi_text_openai_model(
        "DC-character-builder-LLM",
        api_key="key",
        base_url="https://example.test/v1",
        timeout_seconds=12.0,
        profile=None,
    )
    ctx = TrustedEgressContext(
        envelope_id="env-1",
        project_id="proj-1",
        task_type="text.generate",
        requester_user_id="user-1",
        root_task_id="root-1",
        admission_id="adm-1",
        admitted_at="2026-11-27T06:00:00Z",
        membership_id="mem-1",
        authz_version=1,
        billing_principal=BillingPrincipal(kind="organization", id="org-1"),
        credential=CredentialReference(
            source="organization",
            credential_id="cred-1",
            key_version=1,
            org_id="org-1",
        ),
    )

    with pytest.raises(ModelHTTPError):
        with model_gateway_request_scope(ctx):
            asyncio.run(
                Agent(
                    model,
                    output_type=_SimpleResult,
                    model_settings={"openai_reasoning_effort": "high"},
                ).run("test")
            )
    assert len(calls) == 1
