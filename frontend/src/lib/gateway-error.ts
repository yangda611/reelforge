// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import type { TFunction } from "i18next";

/**
 * 把网关/上游抛回的原始错误文本（常是 `status_code: 401, model_name: ...,
 * body: {'code': '', 'message': 'Invalid token ...'}` 这样的 Python dict
 * dump）归类成人话。只做模式识别，不发请求。
 */
export type GatewayErrorKind =
  | "auth_invalid"
  | "timeout"
  | "model_not_found"
  | "rate_limit"
  | "unknown";

export interface ClassifiedGatewayError {
  kind: GatewayErrorKind;
  /** 从原始文本里抠出的 HTTP 状态码（没有时为 null）。 */
  statusCode: number | null;
  /** 从原始文本里抠出的模型名（没有时为 null）。 */
  model: string | null;
}

const STATUS_CODE_PATTERN = /\bstatus_code\s*[:=]\s*(\d{3})\b/i;
const HTTP_STATUS_PATTERN = /\bHTTP\s+(\d{3})\b/i;
const MODEL_NAME_PATTERN = /\bmodel_name\s*[:=]\s*['"]?([\w:./-]+)['"]?/i;

export function classifyGatewayError(
  raw: string | null | undefined,
): ClassifiedGatewayError {
  const text = raw ?? "";
  const statusMatch =
    STATUS_CODE_PATTERN.exec(text) ?? HTTP_STATUS_PATTERN.exec(text);
  const statusCode = statusMatch ? Number(statusMatch[1]) : null;
  const modelMatch = MODEL_NAME_PATTERN.exec(text);
  const model = modelMatch ? modelMatch[1] : null;
  const lowered = text.toLowerCase();

  if (
    statusCode === 401 ||
    statusCode === 403 ||
    /invalid token|invalid api key|unauthorized|authentication failed|api key.*invalid|invalid.*api key/.test(
      lowered,
    )
  ) {
    return { kind: "auth_invalid", statusCode, model };
  }
  if (
    statusCode === 524 ||
    statusCode === 504 ||
    statusCode === 408 ||
    /timed out|timeout|request timed out|网关超时/.test(lowered)
  ) {
    return { kind: "timeout", statusCode, model };
  }
  if (
    /model_not_found|model not found|does not exist|unknown model|no such model/.test(
      lowered,
    )
  ) {
    return { kind: "model_not_found", statusCode, model };
  }
  if (statusCode === 429 || /\brate[\s-]?limit/.test(lowered)) {
    return { kind: "rate_limit", statusCode, model };
  }
  return { kind: "unknown", statusCode, model };
}

/**
 * 按错误码翻成一句人话 + 一个动作。无法归类时返回 null，调用方保留原文。
 */
export function gatewayErrorHumanMessage(
  raw: string | null | undefined,
  t: TFunction,
): string | null {
  const { kind } = classifyGatewayError(raw);
  switch (kind) {
    case "auth_invalid":
      return t("common.gatewayErrors.authInvalid", {
        defaultValue:
          "网关返回 401：API Key 无效，请在 设置 → 模型与渠道 中检查或更换 Key。",
      });
    case "timeout":
      return t("common.gatewayErrors.timeout", {
        defaultValue: "网关超时，请稍后重试或更换模型。",
      });
    case "model_not_found":
      return t("common.gatewayErrors.modelNotFound", {
        defaultValue: "网关没有该模型，请在设置中更换文本模型。",
      });
    case "rate_limit":
      return t("common.generationRateLimited", {
        defaultValue: "模型服务暂时限流（HTTP 429），请稍后重试。",
      });
    default:
      return null;
  }
}
