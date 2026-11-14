"""Fixed-camera spatial lock & mask-guided background paste-back utilities.

Extracts character envelopes directly from color-coded storyboard sketches
(where the scene is monochrome line-art and characters are saturated markers),
optionally restricts edits to the upper body, aligns lighting/color tone, and
composites generated frames back onto a locked anchor frame or sketch.
"""

from __future__ import annotations

import colorsys
from pathlib import Path
from typing import Mapping

import numpy as np
from PIL import Image, ImageFilter

_DEFAULT_SAT_DIFF = 0.06
_DEFAULT_MAXC_MIN = 0.15
_DEFAULT_MINC_MAX = 0.85


def _hex_to_hue_deg(hex_code: str) -> float | None:
    raw = str(hex_code or "").strip().split(" ", 1)[0].lstrip("#")
    if len(raw) != 6:
        return None
    try:
        r = int(raw[0:2], 16) / 255.0
        g = int(raw[2:4], 16) / 255.0
        b = int(raw[4:6], 16) / 255.0
    except ValueError:
        return None
    h, s, v = colorsys.rgb_to_hsv(r, g, b)
    if s < 0.08 or v < 0.08:
        return None
    return h * 360.0


def extract_sketch_character_mask(
    sketch_path: Path | str,
    *,
    sketch_colors: Mapping[str, str] | None = None,
) -> Image.Image:
    """Extract an 'L' mask (0 or 255) of color-coded characters from a sketch."""
    with Image.open(sketch_path) as img:
        rgb = img.convert("RGB")
        arr = np.asarray(rgb, dtype=np.float32) / 255.0

    r = arr[:, :, 0]
    g = arr[:, :, 1]
    b = arr[:, :, 2]
    maxc = np.maximum(np.maximum(r, g), b)
    minc = np.minimum(np.minimum(r, g), b)
    diff = maxc - minc

    sat_mask = (maxc > _DEFAULT_MAXC_MIN) & (minc < _DEFAULT_MINC_MAX) & (diff > _DEFAULT_SAT_DIFF)

    target_hues: list[float] = []
    if sketch_colors:
        for color_str in sketch_colors.values():
            hue = _hex_to_hue_deg(str(color_str))
            if hue is not None:
                target_hues.append(hue)

    final_bool = sat_mask
    if target_hues and sat_mask.any():
        hue = np.zeros_like(maxc)
        valid = sat_mask
        r_m = r[valid]
        g_m = g[valid]
        b_m = b[valid]
        maxc_m = maxc[valid]
        diff_m = diff[valid] + 1e-10

        hue_m = np.zeros_like(r_m)
        red_m = maxc_m == r_m
        green_m = (~red_m) & (maxc_m == g_m)
        blue_m = (~red_m) & (~green_m)
        hue_m[red_m] = (60.0 * ((g_m[red_m] - b_m[red_m]) / diff_m[red_m])) % 360.0
        hue_m[green_m] = 60.0 * (2.0 + (b_m[green_m] - r_m[green_m]) / diff_m[green_m])
        hue_m[blue_m] = 60.0 * (4.0 + (r_m[blue_m] - g_m[blue_m]) / diff_m[blue_m])
        hue[valid] = hue_m % 360.0

        hue_matched = np.zeros_like(sat_mask, dtype=bool)
        for target_h in target_hues:
            dist = np.abs(hue - target_h)
            dist = np.minimum(dist, 360.0 - dist)
            hue_matched |= sat_mask & (dist <= 32.0)

        # Keep hue-filtered mask when it captures real character markers;
        # otherwise keep general saturation mask (handles custom marker colors).
        if int(hue_matched.sum()) >= 50:
            final_bool = hue_matched

    mask_img = Image.fromarray((final_bool.astype(np.uint8) * 255), mode="L")
    return mask_img.filter(ImageFilter.MedianFilter(size=3))


def _odd_kernel_size(radius_px: int, *, max_kernel: int = 151) -> int:
    radius = max(1, int(radius_px))
    kernel = radius * 2 + 1
    if kernel > max_kernel:
        kernel = max_kernel
    if kernel % 2 == 0:
        kernel += 1
    return kernel


def _apply_upper_body_gate(mask_arr: np.ndarray) -> np.ndarray:
    """Keep upper ~62% of each vertical character bbox editable and lock feet/legs."""
    ys, _xs = np.where(mask_arr > 0)
    if len(ys) == 0:
        return mask_arr
    y_min = int(ys.min())
    y_max = int(ys.max())
    span = y_max - y_min
    if span < 20:
        return mask_arr

    cutoff_start = y_min + span * 0.56
    cutoff_end = y_min + span * 0.68
    rows = np.arange(mask_arr.shape[0], dtype=np.float32)[:, None]
    gate = np.clip((cutoff_end - rows) / max(1.0, cutoff_end - cutoff_start), 0.0, 1.0)
    gated = (mask_arr.astype(np.float32) * gate).clip(0.0, 255.0).astype(np.uint8)
    return gated


