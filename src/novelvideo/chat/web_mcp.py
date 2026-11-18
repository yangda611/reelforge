"""MCP server exposing 联网搜索（web search）to 虾导.

Why an MCP server rather than new tools on the existing servers:

* Codex connects MCP servers directly; Hermes gets the same surface through
  ``.hermes/plugins/websearch``, which calls this server's routes over HTTP.
  One tool contract, both backends.
* Search is not a ReelForge business write. Keeping it off ``dramaclaw`` keeps
  the protected canvas/write boundary from growing a tool that reaches the
  public internet.

The server holds no search credential. It calls the API process
(``/api/v1/web/search``, ``/api/v1/web/fetch``) with the turn's agent token, so
the provider key stays in one process and MCP children need no extra env.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server

logger = logging.getLogger("novelvideo.chat.web_mcp")

SERVER = Server("dramaclaw-web", version="1.0.0")

API_PREFIX = "/api/v1"
DEFAULT_TIMEOUT_SECONDS = 60
#: Mirrors the API model's bounds; re-stated so a bad argument fails here with a
#: readable message instead of a 422 the model has to decode.
_MAX_QUERY_CHARS = 512
_MAX_URL_CHARS = 2048
_MAX_COUNT = 50
_MAX_FETCH_CHARS = 40000

TOOL_NAMES = ("web_search", "web_page_read", "web_image_import")


class WebToolError(RuntimeError):
    """A web tool could not complete; the message is safe to show the model."""


def _base_url() -> str:
    value = str(os.environ.get("DRAMACLAW_API_URL") or "").strip()
    if not value:
        raise WebToolError("DRAMACLAW_API_URL is not set")
    return value.rstrip("/")


def _agent_token() -> str:
    token_file = str(os.environ.get("DRAMACLAW_AGENT_TOKEN_FILE") or "").strip()
    if token_file:
        try:
            return Path(token_file).read_text(encoding="utf-8").strip()
        except OSError:
            return ""
    return str(os.environ.get("DRAMACLAW_AGENT_TOKEN") or "").strip()


def _local_trust() -> bool:
    if str(os.environ.get("DRAMACLAW_LOCAL_AGENT_TRUST") or "").strip().lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return False
    host = (urlparse(_base_url()).hostname or "").lower()
    return host in {"127.0.0.1", "::1", "localhost"}


def _ce_owner_mode() -> bool:
    """Tokenless local-owner mode documented for external MCP agents."""

    return str(os.environ.get("DRAMACLAW_CE_OWNER") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _ce_owner_allow_remote() -> bool:
    return str(os.environ.get("DRAMACLAW_CE_OWNER_ALLOW_REMOTE") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _enforce_ce_owner_target() -> None:
    """Mirror the dramaclaw plugin: CE-owner mode must target loopback."""

    if _ce_owner_allow_remote():
        return
    host = (urlparse(_base_url()).hostname or "").lower()
    if host in {"127.0.0.1", "::1", "localhost"}:
        return
    raise WebToolError(
        "DRAMACLAW_CE_OWNER=1 requires a loopback DRAMACLAW_API_URL; "
        "set DRAMACLAW_CE_OWNER_ALLOW_REMOTE=1 to override (unsafe), "
        "or provide DRAMACLAW_AGENT_TOKEN"
    )


def _headers() -> dict[str, str]:
    headers = {"Accept": "application/json", "User-Agent": "dramaclaw-web-mcp/1.0.0"}
    token = _agent_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    elif _ce_owner_mode():
        _enforce_ce_owner_target()
    elif not _local_trust():
        raise WebToolError(
            "联网搜索需要本轮 Agent 身份：未找到 DRAMACLAW_AGENT_TOKEN_FILE"
        )
    return headers


def _request(method: str, path: str, *, body: Any = None) -> dict[str, Any]:
    url = f"{_base_url()}{API_PREFIX}{path}"
    payload = None
    headers = _headers()
    if body is not None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(url, data=payload, headers=headers, method=method.upper())
    try:
        with urlopen(request, timeout=DEFAULT_TIMEOUT_SECONDS) as response:
            text = response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        detail = _error_detail(exc.read(65537).decode("utf-8", errors="replace"))
        raise WebToolError(detail or f"HTTP {exc.code}") from exc
    except URLError as exc:
        raise WebToolError(f"network_error: {exc.reason}") from exc
    try:
        decoded = json.loads(text)
    except ValueError as exc:
        raise WebToolError("API returned a non-JSON body") from exc
    if not isinstance(decoded, dict):
        raise WebToolError("API returned an unexpected payload")
    if decoded.get("ok") is False:
        raise WebToolError(str(decoded.get("error") or decoded.get("message") or "failed"))
    data = decoded.get("data")
    return data if isinstance(data, dict) else {}


def _error_detail(text: str) -> str:
    try:
        decoded = json.loads(text)
    except ValueError:
        return text.strip()[:400]
    if isinstance(decoded, dict):
        for key in ("detail", "message", "error"):
            value = decoded.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:400]
    return text.strip()[:400]


def _require_text(value: Any, *, name: str, limit: int) -> str:
    text = str(value or "").strip()
    if not text:
        raise WebToolError(f"{name} is required")
    if len(text) > limit:
        raise WebToolError(f"{name} is too long (max {limit} characters)")
    return text


def _project_id() -> str:
    return str(os.environ.get("DRAMACLAW_PROJECT_ID") or "").strip()


def _available() -> bool:
    """Tool visibility: search needs a base URL and this turn's identity."""

    try:
        _base_url()
    except WebToolError:
        return False
    return bool(_agent_token()) or _local_trust() or _ce_owner_mode()


