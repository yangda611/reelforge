"""Image model resolution & request-schema profile helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_RESOLUTION_RANKS: dict[str, tuple[float, str]] = {
    "512": (0.5, "512"),
    "0.5k": (0.5, "0.5K"),
    "1k": (1.0, "1K"),
    "2k": (2.0, "2K"),
    "3k": (3.0, "3K"),
    "4k": (4.0, "4K"),
}


@dataclass(frozen=True, slots=True)
class ImageModelProfile:
    """Resolved capability profile for an image generation model."""

    max_image_size: str = "1K"
    request_schema: dict[str, Any] = field(default_factory=dict)


def _normalize_resolution_option(raw: object) -> tuple[float, str] | None:
    text = str(raw or "").strip()
    if not text:
        return None
    lowered = text.lower()
    if lowered in _RESOLUTION_RANKS:
        return _RESOLUTION_RANKS[lowered]
    k_match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*k", lowered)
    if k_match:
        value = float(k_match.group(1))
        label = (
            f"{int(value)}K" if value.is_integer() else f"{k_match.group(1)}K"
        )
        return (value, label)
    dim_match = re.fullmatch(r"(\d+)\s*[xX×]\s*(\d+)", text)
    if dim_match:
        max_edge = max(int(dim_match.group(1)), int(dim_match.group(2)))
        return (max_edge / 1024.0, text)
    return None


def _pick_highest_resolution_option(options: object) -> str:
    if not isinstance(options, list):
        return ""
    best_rank = -1.0
    best_label = ""
    for item in options:
        parsed = _normalize_resolution_option(item)
        if parsed is None:
            continue
        rank, label = parsed
        if rank > best_rank:
            best_rank = rank
            best_label = label
    return best_label


def _catalog_entry_candidates(
    provider: str,
    model: str,
    selection: str | None = None,
) -> list[str]:
    candidates: list[str] = []
    for raw in (selection, model):
        clean = str(raw or "").strip()
        if not clean:
            continue
        if clean not in candidates:
            candidates.append(clean)
        if clean.startswith("newapi_"):
            stripped = clean[len("newapi_") :].strip()
            if stripped and stripped not in candidates:
                candidates.append(stripped)
        if clean.startswith("huimeng_") or clean.startswith("openrouter_") or clean.startswith("openai_"):
            if clean not in candidates:
                candidates.append(clean)
    _ = provider
    return candidates


def _lookup_ce_mapping_entry(candidates: list[str]) -> dict[str, Any] | None:
    try:
        from novelvideo.model_gateway_settings import get_newapi_media_model_mappings

        mappings = get_newapi_media_model_mappings()
    except Exception:
        return None
    if not isinstance(mappings, dict) or not mappings:
        return None
    lowered_candidates = [c.lower() for c in candidates]
    for candidate in candidates:
        item = mappings.get(candidate)
        if isinstance(item, dict) and str(item.get("mediaType") or "image").lower() in {"", "image"}:
            return item
    for key, item in mappings.items():
        if not isinstance(item, dict):
            continue
        if str(item.get("mediaType") or "").strip().lower() not in {"", "image"}:
            continue
        upstream = str(item.get("upstreamModel") or "").strip().lower()
        aliases = [
            str(a or "").strip().lower()
            for a in (item.get("aliases") or [])
            if str(a or "").strip()
        ]
        if (
            str(key).strip().lower() in lowered_candidates
            or (upstream and upstream in lowered_candidates)
            or any(c in aliases for c in lowered_candidates)
        ):
            return item
    return None


def _lookup_official_catalog_entry(candidates: list[str]) -> dict[str, Any] | None:
    try:
        from novelvideo.model_gateway_settings import (
            get_bundled_media_model_catalog,
            get_official_media_model_catalog,
        )

        entries = get_official_media_model_catalog("image")
        if not entries:
            entries = get_bundled_media_model_catalog("image")
    except Exception:
        return None
    lowered_candidates = [c.lower() for c in candidates]
    for item in entries or []:
        if not isinstance(item, dict):
            continue
        identifiers = [
            str(item.get("id") or "").strip().lower(),
            str(item.get("gatewayModel") or "").strip().lower(),
            str(item.get("upstreamModel") or "").strip().lower(),
            *[
                str(a or "").strip().lower()
                for a in (item.get("aliases") or [])
                if str(a or "").strip()
            ],
        ]
        if any(c in identifiers for c in lowered_candidates):
            return item
    return None


def _build_request_schema_from_config(config: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(config, dict) or not config:
        return {}
    schema = dict(config.get("request")) if isinstance(config.get("request"), dict) else {}
    if config.get("qualityOptions"):
        schema["includeQuality"] = True
    if config.get("minPixels") is not None:
        schema["minPixels"] = config["minPixels"]
    return schema


def _infer_max_resolution_from_model_name(provider: str, model: str) -> str:
    clean_model = str(model or "").strip().lower()
    clean_provider = str(provider or "").strip().lower()

    if re.search(r"(?:^|[-_/])4k(?:$|[-_/])", clean_model) or clean_model.endswith("4k"):
        return "4K"
    if re.search(r"(?:^|[-_/])3k(?:$|[-_/])", clean_model) or clean_model.endswith("3k"):
        return "3K"
    if re.search(r"(?:^|[-_/])2k(?:$|[-_/])", clean_model) or clean_model.endswith("2k"):
        return "2K"

    if clean_provider == "google":
        return "4K" if "gemini-3" in clean_model else "1K"

    if any(
        token in clean_model
        for token in (
            "gemini-3",
            "nano-banana-2",
            "nanobanana2",
            "nanobanana-2",
            "nb-2",
            "nb-pro",
            "gpt-image-2",
            "lingshan-g2",
            "seedream-4.5",
        )
    ):
        return "4K"
    if "seedream-5.0-lite" in clean_model:
        return "3K"
    if "seedream" in clean_model:
        return "2K"
    return "1K"


def resolve_image_model_profile(
    provider: str,
    model: str,
    selection: str | None = None,
) -> ImageModelProfile:
    """Resolve the highest supported resolution and request schema for an image model."""
    candidates = _catalog_entry_candidates(provider, model, selection)

    ce_item = _lookup_ce_mapping_entry(candidates)
    ce_config = (
        ce_item.get("config")
        if isinstance(ce_item, dict) and isinstance(ce_item.get("config"), dict)
        else {}
    )
    official_item = _lookup_official_catalog_entry(candidates)
    official_config = (
        official_item.get("config")
        if isinstance(official_item, dict)
        and isinstance(official_item.get("config"), dict)
        else {}
    )

    max_size = _pick_highest_resolution_option(ce_config.get("resolutionOptions"))
    if not max_size:
        max_size = _pick_highest_resolution_option(official_config.get("resolutionOptions"))
    if not max_size:
        max_size = _infer_max_resolution_from_model_name(provider, model)

    merged_config = {**official_config, **ce_config}
    request_schema = _build_request_schema_from_config(merged_config)
    return ImageModelProfile(max_image_size=max_size, request_schema=request_schema)


def resolve_highest_image_resolution(
    provider: str,
    model: str,
    selection: str | None = None,
) -> str:
    """Return the highest supported resolution label (e.g. ``'4K'``) for a model."""
    return resolve_image_model_profile(provider, model, selection=selection).max_image_size
