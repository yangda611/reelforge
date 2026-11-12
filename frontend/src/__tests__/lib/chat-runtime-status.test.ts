// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { beforeEach, describe, expect, it } from "vitest";

import {
  clearRuntimeMissing,
  isRuntimeMissingText,
  markRuntimeMissing,
  readRuntimeMissing,
  translateRuntimeMissingMessageText,
} from "@/lib/chat-runtime-status";

// P1-5：虾导运行环境缺失的识别、记忆与文案纠正。
describe("isRuntimeMissingText", () => {
  it("matches the backend runtime-missing signals", () => {
    expect(isRuntimeMissingText("error_code: runtime_missing")).toBe(true);
    expect(
      isRuntimeMissingText("本轮处理失败：本地助手运行时不可用：请重启应用；若仍失败，请更新应用并导出诊断日志。"),
    ).toBe(true);
    expect(isRuntimeMissingText("CodexRuntimeError: fork failed")).toBe(true);
    expect(isRuntimeMissingText("HermesForkMissing")).toBe(true);
  });

  it("does not match unrelated errors", () => {
    expect(isRuntimeMissingText("status_code: 401, Invalid token")).toBe(false);
    expect(isRuntimeMissingText("")).toBe(false);
    expect(isRuntimeMissingText(null)).toBe(false);
    expect(isRuntimeMissingText(undefined)).toBe(false);
  });
});

describe("runtime missing persistence", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it("remembers the missing state across opens until cleared", () => {
    expect(readRuntimeMissing()).toBe(false);
    markRuntimeMissing();
    expect(readRuntimeMissing()).toBe(true);
    clearRuntimeMissing();
    expect(readRuntimeMissing()).toBe(false);
  });
});

describe("translateRuntimeMissingMessageText", () => {
  it("replaces the misleading restart/update guidance with the real cause", () => {
    const raw =
      "本轮处理失败：本地助手运行时不可用：请重启应用；若仍失败，请更新应用并导出诊断日志。";
    const translated = translateRuntimeMissingMessageText(raw);
    expect(translated).toContain("未检测到本地助手运行环境（Codex），请在设置中下载/安装");
    expect(translated).not.toContain("请重启应用");
    expect(translated).not.toContain("更新应用");
  });

  it("leaves unrelated text untouched", () => {
    expect(translateRuntimeMissingMessageText("生成成功，无失败。")).toBe("生成成功，无失败。");
  });
});