def _object_schema(
    properties: dict[str, Any], required: list[str]
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _output_schema(
    success_properties: dict[str, Any], required: list[str]
) -> dict[str, Any]:
    common = {"ok": {"type": "boolean"}, "status": {"type": "string", "minLength": 1}}
    failure = {
        **common,
        "error": {"type": ["string", "null"]},
        "message": {"type": ["string", "null"]},
        "retryable": {"type": "boolean"},
        "next_action": {"type": ["string", "null"]},
    }
    return {
        "type": "object",
        "oneOf": [
            {
                "properties": {**common, "ok": {"const": True}, **success_properties},
                "required": ["ok", "status", *required],
                "additionalProperties": False,
            },
            {
                "properties": {**failure, "ok": {"const": False}},
                "required": ["ok", "status"],
                "additionalProperties": False,
            },
        ],
    }


_OUTPUT_SCHEMAS: dict[str, dict[str, Any]] = {
    "web_search": _output_schema(
        {
            "query": {"type": "string"},
            "pages": {"type": "array"},
            "images": {"type": "array"},
            "result_count": {"type": "integer"},
            "image_count": {"type": "integer"},
            "agent_instruction": {"type": "string"},
        },
        ["query", "pages", "images"],
    ),
    "web_page_read": _output_schema(
        {
            "url": {"type": "string"},
            "title": {"type": "string"},
            "text": {"type": "string"},
            "chars": {"type": "integer"},
            "truncated": {"type": "boolean"},
        },
        ["url", "text"],
    ),
    "web_image_import": _output_schema(
        {
            "url": {"type": "string"},
            "rel_path": {"type": "string"},
            "filename": {"type": "string"},
            "size": {"type": "integer"},
            "source_url": {"type": "string"},
            "media_type": {"type": "string"},
        },
        ["url", "source_url"],
    ),
}


def _result(tool_name: str, payload: dict[str, Any]) -> types.CallToolResult:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    schema = _OUTPUT_SCHEMAS.get(tool_name)
    if schema is not None:
        from jsonschema import Draft202012Validator

        Draft202012Validator(schema).validate(payload)
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=encoded)],
        structuredContent=payload,
        isError=payload.get("ok") is False,
    )


