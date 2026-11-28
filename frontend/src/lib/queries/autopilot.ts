// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { useEffect, useRef } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { jsonWithBackendError } from "@/lib/api-errors";
import { p } from "@/lib/api-path";
import { queryKeys } from "@/lib/query-keys";
import type { ErrorResponse, OkResponse } from "@/types/api";

export type AutopilotStatus =
  | "phase1_running"
  | "awaiting_asset_review"
  | "phase2_running"
  | "paused"
  | "completed"
  | "failed"
  | "cancelled";

export interface AutopilotOptions {
  mode: "two_stage" | "full_auto";
  video_backend?: string | null;
  resolution?: string | null;
  ratio?: string | null;
  tail_anchor: boolean;
  max_segment_seconds: number;
  max_retries: number;
  max_concurrent_videos: number;
  max_concurrent_assets: number;
  add_subtitles: boolean;
  add_bgm: boolean;
  use_director_render: boolean;
  force_regenerate_videos: boolean;
  target_beat_numbers?: number[];
  skip_compose?: boolean;
}

export interface AutopilotAssetCount {
  ready: number;
  total: number;
}

export interface AutopilotAssetSummary {
  portraits: AutopilotAssetCount;
  identities: AutopilotAssetCount;
  scenes: AutopilotAssetCount;
  props: AutopilotAssetCount;
  failed_count: number;
}

export interface AutopilotFailedItem {
  kind: string;
  name?: string;
  label?: string;
  beat_number?: number;
  error: string;
  category?: string;
  retryable?: boolean;
}

export interface AutopilotProgress {
  message?: string;
  asset_summary?: AutopilotAssetSummary;
  failed_items?: AutopilotFailedItem[];
  video_progress?: {
    completed: number;
    total: number;
    failed: number;
    tail_anchor: boolean;
  };
  final_video_path?: string;
}

export interface AutopilotRun {
  run_id: string;
  project_id: string;
  episode: number;
  status: AutopilotStatus;
  phase: "phase1" | "review" | "phase2" | "done";
  current_step: string;
  mode: "two_stage" | "full_auto";
  options: AutopilotOptions;
  progress: AutopilotProgress;
  last_error: string;
  created_at: string;
  updated_at: string;
}

export interface AutopilotRunsPayload {
  project: string;
  runs: AutopilotRun[];
  summary: {
    total: number;
    running: number;
    awaiting_asset_review: number;
    paused: number;
    completed: number;
  };
}

export interface AutopilotStartPayload {
  mode?: "two_stage" | "full_auto";
  start_phase?: "phase1" | "phase2";
  video_backend?: string;
  resolution?: string;
  ratio?: string;
  tail_anchor?: boolean;
  max_segment_seconds?: number;
  max_retries?: number;
  max_concurrent_videos?: number;
  add_subtitles?: boolean;
  add_bgm?: boolean;
}

export interface AutopilotContinuePayload {
  mode?: "two_stage" | "full_auto";
  video_backend?: string;
  resolution?: string;
  ratio?: string;
  tail_anchor?: boolean;
  max_segment_seconds?: number;
  add_subtitles?: boolean;
  add_bgm?: boolean;
}

export interface BatchEpisodeVideoPayload {
  beat_numbers?: number[];
  only_missing?: boolean;
  resolution?: string;
  ratio?: string;
  video_backend?: string;
  use_director_render?: boolean;
  tail_anchor?: boolean;
}

export interface BatchEpisodeVideoData {
  episode: number;
  tail_anchor_chain?: boolean;
  autopilot_run?: AutopilotRun;
  target_beats?: number[];
  enqueued?: Array<{ beat_number: number; task_id?: string; task_key?: string }>;
  skipped_beats?: number[];
  deferred_beats?: number[];
  errors?: Array<{ beat_number: number; error: string }>;
}

export function isAutopilotActive(status?: AutopilotStatus | null): boolean {
  return status === "phase1_running" || status === "phase2_running";
}

export function useProjectAutopilotRuns(project: string, enabled = true) {
  return useQuery({
    queryKey: queryKeys.autopilotRuns(project),
    queryFn: ({ signal }) =>
      api
        .get(p`api/v1/projects/${project}/autopilot/runs`, { signal })
        .json<OkResponse<AutopilotRunsPayload>>(),
    enabled: !!project && enabled,
    refetchInterval: (query) => {
      const runs = query.state.data?.data?.runs ?? [];
      return runs.some((r) => isAutopilotActive(r.status)) ? 3000 : false;
    },
  });
}

