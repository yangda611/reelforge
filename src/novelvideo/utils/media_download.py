"""Bounded-memory, atomic media downloads."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import ipaddress
import os
import socket
import tempfile
import time
from pathlib import Path
from typing import Callable, Mapping
from urllib.parse import urljoin

import aiohttp
from yarl import URL

from novelvideo.ports.video_delivery import VideoDeliveryError, VideoDeliveryReceipt

_CHUNK_SIZE = 1024 * 1024
_RETRYABLE_HTTP_STATUSES = frozenset({408, 425, 429})


def _is_retryable_http_status(status: int) -> bool:
    """Only transient HTTP failures (timeouts, rate-limits, 5xx) warrant retries."""
    return status in _RETRYABLE_HTTP_STATUSES or status >= 500
#: Addresses a desktop TUN proxy (Clash/Mihomo 一类) hands back for every name.
#: 双栈机器上同一个域名会同时拿到 IPv4 与 IPv6 两个 Fake-IP：只认 IPv4 会让
#: 「任一解析结果不是公网」成立，于是本机 CE 上每个域名都被判成非法目的地。
#: 两个段都不是可路由到内网的地址（RFC 2544 基准测试段 / RFC 5180 基准测试段），
#: 且放行条件与 IPv4 完全一致：仅本机 CE、非生产、name 不是 IP 字面量或 localhost。
_PROXY_FAKE_IP_NETWORKS = (
    ipaddress.ip_network("198.18.0.0/15"),
    ipaddress.ip_network("2001:2::/48"),
)


def _allow_proxy_fake_ip() -> bool:
    """Allow RFC 2544/5180 TUN Fake-IP DNS (e.g. Clash/Mihomo) only in local CE."""
    from novelvideo.shared.runtime_env import uses_local_ce_runtime

    if not uses_local_ce_runtime():
        return False
    return os.environ.get("SUPERTALE_ENV", "").strip().lower() != "production"


def _is_ip_literal(host: str) -> bool:
    candidate = host.strip("[]")
    try:
        ipaddress.ip_address(candidate)
        return True
    except ValueError:
        return False


def _is_fake_ip(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return any(
        address.version == network.version and address in network
        for network in _PROXY_FAKE_IP_NETWORKS
    )


def _is_allowed_resolved_address(
    host: str, address: ipaddress.IPv4Address | ipaddress.IPv6Address
) -> bool:
    if address.is_global:
        return True
    normalized_host = host.strip(".").lower()
    return bool(
        _allow_proxy_fake_ip()
        and _is_fake_ip(address)
        and normalized_host
        and normalized_host != "localhost"
        and not normalized_host.endswith(".localhost")
        and not _is_ip_literal(normalized_host)
    )


class _PublicResolver(aiohttp.abc.AbstractResolver):
    """Resolve public destinations and pin those addresses for the request."""

    async def resolve(self, host, port=0, family=socket.AF_INET):
        try:
            results = await asyncio.get_running_loop().getaddrinfo(
                host,
                port,
                type=socket.SOCK_STREAM,
                family=socket.AF_UNSPEC,
            )
            addresses = [ipaddress.ip_address(item[4][0]) for item in results]
        except ValueError as exc:
            raise VideoDeliveryError(
                "VIDEO_DOWNLOAD_URL_INVALID", retryable=False
            ) from exc
        if not addresses or any(
            not _is_allowed_resolved_address(host, address) for address in addresses
        ):
            raise VideoDeliveryError("VIDEO_DOWNLOAD_URL_INVALID", retryable=False)
        return [
            {
                "hostname": host,
                "host": item[4][0],
                "port": port,
                "family": item[0],
                "proto": item[2],
                "flags": 0,
            }
            for item in results
        ]

    async def close(self):
        pass


def _validate_public_http_url(url: str) -> None:
    try:
        # Match aiohttp's canonical host, including IDNA-normalized IP literals.
        parsed = URL(url)
        parsed.port
    except ValueError as exc:
        raise VideoDeliveryError("VIDEO_DOWNLOAD_URL_INVALID", retryable=False) from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.raw_host
        or parsed.user is not None
        or parsed.password is not None
    ):
        raise VideoDeliveryError("VIDEO_DOWNLOAD_URL_INVALID", retryable=False)
    try:
        address = ipaddress.ip_address(parsed.raw_host)
    except ValueError:
        return
    if not address.is_global:
        raise VideoDeliveryError("VIDEO_DOWNLOAD_URL_INVALID", retryable=False)


async def _validate_public_redirect(session, context, params) -> None:
    location = params.response.headers.get("Location") or params.response.headers.get(
        "URI"
    )
    if location:
        _validate_public_http_url(urljoin(str(params.response.url), location))


def validate_public_http_url(url: str) -> None:
    """Public entry point for callers that fetch a URL they did not author.

    Raises ``VideoDeliveryError`` when the URL is not a public http(s)
    destination. Sharing this instead of re-deriving the rule keeps one
    definition of "public destination" for downloads and page reads.
    """

    _validate_public_http_url(url)


def public_http_session_options() -> dict[str, object]:
    """aiohttp session options that resolve, pin, and re-check public hosts.

    Callers pass the result to ``aiohttp.ClientSession`` so redirects and DNS
    answers are validated exactly as ``download_to_path(public_only=True)``
    does.
    """

    trace = aiohttp.TraceConfig()
    trace.on_request_redirect.append(_validate_public_redirect)
    return {
        "connector": aiohttp.TCPConnector(
            resolver=_PublicResolver(), use_dns_cache=False
        ),
        "trace_configs": [trace],
    }


async def download_to_path(
    url: str,
    output_path: str,
    *,
    expected_size: int | None = None,
    expected_sha256: str | None = None,
    headers: Mapping[str, str] | None = None,
    public_only: bool = False,
    on_progress: Callable[[int, int | None], None] | None = None,
    progress_interval_seconds: float = 15.0,
) -> VideoDeliveryReceipt:
    """Stream media into a sibling temp file and atomically publish it."""

    if public_only:
        _validate_public_http_url(url)

    destination = Path(output_path)
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".part", dir=destination.parent
        )
    except OSError as exc:
        raise VideoDeliveryError("VIDEO_PROJECT_WRITE_FAILED", retryable=True) from exc
    digest = hashlib.sha256()
    size = 0
    try:
        with os.fdopen(fd, "wb") as stream:
            if url.startswith("data:"):
                header, separator, encoded = url.partition(",")
                if not separator or ";base64" not in header.lower() or not encoded:
                    raise VideoDeliveryError("VIDEO_DATA_URL_INVALID", retryable=False)
                try:
                    chunk = base64.b64decode(encoded, validate=True)
                except (binascii.Error, ValueError) as exc:
                    raise VideoDeliveryError(
                        "VIDEO_DATA_URL_INVALID", retryable=False
                    ) from exc
                stream.write(chunk)
                digest.update(chunk)
                size = len(chunk)
            else:
                timeout = aiohttp.ClientTimeout(
                    total=1800,
                    sock_connect=30,
                    sock_read=120,
                )
                session_options = {"timeout": timeout}
                if public_only:
                    trace = aiohttp.TraceConfig()
                    trace.on_request_redirect.append(_validate_public_redirect)
                    session_options["trace_configs"] = [trace]
                    session_options["connector"] = aiohttp.TCPConnector(
                        resolver=_PublicResolver(), use_dns_cache=False
                    )
                async with aiohttp.ClientSession(**session_options) as session:
                    request_options = {"headers": headers} if headers else {}
                    async with session.get(url, **request_options) as response:
                        if response.status < 200 or response.status >= 300:
                            raise VideoDeliveryError(
                                f"VIDEO_DOWNLOAD_HTTP_{response.status}",
                                retryable=_is_retryable_http_status(response.status),
                            )
                        total_bytes = getattr(response, "content_length", None)
                        last_progress_at = time.monotonic()
                        async for chunk in response.content.iter_chunked(_CHUNK_SIZE):
                            stream.write(chunk)
                            digest.update(chunk)
                            size += len(chunk)
                            if on_progress is not None:
                                now = time.monotonic()
                                if now - last_progress_at >= progress_interval_seconds:
                                    last_progress_at = now
                                    on_progress(size, total_bytes)
            stream.flush()
            os.fsync(stream.fileno())

        actual_sha256 = digest.hexdigest()
        if expected_size is not None and size != expected_size:
            raise VideoDeliveryError("VIDEO_ARCHIVE_SIZE_MISMATCH", retryable=True)
        if expected_sha256 and actual_sha256.lower() != expected_sha256.lower():
            raise VideoDeliveryError("VIDEO_ARCHIVE_SHA256_MISMATCH", retryable=True)
        os.replace(temp_name, destination)
        return VideoDeliveryReceipt(
            method="download",
            size=size,
            sha256=actual_sha256,
        )
    except Exception as exc:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        if isinstance(exc, VideoDeliveryError):
            raise
        if isinstance(exc, (aiohttp.ClientError, TimeoutError)):
            raise VideoDeliveryError("VIDEO_DOWNLOAD_FAILED", retryable=True) from exc
        if isinstance(exc, OSError):
            raise VideoDeliveryError(
                "VIDEO_PROJECT_WRITE_FAILED", retryable=True
            ) from exc
        raise
