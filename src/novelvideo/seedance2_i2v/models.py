"""Typed Seedance 2.0 image-to-video configuration."""

from __future__ import annotations

import json
import mimetypes
import urllib.parse
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator


SEEDANCE2_PROMPT_GUIDANCE_TEMPLATE_KEYS = frozenset(
    {"subject", "scene", "lighting", "camera", "style", "no_subtitle"}
)
VIDEO_PROMPT_STYLE_VALUES = frozenset(
    {"seedance_2_0", "seedance_2_5", "minimax_h3", "generic", "s9_segment"}
)


def normalize_video_prompt_style(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if text in VIDEO_PROMPT_STYLE_VALUES else ""


class Seedance2I2VMode(str, Enum):
    TEXT_TO_VIDEO = "text_to_video"
    FIRST_FRAME = "first_frame"
    FIRST_LAST_FRAME = "first_last_frame"
    MULTIMODAL_REFERENCE = "multimodal_reference"


class Seedance2VideoConfig(BaseModel):
    """Per-beat Seedance 2.0 settings persisted in beats.seedance2_config_json."""

    mode: Seedance2I2VMode = Seedance2I2VMode.MULTIMODAL_REFERENCE
    final_prompt: str = ""
    prompt_style: str = ""
    prompt_guidance: str = ""
    prompt_guidance_template_keys: list[str] = Field(default_factory=list)
    prompt_source: str = ""
    prompt_inputs_hash: str = ""
    prompt_updated_at: str = ""
    duration: int = 4
    duration_user_set: bool = False
    resolution: str = "720p"
    ratio: str = "9:16"
    ratio_user_set: bool = False
    generate_audio: bool = True
    generate_audio_user_set: bool = False
    return_last_frame: bool = False
    human_review: bool = True
    human_review_user_set: bool = False
    scene_optimize: str = ""
    reference_image_paths: list[str] = Field(default_factory=list)
    reference_audio_paths: list[str] = Field(default_factory=list)
    text_overlay: dict[str, Any] = Field(default_factory=dict)
    selected_asset_keys: list[str] = Field(default_factory=list)
    s9_tail_anchor: bool = False
    s9_include_first_frame: bool = False

    @field_validator(
        "final_prompt",
        "prompt_style",
        "prompt_guidance",
        "prompt_source",
        "prompt_inputs_hash",
        "prompt_updated_at",
        "resolution",
        "ratio",
        "scene_optimize",
        mode="before",
    )
    @classmethod
    def _strip_text(cls, value: Any) -> str:
        return str(value or "").strip()

    @field_validator("prompt_style", mode="after")
    @classmethod
    def _normalize_prompt_style(cls, value: str) -> str:
        return normalize_video_prompt_style(value)

    @field_validator("duration", mode="before")
    @classmethod
    def _coerce_duration(cls, value: Any) -> int:
        try:
            duration = int(float(value or 4))
        except (TypeError, ValueError):
            duration = 4
        return max(1, duration)

    @field_validator("prompt_guidance_template_keys", mode="before")
    @classmethod
    def _normalize_guidance_template_keys(cls, value: Any) -> list[str]:
        values = value if isinstance(value, (list, tuple)) else []
        normalized: list[str] = []
        for item in values:
            key = str(item or "").strip()
            if key in SEEDANCE2_PROMPT_GUIDANCE_TEMPLATE_KEYS and key not in normalized:
                normalized.append(key)
        return normalized


def default_seedance2_ratio_for_project_aspect(aspect_ratio: str | None) -> str:
    """Map project_config aspect_ratio ('16:9', '2:3', '9:16') to a Seedance 2 ratio."""
    cleaned = str(aspect_ratio or "").strip()
    if cleaned == "16:9":
        return "16:9"
    if cleaned in {"9:16", "2:3"}:
        return "9:16"
    return "9:16"


def default_seedance2_ratio_for_state_dir(state_dir: str | Path | None) -> str | None:
    """Read project_config.json in state_dir and return the implied Seedance 2 ratio."""
    if not state_dir:
        return None
    from novelvideo.project_config import load_project_config_file_from_state_dir

    config = load_project_config_file_from_state_dir(state_dir)
    aspect_ratio = str(config.get("aspect_ratio") or "").strip()
    if not aspect_ratio:
        return None
    return default_seedance2_ratio_for_project_aspect(aspect_ratio)


def _is_s9_preserved_duration(source: Any) -> bool:
    """Return True when config holds a valid s9-calculated segment duration (>= 4s)."""

    if isinstance(source, Seedance2VideoConfig):
        prompt_source = source.prompt_source
        prompt_style = source.prompt_style
        duration = source.duration
    elif isinstance(source, dict):
        prompt_source = str(source.get("prompt_source") or "").strip()
        prompt_style = str(source.get("prompt_style") or "").strip()
        try:
            duration = int(float(source.get("duration") or 0))
        except (TypeError, ValueError):
            duration = 0
    else:
        return False
    return duration >= 4 and (prompt_source == "s9_script" or prompt_style == "s9_segment")


_AUDIO_EXTENSIONS = frozenset(
    {
        ".mp3",
        ".wav",
        ".wave",
        ".m4a",
        ".aac",
        ".ogg",
        ".oga",
        ".flac",
        ".opus",
        ".wma",
        ".amr",
        ".weba",
    }
)
_VIDEO_EXTENSIONS = frozenset(
    {
        ".mp4",
        ".mov",
        ".m4v",
        ".avi",
        ".mkv",
        ".webm",
        ".flv",
        ".wmv",
        ".mpeg",
        ".mpg",
        ".3gp",
    }
)
_IMAGE_EXTENSIONS = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".webp",
        ".gif",
        ".bmp",
        ".tif",
        ".tiff",
        ".heic",
        ".heif",
        ".avif",
    }
)


