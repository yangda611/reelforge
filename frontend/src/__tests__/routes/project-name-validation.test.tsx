// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { describe, expect, it } from "vitest";

import {
  PROJECT_NAME_MAX_LENGTH,
  getProjectNameValidationKey,
} from "@/routes/_app/index";

describe("项目名称长度校验", () => {
  it("接受 64 字符名称", () => {
    expect(PROJECT_NAME_MAX_LENGTH).toBe(64);
    expect(getProjectNameValidationKey("a".repeat(64))).toBeNull();
  });

  it("拒绝 65 字符名称", () => {
    expect(getProjectNameValidationKey("a".repeat(65))).toBe("project.nameTooLong");
  });
});

// P1-4：项目名允许中文等 Unicode，不再只接受 ASCII。
describe("项目名称 Unicode 校验", () => {
  it.each(["我的短剧", "小说改编 · 第一集", "My Drama 我的短剧", "短剧🎬"])(
    "接受 %s",
    (name) => {
      expect(getProjectNameValidationKey(name)).toBeNull();
    },
  );

  it("空名不在本函数里拦截（表单靠禁用确认按钮拦空名）", () => {
    expect(getProjectNameValidationKey("")).toBeNull();
  });

  it("拒绝控制字符", () => {
    expect(getProjectNameValidationKey("短剧\u0001")).toBe("project.nameInvalid");
  });
});
