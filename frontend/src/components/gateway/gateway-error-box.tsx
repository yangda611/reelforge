// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { openSettingsDialog } from "@/components/settings/settings-dialog-events";
import { classifyGatewayError } from "@/lib/gateway-error";

/**
 * 网关错误的人话呈现（P1-1）：一句人话 + 一个动作，原始详情收进可折叠的
 * 「查看详情」。原始 Python-dict dump 永远不做主文案。
 *
 * `raw` 是后端/任务返回的原始错误文本；`human` 是调用方已经翻好的人话
 * （如 taskErrorMessage），不传时按 `raw` 现场归类，归类失败则原样显示。
 */
export function GatewayErrorBox({
  raw,
  human,
}: {
  raw: string | null | undefined;
  human?: string | null;
}) {
  const { t } = useTranslation();
  const classified = classifyGatewayError(raw);
  const message = human ?? raw ?? "";
  const showDetails = Boolean(raw && raw.trim() && raw.trim() !== message.trim());

  return (
    <div>
      <div>{message}</div>
      <div className="mt-2 flex flex-wrap items-center gap-2">
        <Button
          type="button"
          size="sm"
          variant="outline"
          onClick={() => openSettingsDialog("models")}
        >
          {t("common.gatewayErrors.goToSettings", { defaultValue: "去设置" })}
        </Button>
        {showDetails ? (
          <details className="text-[11px]">
            <summary className="cursor-pointer text-muted-foreground underline-offset-2 hover:underline">
              {t("common.gatewayErrors.viewDetails", { defaultValue: "查看详情" })}
            </summary>
            <div className="mt-1 max-h-40 overflow-auto rounded bg-muted p-2 font-mono text-[11px] break-words whitespace-pre-wrap">
              {classified.statusCode != null ? `status_code: ${classified.statusCode}\n` : null}
              {classified.model ? `model_name: ${classified.model}\n` : null}
              {raw}
            </div>
          </details>
        ) : null}
      </div>
    </div>
  );
}
