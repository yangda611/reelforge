"""联网搜索与网页读取：虾导的对外信息入口。

这一层只做两件事，且都不改项目状态：

- :mod:`.search` 把一次查询发给已配置的搜索服务（默认博查），归一成
  「网页结果 / 图片结果」两种结构化条目。
- :mod:`.fetch` 抓取允许公网访问的 http(s) 页面，抽出正文与标题。

为什么单独成一个包：Codex 与 Hermes 两个后端、默认虾导与虾画里的虾导两条
入口，都需要同一套结果契约；把实现放在 ``novelvideo.chat`` 会让 Hermes 的
插件（不依赖应用 venv）无法复用。
"""

from __future__ import annotations

from novelvideo.web_search.search import (
    WEB_SEARCH_ENV_HINT,
    SearchImage,
    SearchOutcome,
    SearchPage,
    WebSearchConfig,
    WebSearchNotConfigured,
    load_web_search_config,
    web_search,
)
from novelvideo.web_search.fetch import (
    PageContent,
    WebFetchError,
    fetch_page,
)

__all__ = [
    "PageContent",
    "SearchImage",
    "SearchOutcome",
    "SearchPage",
    "WEB_SEARCH_ENV_HINT",
    "WebFetchError",
    "WebSearchConfig",
    "WebSearchNotConfigured",
    "fetch_page",
    "load_web_search_config",
    "web_search",
]
