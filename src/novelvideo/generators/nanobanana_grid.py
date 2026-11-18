"""图像生成传输层：多 provider（google / openrouter / huimeng / openai / newapi）的单图生成与参考图编辑。"""

import asyncio
import base64
import contextvars
import hashlib
import logging
import math
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, Optional
from urllib.parse import quote, urlparse

from novelvideo.config import get_grid_generation_config
from novelvideo.ports import get_usage_meter, update_current_model_call_log
from novelvideo.egress_context import (
    TrustedEgressContext,
    ambient_organization_egress_context,
)
from novelvideo.ports.authz import AdmissionContext
from novelvideo.ports.egress import EgressError
from novelvideo.ports.egress_operations import (
    OperationClaimResult,
    HandleKind,
    OperationSpec,
    canonical_request_digest,
)
from novelvideo.ports.model_credentials import ModelCredentialError, RequestCredential
from novelvideo.shared.billing_errors import is_fatal_billing_error
from novelvideo.shared.provider_costs import is_definite_no_cost_http_rejection
from novelvideo.generators.huimengi import (
    HuimengTaskFailed,
    HuimengiTaskClient,
    bytes_to_data_url,
    extract_huimeng_result_url,
    validate_huimeng_media_download,
)
from novelvideo.generators.lingke_media import (
    is_lingke_target,
    lingke_api_path,
    set_nested_value,
    value_at_request_path,
)
from novelvideo.storage.media_relay import (
    IMAGE_TRANSFORM_AI_REFERENCE_JPEG,
    _apply_image_transform,
    media_relay_ttl_seconds,
    relay_tenant_image_bytes_from_context,
    upload_image_bytes,
)

_VALID_IMAGE_SIZES = {"512", "1K", "2K", "4K"}
_OPENROUTER_IMAGE_CAPABILITY_CACHE: dict[str, tuple[bool, str]] = {}
_NEWAPI_UNREACHABLE_RELAY_HOSTS: dict[tuple[str, str], float] = {}
_NEWAPI_UNREACHABLE_RELAY_TTL_SECONDS = 900.0
_OPENAI_VALID_QUALITIES = {"low", "medium", "high", "auto"}
_OPENAI_MIN_PIXELS = 655_360
_OPENAI_MAX_PIXELS = 8_294_400
_OPENAI_MAX_EDGE = 3840
_OPENAI_MAX_RATIO = 3.0
_HUIMENG_IMAGE_POLL_INTERVAL_SECONDS = 2.0
_HUIMENG_IMAGE_MAX_POLLS = 290
logger = logging.getLogger(__name__)
NEWAPI_MEDIA_INPUT_MIN_TTL_SECONDS = 2 * 60 * 60


@dataclass(frozen=True, slots=True)
class _OrganizationImageEgress:
    credential: RequestCredential
    operation_port: Any
    claim: OperationClaimResult


def _validate_egress_context(
    egress_context: TrustedEgressContext | None,
) -> TrustedEgressContext | None:
    if egress_context is not None and type(egress_context) is not TrustedEgressContext:
        raise TypeError("egress_context must be a TrustedEgressContext")
    return egress_context


async def _prepare_organization_image_egress(
    *,
    egress_context: TrustedEgressContext | None,
    provider: str,
    capability: str,
    request: dict[str, object],
    business_task_id: str | None = None,
) -> _OrganizationImageEgress | None:
    context = _validate_egress_context(egress_context)
    if context is None:
        context = ambient_organization_egress_context()
    if context is None or not context.is_organization:
        return None
    if provider != "newapi":
        raise EgressError("ORG_EGRESS_DENIED")

    from novelvideo.ports import get_egress_operation_port, get_model_credentials

    admission = AdmissionContext(
        requester_user_id=context.requester_user_id,
        billing_principal=context.billing_principal,
        credential=context.credential,
        admission_id=context.admission_id,
        root_task_id=context.root_task_id,
        admitted_at=context.admitted_at,
        membership_id=context.membership_id,
        authz_version=context.authz_version,
    )
    operation_port = get_egress_operation_port()
    claim = await operation_port.claim(
        spec=OperationSpec(
            organization_id=context.billing_principal.id,
            project_id=context.project_id,
            root_task_id=context.root_task_id,
            business_task_id=business_task_id or context.envelope_id,
            capability=capability,
            credential_id=context.credential.credential_id,
            credential_version=context.credential.key_version,
            request_digest=canonical_request_digest(request),
            handle_kind=HandleKind.PROVIDER_JOB,
        )
    )
    if not claim.won:
        raise EgressError("EGRESS_OPERATION_REPLAYED")
    try:
        credential = await get_model_credentials().resolve(admission)
        if (
            type(credential) is not RequestCredential
            or credential.reference != context.credential
        ):
            raise ModelCredentialError("ORG_CREDENTIAL_VERSION_MISMATCH")
    except Exception:
        await operation_port.mark_rejected_before_submit(
            operation_id=claim.operation.operation_id,
            transition_token=claim.transition_token,
            expected_version=claim.operation.version,
        )
        raise
    return _OrganizationImageEgress(
        credential=credential,
        operation_port=operation_port,
        claim=claim,
    )


async def _complete_organization_image_egress(
    state: _OrganizationImageEgress | None,
    *,
    trace: dict[str, str],
    result_ref: str,
) -> None:
    if state is None:
        return
    provider_job_id = str(
        trace.get("request_id") or trace.get("response_id") or ""
    ).strip()
    if not provider_job_id:
        await state.operation_port.mark_unknown(
            operation_id=state.claim.operation.operation_id,
            transition_token=state.claim.transition_token,
            expected_version=state.claim.operation.version,
        )
        raise RuntimeError("gateway response is missing a request id")
    accepted = await state.operation_port.mark_accepted(
        operation_id=state.claim.operation.operation_id,
        transition_token=state.claim.transition_token,
        expected_version=state.claim.operation.version,
        provider_job_id=provider_job_id,
    )
    await state.operation_port.mark_completed(
        operation_id=state.claim.operation.operation_id,
        transition_token=state.claim.transition_token,
        expected_version=accepted.version,
        result_ref=result_ref,
    )


async def _abandon_organization_image_egress(
    state: _OrganizationImageEgress | None,
) -> None:
    """Close out a claim whose outcome we cannot observe.

    Deliberately `mark_unknown` rather than `mark_rejected_before_submit`: once the
    credential is out of our hands we cannot prove the request was never submitted,
    and only the ledger's unknown state is honest about that.
    """

    if state is None:
        return
    await state.operation_port.mark_unknown(
        operation_id=state.claim.operation.operation_id,
        transition_token=state.claim.transition_token,
        expected_version=state.claim.operation.version,
    )


_SCENE_REFERENCE_FEATURE_BILLING = contextvars.ContextVar(
    "scene_reference_feature_billing", default=False
)


class scene_reference_feature_billing:
    """Temporarily delegate scene-reference billing to the EE feature ledger."""

    def __enter__(self):
        self._token = _SCENE_REFERENCE_FEATURE_BILLING.set(True)
        return self

    def __exit__(self, exc_type, exc, tb):
        _SCENE_REFERENCE_FEATURE_BILLING.reset(self._token)


def _scene_reference_feature_billing_active() -> bool:
    return _SCENE_REFERENCE_FEATURE_BILLING.get()


async def _mark_paid_image_attempt_accepted(
    *,
    provider_request_id: str = "",
    response_id: str = "",
) -> None:
    try:
        await get_usage_meter().mark_current_paid_execution_attempt(
            status="accepted",
            provider_request_id=provider_request_id,
            provider_response_id=response_id,
        )
    except Exception:
        pass


def _newapi_request_id_from_headers(headers: Any) -> str:
    if not headers:
        return ""
    return (
        headers.get("x-request-id")
        or headers.get("x-newapi-request-id")
        or headers.get("x-oneapi-request-id")
        or ""
    )


NEWAPI_IMAGE_HTTP_TIMEOUT_SECONDS = 1800.0
_LINGKE_IMAGE_POLL_INTERVAL_SECONDS = 5.0
_LINGKE_IMAGE_MAX_REFERENCES = 10


def _newapi_safe_header_summary(headers: Any) -> dict[str, str]:
    if not headers:
        return {}
    safe_keys = ("x-request-id", "x-newapi-request-id", "x-oneapi-request-id", "cf-ray", "date")
    summary: dict[str, str] = {}
    for key in safe_keys:
        value = str(headers.get(key) or "").strip()
        if value:
            summary[key] = value
    return summary


def _newapi_safe_request_context(
    *,
    endpoint: str,
    request_path: str,
    model: str,
    payload: dict[str, object],
    prompt: str,
) -> dict[str, object]:
    media_params = payload.get("params")
    media_params = media_params if isinstance(media_params, dict) else {}
    reference_images = (
        payload.get("image") or payload.get("images") or media_params.get("images")
    )
    reference_image_count = len(reference_images) if isinstance(reference_images, list) else 0
    raw_metadata = payload.get("metadata")
    metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
    geometry_metadata = {
        key: metadata[key]
        for key in ("ratio", "aspect_ratio", "resolution")
        if key in metadata
    }
    if not geometry_metadata:
        if media_params.get("aspect_ratio"):
            geometry_metadata["ratio"] = media_params["aspect_ratio"]
        if media_params.get("size"):
            geometry_metadata["resolution"] = str(media_params["size"]).lower()
    return {
        "endpoint": f"{endpoint}{request_path}",
        "model": model,
        "payload_keys": sorted(payload.keys()),
        "width": payload.get("width") or "",
        "height": payload.get("height") or "",
        "geometry_metadata": geometry_metadata,
        "reference_image_count": reference_image_count,
        "prompt_chars": len(prompt or ""),
        "prompt_sha256": hashlib.sha256((prompt or "").encode("utf-8")).hexdigest()[:16],
    }


_is_lingke_image_target = is_lingke_target
_lingke_image_path = lingke_api_path
_value_at_request_path = value_at_request_path
_set_nested_value = set_nested_value


