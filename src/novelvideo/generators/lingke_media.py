"""Shared helpers for Lingke (/v1/media/generate + /v1/skills/task-status) APIs."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from novelvideo.seedance2_i2v.models import infer_reference_media_kind

LINGKE_PROVIDER_NAMES = frozenset({"lingke", "ai778"})
LINGKE_HOSTS = frozenset({"api.lk888.ai", "api.lingkeai.ai"})

_LINGKE_VIDEO_NO_DURATION_MODELS = frozenset(
    {
        "kling-avatar-image2video",
        "kling-motion-control",
        "kling-motion-control-v3",
        "wan2.2-animate-mix",
        "happyhorse-video-edit",
        "video-enhance",
        "hailuo-h3-zaishengcheng",
    }
)

_LINGKE_VIDEO_NO_RESOLUTION_MODELS = frozenset(
    {
        "kling-v3-omni-cankao",
        "kling-v3-omni-shouweizhen",
        "kling-v3-omni-videoref",
        "kling-avatar-image2video",
        "kling-motion-control",
        "kling-motion-control-v3",
        "wan2.2-animate-mix",
        "hailuo-h3-zaishengcheng",
    }
)

_LINGKE_VIDEO_RATIO_KEY_MODELS = frozenset(
    {
        "wan3.0",
        "wan3.0-video",
        "wan3.0-video-cankaosheng",
        "wan3.0-video-quannengcankao",
        "wan2.7-cankaosheng",
        "happyhorse-t2v",
        "happyhorse-1.1-t2v",
        "happyhorse-r2v",
        "happyhorse-1.1-r2v",
    }
)

_LINGKE_VIDEO_NO_RATIO_MODELS = frozenset(
    {
        "hailuo-h3-shouweizhen",
        "hailuo-h3-max-shouweizhen",
        "hailuo-h3-zaishengcheng",
        "doubao-seedance-2-5-shouweizhen",
        "kling-v3-omni-shouweizhen",
        "kling-avatar-image2video",
        "kling-motion-control",
        "kling-motion-control-v3",
        "wan3.0-video-shouweizhen",
        "wan2.7-shouweizhen",
        "wan2.7-xuxie",
        "wan2.2-animate-mix",
        "viduq3-turbo",
        "happyhorse-i2v",
        "happyhorse-1.1-i2v",
        "happyhorse-video-edit",
        "pixverse-c1-shouweizhen",
        "video-enhance",
    }
)

_LINGKE_VIDEO_ADAPTIVE_RATIO_MODELS = frozenset(
    {
        "hailuo-h3-cankaosheng",
        "hailuo-h3-quannengcankao",
        "minimax-h3",
        "seedance-2.5-guanfang",
        "seedance-2.5-guanfang-anmiao",
        "seedance-2.0-guanfang",
        "seedance-2.0-guanfang-anmiao",
        "doubao-seedance-2-5-260628",
        "doubao-seedance-2-5-cankaosheng",
        "doubao-seedance-2-5-quannengcankao",
        "wand-vega-video-1.5",
        "wand-vega-video-1.0",
        "kwvideo-v2",
        "kwvideo-v2-ref",
        "kwvideo-v2-quannengcankao",
        "wan3.0",
        "wan3.0-video",
        "wan3.0-video-cankaosheng",
        "wan3.0-video-quannengcankao",
    }
)

_LINGKE_VIDEO_DUAL_MODE_MODELS = frozenset(
    {
        "minimax-h3",
        "seedance-2.5-guanfang",
        "seedance-2.5-guanfang-anmiao",
        "seedance-2.0-guanfang",
        "seedance-2.0-guanfang-anmiao",
        "wand-vega-video-1.5",
        "wand-vega-video-1.0",
        "wan3.0",
        "viduq4-preview",
    }
)

_LINGKE_VIDEO_OMNI_IMAGE_URL_MODELS = frozenset(
    {
        "hailuo-h3-quannengcankao",
        "kling-v4-video",
        "seedance-2.5-anmiao-quannengcankao",
        "seedance-2.0-anmiao-quannengcankao",
        "doubao-seedance-2-5-quannengcankao",
        "kwvideo-v2-quannengcankao",
        "wan3.0-video-quannengcankao",
        "kling-motion-control",
        "kling-motion-control-v3",
        "wan2.2-animate-mix",
        "hailuo-h3-zaishengcheng",
    }
)

_LINGKE_VIDEO_REFERENCE_URLS_MODELS = frozenset(
    {
        "wan2.6-cankaosheng",
        "wan3.0-video-cankaosheng",
        "wan2.7-cankaosheng",
    }
)


def is_lingke_target(provider: str, endpoint: str) -> bool:
    """Return True when the target gateway/channel uses Lingke's media protocol."""
    provider_name = str(provider or "").strip().lower()
    if provider_name in LINGKE_PROVIDER_NAMES:
        return True
    host = (urlsplit(str(endpoint or "").strip()).hostname or "").lower()
    return host in LINGKE_HOSTS


