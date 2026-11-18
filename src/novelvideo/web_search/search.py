"""Bocha-backed web search, normalized into results the agent can cite.

Bocha's ``/v1/web-search`` returns both ``webPages`` and ``images`` for one
query, so image search needs no second endpoint. The response is flattened
here rather than in the tool layer because two backends (Codex MCP and the
Hermes plugin) and two surfaces (默认虾导, 虾画) all consume it.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

WEB_SEARCH_ENV_HINT = (
    "联网搜索未配置。请设置 BOCHA_API_KEY（博查开放平台 https://open.bochaai.com "
    "申请），或在 WEB_SEARCH_BASE_URL 填写兼容博查 /v1/web-search 的地址。"
)

_DEFAULT_BASE_URL = "https://api.bochaai.com"
_FRESHNESS_VALUES = frozenset(
    {"noLimit", "oneDay", "oneWeek", "oneMonth", "oneYear"}
)
_MAX_COUNT = 50
_DEFAULT_COUNT = 8
_MAX_SUMMARY_CHARS = 4000


class WebSearchNotConfigured(RuntimeError):
    """No search provider credential is configured for this deployment."""


@dataclass(frozen=True, slots=True)
class WebSearchConfig:
    api_key: str
    base_url: str
    timeout_seconds: float = 20.0

    @property
    def configured(self) -> bool:
        return bool(self.api_key.strip())

    def endpoint(self) -> str:
        return f"{self.base_url.rstrip('/')}/v1/web-search"


@dataclass(frozen=True, slots=True)
class SearchPage:
    """One web result. ``summary`` is Bocha's long AI summary when requested."""

    title: str
    url: str
    snippet: str
    summary: str
    site_name: str
    published_at: str

    def as_dict(self) -> dict[str, str]:
        return {
            "title": self.title,
            "url": self.url,
            "snippet": self.snippet,
            "summary": self.summary,
            "site_name": self.site_name,
            "published_at": self.published_at,
        }


@dataclass(frozen=True, slots=True)
class SearchImage:
    """One image result: a remote URL the canvas can import on request.

    ``image_url`` is what a canvas upload node keeps, ``thumbnail_url`` is the
    cheap preview, and ``host_page_url`` is where it was published (the credit
    an agent should cite).
    """

    title: str
    image_url: str
    thumbnail_url: str
    host_page_url: str
    width: int | None = None
    height: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "image_url": self.image_url,
            "thumbnail_url": self.thumbnail_url,
            "host_page_url": self.host_page_url,
            "width": self.width,
            "height": self.height,
        }


@dataclass(frozen=True, slots=True)
class SearchOutcome:
    query: str
    pages: tuple[SearchPage, ...] = ()
    images: tuple[SearchImage, ...] = ()

    @property
    def empty(self) -> bool:
        return not self.pages and not self.images


def _env_text(name: str) -> str:
    return str(os.environ.get(name) or "").strip()


def _env_float(name: str, default: float) -> float:
    raw = _env_text(name)
    if not raw:
        return default
    try:
        parsed = float(raw)
    except ValueError:
        return default
    return parsed if parsed > 0 else default


def load_web_search_config() -> WebSearchConfig:
    """Read search settings from the environment.

    Deliberately env-only: this credential belongs to the deployment, not to a
    per-user settings row, and both agent backends already receive a scrubbed
    child environment. Never embed a default key.
    """

    base_url = _env_text("WEB_SEARCH_BASE_URL") or _DEFAULT_BASE_URL
    api_key = _env_text("BOCHA_API_KEY") or _env_text("WEB_SEARCH_API_KEY")
    return WebSearchConfig(
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=_env_float("WEB_SEARCH_TIMEOUT_SECONDS", 20.0),
    )


def _text(value: Any) -> str:
    return str(value or "").strip()


def _bounded(value: str, limit: int = _MAX_SUMMARY_CHARS) -> str:
    if len(value) <= limit:
        return value
    return value[:limit].rstrip() + "…"


def _as_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _page_from_item(item: Any) -> SearchPage | None:
    if not isinstance(item, dict):
        return None
    url = _text(item.get("url"))
    if not url:
        return None
    return SearchPage(
        title=_text(item.get("name")) or url,
        url=url,
        snippet=_bounded(_text(item.get("snippet"))),
        summary=_bounded(_text(item.get("summary"))),
        site_name=_text(item.get("siteName")),
        published_at=_text(item.get("datePublished")),
    )


