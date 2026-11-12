// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { GatewayProbeBanner } from "@/components/gateway/gateway-probe-banner";
import type { GatewayProbeResult } from "@/lib/queries/gateway-probe";

// P0-1：网关连接自检横幅。探测端点尚未上线（data 为 null）时不显示、不阻塞；
// auth_invalid 弹红横幅 + 直达设置按钮；unreachable 弹较 soft 的警告。
const probeState = vi.hoisted(() => ({
  data: null as GatewayProbeResult | null,
}));
const settingsPage = vi.hoisted(() => ({ current: undefined as string | undefined }));

vi.mock("@/lib/queries/gateway-probe", () => ({
  useGatewayProbe: () => ({ data: probeState.data }),
}));

vi.mock("@/lib/runtime-config", () => ({
  isCeRuntime: () => true,
}));

vi.mock("@/components/settings/settings-dialog-events", () => ({
  openSettingsDialog: (page: string) => {
    settingsPage.current = page;
  },
}));

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string, opts?: { defaultValue?: string }) =>
      ({
        "project.gatewayProbeAuthInvalid": "网关 Key 已失效，请前往 设置 → 模型与渠道 更换",
        "project.gatewayProbeAuthInvalidAction": "去设置更换",
        "project.gatewayProbeUnreachable": "网关暂时无法连接，请检查网络后重试。",
        "common.close": "关闭",
      })[key] ?? opts?.defaultValue ?? key,
    i18n: { language: "zh", resolvedLanguage: "zh" },
  }),
}));

vi.mock("@/components/ui/button", () => ({
  Button: ({ children, ...props }: React.ComponentProps<"button">) => (
    <button type="button" {...props}>
      {children}
    </button>
  ),
}));

describe("GatewayProbeBanner", () => {
  beforeEach(() => {
    probeState.data = null;
    settingsPage.current = undefined;
  });

  it("renders nothing when the probe endpoint is not live yet", () => {
    render(<GatewayProbeBanner />);
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("renders nothing when the gateway is ok or unconfigured", () => {
    for (const status of ["ok", "unconfigured"] as const) {
      probeState.data = { status, model: null, message: null };
      const { unmount } = render(<GatewayProbeBanner />);
      expect(screen.queryByRole("alert")).toBeNull();
      unmount();
    }
  });

  it("shows a red banner with a settings button on auth_invalid", async () => {
    probeState.data = { status: "auth_invalid", model: "gpt-6-astra", message: "Key 无效" };
    const user = userEvent.setup();
    render(<GatewayProbeBanner />);

    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("网关 Key 已失效");
    expect(alert).toHaveTextContent("gpt-6-astra");
    await user.click(screen.getByRole("button", { name: "去设置更换" }));
    expect(settingsPage.current).toBe("models");
  });

  it("shows a softer warning without a settings button on unreachable", () => {
    probeState.data = { status: "unreachable", model: null, message: "连接超时" };
    render(<GatewayProbeBanner />);

    expect(screen.getByRole("alert")).toHaveTextContent("网关暂时无法连接");
    expect(screen.queryByRole("button", { name: "去设置更换" })).toBeNull();
  });

  it("can be dismissed", async () => {
    probeState.data = { status: "auth_invalid", model: null, message: null };
    const user = userEvent.setup();
    render(<GatewayProbeBanner />);

    await user.click(screen.getByRole("button", { name: "关闭" }));
    expect(screen.queryByRole("alert")).toBeNull();
  });
});