def lingke_api_path(endpoint: str, suffix: str) -> str:
    """Build a /v1-prefixed path without duplicating /v1 when base_url includes it."""
    prefix = "" if str(endpoint or "").rstrip("/").lower().endswith("/v1") else "/v1"
    return f"{prefix}/{suffix.lstrip('/')}"


def value_at_request_path(payload: dict[str, object], path: str) -> object:
    """Read a dotted path from a nested payload dictionary."""
    value: object = payload
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def set_nested_value(target: dict[str, object], path: str, value: object) -> None:
    """Write a dotted path into a nested dictionary, creating intermediate dicts."""
    parts = path.split(".")
    cursor = target
    for part in parts[:-1]:
        nested = cursor.get(part)
        if not isinstance(nested, dict):
            nested = {}
            cursor[part] = nested
        cursor = nested
    cursor[parts[-1]] = value


def _dedupe_urls(values: object) -> list[str]:
    if not isinstance(values, (list, tuple)):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in values:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _format_lingke_video_resolution(
    model_lower: str,
    raw_resolution: object,
    request_schema: dict[str, object],
) -> str:
    clean = str(raw_resolution or "").strip()
    if not clean:
        return ""
    lowered = clean.lower()
    for definition in request_schema.get("parameters") or []:
        if not isinstance(definition, dict):
            continue
        path = str(definition.get("requestPath") or "").strip().removeprefix("params.")
        if path != "resolution":
            continue
        for option in definition.get("options") or []:
            option_text = str(option or "").strip()
            if option_text and option_text.lower() == lowered:
                return option_text
    if model_lower == "video-enhance":
        return lowered
    if lowered in {"2k", "4k", "8k"}:
        return lowered.upper()
    if model_lower.startswith(
        ("hailuo-", "minimax-", "wan", "wand-vega-", "happyhorse-", "pixverse-")
    ):
        return lowered.upper()
    return lowered


def _format_lingke_video_duration(raw_duration: object) -> str:
    text = str(raw_duration or "").strip()
    if not text:
        return ""
    if text.lower() == "auto":
        return "auto"
    try:
        numeric = float(text)
    except (TypeError, ValueError):
        return text
    if numeric <= 0:
        return ""
    if numeric.is_integer():
        return str(int(numeric))
    return str(numeric)


def _normalize_video_mode(mode: str | None) -> str:
    raw = str(mode or "").strip()
    return {
        "textToVideo": "text_to_video",
        "firstFrame": "first_frame",
        "imageToVideo": "image_to_video",
        "firstLastFrame": "first_last_frame",
        "imageReference": "image_reference",
        "allReference": "all_reference",
        "videoEdit": "video_edit",
        "videoExtend": "video_extend",
        "videoUpscale": "video_upscale",
        "videoFrameRate": "video_frame_rate",
    }.get(raw, raw)