def infer_reference_media_kind(value: object, *, fallback: str = "") -> str:
    """Infer ``'image'``, ``'audio'``, or ``'video'`` from a path, URL, or data URI."""
    text = str(value or "").strip()
    if not text:
        return fallback
    if text[:5].lower() == "data:":
        mime_type = text[5:].split(";", 1)[0].split(",", 1)[0].strip().lower()
        kind = mime_type.partition("/")[0]
        return kind if kind in {"image", "audio", "video"} else fallback

    parsed = urllib.parse.urlsplit(text)
    if parsed.scheme.lower() in {"http", "https", "file"}:
        clean_path = urllib.parse.unquote(parsed.path)
    else:
        # Preserve Windows drive paths like C:\dir\file.mp3 while stripping query/fragment.
        clean_path = text.split("?", 1)[0].split("#", 1)[0]

    suffix = Path(clean_path).suffix.lower()
    if suffix in _AUDIO_EXTENSIONS:
        return "audio"
    if suffix in _VIDEO_EXTENSIONS:
        return "video"
    if suffix in _IMAGE_EXTENSIONS:
        return "image"

    guessed = (mimetypes.guess_type(clean_path)[0] or "").partition("/")[0].lower()
    return guessed if guessed in {"image", "audio", "video"} else fallback


def _separate_reference_paths(config: Seedance2VideoConfig) -> Seedance2VideoConfig:
    images = config.reference_image_paths
    audios = config.reference_audio_paths
    misplaced_audio = {
        path for path in images if infer_reference_media_kind(path) == "audio"
    }
    misplaced_images = {
        path for path in audios if infer_reference_media_kind(path) == "image"
    }
    if misplaced_audio or misplaced_images:
        # Append recovered references after the destination's existing entries.
        config.reference_image_paths = list(
            dict.fromkeys(
                [path for path in images if path not in misplaced_audio]
                + [path for path in audios if path in misplaced_images]
            )
        )
        config.reference_audio_paths = list(
            dict.fromkeys(
                [path for path in audios if path not in misplaced_images]
                + [path for path in images if path in misplaced_audio]
            )
        )
    return config


def parse_seedance2_config(
    value: Any,
    *,
    default_ratio: str | None = None,
    default_duration: int | None = None,
) -> Seedance2VideoConfig:
    """Parse a stored dict/JSON config into a normalized config object."""

    safe_default_ratio = str(default_ratio or "").strip() or None
    safe_default_duration = (
        max(1, int(default_duration))
        if isinstance(default_duration, (int, float)) and int(default_duration) > 0
        else None
    )

    if isinstance(value, Seedance2VideoConfig):
        result = value.model_copy()
        if safe_default_ratio and not result.ratio_user_set:
            result.ratio = safe_default_ratio
        if safe_default_duration and not result.duration_user_set and not _is_s9_preserved_duration(result):
            result.duration = safe_default_duration
        return _separate_reference_paths(result)

    if isinstance(value, str):
        text = value.strip()
        if not text:
            kwargs: dict[str, Any] = {}
            if safe_default_ratio:
                kwargs["ratio"] = safe_default_ratio
            if safe_default_duration:
                kwargs["duration"] = safe_default_duration
            return Seedance2VideoConfig(**kwargs)
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            kwargs = {"final_prompt": text}
            if safe_default_ratio:
                kwargs["ratio"] = safe_default_ratio
            if safe_default_duration:
                kwargs["duration"] = safe_default_duration
            return Seedance2VideoConfig(**kwargs)
    if isinstance(value, dict):
        value = dict(value)
        if value.get("generate_audio") is False and not value.get("generate_audio_user_set"):
            value["generate_audio"] = True
        if value.get("human_review") is False and not value.get("human_review_user_set"):
            value["human_review"] = True
        if safe_default_ratio and not value.get("ratio_user_set"):
            value["ratio"] = safe_default_ratio
        if safe_default_duration and not value.get("duration_user_set") and not _is_s9_preserved_duration(value):
            value["duration"] = safe_default_duration
        return _separate_reference_paths(Seedance2VideoConfig.model_validate(value))
    kwargs = {}
    if safe_default_ratio:
        kwargs["ratio"] = safe_default_ratio
    if safe_default_duration:
        kwargs["duration"] = safe_default_duration
    return Seedance2VideoConfig(**kwargs)


def dump_seedance2_config(config: Seedance2VideoConfig | dict[str, Any] | str | None) -> str:
    """Serialize config for SQLite storage."""

    return parse_seedance2_config(config).model_dump_json()
