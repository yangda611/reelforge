"""联网搜索：服务层、MCP server 与插件契约。

三个层次分开测，因为它们各自有独立的失败模式：
- 归一化把博查的响应形状翻译成虾导消费的契约；
- MCP server 负责把工具参数收敛成 API 请求，并把错误变成模型能读的文本；
- 插件声明必须和 MCP 工具同名同参，否则 Codex 与 Hermes 两个后端行为分叉。
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

from novelvideo.chat import web_mcp
from novelvideo.web_search import fetch as fetch_module
from novelvideo.web_search import search as search_module

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# search normalization
# ---------------------------------------------------------------------------


def test_search_normalizes_web_and_image_results(monkeypatch):
    captured: dict = {}

    async def fake_post(config, body):
        captured["body"] = body
        return {
            "queryContext": {"originalQuery": body["query"]},
            "webPages": {
                "value": [
                    {
                        "name": "标题一",
                        "url": "https://example.test/a",
                        "snippet": "摘要",
                        "summary": "长摘要",
                        "siteName": "示例站",
                        "datePublished": "2026-01-02T00:00:00+08:00",
                    },
                    {"name": "没有链接", "snippet": "丢弃"},
                ]
            },
            "images": {
                "value": [
                    {
                        "name": "图一",
                        "contentUrl": "https://img.test/a.jpg",
                        "thumbnailUrl": "https://img.test/a-thumb.jpg",
                        "hostPageUrl": "https://example.test/a",
                        "width": 1920,
                        "height": 1080,
                    }
                ]
            },
        }

    monkeypatch.setattr(search_module, "_post", fake_post)
    config = search_module.WebSearchConfig(api_key="key", base_url="https://api.test")
    outcome = __import__("asyncio").run(
        search_module.web_search("查询", count=5, freshness="oneWeek", config=config)
    )

    assert outcome.query == "查询"
    assert [page.url for page in outcome.pages] == ["https://example.test/a"]
    assert outcome.pages[0].site_name == "示例站"
    assert outcome.images[0].image_url == "https://img.test/a.jpg"
    assert outcome.images[0].width == 1920
    # 请求体只发送博查认识的字段。
    assert captured["body"]["count"] == 5
    assert captured["body"]["freshness"] == "oneWeek"
    assert captured["body"]["summary"] is True


def test_search_without_key_reports_configuration_guidance(monkeypatch):
    for name in ("BOCHA_API_KEY", "WEB_SEARCH_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    config = search_module.load_web_search_config()
    assert not config.configured

    with pytest.raises(search_module.WebSearchNotConfigured) as exc:
        __import__("asyncio").run(
            search_module.web_search("x", config=config)
        )
    assert "BOCHA_API_KEY" in str(exc.value)


def test_search_rejects_auth_failure_and_provider_errors(monkeypatch):
    import httpx

    config = search_module.WebSearchConfig(api_key="key", base_url="https://api.test")

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, *args, **kwargs):
            return httpx.Response(
                401,
                text="unauthorized",
                request=httpx.Request("POST", "https://api.test/v1/web-search"),
            )

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    with pytest.raises(search_module.WebSearchNotConfigured):
        __import__("asyncio").run(search_module.web_search("x", config=config))


def test_search_argument_normalization_is_bounded():
    assert search_module.normalize_count(0) == 1
    assert search_module.normalize_count(9999) == 50
    assert search_module.normalize_count("bad") == 8
    assert search_module.normalize_freshness("oneMonth") == "oneMonth"
    assert search_module.normalize_freshness("2026-01-01") == "2026-01-01"
    assert search_module.normalize_freshness("2026-01-01..2026-02-01") == (
        "2026-01-01..2026-02-01"
    )
    assert search_module.normalize_freshness("yesterday") == "noLimit"


# ---------------------------------------------------------------------------
# page fetch
# ---------------------------------------------------------------------------


def test_html_to_text_drops_scripts_and_keeps_blocks():
    title, text = fetch_module.html_to_text(
        "<html><head><title>标题</title><style>a{color:red}</style></head>"
        "<body><h1>H</h1><p>正文&nbsp;内容</p>"
        "<script>steal()</script><noscript>n</noscript></body></html>"
    )
    assert title == "标题"
    assert "steal" not in text
    assert "color:red" not in text
    assert "正文 内容" in text


def test_fetch_refuses_non_public_and_non_http_targets():
    import asyncio

    for target in ("file:///etc/passwd", "http://127.0.0.1/admin", "http://10.0.0.5/"):
        with pytest.raises(fetch_module.WebFetchError):
            asyncio.run(fetch_module.fetch_page(target))


def test_max_chars_is_clamped():
    assert fetch_module.normalize_max_chars("x") == 8000
    assert fetch_module.normalize_max_chars(1) == 500
    assert fetch_module.normalize_max_chars(10**9) == 40000


# ---------------------------------------------------------------------------
# MCP server contract
# ---------------------------------------------------------------------------


def test_mcp_tool_names_are_stable():
    assert web_mcp.TOOL_NAMES == ("web_search", "web_page_read", "web_image_import")


def test_mcp_requires_turn_identity(monkeypatch):
    monkeypatch.setenv("DRAMACLAW_API_URL", "http://127.0.0.1:8780")
    monkeypatch.delenv("DRAMACLAW_AGENT_TOKEN_FILE", raising=False)
    monkeypatch.delenv("DRAMACLAW_AGENT_TOKEN", raising=False)
    monkeypatch.delenv("DRAMACLAW_LOCAL_AGENT_TRUST", raising=False)
    monkeypatch.delenv("DRAMACLAW_CE_OWNER", raising=False)

    with pytest.raises(web_mcp.WebToolError):
        web_mcp._headers()

    # Local trust is the documented opt-in; it must not be inferred from the URL.
    monkeypatch.setenv("DRAMACLAW_LOCAL_AGENT_TRUST", "1")
    assert "Authorization" not in web_mcp._headers()


def test_mcp_ce_owner_mode_targets_loopback_only(monkeypatch):
    # Tokenless CE-owner mode exists for external MCP agents (docs/guides);
    # it must behave exactly like the dramaclaw plugin's guard.
    monkeypatch.delenv("DRAMACLAW_AGENT_TOKEN_FILE", raising=False)
    monkeypatch.delenv("DRAMACLAW_AGENT_TOKEN", raising=False)
    monkeypatch.delenv("DRAMACLAW_LOCAL_AGENT_TRUST", raising=False)
    monkeypatch.setenv("DRAMACLAW_CE_OWNER", "1")

    monkeypatch.setenv("DRAMACLAW_API_URL", "http://127.0.0.1:8780")
    assert "Authorization" not in web_mcp._headers()

    monkeypatch.setenv("DRAMACLAW_API_URL", "http://10.0.0.5:8780")
    with pytest.raises(web_mcp.WebToolError):
        web_mcp._headers()

    monkeypatch.setenv("DRAMACLAW_CE_OWNER_ALLOW_REMOTE", "1")
    assert "Authorization" not in web_mcp._headers()


def test_mcp_search_tool_shapes_request_and_instruction(monkeypatch):
    captured: dict = {}

    def fake_request(method, path, *, body=None):
        captured["path"] = path
        captured["body"] = body
        return {
            "query": "查询",
            "pages": [{"title": "t", "url": "https://example.test"}],
            "images": [],
        }

    monkeypatch.setattr(web_mcp, "_request", fake_request)
    result = web_mcp._call_search({"query": "查询", "count": 3, "include_site": "a.com"})

    assert captured["path"] == "/web/search"
    assert captured["body"]["count"] == 3
    assert captured["body"]["include"] == "a.com"
    payload = result.structuredContent
    assert payload["ok"] is True
    assert payload["result_count"] == 1
    assert "来源链接" in payload["agent_instruction"]
    assert result.isError is False


def test_mcp_search_tool_failure_is_model_readable(monkeypatch):
    def boom(*args, **kwargs):
        raise web_mcp.WebToolError("联网搜索未配置。请设置 BOCHA_API_KEY")

    monkeypatch.setattr(web_mcp, "_request", boom)
    result = web_mcp._call_search({"query": "查询"})

    payload = result.structuredContent
    assert payload["ok"] is False
    assert "BOCHA_API_KEY" in payload["message"]
    assert "不要编造" in payload["next_action"]
    assert result.isError is True


def test_mcp_image_import_requires_a_bound_project(monkeypatch):
    monkeypatch.delenv("DRAMACLAW_PROJECT_ID", raising=False)
    result = web_mcp._call_image_import({"url": "https://img.test/a.jpg"})
    assert result.structuredContent["ok"] is False
    assert "没有绑定项目" in result.structuredContent["message"]

    monkeypatch.setenv("DRAMACLAW_PROJECT_ID", "project-a")
    captured: dict = {}

    def fake_request(method, path, *, body=None):
        captured["path"] = path
        captured["body"] = body
        return {
            "url": "/static/projects/project-a/freezone/_uploads/a.jpg",
            "rel_path": "freezone/_uploads/a.jpg",
            "filename": "a.jpg",
            "size": 10,
            "source_url": body["url"],
        }

    monkeypatch.setattr(web_mcp, "_request", fake_request)
    result = web_mcp._call_image_import({"url": "https://img.test/a.jpg"})
    assert captured["path"] == "/projects/project-a/freezone/remote-image-import"
    assert result.structuredContent["ok"] is True
    assert result.structuredContent["url"].startswith("/static/projects/")


# ---------------------------------------------------------------------------
# Hermes plugin mirrors the MCP tools
# ---------------------------------------------------------------------------


@pytest.fixture()
def hermes_plugin():
    tools_module = types.ModuleType("tools")
    registry_module = types.ModuleType("tools.registry")
    registry_module.tool_error = lambda value: json.dumps(
        {"ok": False, "error": str(value)}, ensure_ascii=False
    )
    registry_module.tool_result = lambda value: json.dumps(value, ensure_ascii=False)
    previous = {name: sys.modules.get(name) for name in ("tools", "tools.registry")}
    sys.modules["tools"] = tools_module
    sys.modules["tools.registry"] = registry_module
    try:
        path = ROOT / ".hermes" / "plugins" / "websearch" / "__init__.py"
        spec = importlib.util.spec_from_file_location("test_websearch_plugin", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        yield module
    finally:
        for name, value in previous.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


def test_plugin_publishes_the_same_tool_names_and_parameters(hermes_plugin):
    plugin_tools = {
        entry[0]: entry[1]
        for entry in hermes_plugin.TOOLS
        if isinstance(entry, tuple) and len(entry) == 3
    }
    assert tuple(sorted(plugin_tools)) == tuple(sorted(web_mcp.TOOL_NAMES))
    for name, schema in plugin_tools.items():
        assert schema["parameters"]["type"] == "object"
        assert schema["parameters"]["additionalProperties"] is False
        assert schema["parameters"]["required"], name


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("web_search", {"query": "查询", "count": 4, "freshness": "oneWeek"}),
        ("web_page_read", {"url": "https://example.test/a", "max_chars": 1200}),
    ],
)
def test_plugin_handlers_call_the_same_routes(hermes_plugin, monkeypatch, tool, arguments):
    captured: dict = {}

    def fake_request(method, path, *, body=None):
        captured["path"] = path
        captured["body"] = body
        if tool == "web_search":
            return {"query": body["query"], "pages": [], "images": []}
        return {"url": body["url"], "text": "正文", "chars": 2, "truncated": False}

    monkeypatch.setattr(hermes_plugin, "_request", fake_request)
    handler = getattr(hermes_plugin, f"_handle_{tool}")
    payload = json.loads(handler(arguments))

    assert payload["ok"] is True
    expected_path = "/web/search" if tool == "web_search" else "/web/fetch"
    assert captured["path"] == expected_path
    assert captured["body"]["url" if tool != "web_search" else "query"] == (
        arguments["url"] if tool != "web_search" else arguments["query"]
    )


def test_plugin_image_import_targets_the_bound_project(hermes_plugin, monkeypatch):
    monkeypatch.setenv("DRAMACLAW_PROJECT_ID", "project-b")
    captured: dict = {}

    def fake_request(method, path, *, body=None):
        captured["path"] = path
        return {
            "url": "/static/projects/project-b/freezone/_uploads/b.jpg",
            "rel_path": "freezone/_uploads/b.jpg",
            "filename": "b.jpg",
            "size": 5,
            "source_url": body["url"],
        }

    monkeypatch.setattr(hermes_plugin, "_request", fake_request)
    payload = json.loads(
        hermes_plugin._handle_web_image_import({"url": "https://img.test/b.jpg"})
    )
    assert captured["path"] == "/projects/project-b/freezone/remote-image-import"
    assert payload["ok"] is True


def test_plugin_reports_missing_identity(hermes_plugin, monkeypatch):
    monkeypatch.setenv("DRAMACLAW_API_URL", "http://127.0.0.1:8780")
    monkeypatch.delenv("DRAMACLAW_AGENT_TOKEN_FILE", raising=False)
    monkeypatch.delenv("DRAMACLAW_AGENT_TOKEN", raising=False)
    monkeypatch.delenv("DRAMACLAW_LOCAL_AGENT_TRUST", raising=False)

    payload = json.loads(hermes_plugin._handle_web_search({"query": "x"}))
    assert payload["ok"] is False
    assert "Agent 身份" in payload["error"]


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------


def _route(module, name):
    return getattr(module, name)


def test_search_route_returns_pages_and_images(monkeypatch):
    import asyncio

    from novelvideo.api.routes import web as web_route

    async def fake_search(query, **kwargs):
        assert kwargs["count"] == 4
        return search_module.SearchOutcome(
            query=query,
            pages=(search_module.SearchPage("t", "u", "s", "", "site", ""),),
            images=(search_module.SearchImage("i", "img", "thumb", "page"),),
        )

    monkeypatch.setattr(web_route, "web_search", fake_search)
    response = asyncio.run(
        _route(web_route, "web_search_route")(
            web_route.WebSearchRequest(query="查询", count=4), user={"username": "u"}
        )
    )
    assert response["ok"] is True
    assert response["data"]["pages"][0]["url"] == "u"
    assert response["data"]["images"][0]["image_url"] == "img"


def test_search_route_surfaces_missing_configuration_as_503(monkeypatch):
    import asyncio

    from fastapi import HTTPException

    from novelvideo.api.routes import web as web_route

    async def unconfigured(query, **kwargs):
        raise search_module.WebSearchNotConfigured("需要 BOCHA_API_KEY")

    monkeypatch.setattr(web_route, "web_search", unconfigured)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            _route(web_route, "web_search_route")(
                web_route.WebSearchRequest(query="x"), user={}
            )
        )
    assert exc.value.status_code == 503
    assert "BOCHA_API_KEY" in exc.value.detail


def test_fetch_route_rejects_private_targets(monkeypatch):
    import asyncio

    from fastapi import HTTPException

    from novelvideo.api.routes import web as web_route

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            _route(web_route, "web_fetch_route")(
                web_route.WebFetchRequest(url="http://169.254.169.254/latest/meta-data"),
                user={},
            )
        )
    assert exc.value.status_code == 400


def test_remote_image_import_writes_into_the_project(monkeypatch, tmp_path):
    import asyncio

    from novelvideo.api.routes import freezone

    project_dir = tmp_path / "project"
    (project_dir / "freezone" / "_uploads").mkdir(parents=True)
    png = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    )

    async def fake_resolve(project, user, **kwargs):
        return (
            types.SimpleNamespace(project_id="project-a"),
            "admin",
            "demo",
            project_dir,
            str(project_dir),
        )

    async def fake_download(url, output_path, **kwargs):
        assert kwargs["public_only"] is True
        Path(output_path).write_bytes(png)
        return types.SimpleNamespace(size=len(png))

    monkeypatch.setattr(freezone, "_resolve_freezone_project", fake_resolve)
    monkeypatch.setattr(
        "novelvideo.utils.media_download.download_to_path", fake_download
    )

    response = asyncio.run(
        freezone.freezone_remote_image_import(
            "project-a",
            freezone.FreezoneRemoteImageImportRequest(url="https://img.test/a.png"),
            user={},
        )
    )
    assert response["ok"] is True
    assert response["data"]["source_url"] == "https://img.test/a.png"
    written = project_dir / response["data"]["rel_path"]
    assert written.is_file()
    assert written.read_bytes().startswith(b"\x89PNG")


def test_remote_image_import_refuses_non_image_payloads(monkeypatch, tmp_path):
    import asyncio

    from fastapi import HTTPException

    from novelvideo.api.routes import freezone

    project_dir = tmp_path / "project"
    (project_dir / "freezone" / "_uploads").mkdir(parents=True)

    async def fake_resolve(project, user, **kwargs):
        return (
            types.SimpleNamespace(project_id="project-a"),
            "admin",
            "demo",
            project_dir,
            str(project_dir),
        )

    async def fake_download(url, output_path, **kwargs):
        Path(output_path).write_bytes(b"<html>not an image</html>")
        return types.SimpleNamespace(size=24)

    monkeypatch.setattr(freezone, "_resolve_freezone_project", fake_resolve)
    monkeypatch.setattr(
        "novelvideo.utils.media_download.download_to_path", fake_download
    )

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            freezone.freezone_remote_image_import(
                "project-a",
                freezone.FreezoneRemoteImageImportRequest(url="https://img.test/x"),
                user={},
            )
        )
    assert exc.value.status_code == 400
    # 拒绝时不能把半成品留在项目里。
    assert list((project_dir / "freezone" / "_uploads").iterdir()) == []

