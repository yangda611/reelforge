def test_identity_planner_uses_split_newapi_model_envs(monkeypatch):
    from novelvideo.agents.identity_planner import IdentityPlanner
    import novelvideo.agents.identity_planner as identity_planner

    calls = []
    sentinel = object()

    def fake_newapi_model(model_env, default_model=None, **_kwargs):
        calls.append((model_env, default_model))
        return sentinel

    monkeypatch.setattr(
        identity_planner,
        "get_newapi_text_pydantic_model",
        fake_newapi_model,
    )

    assert IdentityPlanner._identity_model("IDENTITY_PLANNER_CAST_MODEL") is sentinel
    assert calls == [("IDENTITY_PLANNER_CAST_MODEL", None)]


def test_structured_output_model_settings_default_to_high_reasoning(monkeypatch):
    from novelvideo.agents.identity_planner import IdentityPlanner

    assert IdentityPlanner._identity_model_settings() == {
        "openai_reasoning_effort": "high"
    }


def test_opaque_newapi_alias_sends_configured_reasoning_effort_default_high(monkeypatch):
    import asyncio
    import json

    import httpx
    from pydantic import BaseModel
    from pydantic_ai import Agent

    import novelvideo.config as config

    requests: list[dict] = []

    class StructuredResult(BaseModel):
        value: str

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        output_tool = payload["tools"][0]["function"]["name"]
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-structured-test",
                "object": "chat.completion",
                "created": 1,
                "model": "DC-structured-test",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call-structured-test",
                                    "type": "function",
                                    "function": {
                                        "name": output_tool,
                                        "arguments": '{"value":"ok"}',
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            },
        )

    monkeypatch.setattr(
        config,
        "_newapi_text_http_client_factory",
        lambda *, timeout_seconds: lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            timeout=timeout_seconds,
        ),
    )
    model = config._newapi_text_openai_model(
        "DC-structured-test",
        api_key="key",
        base_url="https://example.test/v1",
        timeout_seconds=12.0,
        profile=None,
    )
    agent = Agent(
        model,
        output_type=StructuredResult,
        model_settings=config.get_newapi_structured_output_model_settings(),
    )

    result = asyncio.run(agent.run("return a structured result"))

    assert result.output == StructuredResult(value="ok")
    assert len(requests) == 1
    assert requests[0]["reasoning_effort"] == "high"


def test_plain_text_brainclaw_recipe_request_preserves_routing_headers_and_omits_reasoning(
    monkeypatch,
):
    import asyncio
    import json

    import httpx
    from pydantic_ai import Agent

    import novelvideo.config as config

    requests: list[dict] = []
    headers: list[httpx.Headers] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        headers.append(request.headers)
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-recipe-test",
                "object": "chat.completion",
                "created": 1,
                "model": "brainclaw",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": "compiled prompt",
                        },
                    }
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            },
        )

    monkeypatch.setattr(
        config,
        "_newapi_text_http_client_factory",
        lambda *, timeout_seconds: lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            timeout=timeout_seconds,
        ),
    )
    model = config._newapi_text_openai_model(
        "brainclaw",
        api_key="key",
        base_url="https://example.test/v1",
        timeout_seconds=12.0,
        profile=None,
        default_headers={
            "X-BrainClaw-Profile": "freezone_recipe_text_generation",
            "X-BrainClaw-Profile-Version": "1",
            "X-BrainClaw-Profile-Variant": "recipe/general-text@1.0.0",
        },
    )
    result = asyncio.run(Agent(model, output_type=str).run("compile this"))

    assert result.output == "compiled prompt"
    assert len(requests) == 1
    assert "reasoning_effort" not in requests[0]
    assert "reasoning" not in requests[0]
    assert "response_format" not in requests[0]
    assert "tools" not in requests[0]
    assert headers[0]["X-BrainClaw-Profile"] == "freezone_recipe_text_generation"
    assert headers[0]["X-BrainClaw-Profile-Version"] == "1"
    assert (
        headers[0]["X-BrainClaw-Profile-Variant"]
        == "recipe/general-text@1.0.0"
    )