def _build_lingke_image_payload(
    payload: dict[str, object],
    *,
    request_schema: dict[str, object],
    aspect_ratio: str,
    image_size: str,
) -> dict[str, object]:
    params: dict[str, object] = {}
    size = str(image_size or "").strip().upper()
    ratio = str(aspect_ratio or "").strip().lower()
    if size:
        params["size"] = size
    if ratio not in {"", "auto", "adaptive"}:
        params["aspect_ratio"] = ratio
    output_format = str(payload.get("output_format") or "").strip().lower()
    if output_format:
        params["output_format"] = output_format

    image_values = payload.get("images") or payload.get("image")
    if isinstance(image_values, list):
        normalized_images: list[object] = []
        for item in image_values:
            if isinstance(item, dict):
                image_url = item.get("image_url")
                if image_url:
                    normalized_images.append(image_url)
            elif item:
                normalized_images.append(item)
        if len(normalized_images) > _LINGKE_IMAGE_MAX_REFERENCES:
            raise ValueError(
                f"Lingke accepts at most {_LINGKE_IMAGE_MAX_REFERENCES} reference images"
            )
        if normalized_images:
            params["images"] = normalized_images

    transport_fields = {
        "model",
        "prompt",
        "n",
        "response_format",
        "watermark",
        "metadata",
        "width",
        "height",
        "quality",
        "input_fidelity",
        "image",
        "images",
        "output_format",
    }
    for definition in request_schema.get("parameters") or []:
        if not isinstance(definition, dict):
            continue
        path = str(definition.get("requestPath") or "").strip()
        if not path:
            continue
        value = _value_at_request_path(payload, path)
        if value is None:
            continue
        relative_path = path.removeprefix("params.")
        root = relative_path.split(".", 1)[0]
        if root in transport_fields:
            continue
        if root == "ratio":
            relative_path = "aspect_ratio" + relative_path[len("ratio") :]
        elif root == "resolution":
            relative_path = "size" + relative_path[len("resolution") :]
        _set_nested_value(params, relative_path, value)

    return {
        "model": payload.get("model"),
        "prompt": payload.get("prompt"),
        "params": params,
    }


async def _poll_lingke_image_result(
    client: Any,
    *,
    endpoint: str,
    api_key: str,
    task_id: str,
) -> dict[str, object]:
    status_path = _lingke_image_path(
        endpoint,
        f"skills/task-status?task_id={quote(task_id, safe='')}",
    )
    headers = {"Authorization": f"Bearer {api_key}"}
    deadline = time.monotonic() + NEWAPI_IMAGE_HTTP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        await asyncio.sleep(_LINGKE_IMAGE_POLL_INTERVAL_SECONDS)
        response = await client.get(f"{endpoint}{status_path}", headers=headers)
        response.raise_for_status()
        response_data = response.json()
        if not isinstance(response_data, dict):
            raise ValueError("Lingke task status response must be an object")
        nested = response_data.get("data")
        result = nested if isinstance(nested, dict) else response_data
        if result.get("is_final") is not True:
            continue
        state = str(result.get("state") or "").strip().lower()
        if state in {"success", "succeeded"}:
            return result
        detail = str(result.get("error") or result.get("status") or state or "unknown")
        raise RuntimeError(f"Lingke image task failed: {detail}")
    raise TimeoutError("Lingke image task timed out")


def _newapi_context_for_error(context: dict[str, object]) -> str:
    return (
        f"model={context.get('model')}; "
        f"endpoint={context.get('endpoint')}; "
        f"payload_keys={context.get('payload_keys')}; "
        f"width={context.get('width')}; "
        f"height={context.get('height')}; "
        f"geometry_metadata={context.get('geometry_metadata')}; "
        f"reference_image_count={context.get('reference_image_count')}; "
        f"prompt_sha256={context.get('prompt_sha256')}"
    )


class _InlineImagePart:
    """Provider-neutral image part used by OpenRouter/OpenAI branches."""

    def __init__(self, data: bytes, mime_type: str = "image/png"):
        self.inline_data = SimpleNamespace(data=data, mime_type=mime_type)


def normalize_image_size(size: str, provider: str = "google") -> str:
    """Normalize image_size across providers.

    Internal configs may still use 0.5K, but providers expect different values:
    - Gemini direct: 512
    - OpenRouter: 1K (0.5K currently triggers INVALID_ARGUMENT on Gemini image routes)
    """
    if provider in {"huimeng", "newapi"} and size == "0.5K":
        return "1K"
    if size == "0.5K":
        return "1K" if provider == "openrouter" else "512"
    return size


def _newapi_image_model_supports_quality(model: str | None) -> bool:
    model_name = str(model or "").strip().lower()
    return model_name in {
        "lingshan-g2",
        "gpt-image-2",
        "image-2",
        "image-2-official",
    } or "gpt-image" in model_name


def _image_credit_billing_params(
    *,
    image_size: str | None = None,
    quality: str | None = None,
) -> dict[str, str]:
    params: dict[str, str] = {}
    clean_size = str(image_size or "").strip().lower()
    if clean_size:
        params["size"] = clean_size
    clean_quality = str(quality or "").strip().lower()
    if clean_quality:
        params["quality"] = clean_quality
    return params


def _huimeng_image_resolution_for_model(model: str, image_size: str | None) -> str:
    """Map local image_size labels to HuiMeng model resolution params when supported."""
    model_name = (model or "").strip()
    image2_family = model_name in {"image-2", "image-2-official"}
    if not (
        model_name.startswith(("nb-", "seedream-")) or image2_family or "gpt-image" in model_name
    ):
        return ""

    normalized = normalize_image_size(str(image_size or "").strip(), provider="huimeng")
    if image2_family:
        lower = normalized.lower()
        return lower if lower in {"1k", "2k", "4k"} else ""
    return normalized if normalized in {"1K", "2K", "3K", "4K"} else ""


def _round_openai_edge(value: float) -> int:
    return max(16, int(math.ceil(value / 16.0)) * 16)


def resolve_openai_image_size(
    aspect_ratio: str = "1:1",
    image_size: str = "1K",
    model: str | None = None,
    *,
    allow_dynamic_resolution: bool = False,
    min_pixels: int | None = None,
) -> str:
    """Map internal aspect/image_size labels to GPT Image 2 size strings.

    gpt-image-2 supports flexible sizes, but they must satisfy OpenAI's documented
    constraints: both edges are multiples of 16, max edge <= 3840, ratio <= 3:1,
    and total pixels within the valid range. "1K" here means the smallest valid
    draft size near a 1024px long edge.
    """

    ratio_text = str(aspect_ratio or "1:1").replace("-", ":")
    try:
        raw_w, raw_h = [float(part) for part in ratio_text.split(":", 1)]
        if raw_w <= 0 or raw_h <= 0:
            raise ValueError
    except Exception:
        raw_w, raw_h = 1.0, 1.0

    ratio = raw_w / raw_h
    if ratio > _OPENAI_MAX_RATIO:
        ratio = _OPENAI_MAX_RATIO
    elif ratio < 1.0 / _OPENAI_MAX_RATIO:
        ratio = 1.0 / _OPENAI_MAX_RATIO

    normalized_size = normalize_image_size(str(image_size or "1K"), provider="openai")
    long_edges = {
        "512": 1024,
        "0.5K": 1024,
        "0.5k": 1024,
        "1K": 1024,
        "1k": 1024,
        "2K": 2048,
        "2k": 2048,
        "3K": 3072,
        "3k": 3072,
        "4K": 3840,
        "4k": 3840,
    }
    long_edge = long_edges.get(normalized_size)
    dynamic_max_edge = _OPENAI_MAX_EDGE
    dynamic_max_pixels = _OPENAI_MAX_PIXELS
    explicit_dimensions: tuple[int, int] | None = None
    if long_edge is None and allow_dynamic_resolution:
        explicit_size = re.fullmatch(r"(\d+)\s*[xX×]\s*(\d+)", normalized_size)
        if explicit_size:
            width_i, height_i = (int(value) for value in explicit_size.groups())
            if width_i <= 0 or height_i <= 0:
                raise ValueError(f"invalid image resolution: {image_size}")
            explicit_dimensions = (width_i, height_i)
        k_size = re.fullmatch(r"(\d+(?:\.\d+)?)\s*[kK]", normalized_size)
        if explicit_dimensions is not None:
            pass
        elif k_size:
            long_edge = max(16, _round_openai_edge(float(k_size.group(1)) * 1024))
            dynamic_max_edge = long_edge
            dynamic_max_pixels = long_edge * long_edge
        else:
            raise ValueError(
                f"unsupported image resolution: {image_size}; "
                "use a value such as 2K, 3K, 8K, or 2048x2048"
            )
    if long_edge is None:
        long_edge = 1024
    configured_min_pixels = (
        min_pixels
        if type(min_pixels) is int and min_pixels > 0
        else None
    )
    effective_min_pixels = configured_min_pixels or _OPENAI_MIN_PIXELS
    max_pixels = dynamic_max_pixels

    if explicit_dimensions is not None:
        width, height = (float(value) for value in explicit_dimensions)
    elif ratio >= 1:
        width = float(long_edge)
        height = width / ratio
    else:
        height = float(long_edge)
        width = height * ratio

    pixel_count = width * height
    if pixel_count < effective_min_pixels:
        scale = math.sqrt(effective_min_pixels / pixel_count)
        width *= scale
        height *= scale
    elif explicit_dimensions is None and pixel_count > max_pixels:
        scale = math.sqrt(max_pixels / pixel_count)
        width *= scale
        height *= scale

    if explicit_dimensions is not None and configured_min_pixels is None:
        return f"{explicit_dimensions[0]}x{explicit_dimensions[1]}"

    max_edge = (
        max(dynamic_max_edge, _round_openai_edge(width), _round_openai_edge(height))
        if explicit_dimensions is not None
        else dynamic_max_edge
    )
    width_i = min(max_edge, _round_openai_edge(width))
    height_i = min(max_edge, _round_openai_edge(height))

    if width_i * height_i < effective_min_pixels:
        scale = math.sqrt(effective_min_pixels / max(1, width_i * height_i))
        width_i = min(max_edge, _round_openai_edge(width_i * scale))
        height_i = min(max_edge, _round_openai_edge(height_i * scale))

    return f"{width_i}x{height_i}"


def normalize_openai_quality(value: str | None, default: str = "medium") -> str:
    quality = str(value or default or "medium").strip().lower()
    return quality if quality in _OPENAI_VALID_QUALITIES else default


def _extract_openai_unknown_parameter(error_detail: str) -> str:
    match = re.search(r"Unknown parameter:\s*'([^']+)'", error_detail or "")
    if match:
        return match.group(1)
    match = re.search(r'Unknown parameter:\s*"([^"]+)"', error_detail or "")
    if match:
        return match.group(1)
    match = re.search(r"Unsupported parameter:\s*'([^']+)'", error_detail or "")
    if match:
        return match.group(1)
    match = re.search(r'Unsupported parameter:\s*"([^"]+)"', error_detail or "")
    if match:
        return match.group(1)
    match = re.search(r"'param':\s*'([^']+)'", error_detail or "")
    if match:
        return match.group(1)
    match = re.search(r'"param":\s*"([^"]+)"', error_detail or "")
    if match:
        return match.group(1)
    for parameter in ("output_format", "quality", "input_fidelity"):
        if parameter in (error_detail or ""):
            return parameter
    if "input_fidelity" in (error_detail or ""):
        return "input_fidelity"
    return ""


