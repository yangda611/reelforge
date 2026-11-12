"""Assistant (虾导) runtime status endpoint.

The frontend queries this on startup / when the assistant panel opens so a
missing local runtime is surfaced before the user sends a message, instead
of failing the first turn with a misleading "please restart the app" error.
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, Depends

from novelvideo.api.auth import get_api_user

router = APIRouter(prefix="/assistant")

RUNTIME_MISSING_ERROR_CODE = "runtime_missing"

_RUNTIME_LABELS = {
    "codex": "Codex",
    "claude": "Claude",
    "hermes": "Hermes",
}


def assistant_runtime_status() -> dict[str, Any]:
    """Detect the configured local assistant runtime without starting it.

    Never raises and never downloads/installs anything: availability is a
    pure probe (binary present + SDK importable). ``missing`` is the
    machine-readable signal; ``code`` is ``runtime_missing`` exactly when
    the runtime is absent, so the frontend can route to install guidance
    instead of a restart/update dead end.
    """
    from novelvideo.chat import service as chat_service

    preferred = (
        os.environ.get("DRAMACLAW_CHAT_BACKEND")
        or os.environ.get("SUPERTALE_CHAT_BACKEND")
        or "codex"
    ).strip().lower() or "codex"
    if preferred == "hermes":
        available = chat_service.is_hermes_backend_available()
    elif preferred == "claude":
        available = chat_service.is_claude_backend_available()
    elif preferred == "codex":
        available = chat_service.is_codex_backend_available()
    else:
        available = False
    label = _RUNTIME_LABELS.get(preferred, preferred or "Codex")
    if available:
        return {
            "available": True,
            "runtime": preferred,
            "missing": False,
            "code": "ok",
            "message": f"本地助手运行环境（{label}）可用。",
        }
    return {
        "available": False,
        "runtime": preferred,
        "missing": True,
        "code": RUNTIME_MISSING_ERROR_CODE,
        "message": f"未检测到本地助手运行环境（{label}），请在设置中安装后再使用。",
    }


@router.get("/runtime")
async def get_assistant_runtime(
    user: dict = Depends(get_api_user),
) -> dict[str, Any]:
    """Return whether the local assistant runtime is installed."""
    return {"ok": True, "data": assistant_runtime_status()}