def build_character_envelope_mask(
    anchor_sketch_path: Path | str | None,
    target_sketch_path: Path | str | None,
    *,
    target_size: tuple[int, int],
    sketch_colors: Mapping[str, str] | None = None,
    lock_spatial_mode: str = "full_body",
    dilate_ratio: float = 0.055,
    min_dilate_px: int = 18,
    feather_ratio: float = 0.016,
    min_feather_px: int = 6,
) -> Image.Image | None:
    """Build a feathered soft-alpha character envelope mask covering anchor + target poses."""
    width, height = target_size
    if width <= 0 or height <= 0:
        return None

    combined = np.zeros((height, width), dtype=np.uint8)
    has_any_source = False
    for path in (anchor_sketch_path, target_sketch_path):
        if not path:
            continue
        p = Path(path)
        if not p.is_file():
            continue
        has_any_source = True
        raw_mask = extract_sketch_character_mask(p, sketch_colors=sketch_colors)
        if raw_mask.size != (width, height):
            raw_mask = raw_mask.resize((width, height), Image.Resampling.NEAREST)
        combined = np.maximum(combined, np.asarray(raw_mask, dtype=np.uint8))

    if not has_any_source or int((combined > 0).sum()) < 16:
        return None

    if str(lock_spatial_mode or "").strip().lower() == "upper_body":
        combined = _apply_upper_body_gate(combined)
        if int((combined > 0).sum()) < 16:
            return None

    short_side = min(width, height)
    dilate_radius = max(min_dilate_px, round(short_side * dilate_ratio))
    feather_radius = max(min_feather_px, round(short_side * feather_ratio))

    envelope = Image.fromarray(combined, mode="L")
    envelope = envelope.filter(ImageFilter.MaxFilter(size=_odd_kernel_size(dilate_radius)))
    envelope = envelope.filter(ImageFilter.GaussianBlur(radius=feather_radius))
    return envelope


def _color_match_to_anchor(
    generated_rgb: np.ndarray,
    anchor_rgb: np.ndarray,
    alpha_01: np.ndarray,
) -> np.ndarray:
    """Gently align generated image exposure/white balance to the locked anchor background."""
    bg_mask = alpha_01 < 0.15
    if int(bg_mask.sum()) < 100:
        bg_mask = np.ones(alpha_01.shape, dtype=bool)

    matched = generated_rgb.astype(np.float32, copy=True)
    for ch in range(3):
        gen_vals = generated_rgb[:, :, ch][bg_mask].astype(np.float32)
        anc_vals = anchor_rgb[:, :, ch][bg_mask].astype(np.float32)
        gen_mean = float(gen_vals.mean())
        anc_mean = float(anc_vals.mean())
        gen_std = float(gen_vals.std())
        anc_std = float(anc_vals.std())

        gain = 1.0 if gen_std < 3.0 else float(np.clip(anc_std / gen_std, 0.75, 1.35))
        offset = float(np.clip(anc_mean - gen_mean * gain, -40.0, 40.0))
        matched[:, :, ch] = matched[:, :, ch] * gain + offset
    return np.clip(matched, 0.0, 255.0)


def _save_matching_format(image: Image.Image, target_path: Path) -> None:
    suffix = target_path.suffix.lower()
    if suffix in {".jpg", ".jpeg"}:
        image.convert("RGB").save(target_path, format="JPEG", quality=95)
    else:
        image.save(target_path)


def composite_locked_background(
    locked_bg_path: Path | str,
    generated_path: Path | str,
    alpha_mask: Image.Image,
    *,
    output_path: Path | str | None = None,
    match_color: bool = True,
) -> Path:
    """Paste locked_bg pixels outside alpha_mask back onto generated_path."""
    locked_p = Path(locked_bg_path)
    gen_p = Path(generated_path)
    out_p = Path(output_path) if output_path else gen_p

    with Image.open(gen_p) as gen_img_raw:
        gen_img = gen_img_raw.convert("RGB")
        target_size = gen_img.size
        gen_arr = np.asarray(gen_img, dtype=np.float32)

    with Image.open(locked_p) as bg_img_raw:
        bg_img = bg_img_raw.convert("RGB")
        if bg_img.size != target_size:
            bg_img = bg_img.resize(target_size, Image.Resampling.LANCZOS)
        bg_arr = np.asarray(bg_img, dtype=np.float32)

    mask = alpha_mask.convert("L")
    if mask.size != target_size:
        mask = mask.resize(target_size, Image.Resampling.BILINEAR)
    alpha = (np.asarray(mask, dtype=np.float32) / 255.0)[:, :, None]

    if match_color:
        gen_arr = _color_match_to_anchor(gen_arr, bg_arr, alpha[:, :, 0])

    blended = np.clip(gen_arr * alpha + bg_arr * (1.0 - alpha), 0.0, 255.0).astype(np.uint8)
    out_img = Image.fromarray(blended, mode="RGB")
    out_p.parent.mkdir(parents=True, exist_ok=True)
    _save_matching_format(out_img, out_p)
    return out_p


def composite_locked_sketch(
    anchor_sketch_path: Path | str,
    generated_sketch_path: Path | str,
    alpha_mask: Image.Image,
    *,
    output_path: Path | str | None = None,
) -> Path:
    """Keep anchor monochrome line-art outside the character envelope mask."""
    return composite_locked_background(
        locked_bg_path=anchor_sketch_path,
        generated_path=generated_sketch_path,
        alpha_mask=alpha_mask,
        output_path=output_path,
        match_color=False,
    )