def _assign_lingke_video_media_params(
    params: dict[str, object],
    *,
    model_lower: str,
    normalized_mode: str,
    first_frame: str,
    last_frame: str,
    ref_images: list[str],
    ref_videos: list[str],
    ref_audios: list[str],
    ref_file: str,
    ref_link: str,
) -> None:
    misplaced_audios = [
        url for url in ref_images if infer_reference_media_kind(url) == "audio"
    ]
    misplaced_images = [
        url for url in ref_audios if infer_reference_media_kind(url) == "image"
    ]
    clean_ref_images = [
        url for url in ref_images if infer_reference_media_kind(url) != "audio"
    ]
    clean_ref_audios = [
        url for url in ref_audios if infer_reference_media_kind(url) != "image"
    ]
    ref_images = _dedupe_urls([*clean_ref_images, *misplaced_images])
    ref_audios = _dedupe_urls([*clean_ref_audios, *misplaced_audios])
    safe_first = (
        "" if infer_reference_media_kind(first_frame) == "audio" else first_frame
    )
    safe_last = "" if infer_reference_media_kind(last_frame) == "audio" else last_frame
    all_images = _dedupe_urls([safe_first, *ref_images, safe_last])
    keyframe_images = _dedupe_urls([safe_first, safe_last])

    if model_lower in _LINGKE_VIDEO_DUAL_MODE_MODELS:
        use_ref = normalized_mode in {
            "image_reference",
            "all_reference",
            "video_edit",
            "video_extend",
        } or bool(
            ref_videos
            or ref_audios
            or ref_file
            or ref_link
            or (
                ref_images
                and normalized_mode not in {"first_frame", "first_last_frame"}
            )
        )
        if use_ref:
            params["mode"] = "cankaosheng"
            if all_images:
                params["image_url"] = all_images
            if ref_videos:
                params["video_url"] = ref_videos
            if ref_audios:
                params["audio_url"] = ref_audios
            if ref_file and model_lower == "wan3.0":
                params["file_url"] = ref_file
            if ref_link and model_lower == "wan3.0":
                params["link_url"] = ref_link
            if model_lower.startswith(("seedance-2.5-guanfang", "seedance-2.0-guanfang")):
                if normalized_mode == "video_edit" and "anmiao" not in model_lower:
                    params["_quan_neng_mode"] = "edit"
                elif normalized_mode == "video_extend":
                    params["_quan_neng_mode"] = "extend"
                elif ref_videos:
                    params["_quan_neng_mode"] = "quan_neng"
        elif keyframe_images:
            params["mode"] = "shouweizhen"
            params["images"] = (
                keyframe_images[:1]
                if model_lower == "viduq4-preview"
                else keyframe_images[:2]
            )
        return

    if model_lower in _LINGKE_VIDEO_OMNI_IMAGE_URL_MODELS:
        if all_images:
            params["image_url"] = all_images
        if ref_videos:
            params["video_url"] = ref_videos
        if ref_audios:
            params["audio_url"] = ref_audios
        if model_lower == "kwvideo-v2-quannengcankao":
            if normalized_mode == "video_edit":
                params["_quan_neng_mode"] = "edit"
            elif normalized_mode == "video_extend":
                params["_quan_neng_mode"] = "extend"
            elif ref_videos:
                params["_quan_neng_mode"] = "quan_neng"
        return

    if model_lower in _LINGKE_VIDEO_REFERENCE_URLS_MODELS:
        if all_images:
            params["reference_urls"] = all_images
        if ref_videos and model_lower == "wan2.7-cankaosheng":
            params["reference_video"] = ref_videos
        if ref_file and model_lower == "wan3.0-video-cankaosheng":
            params["file_url"] = ref_file
        if ref_link and model_lower == "wan3.0-video-cankaosheng":
            params["link_url"] = ref_link
        return

    if model_lower == "wan2.6-shouzheng":
        if all_images:
            params["img_url"] = all_images[0]
        if ref_audios:
            params["audio_url"] = ref_audios[0]
        return

    if model_lower == "video-enhance":
        if ref_videos:
            params["video_url"] = ref_videos[0]
        return

    if model_lower in {"kling-v3-omni-videoref", "happyhorse-video-edit"}:
        if all_images:
            params["images"] = all_images
        if ref_videos:
            params["video"] = ref_videos[0]
        return

    if model_lower == "wan2.7-xuxie":
        if ref_videos:
            params["clips"] = ref_videos
        return

    if model_lower == "kling-avatar-image2video":
        if all_images:
            params["image"] = all_images[0]
        if ref_audios:
            params["sound_file"] = ref_audios[0]
        return

    if model_lower == "vidu-mv":
        if all_images:
            params["images"] = all_images
        if ref_audios:
            params["audio_url"] = ref_audios[0]
        return

    if all_images:
        params["images"] = all_images
    if model_lower == "minimax-h3-max":
        params["mode"] = "shouweizhen" if all_images else "wensheng"
    elif model_lower == "seedance-2.5-anci":
        params["mode"] = "cankaosheng" if all_images else "wensheng"
    elif model_lower == "seedance-2.0-anci":
        params["mode"] = "cankaosheng"


