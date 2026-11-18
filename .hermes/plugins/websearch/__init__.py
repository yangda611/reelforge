"""Hermes toolset: 联网搜索（web search / page read / image import）.

Mirrors ``novelvideo.chat.web_mcp`` for the Hermes backend. Both surfaces call
the same API routes with the worker's agent token, so the search credential
lives only in the API process and this plugin needs no extra dependency or
egress allowlist entry.

Deliberately read-only toward project state except ``web_image_import``, which
writes into the project's own ``freezone/_uploads`` through the authenticated
route — never a remote hotlink, because the canvas media scope guard rejects
cross-project references.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

from tools.registry import tool_error, tool_result

TOOLSET = "web-acp"
REGISTER_TOOLSETS = ("hermes-acp",)
API_PREFIX = "/api/v1"
TOOL_NAMES = ("web_search", "web_page_read", "web_image_import")

try:
    DEFAULT_TIMEOUT_SECONDS = max(30, int(os.environ.get("DRAMACLAW_API_TIMEOUT_SECONDS", "120")))
except ValueError:
    DEFAULT_TIMEOUT_SECONDS = 120

_MAX_QUERY_CHARS = 512
_MAX_URL_CHARS = 2048
_MAX_COUNT = 50
_MAX_FETCH_CHARS = 40000


def _base_url() -> str:
    value = os.environ.get("DRAMACLAW_API_URL", "").strip()
    if not value:
        raise ValueError("DRAMACLAW_API_URL is not set")
    return value.rstrip("/")


def _current_agent_token() -> str:
    token_file = os.environ.get("DRAMACLAW_AGENT_TOKEN_FILE", "").strip()
    if token_file:
        try:
            return Path(token_file).read_text(encoding="utf-8").strip()
        except OSError:
            return ""
    return os.environ.get("DRAMACLAW_AGENT_TOKEN", "").strip()


def _local_agent_trust_enabled() -> bool:
    raw = os.environ.get("DRAMACLAW_LOCAL_AGENT_TRUST", "").strip().lower()
    if raw not in {"1", "true", "yes", "on"}:
        return False
    parsed = urlparse(os.environ.get("DRAMACLAW_API_URL", "").strip())
    return (parsed.hostname or "").lower() in {"127.0.0.1", "::1", "localhost"}


def _ce_owner_mode() -> bool:
    """Tokenless local-owner mode documented for external MCP agents."""

    return os.environ.get("DRAMACLAW_CE_OWNER", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _ce_owner_allow_remote() -> bool:
    return os.environ.get("DRAMACLAW_CE_OWNER_ALLOW_REMOTE", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _enforce_ce_owner_target() -> None:
    """Mirror the dramaclaw plugin: CE-owner mode must target loopback."""

    if _ce_owner_allow_remote():
        return
    parsed = urlparse(os.environ.get("DRAMACLAW_API_URL", "").strip())
    if (parsed.hostname or "").lower() in {"127.0.0.1", "::1", "localhost"}:
        return
    raise ValueError(
        "DRAMACLAW_CE_OWNER=1 requires a loopback DRAMACLAW_API_URL; "
        "set DRAMACLAW_CE_OWNER_ALLOW_REMOTE=1 to override (unsafe), "
        "or provide DRAMACLAW_AGENT_TOKEN"
    )


def _available() -> bool:
    return bool(
        os.environ.get("DRAMACLAW_API_URL", "").strip()
        and (
            _current_agent_token()
            or _local_agent_trust_enabled()
            or _ce_owner_mode()
        )
    )


def _request_headers() -> dict[str, str]:
    headers = {
        "Accept": "application/json",
        "User-Agent": "websearch-plugin/1.0.0",
    }
    token = _current_agent_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    elif _ce_owner_mode():
        _enforce_ce_owner_target()
    elif not _local_agent_trust_enabled():
        raise ValueError(
            "联网搜索需要本轮 Agent 身份：未找到 DRAMACLAW_AGENT_TOKEN_FILE"
        )
    return headers


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


def _request(method: str, path: str, *, body: Any = None) -> dict[str, Any]:
    url = f"{_base_url()}{API_PREFIX}{path}"
    payload = None
    headers = _request_headers()
    if body is not None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(url, data=payload, headers=headers, method=method.upper())
    try:
        with urlopen(request, timeout=DEFAULT_TIMEOUT_SECONDS) as response:
            text = response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        raise ValueError(
            _error_detail(exc.read(65537).decode("utf-8", errors="replace"))
            or f"HTTP {exc.code}"
        ) from exc
    except URLError as exc:
        raise ValueError(f"network_error: {exc.reason}") from exc
    try:
        decoded = json.loads(text)
    except ValueError as exc:
        raise ValueError("API returned a non-JSON body") from exc
    if not isinstance(decoded, dict):
        raise ValueError("API returned an unexpected payload")
    if decoded.get("ok") is False:
        raise ValueError(
            str(decoded.get("error") or decoded.get("message") or "failed")
        )
    data = decoded.get("data")
    return data if isinstance(data, dict) else {}


def _failure(message: str) -> str:
    return tool_error(message)


def _require_text(value: Any, *, name: str, limit: int) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{name} is required")
    if len(text) > limit:
        raise ValueError(f"{name} is too long (max {limit} characters)")
    return text


def _handle_web_search(args: dict[str, Any], **_: Any) -> str:
    try:
        query = _require_text(args.get("query"), name="query", limit=_MAX_QUERY_CHARS)
        body: dict[str, Any] = {"query": query, "summary": True}
        count = args.get("count")
        if count is not None:
            try:
                body["count"] = max(1, min(int(count), _MAX_COUNT))
            except (TypeError, ValueError):
                pass
        freshness = str(args.get("freshness") or "").strip()
        if freshness:
            body["freshness"] = freshness
        include = str(args.get("include_site") or "").strip()
        if include:
            body["include"] = include
        exclude = str(args.get("exclude_site") or "").strip()
        if exclude:
            body["exclude"] = exclude
        data = _request("POST", "/web/search", body=body)
    except ValueError as exc:
        return _failure(str(exc))
    pages = data.get("pages") if isinstance(data.get("pages"), list) else []
    images = data.get("images") if isinstance(data.get("images"), list) else []
    instruction = (
        "在回答中转述检索结论并附上来源链接（pages[].url）；不要在未检索的情况下"
        "编造事实。需要把图片放进画布时，先对选中的 images[].image_url 调用 "
        "web_image_import，再把返回的 url 写进节点。"
    )
    if not pages and not images:
        instruction = "没有检索到结果。如实告诉用户没有找到，并建议换关键词或放宽时间范围。"
    return tool_result(
        {
            "ok": True,
            "status": "web_search_completed",
            "query": str(data.get("query") or query),
            "pages": pages,
            "images": images,
            "result_count": len(pages),
            "image_count": len(images),
            "agent_instruction": instruction,
        }
    )


def _handle_web_page_read(args: dict[str, Any], **_: Any) -> str:
    try:
        url = _require_text(args.get("url"), name="url", limit=_MAX_URL_CHARS)
        body: dict[str, Any] = {"url": url}
        max_chars = args.get("max_chars")
        if max_chars is not None:
            try:
                body["max_chars"] = max(500, min(int(max_chars), _MAX_FETCH_CHARS))
            except (TypeError, ValueError):
                pass
        data = _request("POST", "/web/fetch", body=body)
    except ValueError as exc:
        return _failure(str(exc))
    text = str(data.get("text") or "")
    return tool_result(
        {
            "ok": True,
            "status": "web_page_read_completed",
            "url": str(data.get("url") or url),
            "title": str(data.get("title") or ""),
            "text": text,
            "chars": int(data.get("chars") or len(text)),
            "truncated": bool(data.get("truncated")),
        }
    )


def _handle_web_image_import(args: dict[str, Any], **_: Any) -> str:
    try:
        url = _require_text(args.get("url"), name="url", limit=_MAX_URL_CHARS)
        project = os.environ.get("DRAMACLAW_PROJECT_ID", "").strip()
        if not project:
            raise ValueError(
                "web_image_import 只能在项目会话中使用：当前会话没有绑定项目。"
            )
        body: dict[str, Any] = {"url": url}
        filename = str(args.get("filename") or "").strip()
        if filename:
            body["filename"] = filename
        data = _request(
            "POST",
            f"/projects/{quote(project, safe='')}/freezone/remote-image-import",
            body=body,
        )
    except ValueError as exc:
        return _failure(str(exc))
    return tool_result(
        {
            "ok": True,
            "status": "web_image_imported",
            "url": str(data.get("url") or ""),
            "rel_path": str(data.get("rel_path") or ""),
            "filename": str(data.get("filename") or ""),
            "size": int(data.get("size") or 0),
            "source_url": str(data.get("source_url") or url),
            "media_type": "image",
        }
    )


def _schema(
    name: str,
    description: str,
    properties: dict[str, Any],
    required: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": required or [],
            "additionalProperties": False,
        },
    }


TOOLS = (
    (
        "web_search",
        _schema(
            "web_search",
            "联网搜索：用博查检索引擎查询公网网页与图片，返回标题、链接、摘要、发布时间与"
            "图片直链。用于需要实时或外部资料的问题（新闻、事实核查、真实地点/年代/建筑"
            "考据、找参考图）。拿到的信息必须转述并给出来源链接；不要编造结果。若某条摘要"
            "不够，再用 web_page_read 读原文。",
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
                    "description": "网页结果条数上限，默认 8。",
                },
                "freshness": {
                    "type": "string",
                    "description": (
                        "时间范围：noLimit（默认）、oneDay、oneWeek、oneMonth、oneYear，"
                        "或 YYYY-MM-DD / YYYY-MM-DD..YYYY-MM-DD。"
                    ),
                },
                "want_images": {
                    "type": "boolean",
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
        _handle_web_search,
    ),
    (
        "web_page_read",
        _schema(
            "web_page_read",
            "读取一个公网网页的正文纯文本（默认最多 8000 字），用于搜索结果摘要不够时深入"
            "核对。只允许公网 http(s) 地址；内网地址会被拒绝。",
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
                    "description": "返回正文的最大字符数，默认 8000。",
                },
            },
            ["url"],
        ),
        _handle_web_page_read,
    ),
    (
        "web_image_import",
        _schema(
            "web_image_import",
            "把一张公网图片下载进当前项目（freezone/_uploads），返回本项目静态地址。只有需要"
            "把搜索到的图片放进画布时才调用：先把返回值里的 url 写进画布图片/上传节点的 "
            "imageUrl，节点才能持久化并且对其他成员可见。直接外链会被画布媒体作用域守卫拒绝。",
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
        _handle_web_image_import,
    ),
)


def register(ctx) -> None:
    for name, schema, handler in TOOLS:
        for toolset in REGISTER_TOOLSETS:
            ctx.register_tool(
                name=name,
                toolset=toolset,
                schema=schema,
                handler=handler,
                check_fn=_available,
                requires_env=["DRAMACLAW_API_URL", "DRAMACLAW_AGENT_TOKEN"],
                description=schema["description"],
                emoji="",
            )