def _truncate_openrouter_debug(value: object, limit: int = 240) -> str:
    """截断 OpenRouter 调试字段，避免日志过长。"""
    text = str(value or "")
    if len(text) <= limit:
        return text
    return f"{text[:limit]}..."


async def _check_openrouter_image_capability(api_key: str, model: str) -> tuple[bool, str]:
    """检查 OpenRouter 模型是否声明支持 image output。"""
    import httpx

    cache_key = f"{model}:{hashlib.sha1((api_key or '').encode('utf-8')).hexdigest()[:8]}"
    cached = _OPENROUTER_IMAGE_CAPABILITY_CACHE.get(cache_key)
    if cached is not None:
        return cached

    base_url = "https://openrouter.ai/api/v1"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://novelvideo.ai",
        "X-Title": "NovelVideo Studio",
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(f"{base_url}/models", headers=headers)
            response.raise_for_status()
            result = response.json()

        models = result.get("data", [])
        model_info = next((item for item in models if item.get("id") == model), None)
        if not model_info:
            detail = f"模型 {model} 不在 OpenRouter /models 列表中，跳过 image capability 预检"
            print(f"[OpenRouter] {detail}")
            outcome = (True, detail)
            _OPENROUTER_IMAGE_CAPABILITY_CACHE[cache_key] = outcome
            return outcome

        output_modalities = (model_info.get("architecture") or {}).get("output_modalities") or []
        supports_image = "image" in output_modalities
        detail = (
            f"model={model}, output_modalities={output_modalities}"
            if output_modalities
            else f"model={model}, output_modalities=[]"
        )
        outcome = (supports_image, detail)
        _OPENROUTER_IMAGE_CAPABILITY_CACHE[cache_key] = outcome
        return outcome
    except Exception as exc:
        detail = "image capability 预检失败，跳过阻断: " f"{type(exc).__name__}: {exc!r}"
        print(f"[OpenRouter] {detail}")
        outcome = (True, detail)
        _OPENROUTER_IMAGE_CAPABILITY_CACHE[cache_key] = outcome
        return outcome


def clamp_image_size(size: str) -> str:
    """Clamp image_size to values accepted by Gemini image APIs."""
    normalized = normalize_image_size(size, provider="google")
    return normalized if normalized in _VALID_IMAGE_SIZES else "1K"


# =============================================================================
# 模式配置表（Mode Configs）
# =============================================================================
# mode_key 格式: "{rows}x{cols}_{aspect_ratio_normalized}"
# 供计费（model_credits）按 mode_key 查询出图尺寸

REGEN_MODE_CONFIGS: Dict[str, dict] = {
    "1x1_1-1": {
        "rows": 1,
        "cols": 1,
        "aspect_ratio": "1:1",
        "image_size": "1K",
        "label": "1x1_1:1 1K",
        "capacity": 1,
        "model": "nanobanana",
    },
    "1x1_9-16": {
        "rows": 1,
        "cols": 1,
        "aspect_ratio": "9:16",
        "image_size": "1K",
        "label": "1x1_9:16 1K",
        "capacity": 1,
        "model": "nanobanana",
    },
    "1x1_2-3": {
        "rows": 1,
        "cols": 1,
        "aspect_ratio": "2:3",
        "image_size": "1K",
        "label": "1x1_2:3 1K",
        "capacity": 1,
        "model": "nanobanana",
    },
    "1x1_16-9": {
        "rows": 1,
        "cols": 1,
        "aspect_ratio": "16:9",
        "image_size": "1K",
        "label": "1x1_16:9 1K",
        "capacity": 1,
        "model": "nanobanana",
    },
    "1x2_4-3": {
        "rows": 1,
        "cols": 2,
        "aspect_ratio": "4:3",
        "image_size": "1K",
        "label": "1x2_4:3 1K",
        "capacity": 2,
        "model": "nanobanana",
    },
    "1x2_16-9": {
        "rows": 1,
        "cols": 2,
        "aspect_ratio": "16:9",
        "image_size": "1K",
        "label": "1x2_16:9 1K",
        "capacity": 2,
        "model": "nanobanana",
    },
    "1x3_16-9": {
        "rows": 1,
        "cols": 3,
        "aspect_ratio": "16:9",
        "image_size": "1K",
        "label": "1x3_16:9 1K",
        "capacity": 3,
        "model": "nanobanana",
    },
    "1x3_21-9": {
        "rows": 1,
        "cols": 3,
        "aspect_ratio": "21:9",
        "image_size": "1K",
        "label": "1x3_21:9 1K",
        "capacity": 3,
        "model": "nanobanana",
    },
    "1x4_21-9": {
        "rows": 1,
        "cols": 4,
        "aspect_ratio": "21:9",
        "image_size": "1K",
        "label": "1x4_21:9 1K",
        "capacity": 4,
        "model": "nanobanana",
    },
    "1x6_4-1": {
        "rows": 1,
        "cols": 6,
        "aspect_ratio": "4:1",
        "image_size": "1K",
        "label": "1x6_4:1 1K",
        "capacity": 6,
        "model": "nanobanana",
    },
    "2x2_1-1": {
        "rows": 2,
        "cols": 2,
        "aspect_ratio": "1:1",
        "image_size": "2K",
        "label": "2x2_1:1 2K",
        "capacity": 4,
        "model": "nanobanana",
    },
    "2x2_9-16": {
        "rows": 2,
        "cols": 2,
        "aspect_ratio": "9:16",
        "image_size": "2K",
        "label": "2x2_9:16 2K",
        "capacity": 4,
        "model": "nanobanana",
    },
    "2x3_1-1": {
        "rows": 2,
        "cols": 3,
        "aspect_ratio": "1:1",
        "image_size": "2K",
        "label": "2x3_1:1 2K",
        "capacity": 6,
        "model": "nanobanana",
    },
    "2x4_4-3": {
        "rows": 2,
        "cols": 4,
        "aspect_ratio": "4:3",
        "image_size": "2K",
        "label": "2x4_4:3 2K",
        "capacity": 8,
        "model": "nanobanana",
    },
    "2x2_2-3": {
        "rows": 2,
        "cols": 2,
        "aspect_ratio": "2:3",
        "image_size": "2K",
        "label": "2x2_2:3 2K",
        "capacity": 4,
        "model": "nanobanana",
    },
    "2x2_16-9": {
        "rows": 2,
        "cols": 2,
        "aspect_ratio": "16:9",
        "image_size": "2K",
        "label": "2x2_16:9 2K",
        "capacity": 4,
        "model": "nanobanana",
    },
    "3x2_9-16": {
        "rows": 3,
        "cols": 2,
        "aspect_ratio": "9:16",
        "image_size": "2K",
        "label": "3x2_9:16 2K",
        "capacity": 6,
        "model": "nanobanana",
    },
    "3x2_2-3": {
        "rows": 3,
        "cols": 2,
        "aspect_ratio": "2:3",
        "image_size": "2K",
        "label": "3x2_2:3 2K",
        "capacity": 6,
        "model": "nanobanana",
    },
    "3x3_1-1": {
        "rows": 3,
        "cols": 3,
        "aspect_ratio": "1:1",
        "image_size": "4K",
        "label": "3x3_1:1 4K",
        "capacity": 9,
        "model": "nanobanana",
    },
    "3x3_9-16": {
        "rows": 3,
        "cols": 3,
        "aspect_ratio": "9:16",
        "image_size": "4K",
        "label": "3x3_9:16 4K",
        "capacity": 9,
        "model": "nanobanana",
    },
    "3x3_2-3": {
        "rows": 3,
        "cols": 3,
        "aspect_ratio": "2:3",
        "image_size": "2K",
        "label": "3x3_2:3 2K",
        "capacity": 9,
        "model": "nanobanana",
    },
    "3x3_16-9": {
        "rows": 3,
        "cols": 3,
        "aspect_ratio": "16:9",
        "image_size": "4K",
        "label": "3x3_16:9 4K",
        "capacity": 9,
        "model": "nanobanana",
    },
    "4x3_9-16": {
        "rows": 4,
        "cols": 3,
        "aspect_ratio": "9:16",
        "image_size": "4K",
        "label": "4x3_9:16 4K",
        "capacity": 12,
        "model": "nanobanana",
    },
    "4x3_3-4": {
        "rows": 4,
        "cols": 3,
        "aspect_ratio": "3:4",
        "image_size": "4K",
        "label": "4x3_3:4 4K",
        "capacity": 12,
        "model": "nanobanana",
    },
    "4x4_1-1": {
        "rows": 4,
        "cols": 4,
        "aspect_ratio": "1:1",
        "image_size": "4K",
        "label": "4x4_1:1 4K",
        "capacity": 16,
        "model": "nanobanana",
    },
    "4x4_16-9": {
        "rows": 4,
        "cols": 4,
        "aspect_ratio": "16:9",
        "image_size": "4K",
        "label": "4x4_16:9 4K",
        "capacity": 16,
        "model": "nanobanana",
    },
    "5x4_9-16": {
        "rows": 5,
        "cols": 4,
        "aspect_ratio": "9:16",
        "image_size": "4K",
        "label": "5x4_9:16 4K",
        "capacity": 20,
        "model": "nanobanana",
    },
    "5x5_1-1": {
        "rows": 5,
        "cols": 5,
        "aspect_ratio": "1:1",
        "image_size": "4K",
        "label": "5x5_1:1 4K",
        "capacity": 25,
        "model": "nanobanana",
    },
}


def resolve_render_reference_order(
    ctx,
    beats: list[dict],
    grid_capacity: int,
    valid_character_map: dict,
) -> list[str]:
    """统一 Render 模式参考图顺序。

    Single source of truth:
    1. 只复用 prompt_builder 在 build() 阶段解析出的顺序
    2. 缺失即报错，禁止运行时重新推导导致所见非所得
    """
    if not valid_character_map:
        return []

    ordered_chars = list(getattr(ctx, "resolved_render_chars", []) or [])
    if ordered_chars:
        return ordered_chars

    raise RuntimeError(
        "Render reference order missing: ctx.resolved_render_chars was not populated "
        "by prompt_builder. Refusing to recompute attachment order at runtime."
    )