def build_lingke_video_payload(
    payload: dict[str, object],
    *,
    request_schema: dict[str, object] | None = None,
    mode: str | None = None,
) -> dict[str, object]:
    """Convert a canonical NewAPI video payload into Lingke /v1/media/generate format."""
    schema = request_schema if isinstance(request_schema, dict) else {}
    model_name = str(payload.get("model") or "").strip()
    model_lower = model_name.lower()
    raw_metadata = payload.get("metadata")
    metadata = raw_metadata if isinstance(raw_metadata, dict) else {}

    params: dict[str, object] = {}

    if model_lower not in _LINGKE_VIDEO_NO_DURATION_MODELS:
        duration_text = _format_lingke_video_duration(
            payload.get("duration", payload.get("seconds"))
        )
        if duration_text:
            params["duration"] = duration_text

    if model_lower not in _LINGKE_VIDEO_NO_RESOLUTION_MODELS:
        formatted_resolution = _format_lingke_video_resolution(
            model_lower,
            metadata.get("resolution") or payload.get("resolution"),
            schema,
        )
        if formatted_resolution:
            params["resolution"] = formatted_resolution

    raw_ratio = (
        str(
            metadata.get("ratio")
            or metadata.get("aspect_ratio")
            or payload.get("ratio")
            or payload.get("aspect_ratio")
            or ""
        )
        .strip()
        .lower()
    )
    ratio_key = (
        "ratio" if model_lower in _LINGKE_VIDEO_RATIO_KEY_MODELS else "aspect_ratio"
    )
    if raw_ratio and model_lower not in _LINGKE_VIDEO_NO_RATIO_MODELS:
        if raw_ratio in {"auto", "adaptive"}:
            if model_lower == "viduq3-cankaosheng":
                params[ratio_key] = "auto"
            elif model_lower in _LINGKE_VIDEO_ADAPTIVE_RATIO_MODELS:
                params[ratio_key] = "adaptive"
        else:
            params[ratio_key] = raw_ratio

    _assign_lingke_video_media_params(
        params,
        model_lower=model_lower,
        normalized_mode=_normalize_video_mode(mode),
        first_frame=str(payload.get("image") or "").strip(),
        last_frame=str(metadata.get("last_frame_image") or "").strip(),
        ref_images=_dedupe_urls(
            payload.get("images") or metadata.get("reference_images") or []
        ),
        ref_videos=_dedupe_urls(metadata.get("reference_videos") or []),
        ref_audios=_dedupe_urls(
            payload.get("audios") or metadata.get("reference_audios") or []
        ),
        ref_file=str(metadata.get("reference_file") or "").strip(),
        ref_link=str(metadata.get("reference_link") or "").strip(),
    )

    transport_fields = {
        "model",
        "prompt",
        "n",
        "response_format",
        "watermark",
        "width",
        "height",
        "image",
        "images",
        "audios",
        "seconds",
        "duration",
        "generate_audio",
        "human_review",
        "return_last_frame",
        "omni_reference_task_type",
        "last_frame_image",
        "reference_images",
        "reference_videos",
        "reference_audios",
        "reference_file",
        "reference_link",
    }
    notify_url: object = None
    for definition in schema.get("parameters") or []:
        if not isinstance(definition, dict):
            continue
        path = str(definition.get("requestPath") or "").strip()
        if not path:
            continue
        value = value_at_request_path(payload, path)
        if value is None:
            continue
        relative_path = path.removeprefix("params.").removeprefix("metadata.")
        root = relative_path.split(".", 1)[0]
        if root == "notify_url":
            notify_url = value
            continue
        if root in transport_fields:
            continue
        if root in {"ratio", "aspect_ratio"}:
            if model_lower in _LINGKE_VIDEO_NO_RATIO_MODELS:
                continue
            relative_path = ratio_key + relative_path[len(root) :]
        elif root == "resolution":
            if model_lower in _LINGKE_VIDEO_NO_RESOLUTION_MODELS:
                continue
            value = _format_lingke_video_resolution(model_lower, value, schema)
        set_nested_value(params, relative_path, value)

    result: dict[str, object] = {
        "model": model_name,
        "prompt": payload.get("prompt"),
        "params": params,
    }
    if isinstance(notify_url, str) and notify_url.strip():
        result["notify_url"] = notify_url.strip()
    return result


