"""Structured errors for model-gateway failures.

A gateway that rejects a model (unknown model, bad key, unreachable host)
used to surface as a bare HTTP 500 "Internal Server Error": the real cause
only existed in the server log, so a user could not tell whether they had
misconfigured the gateway, the gateway was down, or the software was
broken. These helpers turn a gateway failure into a displayable error
that names the model and the gateway's own message.
"""

from __future__ import annotations

import re
from typing import Any

_MODEL_IN_MESSAGE = re.compile(r"[Mm]odel\s+([A-Za-z0-9][\w:./-]{0,127})")


class GatewayModelError(RuntimeError):
    """A text-model call the gateway refused or could not serve."""

    def __init__(
        self,
        model: str,
        *,
        gateway_status: int | None = None,
        gateway_message: str = "",
    ) -> None:
        self.model = model
        self.gateway_status = gateway_status
        self.gateway_message = gateway_message or ""
        if gateway_status is not None:
            detail = (
                f"模型 {model} 调用失败（网关返回 {gateway_status}）"
                if model
                else f"模型调用失败（网关返回 {gateway_status}）"
            )
        elif model:
            detail = f"模型 {model} 调用失败"
        else:
            detail = "模型调用失败"
        if self.gateway_message:
            detail = f"{detail}：{self.gateway_message}"
        super().__init__(detail)


def _gateway_message_from_body(body: Any) -> str:
    """Extract the human message from an OpenAI-style error payload."""
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            message = str(error.get("message", "") or "").strip()
            if message:
                return message
        message = str(body.get("message", "") or "").strip()
        if message:
            return message
    if isinstance(body, str) and body.strip():
        return body.strip()
    return ""


def extract_gateway_error(exc: BaseException) -> dict[str, Any] | None:
    """Find a gateway failure in an exception chain, if one is there.

    Text calls go through PydanticAI, which wraps the OpenAI-compatible
    gateway error in ``ModelHTTPError`` (whose ``.status_code`` and
    ``.body`` carry the gateway's response) or lets the OpenAI SDK's
    ``APIStatusError`` through. Walks ``__cause__`` (and ``__context__``
    as a fallback) so a wrapped failure is still recognised. Returns
    ``{"status", "message", "model"}`` or ``None`` when the chain holds
    no gateway error. The message is the gateway's own text, truncated
    so a huge upstream payload never reaches the UI.
    """
    chain: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None:
        if id(current) in seen:
            break
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or current.__context__
    for item in chain:
        status_code = getattr(item, "status_code", None)
        if status_code is None:
            continue
        try:
            status = int(status_code)
        except (TypeError, ValueError):
            continue
        if not 400 <= status < 600:
            continue
        body = getattr(item, "body", None)
        message = _gateway_message_from_body(body) or str(item).strip()
        # Never leak a full upstream payload or a key into the UI.
        if len(message) > 500:
            message = message[:500] + "…"
        model = ""
        match = _MODEL_IN_MESSAGE.search(message)
        if match:
            model = match.group(1).strip(".,;:\"'")
        return {"status": status, "message": message, "model": model}
    return None


def gateway_error_message(
    exc: BaseException,
    *,
    model: str = "",
) -> GatewayModelError | None:
    """Convert an exception into a GatewayModelError, when it is one.

    ``model`` is the model the caller resolved for this route; it is used
    when the gateway's own message does not name the model. Returns None
    when the exception chain holds no gateway failure.
    """
    found = extract_gateway_error(exc)
    if found is None:
        return None
    return GatewayModelError(
        found["model"] or model,
        gateway_status=found["status"],
        gateway_message=found["message"],
    )
