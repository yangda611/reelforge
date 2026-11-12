// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { useGatewayProbe } from "@/lib/queries/gateway-probe";
import { isCeRuntime } from "@/lib/runtime-config";
import { openSettingsDialog } from "@/components/settings/settings-dialog-events";
import { AlertTriangle, X } from "lucide-react";
import { useTranslation } from "react-i18next";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

/**
 * 网关连接自检横幅（P0-1）：应用启动时（首页挂载）探测一次已保存的网关。
 *
 * - `auth_invalid`：红色横幅 + 直达「设置 → 模型与渠道」的按钮。
 * - `unreachable`：较 soft 的琥珀色警告横幅。
 * - `ok` / `unconfigured`：不显示（未配置由既有的首跑引导覆盖）。
 * - 探测端点尚未上线或请求失败：不显示、不阻塞界面。
 */
export function GatewayProbeBanner() {
  const { t } = useTranslation();
  const [dismissed, setDismissed] = useState(false);
  const probe = useGatewayProbe(isCeRuntime());
  const status = probe.data?.status ?? null;

  if (dismissed || !status || status === "ok" || status === "unconfigured") {
    return null;
  }

  const isAuthInvalid = status === "auth_invalid";

  return (
    <div
      role="alert"
      className={cn(
        "mb-6 flex flex-wrap items-center gap-3 rounded-md border px-3 py-2.5",
        isAuthInvalid
          ? "border-destructive/50 bg-destructive/10"
          : "border-amber-500/35 bg-amber-500/10",
      )}
    >
      <AlertTriangle
        className={cn(
          "size-4 shrink-0",
          isAuthInvalid ? "text-destructive" : "text-amber-300",
        )}
        aria-hidden
      />
      <div className="min-w-0 flex-1">
        <p
          className={cn(
            "text-xs font-medium",
            isAuthInvalid ? "text-destructive" : "text-amber-100",
          )}
        >
          {isAuthInvalid
            ? t("project.gatewayProbeAuthInvalid", {
                defaultValue:
                  "网关 Key 已失效，请前往 设置 → 模型与渠道 更换",
              })
            : t("project.gatewayProbeUnreachable", {
                defaultValue: "网关暂时无法连接，请检查网络后重试。",
              })}
        </p>
        {probe.data?.model ? (
          <p className="mt-0.5 text-[11px] leading-relaxed text-muted-foreground">
            {probe.data.model}
          </p>
        ) : null}
      </div>
      {isAuthInvalid ? (
        <Button
          type="button"
          size="sm"
          variant="outline"
          onClick={() => openSettingsDialog("models")}
        >
          {t("project.gatewayProbeAuthInvalidAction", {
            defaultValue: "去设置更换",
          })}
        </Button>
      ) : null}
      <button
        type="button"
        aria-label={t("common.close", { defaultValue: "关闭" })}
        className="flex size-6 shrink-0 items-center justify-center rounded-full text-muted-foreground transition-colors hover:bg-white/10 hover:text-foreground"
        onClick={() => setDismissed(true)}
      >
        <X className="size-3.5" aria-hidden />
      </button>
    </div>
  );
}