export function useEpisodeAutopilot(project: string, episode: number, enabled = true) {
  const qc = useQueryClient();
  const prevStatusRef = useRef<AutopilotStatus | null>(null);

  const query = useQuery({
    queryKey: queryKeys.autopilotEpisode(project, episode),
    queryFn: ({ signal }) =>
      api
        .get(p`api/v1/projects/${project}/autopilot/episodes/${episode}`, { signal })
        .json<OkResponse<AutopilotRun | null>>(),
    enabled: !!project && episode > 0 && enabled,
    refetchInterval: (q) => {
      const status = q.state.data?.data?.status;
      return isAutopilotActive(status) ? 2500 : false;
    },
  });

  const currentStatus = query.data?.data?.status ?? null;
  useEffect(() => {
    const prev = prevStatusRef.current;
    prevStatusRef.current = currentStatus;
    if (!prev || prev === currentStatus) return;
    if (
      currentStatus === "awaiting_asset_review" ||
      currentStatus === "completed" ||
      currentStatus === "paused"
    ) {
      qc.invalidateQueries({ queryKey: queryKeys.autopilotRuns(project) });
      qc.invalidateQueries({ queryKey: queryKeys.episodes(project) });
      qc.invalidateQueries({ queryKey: queryKeys.episodeDetail(project, episode) });
      qc.invalidateQueries({ queryKey: queryKeys.characters(project) });
      qc.invalidateQueries({ queryKey: queryKeys.scenes(project) });
      qc.invalidateQueries({ queryKey: queryKeys.props(project) });
      qc.invalidateQueries({ queryKey: queryKeys.beats(project, episode) });
      qc.invalidateQueries({ queryKey: queryKeys.script(project, episode) });
      qc.invalidateQueries({ queryKey: queryKeys.finalVideo(project, episode) });
      qc.invalidateQueries({ queryKey: queryKeys.pipelineStatus(project) });
    }
  }, [currentStatus, episode, project, qc]);

  return query;
}

export function useStartEpisodeAutopilot(project: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      episode,
      body,
    }: {
      episode: number;
      body?: AutopilotStartPayload;
    }) =>
      jsonWithBackendError<OkResponse<AutopilotRun> | ErrorResponse>(
        api.post(p`api/v1/projects/${project}/autopilot/episodes/${episode}/start`, {
          json: body ?? { mode: "two_stage" },
          throwHttpErrors: false,
        }),
      ),
    onSuccess: (_res, vars) => {
      qc.invalidateQueries({ queryKey: queryKeys.autopilotRuns(project) });
      qc.invalidateQueries({ queryKey: queryKeys.autopilotEpisode(project, vars.episode) });
    },
  });
}

export function useBatchStartAutopilot(project: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: AutopilotStartPayload & { episodes: number[] }) =>
      jsonWithBackendError<
        OkResponse<{ runs: AutopilotRun[]; count: number }> | ErrorResponse
      >(
        api.post(p`api/v1/projects/${project}/autopilot/batch-start`, {
          json: body,
          throwHttpErrors: false,
        }),
      ),
    onSuccess: (_res, vars) => {
      qc.invalidateQueries({ queryKey: queryKeys.autopilotRuns(project) });
      for (const ep of vars.episodes) {
        qc.invalidateQueries({ queryKey: queryKeys.autopilotEpisode(project, ep) });
      }
    },
  });
}

export function useContinueEpisodeAutopilot(project: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      episode,
      body,
    }: {
      episode: number;
      body?: AutopilotContinuePayload;
    }) =>
      jsonWithBackendError<OkResponse<AutopilotRun> | ErrorResponse>(
        api.post(p`api/v1/projects/${project}/autopilot/episodes/${episode}/continue`, {
          json: body ?? {},
          throwHttpErrors: false,
        }),
      ),
    onSuccess: (_res, vars) => {
      qc.invalidateQueries({ queryKey: queryKeys.autopilotRuns(project) });
      qc.invalidateQueries({ queryKey: queryKeys.autopilotEpisode(project, vars.episode) });
    },
  });
}

export function usePauseEpisodeAutopilot(project: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (episode: number) =>
      jsonWithBackendError<OkResponse<AutopilotRun> | ErrorResponse>(
        api.post(p`api/v1/projects/${project}/autopilot/episodes/${episode}/pause`, {
          throwHttpErrors: false,
        }),
      ),
    onSuccess: (_res, episode) => {
      qc.invalidateQueries({ queryKey: queryKeys.autopilotRuns(project) });
      qc.invalidateQueries({ queryKey: queryKeys.autopilotEpisode(project, episode) });
    },
  });
}

export function useCancelEpisodeAutopilot(project: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      episode,
      cancelEpisodeTasks = true,
    }: {
      episode: number;
      cancelEpisodeTasks?: boolean;
    }) =>
      jsonWithBackendError<OkResponse<AutopilotRun> | ErrorResponse>(
        api.post(p`api/v1/projects/${project}/autopilot/episodes/${episode}/cancel`, {
          json: { cancel_episode_tasks: cancelEpisodeTasks },
          throwHttpErrors: false,
        }),
      ),
    onSuccess: (_res, vars) => {
      qc.invalidateQueries({ queryKey: queryKeys.autopilotRuns(project) });
      qc.invalidateQueries({ queryKey: queryKeys.autopilotEpisode(project, vars.episode) });
    },
  });
}

export function useBatchGenerateEpisodeVideos(project: string, episode: number) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: BatchEpisodeVideoPayload) =>
      jsonWithBackendError<OkResponse<BatchEpisodeVideoData> | ErrorResponse>(
        api.post(
          p`api/v1/projects/${project}/episodes/${episode}/videos/batch-generate`,
          {
            json: body,
            throwHttpErrors: false,
          },
        ),
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: queryKeys.beats(project, episode) });
      qc.invalidateQueries({ queryKey: queryKeys.autopilotRuns(project) });
      qc.invalidateQueries({ queryKey: queryKeys.autopilotEpisode(project, episode) });
      qc.invalidateQueries({ queryKey: queryKeys.pipelineStatus(project) });
    },
  });
}
