// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { useQuery } from "@tanstack/react-query";

import { api } from "@/lib/api";
import { queryKeys } from "@/lib/query-keys";
import type { OkResponse } from "@/types/api";

/**
 * 网关连接自检（P0-1）：对已保存的网关做一次轻量鉴权探测。
 *
 * 后端 `GET /api/v1/model-gateway/probe` 由并行后端任务按以下契约提供：
 * `{ ok: true, data: { status: "ok" | "auth_invalid" | "unreachable" | "unconfigured", model, message } }`。
 *
 * 端点尚不存在（404）或返回异常时返回 null —— 调用方一律按「未知」处理，
 * 不弹横幅、不阻塞界面、不抛错，等后端上线后自动生效。
 */
export type GatewayProbeStatus =
  | "ok"
  | "auth_invalid"
  | "unreachable"
  | "unconfigured";

export interface GatewayProbeResult {
  status: GatewayProbeStatus;
  model: string | null;
  message: string | null;
}

async function fetchGatewayProbe(signal?: AbortSignal): Promise<GatewayProbeResult | null> {
  try {
    const response = await api
      .get("api/v1/model-gateway/probe", { signal, throwHttpErrors: false })
      .json<OkResponse<GatewayProbeResult> | { detail?: unknown }>()
      .catch(() => null);
    if (!response || typeof response !== "object") return null;
    if (!("ok" in response) || response.ok !== true) return null;
    const data = (response as OkResponse<GatewayProbeResult>).data;
    if (!data || typeof data !== "object") return null;
    const status = (data as GatewayProbeResult).status;
    if (
      status !== "ok" &&
      status !== "auth_invalid" &&
      status !== "unreachable" &&
      status !== "unconfigured"
    ) {
      return null;
    }
    return {
      status,
      model: typeof data.model === "string" ? data.model : null,
      message: typeof data.message === "string" ? data.message : null,
    };
  } catch {
    return null;
  }
}

export const GATEWAY_PROBE_QUERY_KEY = [
  ...queryKeys.modelGateway(),
  "probe",
] as const;

/**
 * 探测已保存的网关。`enabled` 为 false 时不发请求；调用方用它控制
 * 「应用启动时」和「设置页打开时」两次探测。失败/未上线时 data 为 null。
 */
export function useGatewayProbe(enabled = true) {
  return useQuery({
    queryKey: GATEWAY_PROBE_QUERY_KEY,
    queryFn: ({ signal }) => fetchGatewayProbe(signal),
    enabled,
    retry: false,
    staleTime: 60_000,
    refetchOnWindowFocus: false,
  });
}