async def generate_text_to_image(
    prompt: str,
    output_path: str,
    *,
    aspect_ratio: str = "1:1",
    image_size: str = "2K",
    quality: str | None = None,
    api_key: Optional[str] = None,
    config: Optional[dict] = None,
    egress_context: TrustedEgressContext | None = None,
    egress_capability: str = "image.generate",
) -> Path:
    """Generate one image from a prompt only — no reference images.

    Routes through the same 4 providers as `generate_reference_edit_image`
    (google / openrouter / huimeng / openai). Use `config` to override the
    provider/model picked from env defaults.
    """
    return await _generate_image(
        prompt=prompt,
        reference_image_paths=[],
        output_path=output_path,
        aspect_ratio=aspect_ratio,
        image_size=image_size,
        quality=quality,
        api_key=api_key,
        config=config,
        egress_context=egress_context,
        egress_capability=egress_capability,
    )


async def generate_reference_edit_image(
    prompt: str,
    reference_images: list[str],
    output_path: str,
    *,
    aspect_ratio: str = "2:3",
    image_size: str = "2K",
    quality: str | None = None,
    api_key: Optional[str] = None,
    config: Optional[dict] = None,
    egress_context: TrustedEgressContext | None = None,
    egress_capability: str = "image.edit",
) -> Path:
    """Generate one edited image from reference images plus a free-form edit prompt.

    `config` lets callers override the provider / model selection (passed through
    to `NanoBananaGridGenerator`); when None, falls back to env-var defaults via
    `get_grid_generation_config()`.
    """
    ref_paths = [path for path in reference_images if path and os.path.exists(path)]
    if not ref_paths:
        raise FileNotFoundError("No valid reference images provided for edit generation")
    return await _generate_image(
        prompt=prompt,
        reference_image_paths=ref_paths,
        output_path=output_path,
        aspect_ratio=aspect_ratio,
        image_size=image_size,
        quality=quality,
        api_key=api_key,
        config=config,
        egress_context=egress_context,
        egress_capability=egress_capability,
    )


async def _generate_image(
    *,
    prompt: str,
    reference_image_paths: list[str],
    output_path: str,
    aspect_ratio: str,
    image_size: str,
    quality: str | None,
    api_key: Optional[str],
    config: Optional[dict],
    egress_context: TrustedEgressContext | None,
    egress_capability: str,
) -> Path:
    """Shared body for text-only and image-edit single-image generation."""
    ref_paths = list(reference_image_paths or [])
    context = _validate_egress_context(egress_context)
    if context is None:
        # claim 本身不漏（`_prepare_organization_image_egress` 自己回落到作用域），漏的是
        # 转发给叶子的身份：`context` 留成 None，参考图中继就静默走平台分支而不是
        # `relay_tenant_image_bytes_from_context`。只补组织这一支——把平台身份也回落
        # 进去，会让平台流量撞上组织 deny 闸门（OI-48 的房规）。
        context = ambient_organization_egress_context()
    configured_provider = (
        str((config or {}).get("provider") or "newapi").strip().lower()
    )
    request = {
        "model": str((config or {}).get("model") or ""),
        "prompt": prompt,
        "reference_sha256": [
            hashlib.sha256(Path(path).read_bytes()).hexdigest() for path in ref_paths
        ],
        "aspect_ratio": aspect_ratio,
        "image_size": image_size,
        "quality": quality or "",
    }
    organization_egress = await _prepare_organization_image_egress(
        egress_context=context,
        provider=configured_provider,
        capability=egress_capability,
        request=request,
    )
    effective_config = config
    effective_api_key = api_key
    if organization_egress is not None:
        effective_config = dict(config or {})
        effective_config.update(
            {
                "provider": "newapi",
                "api_key": organization_egress.credential.api_key,
                "base_url": organization_egress.credential.base_url,
            }
        )
        effective_api_key = organization_egress.credential.api_key
    generator = NanoBananaGridGenerator(
        api_key=effective_api_key, config=effective_config
    )

    if generator.provider == "openrouter":
        ref_bytes = [Path(path).read_bytes() for path in ref_paths]
        image_bytes, _, error_detail = await _call_openrouter_image_api(
            api_key=generator.api_key,
            model=generator.model,
            prompt=prompt,
            reference_images=ref_bytes,
            image_config={"aspect_ratio": aspect_ratio, "image_size": image_size},
        )
        if not image_bytes:
            raise ValueError(
                f"OpenRouter edit image generation failed: {error_detail or 'empty image'}"
            )
    elif generator.provider == "huimeng":
        ref_bytes = [Path(path).read_bytes() for path in ref_paths]
        image_bytes, _, error_detail = await _call_huimeng_image_api(
            api_key=generator.api_key,
            model=generator.model,
            prompt=prompt,
            reference_images=ref_bytes,
            image_config={
                "aspect_ratio": aspect_ratio,
                "image_size": image_size,
                "quality": quality or generator.huimeng_image_quality,
                "huimeng_image_quality": quality or generator.huimeng_image_quality,
            },
        )
        if not image_bytes:
            raise ValueError(
                f"HuiMeng edit image generation failed: {error_detail or 'empty image'}"
            )
    elif generator.provider == "openai":
        ref_bytes = [Path(path).read_bytes() for path in ref_paths]
        image_bytes, _, error_detail = await _call_openai_image_api(
            api_key=generator.api_key,
            model=generator.model,
            prompt=prompt,
            reference_images=ref_bytes,
            image_config={
                "aspect_ratio": aspect_ratio,
                "image_size": image_size,
                "quality": quality or generator.openai_image_quality,
                "output_format": "png",
            },
        )
        if not image_bytes:
            raise ValueError(
                f"OpenAI edit image generation failed: {error_detail or 'empty image'}"
            )
    elif generator.provider == "newapi":
        ref_bytes = [(Path(path).read_bytes(), path) for path in ref_paths]
        trace: dict[str, str] = {}
        delivery_state: dict[str, bool | str] = {}
        image_bytes, _, error_detail = await _call_newapi_image_api(
            api_key=generator.api_key,
            model=generator.model,
            prompt=prompt,
            reference_images=ref_bytes or None,
            image_config={
                "aspect_ratio": aspect_ratio,
                "image_size": image_size,
                "quality": quality or generator.openai_image_quality,
                "request_schema": generator.newapi_request_schema,
                "model_params": generator.newapi_model_params,
            },
            base_url=generator.base_url,
            trace=trace,
            egress_context=context,
            delivery_path=output_path,
            delivery_state=delivery_state,
            read_copied_bytes=False,
        )
        if not image_bytes and not delivery_state.get("copied"):
            raise ValueError(
                f"DramaClawAPI image generation failed: {error_detail or 'empty image'}"
            )
    else:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=generator.api_key)
        contents = [prompt]
        for ref_path in ref_paths:
            ref_image = generator._load_image_as_part(ref_path)
            if ref_image:
                contents.append(ref_image)

        is_gemini3 = "gemini-3" in generator.model
        if is_gemini3:
            image_config = types.ImageConfig(
                aspect_ratio=aspect_ratio,
                image_size=image_size,
            )
        else:
            image_config = types.ImageConfig(aspect_ratio=aspect_ratio)

        response = await asyncio.to_thread(
            client.models.generate_content,
            model=generator.model,
            contents=contents,
            config=types.GenerateContentConfig(
                response_modalities=["IMAGE", "TEXT"],
                image_config=image_config,
            ),
        )
        image_bytes = None
        if response.candidates and response.candidates[0].content:
            for part in response.candidates[0].content.parts:
                if hasattr(part, "inline_data") and part.inline_data:
                    image_bytes = part.inline_data.data
                    break
        if not image_bytes:
            raise ValueError("Google edit image generation returned no image data")

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    if not (generator.provider == "newapi" and delivery_state.get("copied")):
        output.write_bytes(image_bytes)
    await _complete_organization_image_egress(
        organization_egress,
        trace=trace if generator.provider == "newapi" else {},
        result_ref=str(output),
    )
    return output


async def _call_openrouter_image_api(
    api_key: str,
    model: str,
    prompt: str,
    reference_images: list[bytes] | None = None,
    image_config: dict | None = None,
) -> tuple[bytes | None, str, str]:
    """通过 OpenRouter API 调用 Gemini 图像生成。

    Returns:
        (image_bytes, text_response, error_detail)
        - image_bytes: 生成的图像 bytes，失败返回 None
        - text_response: provider 返回的文本内容（如 panel hints JSON）
        - error_detail: 失败原因摘要，成功时为空字符串

    Args:
        api_key: OpenRouter API Key
        model: 模型名称（如 google/gemini-3-pro-image-preview）
        prompt: 图像生成提示词
        reference_images: 参考图像列表（bytes 格式）
        image_config: 图像配置（aspect_ratio, image_size）
    """
    import httpx

    base_url = "https://openrouter.ai/api/v1"

    # 构建 content 数组（按 OpenRouter 官方建议：文本在前，图片在后）
    content = []

    # 先添加文本提示词
    content.append({"type": "text", "text": prompt})

    # 再添加参考图（如果有）
    if reference_images:
        for img_bytes in reference_images:
            img_b64 = base64.b64encode(img_bytes).decode("utf-8")
            content.append(
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img_b64}"}}
            )

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "modalities": ["image", "text"],
    }

    # 添加 image_config
    if image_config:
        effective_image_size = normalize_image_size(
            image_config.get("image_size", "1K"),
            provider="openrouter",
        )
        payload["image_config"] = {
            "aspect_ratio": image_config.get("aspect_ratio", "1:1"),
            "image_size": effective_image_size,
        }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://novelvideo.ai",
        "X-Title": "NovelVideo Studio",
    }

    try:
        supports_image, capability_detail = await _check_openrouter_image_capability(api_key, model)
        if not supports_image:
            detail = f"OpenRouter 模型未声明 image output 支持: {capability_detail}"
            print(f"[OpenRouter] {detail}")
            return None, "", detail

        async with httpx.AsyncClient(timeout=300.0) as client:
            response = await client.post(
                f"{base_url}/chat/completions",
                json=payload,
                headers=headers,
            )
            response.raise_for_status()

            result = response.json()

            # 提取图像
            choices = result.get("choices", [])
            if not choices:
                print(f"[OpenRouter] 响应无 choices: {_truncate_openrouter_debug(result)}")
                return None, "", "响应无 choices"

            message = choices[0].get("message", {})

            # 提取文本和图像（兼容多种 OpenRouter 响应格式）
            text_content = ""
            image_data_url = ""

            content = message.get("content", "")
            if isinstance(content, list):
                # content 是 list[{type, text/image_url}]
                text_parts = []
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    if part.get("type") == "text":
                        text_parts.append(part.get("text", ""))
                    elif part.get("type") == "image_url":
                        url = part.get("image_url", {}).get("url", "")
                        if url.startswith("data:image"):
                            image_data_url = url
                text_content = " ".join(text_parts)
            elif isinstance(content, str):
                text_content = content

            if text_content:
                print(f"[OpenRouter] 文本响应: {text_content[:500]}")

            # 优先从 message.images 取图（旧格式）
            images = message.get("images", [])
            if images:
                url = images[0].get("image_url", {}).get("url", "")
                if url.startswith("data:image"):
                    image_data_url = url

            if not image_data_url:
                print(f"[OpenRouter] 响应无图像: {_truncate_openrouter_debug(message)}")
                detail = "模型未返回图像"
                if text_content:
                    detail = f"{detail}，仅返回文本: {text_content[:200]}"
                return None, text_content or "", detail

            # 提取 base64 部分
            _, b64_data = image_data_url.split(",", 1)
            return base64.b64decode(b64_data), text_content or "", ""

    except httpx.HTTPStatusError as e:
        print(
            "[OpenRouter] HTTP 错误: "
            f"{e.response.status_code} - {_truncate_openrouter_debug(e.response.text, limit=500)}"
        )
        body = _truncate_openrouter_debug(e.response.text, limit=280)
        return (
            None,
            "",
            f"HTTP {e.response.status_code}: {body}" if body else f"HTTP {e.response.status_code}",
        )
    except Exception as e:
        if is_fatal_billing_error(e):
            raise
        detail = f"{type(e).__name__}: {e!r}"
        print(f"[OpenRouter] 请求异常: {detail}")
        return None, "", f"请求异常: {detail}"