def _image_from_item(item: Any) -> SearchImage | None:
    if not isinstance(item, dict):
        return None
    image_url = _text(item.get("contentUrl")) or _text(item.get("thumbnailUrl"))
    if not image_url:
        return None
    return SearchImage(
        title=_text(item.get("name")) or _text(item.get("hostPageUrl")),
        image_url=image_url,
        thumbnail_url=_text(item.get("thumbnailUrl")) or image_url,
        host_page_url=_text(item.get("hostPageUrl")),
        width=_as_int(item.get("width")),
        height=_as_int(item.get("height")),
    )


def _collect(values: Any, builder: Any) -> list[Any]:
    items = values.get("value") if isinstance(values, dict) else None
    if not isinstance(items, list):
        return []
    collected = []
    for item in items:
        parsed = builder(item)
        if parsed is not None:
            collected.append(parsed)
    return collected


def normalize_freshness(value: str | None) -> str:
    """Clamp a model-supplied freshness to Bocha's accepted vocabulary."""

    candidate = _text(value)
    if candidate in _FRESHNESS_VALUES:
        return candidate
    # ``YYYY-MM-DD`` and ``YYYY-MM-DD..YYYY-MM-DD`` pass through: Bocha accepts
    # both and rejecting them would lose the main "recent news" workflow.
    if candidate and candidate[:4].isdigit() and "-" in candidate:
        return candidate[:32]
    return "noLimit"


def normalize_count(value: Any, *, default: int = _DEFAULT_COUNT) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(parsed, _MAX_COUNT))


def _payload(
    *, query: str, count: int, freshness: str, summary: bool, include: str, exclude: str
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "query": query,
        "count": count,
        "freshness": freshness,
        "summary": summary,
    }
    if include:
        body["include"] = include
    if exclude:
        body["exclude"] = exclude
    return body


async def _post(config: WebSearchConfig, body: dict[str, Any]) -> dict[str, Any]:
    import httpx

    headers = {
        "Authorization": f"Bearer {config.api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    async with httpx.AsyncClient(
        timeout=config.timeout_seconds, trust_env=False
    ) as client:
        response = await client.post(config.endpoint(), json=body, headers=headers)
    if response.status_code == 401 or response.status_code == 403:
        raise WebSearchNotConfigured(
            "联网搜索鉴权失败：BOCHA_API_KEY 无效或无该接口权限。"
        )
    if response.status_code >= 400:
        raise RuntimeError(
            f"search provider returned HTTP {response.status_code}: "
            f"{response.text[:300]}"
        )
    try:
        decoded = response.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError("search provider returned a non-JSON body") from exc
    if not isinstance(decoded, dict):
        raise RuntimeError("search provider returned an unexpected payload")
    if decoded.get("code") and str(decoded.get("code")) not in {"200", "0"}:
        message = _text(decoded.get("msg")) or "search provider rejected the query"
        raise RuntimeError(message)
    data = decoded.get("data")
    return data if isinstance(data, dict) else decoded


async def web_search(
    query: str,
    *,
    count: int = _DEFAULT_COUNT,
    freshness: str | None = None,
    summary: bool = True,
    include: str = "",
    exclude: str = "",
    config: WebSearchConfig | None = None,
) -> SearchOutcome:
    """Run one search and normalize both result kinds.

    Raises :class:`WebSearchNotConfigured` when no key is present, so callers
    surface configuration guidance instead of a silent empty result.
    """

    normalized_query = _text(query)
    if not normalized_query:
        raise ValueError("query is required")
    resolved = config or load_web_search_config()
    if not resolved.configured:
        raise WebSearchNotConfigured(WEB_SEARCH_ENV_HINT)
    body = _payload(
        query=normalized_query,
        count=normalize_count(count),
        freshness=normalize_freshness(freshness),
        summary=bool(summary),
        include=_text(include),
        exclude=_text(exclude),
    )
    data = await _post(resolved, body)
    query_context = data.get("queryContext")
    echoed = (
        _text(query_context.get("originalQuery"))
        if isinstance(query_context, dict)
        else ""
    )
    return SearchOutcome(
        query=echoed or normalized_query,
        pages=tuple(_collect(data.get("webPages"), _page_from_item)),
        images=tuple(_collect(data.get("images"), _image_from_item)),
    )


__all__ = [
    "SearchImage",
    "SearchOutcome",
    "SearchPage",
    "WEB_SEARCH_ENV_HINT",
    "WebSearchConfig",
    "WebSearchNotConfigured",
    "load_web_search_config",
    "normalize_count",
    "normalize_freshness",
    "web_search",
]
