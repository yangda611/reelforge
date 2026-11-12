// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { describe, expect, it } from "vitest";

import {
  classifyGatewayError,
  gatewayErrorHumanMessage,
} from "@/lib/gateway-error";
import type { TFunction } from "i18next";

// P1-1：网关原始错误文本（常是 Python dict dump）要翻成一句人话 + 动作，
// 不能把 raw dump 当主文案。t 用桩：返回 key，方便断言归类结果。
const t = ((key: string) => key) as unknown as TFunction;

describe("classifyGatewayError", () => {
  it("classifies a 401 Python-dict dump as auth_invalid and extracts status/model", () => {
    const raw =
      "status_code: 401, model_name: gpt-6-astra, body: {'code': '', 'message': 'Invalid token'}";
    expect(classifyGatewayError(raw)).toEqual({
      kind: "auth_invalid",
      statusCode: 401,
      model: "gpt-6-astra",
    });
  });

  it("classifies 524/504 timeouts as timeout", () => {
    expect(classifyGatewayError("status_code: 524, body: timeout").kind).toBe("timeout");
    expect(classifyGatewayError("HTTP 504 Gateway Time-out").kind).toBe("timeout");
    expect(classifyGatewayError("request timed out after 30s").kind).toBe("timeout");
  });

  it("classifies model_not_found only on the model_not_found text, not a bare 503", () => {
    expect(
      classifyGatewayError("status_code: 503, body: {'error': {'code': 'model_not_found'}}").kind,
    ).toBe("model_not_found");
    // 裸 503 可能是上游整体不可用，不能断言成「没有该模型」。
    expect(classifyGatewayError("status_code: 503, body: service unavailable").kind).toBe(
      "unknown",
    );
  });

  it("classifies 429 as rate_limit and unknown text as unknown", () => {
    expect(classifyGatewayError("status_code: 429, body: too many requests").kind).toBe(
      "rate_limit",
    );
    expect(classifyGatewayError("something went wrong").kind).toBe("unknown");
    expect(classifyGatewayError(null).kind).toBe("unknown");
    expect(classifyGatewayError("").kind).toBe("unknown");
  });
});

describe("gatewayErrorHumanMessage", () => {
  it("returns the human sentence key per kind and null when unknown", () => {
    expect(gatewayErrorHumanMessage("status_code: 401", t)).toBe(
      "common.gatewayErrors.authInvalid",
    );
    expect(gatewayErrorHumanMessage("status_code: 524", t)).toBe(
      "common.gatewayErrors.timeout",
    );
    expect(gatewayErrorHumanMessage("model_not_found", t)).toBe(
      "common.gatewayErrors.modelNotFound",
    );
    expect(gatewayErrorHumanMessage("unrelated failure", t)).toBeNull();
  });
});