async def _call_openai_image_api(
    *,
    api_key: str,
    model: str,
    prompt: str,
    reference_images: list[bytes | tuple[bytes, str] | tuple[str, bytes, str]] | None = None,
    image_config: dict | None = None,
) -> tuple[bytes | None, str, str]:
    """Call OpenAI Image API using GPT Image models.

    Returns:
        (image_bytes, text_response, error_detail)
    """

    try:
        from openai import AsyncOpenAI
    except ImportError:
        return None, "", "openai SDK not installed; install openai>=2.14.0"

    if not api_key:
        return None, "", "OPENAI_API_KEY is missing"

    image_config = image_config or {}
    image_size = normalize_image_size(str(image_config.get("image_size") or "1K"), "openai")
    size = resolve_openai_image_size(
        image_config.get("aspect_ratio", "1:1"),
        image_size,
    )
    request_options: dict[str, object] = {"size": size}
    is_gpt_image_2 = str(model or "").strip().lower().startswith("gpt-image-2")
    quality = normalize_openai_quality(str(image_config.get("quality") or ""), default="medium")
    if quality:
        request_options["quality"] = quality
    output_format = str(image_config.get("output_format") or "png").strip().lower()
    # The current Image Edit endpoint rejects output_format for some gpt-image-2
    # reference-image requests. Edits return PNG-compatible b64 output by default.
    if output_format and not reference_images:
        request_options["output_format"] = output_format
    input_fidelity = str(image_config.get("input_fidelity") or "").strip().lower()
    # gpt-image-2 always processes image inputs at high fidelity; the API rejects
    # attempts to change input_fidelity for that model.
    if input_fidelity and not is_gpt_image_2:
        request_options["input_fidelity"] = input_fidelity

    async def _reserve(source: str) -> str:
        return await get_usage_meter().reserve_current_model_call_credit(
            model=model,
            billing_kind="image",
            billing_params=_image_credit_billing_params(
                image_size=image_size,
                quality=quality,
            ),
            metadata={"source": source},
        )

    async def _refund(reservation_id: str, source: str, error: str) -> None:
        try:
            await get_usage_meter().refund_model_call_credit_reservation(
                reservation_id,
                metadata={"source": source, "error": error[:200]},
            )
        except Exception:
            pass

    async def _confirm(
        reservation_id: str,
        *,
        provider_request_id: str = "",
        response_id: str = "",
    ) -> None:
        try:
            if not reservation_id:
                await get_usage_meter().mark_current_paid_execution_attempt(
                    status="completed",
                    provider_request_id=provider_request_id,
                    provider_response_id=response_id,
                )
                return
            await get_usage_meter().bump_model_call(
                user_id=None,
                model=model,
                provider_request_id=provider_request_id,
                credit_reservation_id=reservation_id,
                metadata={"response_id": response_id} if response_id else None,
            )
        except Exception:
            pass

    reservation_id = ""
    try:
        reservation_id = await _reserve("openai_image_api")

        client = AsyncOpenAI(api_key=api_key, timeout=300.0, max_retries=0)
        result = None
        for _attempt in range(4):
            try:
                if reference_images:
                    image_files = []
                    for idx, image_ref in enumerate(reference_images):
                        filename = f"reference_{idx + 1}.png"
                        mime_type = "image/png"
                        image_bytes: bytes
                        if isinstance(image_ref, tuple):
                            if len(image_ref) == 3:
                                filename, image_bytes, mime_type = image_ref
                            elif len(image_ref) == 2:
                                image_bytes, mime_type = image_ref
                                ext = "jpg" if mime_type == "image/jpeg" else "png"
                                filename = f"reference_{idx + 1}.{ext}"
                            else:
                                image_bytes = bytes(image_ref[0])
                        else:
                            image_bytes = bytes(image_ref)
                        image_files.append((filename, bytes(image_bytes), mime_type))
                    result = await client.images.edit(
                        model=model,
                        image=image_files,
                        prompt=prompt,
                        **request_options,
                    )
                else:
                    result = await client.images.generate(
                        model=model,
                        prompt=prompt,
                        **request_options,
                    )
                break
            except Exception as exc:
                detail = f"{type(exc).__name__}: {exc!r}"
                unknown_parameter = _extract_openai_unknown_parameter(detail)
                if unknown_parameter and unknown_parameter in request_options:
                    print(
                        f"[OpenAI Image] 参数 {unknown_parameter!r} 不被当前端点接受，" "移除后重试"
                    )
                    request_options.pop(unknown_parameter, None)
                    continue
                transient_error = any(
                    token in detail
                    for token in (
                        "InternalServerError",
                        "APIConnectionError",
                        "APITimeoutError",
                        "server_error",
                        "Connection error",
                    )
                )
                if transient_error and _attempt < 3:
                    wait_seconds = 2**_attempt
                    print(
                        f"[OpenAI Image] 暂时性错误，{wait_seconds}s 后重试 "
                        f"({_attempt + 1}/4): {detail}"
                    )
                    await asyncio.sleep(wait_seconds)
                    continue
                raise

        if result is None:
            await _refund(reservation_id, "openai_image_api", "empty_response")
            return None, "", "OpenAI Image API returned no response"

        provider_request_id = str(getattr(result, "_request_id", "") or "").strip()
        response_id = str(getattr(result, "id", "") or "").strip()
        await _mark_paid_image_attempt_accepted(
            provider_request_id=provider_request_id,
            response_id=response_id,
        )
        if not result.data:
            await _refund(reservation_id, "openai_image_api", "missing_data")
            return None, "", "OpenAI Image API returned no data"

        image_item = result.data[0]
        image_base64 = getattr(image_item, "b64_json", None) or ""
        if not image_base64:
            await _refund(reservation_id, "openai_image_api", "missing_b64_json")
            return None, "", f"OpenAI Image API returned no b64_json: {image_item}"

        image_bytes = base64.b64decode(image_base64)
        await _confirm(
            reservation_id,
            provider_request_id=provider_request_id,
            response_id=response_id,
        )
        return image_bytes, "", ""
    except Exception as exc:
        await _refund(reservation_id, "openai_image_api", type(exc).__name__)
        if is_fatal_billing_error(exc):
            raise
        detail = f"{type(exc).__name__}: {exc!r}"
        print(f"[OpenAI Image] 请求异常: {detail}")
        return None, "", f"请求异常: {detail}"


def _should_include_third_party_newapi_size_fields(
    *,
    endpoint: str,
    direct_target_used: bool,
    is_organization: bool,
) -> bool:
    """Return True when calling a third-party OneAPI/NewAPI relay directly.

    DramaClaw's own RelayClaw and local proxy read ``width``/``height`` and
    ``metadata.resolution``, whereas standard third-party OneAPI/NewAPI relays
    (such as ``zeroshell.top``) parse OpenAI ``size`` and/or top-level
    ``resolution`` on ``/images/generations`` and ``/images/edits`` and
    default to ``1K`` when neither is present.
    """
    if is_organization:
        return False
    if direct_target_used:
        return True
    parsed = urlparse(str(endpoint or "").strip())
    host = (parsed.hostname or "").strip().lower()
    if not host:
        return False
    if host in {"localhost", "127.0.0.1", "newapi", "relayclaw.cdnfg.com"}:
        return False
    if host.endswith((".test", ".invalid", ".example", ".local")):
        return False
    return True