def extract_lingke_submit_error(submitted: object) -> str:
    """Return a concise error message if a Lingke HTTP-200 submit body signals failure."""
    if not isinstance(submitted, dict):
        return ""
    code = submitted.get("code")
    if code is None:
        return ""
    try:
        numeric_code = int(code)
    except (TypeError, ValueError):
        return ""
    if 200 <= numeric_code < 300:
        return ""
    parts: list[str] = []
    msg = str(submitted.get("msg") or submitted.get("message") or "").strip()
    if msg:
        parts.append(msg)
    data = submitted.get("data")
    if isinstance(data, dict):
        reasons = data.get("失败原因")
        if isinstance(reasons, list) and reasons:
            parts.append(", ".join(str(item) for item in reasons if item))
        elif isinstance(reasons, str) and reasons.strip():
            parts.append(reasons.strip())
        details = data.get("详情") or data.get("error")
        if details:
            parts.append(str(details))
    return f"Lingke submit failed (code={numeric_code}): " + (
        "; ".join(parts) if parts else str(submitted)
    )


def lingke_task_outcome(task: dict[str, Any]) -> tuple[str, str]:
    """Interpret Lingke task-status polling payload into (outcome, detail).

    ``outcome`` is one of ``'completed'``, ``'failed'``, or ``'pending'``.
    """
    is_final = task.get("is_final")
    state = str(task.get("state") or "").strip().lower()
    raw_status = str(task.get("status") or "").strip().lower()

    if is_final is False:
        return "pending", state or raw_status or "running"

    if is_final is True:
        if state in {"success", "succeeded", "completed", "done"} or (
            not state and raw_status in {"success", "succeeded", "completed", "done"}
        ):
            return "completed", ""
        detail = (
            task.get("error")
            or task.get("error_code")
            or task.get("fail_reason")
            or task.get("status")
            or state
            or "Lingke video task failed"
        )
        return "failed", str(detail)

    candidate = state or raw_status
    if candidate in {"completed", "succeeded", "success", "done"}:
        return "completed", ""
    if candidate in {"failure", "failed", "error", "canceled", "cancelled", "expired"}:
        detail = (
            task.get("error")
            or task.get("error_code")
            or task.get("fail_reason")
            or task.get("status")
            or candidate
            or "Lingke video task failed"
        )
        return "failed", str(detail)
    return "pending", candidate or "queued"
