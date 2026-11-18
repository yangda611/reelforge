from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


pytestmark = pytest.mark.m09


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_render_plan_routes_are_removed() -> None:
    source = (REPO_ROOT / "src/novelvideo/api/routes/generation.py").read_text(
        encoding="utf-8"
    )

    assert "/videos/generate" not in source
    for removed in ("/render/plan", "RenderPlanRequest", "start_render_plan_task"):
        assert removed not in source
    # first-frame upload is the one render-prefixed route that stays
    assert "/render/upload" in source


def test_legacy_video_generation_task_surface_is_removed() -> None:
    from novelvideo.task_identity import TASK_IDENTITY_SPECS

    assert "video_generation" not in TASK_IDENTITY_SPECS
    assert importlib.util.find_spec("novelvideo.ray_tasks") is None
