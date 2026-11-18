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


def _extract_entry_config(item: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {}
    nested = item.get("config")
    if isinstance(nested, dict) and nested:
        return nested
    return item


def _build_request_schema_from_config(
    config: dict[str, Any] | None, *, model: str = ""
) -> dict[str, Any]:
    if not isinstance(config, dict) or not config:
        return {}
    raw_req = config.get("request") if isinstance(config.get("request"), dict) else {}
    parameters = raw_req.get("parameters") if isinstance(raw_req.get("parameters"), list) else []
    omit_paths = raw_req.get("omitPaths") if isinstance(raw_req.get("omitPaths"), list) else []
    min_pixels = config.get("minPixels")
    lowered_model = str(model or "").strip().lower()
    already_supports_quality = (
        lowered_model in {"lingshan-g2", "gpt-image-2", "image-2", "image-2-official"}
        or "gpt-image" in lowered_model
    )
    needs_quality_flag = bool(config.get("qualityOptions")) and not already_supports_quality
    if not parameters and not omit_paths and min_pixels is None and not needs_quality_flag:
        return {}
    schema = dict(raw_req)
    if config.get("qualityOptions"):
        schema["includeQuality"] = True
    if min_pixels is not None:
        schema["minPixels"] = min_pixels
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
    ce_config = _extract_entry_config(ce_item)
    official_item = _lookup_official_catalog_entry(candidates)
    official_config = _extract_entry_config(official_item)

    max_size = _pick_highest_resolution_option(ce_config.get("resolutionOptions"))
    if not max_size:
        max_size = _pick_highest_resolution_option(official_config.get("resolutionOptions"))
    if not max_size:
        max_size = _infer_max_resolution_from_model_name(provider, model)

    merged_config = {**official_config, **ce_config}
    request_schema = _build_request_schema_from_config(merged_config, model=model)
    return ImageModelProfile(max_image_size=max_size, request_schema=request_schema)


def resolve_highest_image_resolution(
    provider: str,
    model: str,
    selection: str | None = None,
) -> str:
    """Return the highest supported resolution label (e.g. ``'4K'``) for a model."""
    return resolve_image_model_profile(provider, model, selection=selection).max_image_size


def resolve_supported_image_resolutions(
    provider: str,
    model: str,
    selection: str | None = None,
) -> list[str]:
    """Return sorted supported named resolution labels when known, else []."""
    candidates = _catalog_entry_candidates(provider, model, selection)
    ce_item = _lookup_ce_mapping_entry(candidates)
    ce_config = _extract_entry_config(ce_item)
    official_item = _lookup_official_catalog_entry(candidates)
    official_config = _extract_entry_config(official_item)
    options = ce_config.get("resolutionOptions")
    if not isinstance(options, list) or not options:
        options = official_config.get("resolutionOptions")

    parsed: list[tuple[float, str]] = []
    if isinstance(options, list) and options:
        for item in options:
            info = _normalize_resolution_option(item)
            if info is not None and not re.fullmatch(r"\d+\s*[xX×]\s*\d+", info[1]):
                parsed.append(info)
    if parsed:
        dedup: dict[str, float] = {}
        for rank, label in sorted(parsed, key=lambda pair: pair[0]):
            dedup[label] = rank
        return list(dedup.keys())

    clean_model = str(model or "").strip().lower()
    if re.search(r"(?:^|[-_/])4k(?:$|[-_/])", clean_model) or clean_model.endswith("4k"):
        return ["4K"]
    if re.search(r"(?:^|[-_/])3k(?:$|[-_/])", clean_model) or clean_model.endswith("3k"):
        return ["3K"]
    if re.search(r"(?:^|[-_/])2k(?:$|[-_/])", clean_model) or clean_model.endswith("2k"):
        return ["2K"]
    _ = provider
    return []


def clamp_image_size_for_model(
    provider: str,
    model: str,
    requested_size: str,
    selection: str | None = None,
) -> str:
    """Clamp a named resolution tier to the model's supported tiers when known.

    Explicit pixel strings (``WxH``) and unrecognized values are passed through
    untouched so existing validation continues to handle them.
    """
    raw = str(requested_size or "").strip()
    if not raw or re.fullmatch(r"\d+\s*[xX×]\s*\d+", raw):
        return raw
    parsed_req = _normalize_resolution_option(raw)
    if parsed_req is None:
        return raw
    supported = resolve_supported_image_resolutions(provider, model, selection=selection)
    if not supported:
        return raw

    req_rank, req_label = parsed_req
    supported_parsed = [
        _normalize_resolution_option(item) for item in supported
    ]
    supported_pairs = [pair for pair in supported_parsed if pair is not None]
    if not supported_pairs:
        return raw
    for rank, label in supported_pairs:
        if label.lower() == req_label.lower() or rank == req_rank:
            return label
    supported_pairs.sort(key=lambda pair: pair[0])
    if req_rank < supported_pairs[0][0]:
        return supported_pairs[0][1]
    if req_rank > supported_pairs[-1][0]:
        return supported_pairs[-1][1]
    return min(supported_pairs, key=lambda pair: abs(pair[0] - req_rank))[1]