def test_newapi_text_provider_default_trusts_env(monkeypatch):
    import asyncio

    import novelvideo.config as config

    monkeypatch.delenv("NEWAPI_TEXT_TRUST_ENV", raising=False)

    provider = config._newapi_text_openai_provider(
        api_key="key",
        base_url="https://example.test/v1",
        timeout_seconds=12.0,
    )
    http_client = provider._own_http_client
    try:
        assert http_client is not None
        assert http_client.trust_env is True
        assert provider._http_client_factory is not None
    finally:
        if http_client is not None:
            asyncio.run(http_client.aclose())


def test_newapi_text_provider_can_disable_system_proxy(monkeypatch):
    import asyncio

    import novelvideo.config as config

    monkeypatch.setenv("NEWAPI_TEXT_TRUST_ENV", "false")

    provider = config._newapi_text_openai_provider(
        api_key="key",
        base_url="https://example.test/v1",
        timeout_seconds=12.0,
    )
    http_client = provider._own_http_client
    try:
        assert http_client is not None
        assert http_client.trust_env is False
    finally:
        if http_client is not None:
            asyncio.run(http_client.aclose())


def test_newapi_text_model_closes_owned_http_client_after_request(monkeypatch):
    import asyncio

    from pydantic_ai.models.openai import OpenAIChatModel

    import novelvideo.config as config

    model = config._newapi_text_openai_model(
        "gpt-test",
        api_key="key",
        base_url="https://example.test/v1",
        timeout_seconds=12.0,
        profile=None,
    )
    provider = model.provider
    http_client = provider._own_http_client
    assert http_client is not None
    assert not http_client.is_closed

    original_request = OpenAIChatModel.request

    async def fake_request(self, *args, **kwargs):
        assert not self.provider._own_http_client.is_closed
        return "ok"

    monkeypatch.setattr(OpenAIChatModel, "request", fake_request)

    try:
        result = asyncio.run(model.request([], None, None))
    finally:
        monkeypatch.setattr(OpenAIChatModel, "request", original_request)
        if not http_client.is_closed:
            asyncio.run(http_client.aclose())

    assert result == "ok"
    assert http_client.is_closed


