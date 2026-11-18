"""Read one public web page as bounded text.

The agent gets search summaries first and only reads a page when a summary is
not enough. Fetches therefore share the download layer's public-destination
rule (resolve, pin, re-check redirects) instead of opening a second, unguarded
egress path; ``novelvideo.utils.media_download`` owns that rule.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit

DEFAULT_MAX_CHARS = 8000
MAX_MAX_CHARS = 40000
MAX_PAGE_BYTES = 3 * 1024 * 1024
FETCH_TIMEOUT_SECONDS = 20.0

_TEXTUAL_CONTENT_TYPES = (
    "text/html",
    "application/xhtml",
    "text/plain",
    "application/xml",
    "text/xml",
)
_SKIPPED_TAGS = frozenset(
    {"script", "style", "noscript", "template", "svg", "canvas", "iframe"}
)
_BLOCK_TAGS = frozenset(
    {
        "address",
        "article",
        "aside",
        "blockquote",
        "br",
        "dd",
        "div",
        "dl",
        "dt",
        "figcaption",
        "figure",
        "footer",
        "form",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "li",
        "main",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "td",
        "th",
        "tr",
        "ul",
    }
)
_WHITESPACE_RE = re.compile(r"[ \t\r\f\v]+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")


class WebFetchError(RuntimeError):
    """The page could not be read as public text."""


@dataclass(frozen=True, slots=True)
class PageContent:
    url: str
    title: str
    text: str
    truncated: bool
    content_type: str

    @property
    def chars(self) -> int:
        return len(self.text)

    def as_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "title": self.title,
            "text": self.text,
            "chars": self.chars,
            "truncated": self.truncated,
            "content_type": self.content_type,
        }


class _TextExtractor(HTMLParser):
    """Collect visible text; keep ``<title>`` for the result header."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._title_parts: list[str] = []
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lowered = tag.lower()
        if lowered in _SKIPPED_TAGS:
            self._skip_depth += 1
            return
        if lowered == "title":
            self._in_title = True
            return
        if lowered in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        if lowered in _SKIPPED_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if lowered == "title":
            self._in_title = False
            return
        if lowered in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self._title_parts.append(data)
            return
        self._parts.append(data)

    @property
    def title(self) -> str:
        return _collapse(" ".join(self._title_parts))

    @property
    def text(self) -> str:
        return _collapse("".join(self._parts))


def _collapse(value: str) -> str:
    normalized = unescape(value).replace("\u00a0", " ")
    normalized = _WHITESPACE_RE.sub(" ", normalized)
    normalized = "\n".join(line.strip() for line in normalized.split("\n"))
    return _BLANK_LINES_RE.sub("\n\n", normalized).strip()


def html_to_text(html: str) -> tuple[str, str]:
    """Return ``(title, text)`` for one HTML document."""

    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 - malformed markup must not fail a read
        pass
    return parser.title, parser.text


def normalize_max_chars(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return DEFAULT_MAX_CHARS
    return max(500, min(parsed, MAX_MAX_CHARS))


def _charset(content_type: str) -> str:
    for part in content_type.split(";")[1:]:
        name, _, raw = part.partition("=")
        if name.strip().lower() == "charset" and raw.strip():
            return raw.strip().strip('"').strip("'")
    return "utf-8"


def _textual(content_type: str) -> bool:
    lowered = content_type.lower()
    return any(lowered.startswith(prefix) for prefix in _TEXTUAL_CONTENT_TYPES)


async def fetch_page(url: str, *, max_chars: Any = DEFAULT_MAX_CHARS) -> PageContent:
    """Fetch one public http(s) page and return bounded visible text.

    Only public destinations are reachable: a non-global address (including any
    redirect hop to one) raises :class:`WebFetchError`. Non-textual documents
    are refused rather than returned as binary noise.
    """

    import aiohttp

    from novelvideo.ports.video_delivery import VideoDeliveryError
    from novelvideo.utils.media_download import (
        public_http_session_options,
        validate_public_http_url,
    )

    target = str(url or "").strip()
    parsed = urlsplit(target)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise WebFetchError("web_fetch 只接受 http(s) 网址。")
    try:
        validate_public_http_url(target)
    except VideoDeliveryError as exc:
        raise WebFetchError(
            "web_fetch 拒绝内网/保留地址：只允许公网页面。"
        ) from exc

    limit = normalize_max_chars(max_chars)
    timeout = aiohttp.ClientTimeout(total=FETCH_TIMEOUT_SECONDS)
    try:
        async with aiohttp.ClientSession(
            timeout=timeout, **public_http_session_options()
        ) as session:
            async with session.get(
                target, headers={"Accept": "text/html,application/xhtml+xml,text/plain"}
            ) as response:
                if response.status >= 400:
                    raise WebFetchError(
                        f"web_fetch 读取失败：HTTP {response.status}"
                    )
                content_type = str(response.headers.get("Content-Type") or "")
                if content_type and not _textual(content_type):
                    raise WebFetchError(
                        "web_fetch 只能读取网页正文，当前地址返回 "
                        f"{content_type.split(';')[0] or '未知类型'}。"
                    )
                raw = await response.content.read(MAX_PAGE_BYTES + 1)
                final_url = str(response.url)
    except WebFetchError:
        raise
    except VideoDeliveryError as exc:
        raise WebFetchError("web_fetch 拒绝内网/保留地址：只允许公网页面。") from exc
    except (aiohttp.ClientError, TimeoutError) as exc:
        raise WebFetchError(f"web_fetch 网络错误：{type(exc).__name__}") from exc

    truncated_bytes = len(raw) > MAX_PAGE_BYTES
    body = raw[:MAX_PAGE_BYTES].decode(_charset(content_type), errors="replace")
    if _textual(content_type) and "html" in content_type.lower():
        title, text = html_to_text(body)
    else:
        title, text = "", _collapse(body)
    if not text:
        raise WebFetchError("web_fetch 没有抽到可读正文。")
    truncated = truncated_bytes or len(text) > limit
    return PageContent(
        url=final_url,
        title=title,
        text=text[:limit].rstrip(),
        truncated=truncated,
        content_type=content_type.split(";")[0].strip(),
    )


__all__ = [
    "DEFAULT_MAX_CHARS",
    "MAX_MAX_CHARS",
    "PageContent",
    "WebFetchError",
    "fetch_page",
    "html_to_text",
    "normalize_max_chars",
]
