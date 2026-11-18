"""联网搜索与网页读取的 HTTP 入口。

虾导的两个后端（Codex MCP、Hermes 插件）都不直接持有搜索服务的 Key：它们
只带本轮的 agent token 调这里，由 API 进程按部署配置访问外网。这样凭证只
存在于一个进程，插件侧也不需要额外的依赖或出网白名单。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from novelvideo.api.auth import get_api_user
from novelvideo.web_search.fetch import WebFetchError, fetch_page
from novelvideo.web_search.search import (
    WEB_SEARCH_ENV_HINT,
    WebSearchNotConfigured,
    web_search,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/web")


class WebSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=512)
    count: int = Field(default=8, ge=1, le=50)
    freshness: str | None = None
    summary: bool = True
    include: str = ""
    exclude: str = ""


class WebFetchRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2048)
    max_chars: int = Field(default=8000, ge=500, le=40000)


@router.post("/search")
async def web_search_route(
    body: WebSearchRequest,
    user: dict = Depends(get_api_user),
):
    """Run one web/image search through the configured provider."""
    del user
    try:
        outcome = await web_search(
            body.query,
            count=body.count,
            freshness=body.freshness,
            summary=body.summary,
            include=body.include,
            exclude=body.exclude,
        )
    except WebSearchNotConfigured as exc:
        raise HTTPException(503, WEB_SEARCH_ENV_HINT) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - provider failures are user-visible
        logger.warning("web search failed: %s", exc)
        raise HTTPException(502, f"联网搜索失败：{exc}") from exc
    return {
        "ok": True,
        "data": {
            "query": outcome.query,
            "pages": [page.as_dict() for page in outcome.pages],
            "images": [image.as_dict() for image in outcome.images],
        },
    }


@router.post("/fetch")
async def web_fetch_route(
    body: WebFetchRequest,
    user: dict = Depends(get_api_user),
):
    """Read one public page as bounded plain text."""
    del user
    try:
        page = await fetch_page(body.url, max_chars=body.max_chars)
    except WebFetchError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "data": page.as_dict()}
