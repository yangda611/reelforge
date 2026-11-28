// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { describe, it, expect } from "vitest";
import { taskIoLabel } from "@/lib/task-io-label";
import { zhT, enT } from "../helpers/i18n-fixtures";

const zh = zhT as unknown as (k: string, o?: Record<string, unknown>) => string;
const en = enT as unknown as (k: string, o?: Record<string, unknown>) => string;

describe("taskIoLabel", () => {
  it("localizes freezone source/target labels in English", () => {
    expect(taskIoLabel("导演合成图", en)).toBe("Director composite");
    expect(taskIoLabel("当前分镜", en)).toBe("Current storyboard");
    expect(taskIoLabel("360 全景", en)).toBe("360° panorama");
  });

  it("renders localized labels in Chinese", () => {
    expect(taskIoLabel("Beat 上下文", zh)).toBe("Clip 上下文");
    for (const raw of [
      "Master + Reverse",
      "场景 Master + Reverse",
      "导演合成图",
      "背景",
      "当前背景",
      "背景候选",
      "输入参考",
      "360 全景",
      "当前分镜",
      "分镜候选",
    ]) {
      expect(taskIoLabel(raw, zh)).toBe(raw);
    }
  });

  it("passes unknown labels through untouched", () => {
    expect(taskIoLabel("某个新标签", en)).toBe("某个新标签");
    expect(taskIoLabel("", en)).toBe("");
    expect(taskIoLabel(null, en)).toBe("");
    expect(taskIoLabel(undefined, en)).toBe("");
  });
});