async def _call_newapi_image_api(
    *,
    api_key: str,
    model: str,
    prompt: str,
    reference_images: (
        list[bytes | tuple[bytes, str] | tuple[str, bytes, str]] | None
    ) = None,
    image_config: dict | None = None,
    base_url: str | None = None,
    trace: dict[str, str] | None = None,
    egress_context: TrustedEgressContext | None = None,
    delivery_path: str | Path | None = None,
    delivery_state: dict[str, bool | str] | None = None,
    before_delivery_copy: Callable[[], None] | None = None,
    read_copied_bytes: bool = True,
) -> tuple[bytes | None, str, str]:
    """Call newAPI's OpenAI-compatible Images API."""
    import httpx

    context = _validate_egress_context(egress_context)
    direct_target_used = False
    direct_provider = ""
    if context is None or not context.is_organization:
        from novelvideo.model_gateway_settings import (
            resolve_direct_media_gateway_target,
        )

        direct_target = resolve_direct_media_gateway_target(model, "image")
        if direct_target is not None:
            direct_target_used = True
            direct_provider = getattr(direct_target, "provider", "")
            if direct_target.api_key:
                api_key = direct_target.api_key
            if direct_target.base_url:
                base_url = direct_target.base_url
            model = direct_target.upstream_model
    if not api_key:
        return None, "", "DramaClawAPI API key is missing"

    image_config = image_config or {}
    reference_image_format = str(
        image_config.get("reference_image_format")
        or os.environ.get("NEWAPI_IMAGE_EDIT_REFERENCE_FORMAT")
        or "dc-media"
    ).strip().lower()
    if reference_images and reference_image_format not in {"dc-media", "images", "data-url"}:
        return None, "", "Unsupported reference image format; use dc-media, images, or data-url"
    aspect_ratio = str(image_config.get("aspect_ratio") or "1:1").strip().lower() or "1:1"
    image_size = normalize_image_size(str(image_config.get("image_size") or "1K"), "newapi")
    from novelvideo.generators.image_resolution import clamp_image_size_for_model

    image_size = clamp_image_size_for_model("newapi", model, image_size)
    request_schema = image_config.get("request_schema") or {}
    from novelvideo.media_model_request_schema import normalize_media_resolution_value

    resolution = normalize_media_resolution_value(image_size)

    metadata: dict[str, object] = {"resolution": resolution}
    payload: dict[str, object] = {
        "model": model,
        "prompt": prompt,
        "n": 1,
        "response_format": "url",
        "watermark": False,
        "metadata": metadata,
    }
    if aspect_ratio in {"auto", "adaptive"}:
        # Images use ``auto`` as the public follow-input ratio value. Keep
        # resolution independent and let NewAPI infer the geometry.
        metadata["ratio"] = "auto"
    else:
        # Fixed geometry keeps the selected ratio as semantic metadata while
        # width/height carry the pixel expectation. All three values come from
        # this same aspect-ratio/resolution pair.
        metadata["ratio"] = aspect_ratio
        try:
            size = resolve_openai_image_size(
                aspect_ratio,
                image_size,
                model,
                allow_dynamic_resolution=True,
                min_pixels=request_schema.get("minPixels"),
            )
        except ValueError as exc:
            return None, "", str(exc)
        width, height = (int(value) for value in size.split("x", 1))
        payload["width"] = width
        payload["height"] = height
    include_quality = bool(
        request_schema.get("includeQuality")
    ) or _newapi_image_model_supports_quality(model)
    if include_quality and str(image_config.get("quality") or "").strip():
        quality = str(image_config["quality"]).strip()
        payload["quality"] = quality
    else:
        quality = ""

    output_format = str(image_config.get("output_format") or "").strip().lower()
    if output_format:
        payload["output_format"] = output_format
    input_fidelity = str(image_config.get("input_fidelity") or "").strip().lower()
    if input_fidelity and reference_images:
        payload["input_fidelity"] = input_fidelity

    if base_url:
        endpoint = base_url.rstrip("/")
    else:
        from novelvideo.config import get_effective_newapi_gateway_config

        endpoint = get_effective_newapi_gateway_config().base_url.rstrip("/")
    lingke_target = _is_lingke_image_target(direct_provider, endpoint)
    if lingke_target and len(reference_images or []) > _LINGKE_IMAGE_MAX_REFERENCES:
        return (
            None,
            "",
            f"Lingke accepts at most {_LINGKE_IMAGE_MAX_REFERENCES} reference images",
        )

    relayed_urls: list[str] = []
    used_data_uri_references = False
    if reference_images:
        if reference_image_format == "data-url":
            image_values = _encode_newapi_reference_data_uris(reference_images)
            used_data_uri_references = True
        else:
            try:
                relay_helper = _relay_reference_images_for_newapi
                if relay_helper is _ORIGINAL_RELAY_REFERENCE_IMAGES_FOR_NEWAPI:
                    relayed_urls = await relay_helper(
                        reference_images,
                        egress_context=context,
                    )
                else:
                    relayed_urls = await relay_helper(reference_images)
            except Exception as exc:
                if context is not None and context.is_organization:
                    # The organization path hides `exc` because an arbitrary
                    # exception may carry a signed URL or a key. The relay's own
                    # three failures are secret-free by construction, though, and
                    # they need three different fixes — a port registration, a
                    # retry, an object-storage policy — so name which one fired
                    # (OI-45). Anything else stays opaque.
                    code = getattr(type(exc), "code", None)
                    reason = getattr(exc, "reason", None)
                    if not isinstance(code, str):
                        return None, "", "media relay upload failed"
                    logger.warning(
                        "organization media relay failed: code=%s reason=%s "
                        "envelope=%s project=%s",
                        code,
                        reason or "-",
                        context.envelope_id,
                        context.project_id,
                    )
                    return None, "", f"media relay upload failed ({code})"
                return None, "", f"media relay upload failed: {exc}"
            if _has_unreachable_relay_host(endpoint, relayed_urls):
                logger.info(
                    "DramaClawAPI image relay host recently unreachable from upstream (%s); "
                    "using inline data URIs directly",
                    endpoint,
                )
                image_values = _encode_newapi_reference_data_uris(reference_images)
                used_data_uri_references = True
            else:
                image_values = relayed_urls
        # DC-Media uses image: [URL]; Cockpit's JSON edit parser accepts
        # images: [{image_url: URL}]. Select explicitly per deployment.
        _set_newapi_reference_images_payload(
            payload,
            image_values,
            reference_image_format,
        )
        request_path = "/images/edits"
    else:
        request_path = "/images/generations"

    from novelvideo.media_model_request_schema import (
        apply_media_request_schema,
        enforce_newapi_media_geometry_contract,
    )

    payload = apply_media_request_schema(
        payload,
        request_schema,
        image_config.get("model_params") or {},
    )
    payload = enforce_newapi_media_geometry_contract(payload, media_type="image")
    if lingke_target:
        try:
            payload = _build_lingke_image_payload(
                payload,
                request_schema=request_schema,
                aspect_ratio=aspect_ratio,
                image_size=image_size,
            )
        except ValueError as exc:
            return None, "", str(exc)
        request_path = _lingke_image_path(endpoint, "media/generate")
    elif _should_include_third_party_newapi_size_fields(
        endpoint=endpoint,
        direct_target_used=direct_target_used,
        is_organization=bool(context is not None and context.is_organization),
    ):
        if "width" in payload and "height" in payload:
            payload.setdefault("size", f"{payload['width']}x{payload['height']}")
        elif image_size:
            payload.setdefault("size", str(image_size).upper())
        if image_size:
            payload.setdefault("resolution", str(image_size).upper())

    request_context = _newapi_safe_request_context(
        endpoint=endpoint,
        request_path=request_path,
        model=model,
        payload=payload,
        prompt=prompt,
    )
    logger.info("DramaClawAPI image request: %s", request_context)
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    async def _reserve(source: str) -> str:
        return await get_usage_meter().reserve_current_model_call_credit(
            model=model,
            billing_kind="image",
            billing_params=_image_credit_billing_params(
                image_size=image_size,
                quality=quality,
            ),
            metadata={"source": source},
        )

    async def _refund(
        reservation_id: str,
        source: str,
        error: str,
        *,
        request_id: str = "",
        http_status: int | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        try:
            metadata: dict[str, object] = {"source": source, "error": error[:200]}
            if request_id:
                metadata["request_id"] = request_id
            if http_status is not None:
                metadata["http_status"] = http_status
            if headers:
                metadata["response_headers"] = headers
            await get_usage_meter().refund_model_call_credit_reservation(
                reservation_id,
                metadata=metadata,
            )
        except Exception:
            pass

    async def _confirm(
        reservation_id: str,
        *,
        provider_request_id: str = "",
        response_id: str = "",
    ) -> None:
        try:
            if not reservation_id:
                await get_usage_meter().mark_current_paid_execution_attempt(
                    status="completed",
                    provider_request_id=provider_request_id,
                    provider_response_id=response_id,
                )
                return
            await get_usage_meter().bump_model_call(
                user_id=None,
                model=model,
                provider_request_id=provider_request_id,
                credit_reservation_id=reservation_id,
                metadata={"response_id": response_id} if response_id else None,
            )
        except Exception:
            pass

    def _record_trace(
        *,
        provider_request_id: str = "",
        response_id: str = "",
    ) -> None:
        if trace is None:
            return
        if provider_request_id:
            trace["request_id"] = provider_request_id
        if response_id:
            trace["response_id"] = response_id

    reservation_id = ""
    provider_request_id = ""
    try:
        reservation_id = await _reserve("newapi_image_api")
        await update_current_model_call_log(
            request_payload=_sanitize_newapi_payload_for_log(payload),
        )

        async with httpx.AsyncClient(
            timeout=NEWAPI_IMAGE_HTTP_TIMEOUT_SECONDS,
            follow_redirects=True,
        ) as client:
            while True:
                logger.info("DramaClawAPI image POST start: %s", request_context.get("endpoint"))
                response = await client.post(
                    f"{endpoint}{request_path}",
                    headers=headers,
                    json=payload,
                )
                logger.info(
                    "DramaClawAPI image POST response: status=%s bytes=%s",
                    getattr(response, "status_code", "?"),
                    (getattr(response, "headers", None) or {}).get("content-length", "?"),
                )
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    body = (exc.response.text or "")[:2000]
                    if (
                        reference_images
                        and not used_data_uri_references
                        and not lingke_target
                        and _is_upstream_reference_fetch_failure(
                            exc.response.status_code,
                            body,
                        )
                    ):
                        _record_unreachable_relay_hosts(endpoint, relayed_urls)
                        _set_newapi_reference_images_payload(
                            payload,
                            _encode_newapi_reference_data_uris(reference_images),
                            reference_image_format,
                        )
                        used_data_uri_references = True
                        request_context = _newapi_safe_request_context(
                            endpoint=endpoint,
                            request_path=request_path,
                            model=model,
                            payload=payload,
                            prompt=prompt,
                        )
                        logger.warning(
                            "DramaClawAPI image upstream failed to fetch relayed reference URL "
                            "(status=%s); retrying with inline data URIs (%s)",
                            exc.response.status_code,
                            endpoint,
                        )
                        await update_current_model_call_log(
                            request_payload=_sanitize_newapi_payload_for_log(payload),
                        )
                        continue
                    raise
                break
            response_headers = getattr(response, "headers", {}) or {}
            provider_request_id = _newapi_request_id_from_headers(response_headers)
            result = response.json()
            await update_current_model_call_log(
                response_payload=result,
            )
            logger.info(
                "DramaClawAPI image POST parsed: data_count=%d keys=%s",
                len(result.get("data") or []),
                sorted(result.keys())[:5],
            )
            provider_request_id = (
                provider_request_id
                or str(result.get("request_id") or result.get("requestId") or "").strip()
            )
            response_id = str(result.get("id") or "").strip()
            if lingke_target:
                submitted_data = result.get("data")
                submitted_data = (
                    submitted_data if isinstance(submitted_data, dict) else {}
                )
                task_id = str(
                    submitted_data.get("task_id") or result.get("task_id") or ""
                ).strip()
                if not task_id:
                    raise ValueError("Lingke image response missing task_id")
                provider_request_id = provider_request_id or task_id
                response_id = task_id
                _record_trace(
                    provider_request_id=provider_request_id,
                    response_id=response_id,
                )
                await _mark_paid_image_attempt_accepted(
                    provider_request_id=provider_request_id,
                    response_id=response_id,
                )
                task_result = await _poll_lingke_image_result(
                    client,
                    endpoint=endpoint,
                    api_key=api_key,
                    task_id=task_id,
                )
                await update_current_model_call_log(
                    response_payload={"submission": result, "task_status": task_result},
                )
                if str(task_result.get("result_type") or "image").strip().lower() != "image":
                    raise ValueError("Lingke image task returned a non-image result")
                image_url = str(task_result.get("result_url") or "").strip()
                if not image_url:
                    raise ValueError("Lingke image task completed without result_url")
                if image_url.startswith("data:image"):
                    _, b64_data = image_url.split(",", 1)
                    image_bytes = base64.b64decode(b64_data)
                else:
                    logger.info("Lingke image result download start")
                    async with httpx.AsyncClient(
                        timeout=60.0,
                        follow_redirects=True,
                    ) as fetch:
                        image_response = await fetch.get(image_url)
                    image_response.raise_for_status()
                    image_bytes = image_response.content
                await _confirm(
                    reservation_id,
                    provider_request_id=provider_request_id,
                    response_id=response_id,
                )
                return image_bytes, "", ""
            _record_trace(provider_request_id=provider_request_id, response_id=response_id)
            await _mark_paid_image_attempt_accepted(
                provider_request_id=provider_request_id,
                response_id=response_id,
            )

            data = result.get("data") or []
            if not data:
                await _refund(
                    reservation_id,
                    "newapi_image_api",
                    "missing_data",
                    request_id=provider_request_id,
                )
                return None, "", f"DramaClawAPI Images response missing data: {sorted(result.keys())}"

            first = data[0] or {}
            if delivery_path is not None:
                from novelvideo.media_archive_copy import copy_archived_result

                if await copy_archived_result(
                    first.get("archive"), delivery_path, before_copy=before_delivery_copy
                ):
                    await _confirm(
                        reservation_id,
                        provider_request_id=provider_request_id,
                        response_id=response_id,
                    )
                    if not read_copied_bytes:
                        if delivery_state is not None:
                            delivery_state["copied"] = True
                            delivery_state["sha256"] = str(
                                first["archive"].get("sha256") or ""
                            )
                        return None, "", ""
                    image_bytes = Path(delivery_path).read_bytes()
                    if delivery_state is not None:
                        delivery_state["copied"] = True
                    return image_bytes, "", ""
            image_b64 = first.get("b64_json") or ""
            if image_b64:
                image_bytes = base64.b64decode(image_b64)
                await _confirm(
                    reservation_id,
                    provider_request_id=provider_request_id,
                    response_id=response_id,
                )
                return image_bytes, "", ""

            image_url = first.get("url") or first.get("image_url") or ""
            if image_url.startswith("data:image"):
                _, b64_data = image_url.split(",", 1)
                image_bytes = base64.b64decode(b64_data)
                await _confirm(
                    reservation_id,
                    provider_request_id=provider_request_id,
                    response_id=response_id,
                )
                return image_bytes, "", ""
            if image_url:
                # NewAPI 返 URL 而非 b64 时,要二次 GET 拉图。这个 await 是常见的
                # "newapi 已生成但任务还在 await" hang 点 —— 用单独的短 timeout
                # (60s),避免落入外层 client 的 600s global timeout 拖很久。
                # 加 phase log 让 hang 时能定位卡在哪。
                logger.info("DramaClawAPI image GET url start: %s", image_url[:120])
                async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as fetch:
                    image_response = await fetch.get(image_url)
                logger.info(
                    "DramaClawAPI image GET url done: status=%d bytes=%d",
                    image_response.status_code,
                    len(image_response.content),
                )
                image_response.raise_for_status()
                image_bytes = image_response.content
                await _confirm(
                    reservation_id,
                    provider_request_id=provider_request_id,
                    response_id=response_id,
                )
                return image_bytes, "", ""

            await _refund(
                reservation_id,
                "newapi_image_api",
                "missing_image_payload",
                request_id=provider_request_id,
            )
            return None, "", f"DramaClawAPI Images response missing b64_json/url: {first}"
    except httpx.HTTPStatusError as exc:
        body = (exc.response.text or "")[:2000]
        response_headers = getattr(exc.response, "headers", {}) or {}
        safe_headers = _newapi_safe_header_summary(response_headers)
        request_id = _newapi_request_id_from_headers(response_headers) or provider_request_id
        await update_current_model_call_log(
            response_payload={
                "status_code": exc.response.status_code,
                "headers": safe_headers,
                "body": body,
            },
            error_message=f"HTTP {exc.response.status_code}",
        )
        if is_definite_no_cost_http_rejection(exc.response.status_code):
            try:
                await get_usage_meter().mark_current_paid_execution_attempt(
                    status="rejected_no_cost",
                    provider_request_id=request_id,
                    metadata={
                        "source": "newapi_image_api",
                        "http_status": exc.response.status_code,
                        "error": "provider_request_rejected",
                    },
                )
            except Exception:
                pass
        await _refund(
            reservation_id,
            "newapi_image_api",
            f"HTTP {exc.response.status_code}",
            request_id=request_id,
            http_status=exc.response.status_code,
            headers=safe_headers,
        )
        error_context = _newapi_context_for_error(request_context)
        header_context = (
            f"request_id={request_id}; headers={safe_headers}; "
            if request_id or safe_headers
            else ""
        )
        logger.warning(
            "DramaClawAPI image failed: status=%s; %s%s; body=%s",
            exc.response.status_code,
            header_context,
            error_context,
            body,
        )
        return (
            None,
            "",
            f"HTTP {exc.response.status_code}: {header_context}{error_context}; body={body}",
        )
    except Exception as exc:
        await update_current_model_call_log(
            error_message=type(exc).__name__,
        )
        await _refund(
            reservation_id,
            "newapi_image_api",
            type(exc).__name__,
            request_id=provider_request_id,
        )
        if is_fatal_billing_error(exc):
            raise
        error_context = _newapi_context_for_error(request_context)
        detail = f"{type(exc).__name__}: {exc!r}; {error_context}"
        logger.warning("DramaClawAPI image request exception: %s", detail)
        return None, "", f"请求异常: {detail}"


def _reference_image_bytes(
    image_ref: bytes | tuple[bytes, str] | tuple[str, bytes, str],
) -> bytes:
    """Unpack the three reference-image shapes the newAPI path accepts."""
    if isinstance(image_ref, tuple):
        if len(image_ref) == 3:
            return bytes(image_ref[1])
        if len(image_ref) == 2:
            return bytes(image_ref[0])
        return bytes(image_ref[0])
    return bytes(image_ref)


def _reference_image_ext(
    image_ref: bytes | tuple[bytes, str] | tuple[str, bytes, str],
) -> str:
    """Extract normalized image extension from any accepted reference-image shape."""
    if isinstance(image_ref, tuple):
        if len(image_ref) == 3:
            filename = str(image_ref[0] or "")
            mime_type = str(image_ref[2] or "")
            suffix = Path(filename).suffix.lstrip(".")
            if suffix:
                return suffix
            if mime_type.startswith("image/"):
                return mime_type.split("/", 1)[1]
        if len(image_ref) == 2:
            hint = str(image_ref[1] or "")
            if hint.startswith("image/"):
                return hint.split("/", 1)[1]
            suffix = Path(hint).suffix.lstrip(".")
            if suffix:
                return suffix
    return "png"


def _set_newapi_reference_images_payload(
    payload: dict[str, object],
    image_values: list[str],
    reference_image_format: str,
) -> None:
    """Populate payload reference-image field according to configured edit format."""
    if reference_image_format == "images":
        payload["images"] = [{"image_url": value} for value in image_values]
    else:
        payload["image"] = image_values


def _encode_newapi_reference_data_uris(
    reference_images: list[bytes | tuple[bytes, str] | tuple[str, bytes, str]],
) -> list[str]:
    """Encode reference images as compact inline JPEG/PNG data URIs."""
    data_uris: list[str] = []
    for image_ref in reference_images:
        raw_bytes = _reference_image_bytes(image_ref)
        raw_ext = _reference_image_ext(image_ref)
        encoded_bytes, encoded_ext = _apply_image_transform(
            raw_bytes,
            ext=raw_ext,
            image_transform=IMAGE_TRANSFORM_AI_REFERENCE_JPEG,
        )
        if encoded_bytes == raw_bytes and raw_ext:
            clean_ext = str(encoded_ext or raw_ext).lower()
            mime_subtype = "jpeg" if clean_ext in {"jpg", "jpeg"} else clean_ext
            b64_payload = base64.b64encode(encoded_bytes).decode("ascii")
            data_uris.append(f"data:image/{mime_subtype};base64,{b64_payload}")
        else:
            data_uris.append(bytes_to_data_url(encoded_bytes))
    return data_uris


def _is_upstream_reference_fetch_failure(status_code: int | None, body: str) -> bool:
    """Return True when the upstream rejected an edit call before generation because downloading a reference URL failed."""
    if status_code not in {400, 422}:
        return False
    lowered = str(body or "").lower()
    if "fetch reference image" in lowered or "fail_to_fetch_task" in lowered:
        return True
    if "reference image" not in lowered:
        return False
    return any(
        marker in lowered
        for marker in (
            "network is unreachable",
            "dial tcp",
            "connection refused",
            "no such host",
            "i/o timeout",
            "tls handshake timeout",
        )
    )


def _relay_hosts_from_urls(urls: list[str]) -> list[str]:
    hosts: list[str] = []
    for url in urls:
        parsed = urlparse(str(url or ""))
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            hosts.append(parsed.netloc.lower())
    return hosts


def _record_unreachable_relay_hosts(endpoint: str, urls: list[str]) -> None:
    expires_at = time.monotonic() + _NEWAPI_UNREACHABLE_RELAY_TTL_SECONDS
    for host in _relay_hosts_from_urls(urls):
        _NEWAPI_UNREACHABLE_RELAY_HOSTS[(endpoint, host)] = expires_at


def _has_unreachable_relay_host(endpoint: str, urls: list[str]) -> bool:
    now = time.monotonic()
    for host in _relay_hosts_from_urls(urls):
        key = (endpoint, host)
        expires_at = _NEWAPI_UNREACHABLE_RELAY_HOSTS.get(key)
        if expires_at is None:
            continue
        if expires_at > now:
            return True
        _NEWAPI_UNREACHABLE_RELAY_HOSTS.pop(key, None)
    return False


def _sanitize_newapi_payload_for_log(payload: dict[str, object]) -> dict[str, object]:
    """Return a shallow copy of payload with inline data URIs summarized for call logging."""
    has_data_uri = False
    for key in ("image", "images"):
        val = payload.get(key)
        if isinstance(val, list) and any(
            (isinstance(item, str) and item.startswith("data:"))
            or (
                isinstance(item, dict)
                and isinstance(item.get("image_url"), str)
                and item["image_url"].startswith("data:")
            )
            for item in val
        ):
            has_data_uri = True
            break
    if not has_data_uri:
        return payload
    sanitized = dict(payload)
    if isinstance(sanitized.get("image"), list):
        sanitized["image"] = [
            f"<data-uri:{len(item)}-chars>"
            if isinstance(item, str) and item.startswith("data:")
            else item
            for item in sanitized["image"]
        ]
    if isinstance(sanitized.get("images"), list):
        sanitized["images"] = [
            {
                **item,
                "image_url": f"<data-uri:{len(item['image_url'])}-chars>",
            }
            if isinstance(item, dict)
            and isinstance(item.get("image_url"), str)
            and item["image_url"].startswith("data:")
            else item
            for item in sanitized["images"]
        ]
    return sanitized


async def _relay_reference_images_for_newapi(
    reference_images: list[bytes | tuple[bytes, str] | tuple[str, bytes, str]],
    *,
    egress_context: TrustedEgressContext | None = None,
) -> list[str]:
    """Upload reference image bytes to OSS relay for URL-only upstream channels."""

    context = _validate_egress_context(egress_context)

    if context is not None and context.is_organization:
        urls: list[str] = []
        for index, image_ref in enumerate(reference_images):
            data = _reference_image_bytes(image_ref)
            content_digest = hashlib.sha256(data).hexdigest()
            object_id = (
                f"{context.envelope_id}:{context.project_id}:{index}:{content_digest}"
            )
            urls.append(
                await relay_tenant_image_bytes_from_context(
                    data,
                    object_id=object_id,
                    context=context,
                    ext=_reference_image_ext(image_ref),
                    ttl=NEWAPI_MEDIA_INPUT_MIN_TTL_SECONDS,
                )
            )
        return urls

    def upload_all() -> list[str]:
        urls: list[str] = []
        relay_ttl = media_relay_ttl_seconds(
            minimum=NEWAPI_MEDIA_INPUT_MIN_TTL_SECONDS,
        )
        for image_ref in reference_images:
            urls.append(
                upload_image_bytes(
                    _reference_image_bytes(image_ref),
                    ext=_reference_image_ext(image_ref),
                    ttl=relay_ttl,
                    image_transform=IMAGE_TRANSFORM_AI_REFERENCE_JPEG,
                )
            )
        return urls

    return await asyncio.to_thread(upload_all)


_ORIGINAL_RELAY_REFERENCE_IMAGES_FOR_NEWAPI = _relay_reference_images_for_newapi


async def _call_huimeng_image_api(
    *,
    api_key: str,
    model: str,
    prompt: str,
    reference_images: list[bytes | tuple[bytes, str] | tuple[str, bytes, str]] | None = None,
    image_config: dict | None = None,
) -> tuple[bytes | None, str, str]:
    """Call HuiMeng's async task API for image generation/editing."""
    import httpx

    if not api_key:
        return None, "", "HUIMENGI_API_KEY is missing"

    image_config = image_config or {}
    ratio = str(image_config.get("aspect_ratio") or "1:1").strip() or "1:1"
    resolution = _huimeng_image_resolution_for_model(model, image_config.get("image_size"))
    params: dict[str, object] = {"prompt": prompt, "ratio": ratio}
    if resolution:
        params["resolution"] = resolution
    if model == "image-2-official":
        quality = (
            str(
                image_config.get("quality") or image_config.get("huimeng_image_quality") or "medium"
            )
            .strip()
            .lower()
        )
        params["quality"] = quality if quality in {"low", "medium", "high"} else "medium"
    if reference_images:
        ref_urls = []
        for image_ref in reference_images[:9]:
            if isinstance(image_ref, tuple):
                if len(image_ref) == 3:
                    image_bytes = image_ref[1]
                elif len(image_ref) == 2:
                    image_bytes = image_ref[0]
                else:
                    image_bytes = bytes(image_ref[0])
            else:
                image_bytes = bytes(image_ref)
            ref_urls.append(bytes_to_data_url(bytes(image_bytes)))
        params["image"] = ref_urls[0] if len(ref_urls) == 1 else ref_urls

    request_context = f"model={model}, ratio={ratio}, refs={len(reference_images or [])}"
    try:
        client = HuimengiTaskClient(api_key=api_key)
        submit = await client.submit_task(model=model, params=params)
        task_id = submit["task_id"]
        print(f"[HuiMeng Images] submitted task_id={task_id} ({request_context})")
        task = await client.wait_for_completion(
            task_id,
            poll_interval=_HUIMENG_IMAGE_POLL_INTERVAL_SECONDS,
            max_polls=_HUIMENG_IMAGE_MAX_POLLS,
        )
        result = task.get("result") or {}
        image_url = extract_huimeng_result_url(result, "image_url", "image_urls")
        if not image_url:
            return None, "", f"HuiMeng result missing image_url: {result}"

        async with httpx.AsyncClient(timeout=180.0, follow_redirects=True) as http_client:
            response = await http_client.get(image_url)
        response.raise_for_status()
        validate_huimeng_media_download(
            response.content,
            response.headers.get("content-type"),
            expected_media_type="image",
            url=image_url,
        )
        return response.content, "", ""
    except httpx.HTTPStatusError as exc:
        body = (exc.response.text or "")[:500]
        return None, "", f"{request_context} | HTTP {exc.response.status_code}: {body}"
    except HuimengTaskFailed as exc:
        return None, "", f"{request_context} | HuiMeng task failed: {exc}"
    except Exception as exc:
        return None, "", f"{request_context} | request failed: {exc}"


class NanoBananaGridGenerator:
    """按 provider 配置解析出图凭据与模型参数，并提供参考图加载。"""

    def __init__(self, api_key: Optional[str] = None, config: Optional[dict] = None):
        """初始化生成器。

        Args:
            api_key: Google AI API Key，默认从环境变量读取
        """
        config = config or get_grid_generation_config()
        self.provider = config.get("provider", "google")
        self.api_key = api_key or config["api_key"]
        self.model = config["model"]
        self.base_url = config.get("base_url", "")
        self.openai_image_quality = config.get("openai_image_quality", "medium")
        self.huimeng_image_quality = config.get("huimeng_image_quality", "medium")
        self.newapi_request_schema = config.get("newapi_request_schema") or {}
        self.newapi_model_params = config.get("newapi_model_params") or {}

        if not self.api_key:
            if self.provider == "openrouter":
                key_name = "OPENROUTER_API_KEY"
            elif self.provider == "huimeng":
                key_name = "HUIMENGI_API_KEY"
            elif self.provider == "openai":
                key_name = "OPENAI_API_KEY"
            elif self.provider == "newapi":
                key_name = "NEWAPI_API_KEY"
            else:
                key_name = "GOOGLE_AI_API_KEY"
            raise ValueError(f"API key not set. Set {key_name} environment variable.")

        # EG-09b（OI-52）：组织只许经网关出图。提到构造点是因为所有入口共用
        # `self.provider` 选分支，一处判就够。
        # 只对组织身份生效——平台/个人走 direct provider 行为不变。
        if self.provider != "newapi" and ambient_organization_egress_context() is not None:
            raise EgressError("ORG_EGRESS_DENIED")

        print(f"[NanoBanana Grid] Provider: {self.provider}, Model: {self.model}")

    def _load_image_as_part(
        self,
        image_path: str,
        compress_quality: int = 60,
        min_short_side: int = 0,
    ):
        """加载图像作为 Gemini API 的 Part（带 JPEG 压缩）。

        Args:
            image_path: 图像路径
            compress_quality: JPEG 压缩质量 (1-100)，设为 0 或 None 禁用压缩
            min_short_side: 提交前放大参考图，避免小尺寸空间图被模型读丢

        Returns:
            Gemini Part 对象
        """
        try:
            from PIL import Image
            import io

            # OpenAI director refs are line-art geometry anchors. JPEG compression can erase
            # subtle table/window/stool lines, so keep those references lossless.
            if self.provider == "openai" and Path(image_path).name == "director_color_ref.png":
                compress_quality = 0

            # 加载图片
            img = Image.open(image_path)
            original_size = os.path.getsize(image_path)
            original_dimensions = img.size

            if min_short_side > 0:
                short_side = min(img.size)
                if 0 < short_side < min_short_side:
                    scale = min_short_side / short_side
                    new_size = (
                        max(1, int(round(img.size[0] * scale))),
                        max(1, int(round(img.size[1] * scale))),
                    )
                    img = img.resize(new_size, Image.Resampling.LANCZOS)
                    print(
                        f"[参考图放大] {os.path.basename(image_path)}: "
                        f"{original_dimensions[0]}x{original_dimensions[1]} → "
                        f"{new_size[0]}x{new_size[1]}"
                    )

            # 压缩为 JPEG（如果启用）
            if compress_quality and compress_quality > 0:
                # 转为 RGB（JPEG 不支持 alpha）
                if img.mode in ("RGBA", "P"):
                    img = img.convert("RGB")

                # 压缩到内存
                buffer = io.BytesIO()
                img.save(buffer, format="JPEG", quality=compress_quality, optimize=True)
                image_data = buffer.getvalue()
                mime_type = "image/jpeg"

                compressed_size = len(image_data)
                ratio = (1 - compressed_size / original_size) * 100
                print(
                    f"[压缩] {os.path.basename(image_path)}: "
                    f"{original_size/1024:.0f}KB → {compressed_size/1024:.0f}KB "
                    f"({ratio:.0f}% 压缩)"
                )
            else:
                if img.size != original_dimensions:
                    buffer = io.BytesIO()
                    img.save(buffer, format="PNG")
                    image_data = buffer.getvalue()
                    mime_type = "image/png"
                else:
                    # 不压缩，直接读取原文件
                    with open(image_path, "rb") as f:
                        image_data = f.read()

                    if image_path.lower().endswith(".png"):
                        mime_type = "image/png"
                    elif image_path.lower().endswith(".webp"):
                        mime_type = "image/webp"
                    else:
                        mime_type = "image/jpeg"

            if self.provider != "google":
                return _InlineImagePart(image_data, mime_type)

            from google.genai import types

            try:
                return types.Part.from_bytes(
                    data=image_data,
                    mime_type=mime_type,
                    media_resolution=types.MediaResolution.MEDIA_RESOLUTION_HIGH,
                )
            except (TypeError, AttributeError):
                # Gemini 2.x SDK 不支持 media_resolution
                return types.Part.from_bytes(data=image_data, mime_type=mime_type)

        except Exception as e:
            print(f"[NanoBananaPro] 加载参考图失败: {image_path}, {e}")
            return None