def _failure(tool_name: str, message: str) -> types.CallToolResult:
    logger.warning("web tool failed tool=%s message=%s", tool_name, message[:240])
    return _result(
        tool_name,
        {
            "ok": False,
            "status": "web_tool_failed",
            "error": message,
            "message": message,
            "retryable": False,
            "next_action": "向用户说明联网搜索当前不可用，不要编造检索结果。",
        },
    )


@SERVER.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="web_search",
            description=(
                "联网搜索：用博查检索引擎查询公网网页与图片，返回标题、链接、摘要、"
                "发布时间与图片直链。用于需要实时或外部资料的问题（新闻、事实核查、"
                "真实地点/年代/建筑考据、找参考图）。拿到的信息必须转述并给出来源链接；"
                "不要编造结果。若某条摘要不够，再用 web_page_read 读原文。"
            ),
            inputSchema=_object_schema(
                {
                    "query": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": _MAX_QUERY_CHARS,
                        "description": "自然语言查询，中文即可。",
                    },
                    "count": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": _MAX_COUNT,
                        "default": 8,
                        "description": "网页结果条数上限。",
                    },
                    "freshness": {
                        "type": "string",
                        "description": (
                            "时间范围：noLimit（默认）、oneDay、oneWeek、oneMonth、"
                            "oneYear，或 YYYY-MM-DD / YYYY-MM-DD..YYYY-MM-DD。"
                        ),
                    },
                    "want_images": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "用户要找图片/参考图时设为 true 并在回答里重点用 images；"
                            "images 始终会随响应返回。"
                        ),
                    },
                    "include_site": {
                        "type": "string",
                        "description": "只在指定域名内搜索，多个用 | 分隔（可选）。",
                    },
                    "exclude_site": {
                        "type": "string",
                        "description": "排除指定域名，多个用 | 分隔（可选）。",
                    },
                },
                ["query"],
            ),
            outputSchema=_OUTPUT_SCHEMAS["web_search"],
        ),
        types.Tool(
            name="web_page_read",
            description=(
                "读取一个公网网页的正文纯文本（最多约 8000 字），用于搜索结果摘要不够时"
                "深入核对。只允许公网 http(s) 地址；内网地址会被拒绝。"
            ),
            inputSchema=_object_schema(
                {
                    "url": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": _MAX_URL_CHARS,
                        "description": "网页地址，通常来自 web_search 的 pages[].url。",
                    },
                    "max_chars": {
                        "type": "integer",
                        "minimum": 500,
                        "maximum": _MAX_FETCH_CHARS,
                        "default": 8000,
                        "description": "返回正文的最大字符数。",
                    },
                },
                ["url"],
            ),
            outputSchema=_OUTPUT_SCHEMAS["web_page_read"],
        ),
        types.Tool(
            name="web_image_import",
            description=(
                "把一张公网图片下载进当前项目（freezone/_uploads），返回本项目静态地址。"
                "只有需要把搜索到的图片放进画布时才调用：先把返回值里的 url 写进画布"
                "图片/上传节点的 imageUrl，节点才能持久化并且对其他成员可见。"
                "直接外链会被画布媒体作用域守卫拒绝。"
            ),
            inputSchema=_object_schema(
                {
                    "url": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": _MAX_URL_CHARS,
                        "description": "图片直链，通常来自 web_search 的 images[].image_url。",
                    },
                    "filename": {
                        "type": "string",
                        "maxLength": 128,
                        "description": "可选文件名（不含路径）。",
                    },
                },
                ["url"],
            ),
            outputSchema=_OUTPUT_SCHEMAS["web_image_import"],
        ),
    ]


@SERVER.call_tool(validate_input=False)
async def call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
    arguments = dict(arguments or {})
    if name == "web_search":
        return _call_search(arguments)
    if name == "web_page_read":
        return _call_page_read(arguments)
    if name == "web_image_import":
        return _call_image_import(arguments)
    raise ValueError(f"unknown web tool: {name}")