def test_asset_compiler_scene_planner_uses_scene_newapi_env(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    import novelvideo.agents.asset_compiler as asset_compiler

    model_calls = []
    settings_calls = []
    agent_kwargs = {}

    def fake_newapi_model(model_env, default_model=None, **_kwargs):
        model_calls.append((model_env, default_model))
        return "scene-model"

    def fake_settings():
        settings_calls.append(())
        return {"openai_reasoning_effort": "none"}

    class FakeAgent:
        def __init__(self, model, **kwargs):
            agent_kwargs["model"] = model
            agent_kwargs.update(kwargs)

        async def run(self, task):
            return SimpleNamespace(output=SimpleNamespace(derived_scenes=[]))

    monkeypatch.setattr(
        asset_compiler, "get_newapi_text_pydantic_model", fake_newapi_model
    )
    monkeypatch.setattr(
        asset_compiler,
        "get_newapi_structured_output_model_settings",
        fake_settings,
    )
    monkeypatch.setattr(asset_compiler, "Agent", FakeAgent)

    compiler = asset_compiler.AssetCompiler(cognee_store=None)
    block = SimpleNamespace(
        header_line="古董店 内 日", lines=["△ 古董堆满房间", "李雷环顾四周", "灯光昏暗"]
    )

    result = asyncio.run(compiler._analyze_derived_scenes("古董店", block))

    assert result == []
    assert model_calls == [("EPISODE_SCENE_PLANNER_MODEL", None)]
    assert settings_calls == [()]
    assert agent_kwargs["model"] == "scene-model"
    assert agent_kwargs["name"] == "派生场景分析师"


def test_asset_compiler_prop_planner_uses_prop_newapi_env(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    import novelvideo.agents.asset_compiler as asset_compiler

    model_calls = []
    settings_calls = []
    agent_kwargs = {}

    def fake_newapi_model(model_env, default_model=None, **_kwargs):
        model_calls.append((model_env, default_model))
        return "prop-model"

    def fake_settings():
        settings_calls.append(())
        return {"openai_reasoning_effort": "none"}

    class FakeAgent:
        def __init__(self, model, **kwargs):
            agent_kwargs["model"] = model
            agent_kwargs.update(kwargs)

        async def run(self, task):
            return SimpleNamespace(output=SimpleNamespace(requirements=[]))

    monkeypatch.setattr(
        asset_compiler, "get_newapi_text_pydantic_model", fake_newapi_model
    )
    monkeypatch.setattr(
        asset_compiler,
        "get_newapi_structured_output_model_settings",
        fake_settings,
    )
    monkeypatch.setattr(asset_compiler, "Agent", FakeAgent)

    compiler = asset_compiler.AssetCompiler(cognee_store=None)
    block = SimpleNamespace(
        header_line="古董店 内 日", lines=["李雷拿起龙符咒", "龙符咒发出红光"]
    )

    result = asyncio.run(
        compiler._analyze_block_props(
            block,
            preselected=[],
            prior_selected_prop_ids=[],
        )
    )

    assert result == []
    assert model_calls == [("EPISODE_PROP_PLANNER_MODEL", None)]
    assert settings_calls == [()]
    assert agent_kwargs["model"] == "prop-model"
    assert agent_kwargs["name"] == "场景块道具分析师"


def test_literal_script_writer_uses_literal_newapi_env(monkeypatch):
    import novelvideo.workflows.literal_script_writing as literal_script_writing

    model_calls = []
    settings_calls = []
    agent_kwargs = {}

    def fake_newapi_model(model_env, default_model=None, **_kwargs):
        model_calls.append((model_env, default_model))
        return "literal-model"

    def fake_settings():
        settings_calls.append(())
        return {"openai_reasoning_effort": "none"}

    class FakeAgent:
        def __init__(self, model, **kwargs):
            agent_kwargs["model"] = model
            agent_kwargs.update(kwargs)

    monkeypatch.setattr(
        literal_script_writing,
        "get_newapi_text_pydantic_model",
        fake_newapi_model,
    )
    monkeypatch.setattr(
        literal_script_writing,
        "get_newapi_structured_output_model_settings",
        fake_settings,
    )
    monkeypatch.setattr(literal_script_writing, "Agent", FakeAgent)

    workflow = literal_script_writing.LiteralScriptWritingWorkflow(cognee_store=None)

    assert workflow.agent is workflow.agent
    assert model_calls == [("LITERAL_BEAT_META_MODEL", None)]
    assert settings_calls == [()]
    assert agent_kwargs["model"] == "literal-model"
    assert agent_kwargs["name"] == "逐行剧本分镜标注师"
    assert agent_kwargs["output_type"] is literal_script_writing.LiteralBeatMetaOutput
    assert agent_kwargs["output_retries"] == 2


def test_seedance2_prompt_composer_uses_newapi_composer_model_env(monkeypatch):
    import novelvideo.config as config
    import novelvideo.seedance2_i2v.prompt as seedance2_prompt

    model_calls = []
    agent_kwargs = {}

    def fake_newapi_model(model_env, default_model=None, **_kwargs):
        model_calls.append((model_env, default_model))
        return "composer-model"

    class FakeAgent:
        def __init__(self, model, **kwargs):
            agent_kwargs["model"] = model
            agent_kwargs.update(kwargs)

    monkeypatch.setattr(config, "get_newapi_text_pydantic_model", fake_newapi_model)
    monkeypatch.setattr("pydantic_ai.Agent", FakeAgent)

    seedance2_prompt.create_seedance2_prompt_composer_agent()

    assert model_calls == [("SEEDANCE2_PROMPT_COMPOSER_MODEL", None)]
    assert agent_kwargs["model"] == "composer-model"
    assert "model_settings" not in agent_kwargs
    assert agent_kwargs["name"] == "Seedance 2.0 Prompt Composer"
    assert agent_kwargs["output_type"] is str
    assert "output_retries" not in agent_kwargs