def _call_search(arguments: dict[str, Any]) -> types.CallToolResult:
    try:
        query = _require_text(arguments.get("query"), name="query", limit=_MAX_QUERY_CHARS)
        count = arguments.get("count")
        body: dict[str, Any] = {"query": query, "summary": True}
        if count is not None:
            try:
                body["count"] = max(1, min(int(count), _MAX_COUNT))
            except (TypeError, ValueError):
                pass
        freshness = str(arguments.get("freshness") or "").strip()
        if freshness:
            body["freshness"] = freshness
        include = str(arguments.get("include_site") or "").strip()
        if include:
            body["include"] = include
        exclude = str(arguments.get("exclude_site") or "").strip()
        if exclude:
            body["exclude"] = exclude
        data = _request("POST", "/web/search", body=body)
    except WebToolError as exc:
        return _failure("web_search", str(exc))
    pages = data.get("pages") if isinstance(data.get("pages"), list) else []
    images = data.get("images") if isinstance(data.get("images"), list) else []
    instruction = (
        "在回答中转述检索结论并附上来源链接（pages[].url）；不要在未检索的情况下"
        "编造事实。需要把图片放进画布时，先对选中的 images[].image_url 调用 "
        "web_image_import，再把返回的 url 写进节点。"
    )
    if not pages and not images:
        instruction = "没有检索到结果。如实告诉用户没有找到，并建议换关键词或放宽时间范围。"
    return _result(
        "web_search",
        {
            "ok": True,
            "status": "web_search_completed",
            "query": str(data.get("query") or query),
            "pages": pages,
            "images": images,
            "result_count": len(pages),
            "image_count": len(images),
            "agent_instruction": instruction,
        },
    )


def _call_page_read(arguments: dict[str, Any]) -> types.CallToolResult:
    try:
        url = _require_text(arguments.get("url"), name="url", limit=_MAX_URL_CHARS)
        body: dict[str, Any] = {"url": url}
        max_chars = arguments.get("max_chars")
        if max_chars is not None:
            try:
                body["max_chars"] = max(500, min(int(max_chars), _MAX_FETCH_CHARS))
            except (TypeError, ValueError):
                pass
        data = _request("POST", "/web/fetch", body=body)
    except WebToolError as exc:
        return _failure("web_page_read", str(exc))
    text = str(data.get("text") or "")
    return _result(
        "web_page_read",
        {
            "ok": True,
            "status": "web_page_read_completed",
            "url": str(data.get("url") or url),
            "title": str(data.get("title") or ""),
            "text": text,
            "chars": int(data.get("chars") or len(text)),
            "truncated": bool(data.get("truncated")),
        },
    )


def _call_image_import(arguments: dict[str, Any]) -> types.CallToolResult:
    try:
        url = _require_text(arguments.get("url"), name="url", limit=_MAX_URL_CHARS)
        project = _project_id()
        if not project:
            raise WebToolError(
                "web_image_import 只能在项目会话中使用：当前会话没有绑定项目。"
            )
        body: dict[str, Any] = {"url": url}
        filename = str(arguments.get("filename") or "").strip()
        if filename:
            body["filename"] = filename
        data = _request(
            "POST",
            f"/projects/{quote(project, safe='')}/freezone/remote-image-import",
            body=body,
        )
    except WebToolError as exc:
        return _failure("web_image_import", str(exc))
    return _result(
        "web_image_import",
        {
            "ok": True,
            "status": "web_image_imported",
            "url": str(data.get("url") or ""),
            "rel_path": str(data.get("rel_path") or ""),
            "filename": str(data.get("filename") or ""),
            "size": int(data.get("size") or 0),
            "source_url": str(data.get("source_url") or url),
            "media_type": "image",
        },
    )


async def _main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await SERVER.run(
            read_stream, write_stream, SERVER.create_initialization_options()
        )


def main() -> None:
    import asyncio

    logging.basicConfig(level=logging.INFO)
    started = time.monotonic()
    logger.info("web mcp server starting available=%s", _available())
    asyncio.run(_main())
    logger.info("web mcp server stopped after %.1fs", time.monotonic() - started)


if __name__ == "__main__":
    main()
