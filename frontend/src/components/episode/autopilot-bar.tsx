// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { useMemo, useState, type SyntheticEvent } from "react";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";
import {
  AlertTriangle,
  CheckCircle2,
  Link2,
  Loader2,
  Pause,
  Play,
  RotateCcw,
  Sparkles,
  Square,
} from "lucide-react";

import {
  isAutopilotActive,
  useBatchStartAutopilot,
  useCancelEpisodeAutopilot,
  useContinueEpisodeAutopilot,
  useEpisodeAutopilot,
  usePauseEpisodeAutopilot,
  useStartEpisodeAutopilot,
  type AutopilotRun,
} from "@/lib/queries/autopilot";
import { backendErrorToastMessage } from "@/lib/api-errors";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import type { Episode } from "@/types/episode";

const KNOWN_STEPS = new Set([
  "project_bootstrap",
  "asset_planning",
  "asset_images",
  "asset_review",
  "script_generation",
  "prompt_generation",
  "video_generation",
  "compose",
  "done",
]);

function useStepLabel() {
  const { t } = useTranslation();
  return (step?: string | null): string => {
    if (!step) return "";
    return KNOWN_STEPS.has(step)
      ? t(`episode.autopilot.steps.${step}`, { defaultValue: step })
      : step;
  };
}

// ─── Episode Detail Header Bar ──────────────────────────────────────────────

export function EpisodeAutopilotBar({
  project,
  episode,
}: {
  project: string;
  episode: number;
}) {
  const { t } = useTranslation();
  const stepLabel = useStepLabel();
  const { data: runRes } = useEpisodeAutopilot(project, episode);
  const run = runRes?.data ?? null;

  const startAutopilot = useStartEpisodeAutopilot(project);
  const continueAutopilot = useContinueEpisodeAutopilot(project);
  const pauseAutopilot = usePauseEpisodeAutopilot(project);
  const cancelAutopilot = useCancelEpisodeAutopilot(project);

  const [tailAnchorOverride, setTailAnchorOverride] = useState<boolean | null>(
    null,
  );
  const effectiveTailAnchor =
    tailAnchorOverride ?? Boolean(run?.options?.tail_anchor);
  const toggleTailAnchor = () => setTailAnchorOverride(!effectiveTailAnchor);

  const busy =
    startAutopilot.isPending ||
    continueAutopilot.isPending ||
    pauseAutopilot.isPending ||
    cancelAutopilot.isPending;

  const handleStart = async () => {
    try {
      const res = await startAutopilot.mutateAsync({
        episode,
        body: {
          mode: "two_stage",
          start_phase: "phase1",
          tail_anchor: effectiveTailAnchor,
        },
      });
      if (res.ok === false) {
        toast.error(backendErrorToastMessage(res.error, t));
        return;
      }
      toast.success(t("episode.autopilot.toastStartedPhase1", { episode }));
    } catch (err) {
      toast.error(backendErrorToastMessage(err, t));
    }
  };

  const handleContinue = async () => {
    try {
      const res = await continueAutopilot.mutateAsync({
        episode,
        body: { tail_anchor: effectiveTailAnchor },
      });
      if (res.ok === false) {
        toast.error(backendErrorToastMessage(res.error, t));
        return;
      }
      toast.success(
        run?.status === "awaiting_asset_review"
          ? t("episode.autopilot.toastEnteredPhase2", { episode })
          : t("episode.autopilot.toastResumed", { episode }),
      );
    } catch (err) {
      toast.error(backendErrorToastMessage(err, t));
    }
  };

  const handlePause = async () => {
    try {
      const res = await pauseAutopilot.mutateAsync(episode);
      if (res.ok === false) {
        toast.error(backendErrorToastMessage(res.error, t));
        return;
      }
      toast.info(t("episode.autopilot.toastPaused", { episode }));
    } catch (err) {
      toast.error(backendErrorToastMessage(err, t));
    }
  };

  const handleCancel = async () => {
    try {
      const res = await cancelAutopilot.mutateAsync({
        episode,
        cancelEpisodeTasks: true,
      });
      if (res.ok === false) {
        toast.error(backendErrorToastMessage(res.error, t));
        return;
      }
      toast.info(t("episode.autopilot.toastCancelled", { episode }));
    } catch (err) {
      toast.error(backendErrorToastMessage(err, t));
    }
  };

  if (!run || run.status === "cancelled") {
    return (
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border/30 bg-white/[0.02] px-9 py-2 text-xs">
        <div className="flex items-center gap-2 text-muted-foreground">
          <Sparkles className="size-3.5 text-primary" />
          <span>{t("episode.autopilot.idleDesc")}</span>
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={toggleTailAnchor}
            className={cn(
              "inline-flex h-7 items-center gap-1 rounded-[7px] border px-2 text-[11px] transition-colors",
              effectiveTailAnchor
                ? "border-primary/45 bg-primary/12 text-primary"
                : "border-white/10 bg-transparent text-muted-foreground hover:text-foreground",
            )}
            title={t("episode.autopilot.tailAnchorTooltip")}
          >
            <Link2 className="size-3" />
            {t("episode.autopilot.tailAnchor")}
          </button>
          <Button
            type="button"
            size="sm"
            onClick={handleStart}
            disabled={busy}
            className="h-7 gap-1.5 rounded-[7px] bg-primary px-3 text-[11px] font-medium text-primary-foreground shadow-none hover:bg-primary/85"
          >
            {startAutopilot.isPending ? (
              <Loader2 className="size-3 animate-spin" />
            ) : (
              <Play className="size-3" />
            )}
            {t("episode.autopilot.startEpisode")}
          </Button>
        </div>
      </div>
    );
  }

  const active = isAutopilotActive(run.status);
  const review = run.status === "awaiting_asset_review";
  const paused = run.status === "paused" || run.status === "failed";
  const completed = run.status === "completed";
  const summary = run.progress?.asset_summary;
  const videoProg = run.progress?.video_progress;
  const failedItems = run.progress?.failed_items ?? [];
  const currentStepText = stepLabel(run.current_step);

  return (
    <div
      className={cn(
        "flex flex-wrap items-center justify-between gap-3 border-b px-9 py-2.5 text-xs",
        review
          ? "border-amber-500/30 bg-amber-500/[0.07]"
          : paused
            ? "border-destructive/30 bg-destructive/[0.06]"
            : completed
              ? "border-emerald-500/25 bg-emerald-500/[0.06]"
              : "border-primary/25 bg-primary/[0.06]",
      )}
    >
      <div className="flex min-w-0 flex-1 flex-wrap items-center gap-x-3 gap-y-1">
        {active && (
          <Loader2 className="size-3.5 shrink-0 animate-spin text-primary" />
        )}
        {review && <Sparkles className="size-3.5 shrink-0 text-amber-400" />}
        {paused && (
          <AlertTriangle className="size-3.5 shrink-0 text-destructive" />
        )}
        {completed && (
          <CheckCircle2 className="size-3.5 shrink-0 text-emerald-400" />
        )}

        <span className="font-medium text-foreground">
          {run.status === "phase1_running" &&
            t("episode.autopilot.phase1Label", { step: currentStepText })}
          {review && t("episode.autopilot.reviewLabel")}
          {run.status === "phase2_running" &&
            t("episode.autopilot.phase2Label", { step: currentStepText })}
          {paused &&
            t("episode.autopilot.pausedLabel", { step: currentStepText })}
          {completed && t("episode.autopilot.completedLabel")}
        </span>

        {run.progress?.message && (
          <span className="truncate text-muted-foreground">
            {run.progress.message}
          </span>
        )}

        {summary && (review || paused) && (
          <span className="inline-flex items-center gap-2 rounded-full border border-white/10 bg-black/20 px-2.5 py-0.5 text-[11px] tabular-nums text-foreground/85">
            <span>
              {t("episode.autopilot.summaryPortraits", {
                ready: summary.portraits.ready,
                total: summary.portraits.total,
              })}
            </span>
            <span>
              {t("episode.autopilot.summaryIdentities", {
                ready: summary.identities.ready,
                total: summary.identities.total,
              })}
            </span>
            <span>
              {t("episode.autopilot.summaryScenes", {
                ready: summary.scenes.ready,
                total: summary.scenes.total,
              })}
            </span>
            <span>
              {t("episode.autopilot.summaryProps", {
                ready: summary.props.ready,
                total: summary.props.total,
              })}
            </span>
          </span>
        )}

        {videoProg && run.status === "phase2_running" && (
          <span className="inline-flex items-center gap-1.5 rounded-full border border-primary/30 bg-primary/10 px-2.5 py-0.5 text-[11px] tabular-nums text-primary">
            {t("episode.autopilot.videoProgress", {
              completed: videoProg.completed,
              total: videoProg.total,
            })}
            {videoProg.tail_anchor ? t("episode.autopilot.tailAnchorTag") : ""}
          </span>
        )}

        {failedItems.length > 0 && (
          <span className="inline-flex items-center gap-1 rounded-full border border-destructive/30 bg-destructive/15 px-2 py-0.5 text-[11px] text-destructive">
            {t("episode.autopilot.failedIsolated", {
              count: failedItems.length,
            })}
          </span>
        )}
      </div>

      <div className="flex shrink-0 items-center gap-2">
        {(review || paused || completed) && (
          <button
            type="button"
            onClick={toggleTailAnchor}
            className={cn(
              "inline-flex h-7 items-center gap-1 rounded-[7px] border px-2 text-[11px] transition-colors",
              effectiveTailAnchor
                ? "border-primary/45 bg-primary/12 text-primary"
                : "border-white/10 bg-transparent text-muted-foreground hover:text-foreground",
            )}
            title={t("episode.autopilot.tailAnchorTooltip")}
          >
            <Link2 className="size-3" />
            {t("episode.autopilot.tailAnchor")}
          </button>
        )}

        {review && (
          <Button
            type="button"
            size="sm"
            onClick={handleContinue}
            disabled={busy}
            className="h-7 gap-1.5 rounded-[7px] bg-primary px-3 text-[11px] font-medium text-primary-foreground shadow-none hover:bg-primary/85"
          >
            {continueAutopilot.isPending ? (
              <Loader2 className="size-3 animate-spin" />
            ) : (
              <Play className="size-3" />
            )}
            {t("episode.autopilot.approveContinue")}
          </Button>
        )}

        {paused && (
          <Button
            type="button"
            size="sm"
            onClick={handleContinue}
            disabled={busy}
            className="h-7 gap-1.5 rounded-[7px] bg-primary px-3 text-[11px] font-medium text-primary-foreground shadow-none hover:bg-primary/85"
          >
            {continueAutopilot.isPending ? (
              <Loader2 className="size-3 animate-spin" />
            ) : (
              <RotateCcw className="size-3" />
            )}
            {t("episode.autopilot.resumeContinue")}
          </Button>
        )}

        {completed && (
          <Button
            type="button"
            size="sm"
            variant="outline"
            onClick={handleStart}
            disabled={busy}
            className="h-7 gap-1.5 rounded-[7px] border-white/15 bg-transparent px-2.5 text-[11px] text-foreground shadow-none hover:bg-white/[0.06]"
          >
            <RotateCcw className="size-3" />
            {t("episode.autopilot.rerunAutopilot")}
          </Button>
        )}

        {active && (
          <Button
            type="button"
            size="sm"
            variant="outline"
            onClick={handlePause}
            disabled={busy}
            className="h-7 gap-1 rounded-[7px] border-white/15 bg-transparent px-2.5 text-[11px] text-foreground shadow-none hover:bg-white/[0.06]"
          >
            <Pause className="size-3" />
            {t("episode.autopilot.pause")}
          </Button>
        )}

        {!completed && (
          <Button
            type="button"
            size="sm"
            variant="ghost"
            onClick={handleCancel}
            disabled={busy}
            className="h-7 gap-1 rounded-[7px] px-2 text-[11px] text-muted-foreground hover:bg-destructive/10 hover:text-destructive"
          >
            <Square className="size-3" />
            {t("episode.autopilot.cancel")}
          </Button>
        )}
      </div>
    </div>
  );
}

// ─── Episode Card Inline Control (Episodes Grid) ────────────────────────────

export function EpisodeCardAutopilotControl({
  project,
  episodeNumber,
  run,
}: {
  project: string;
  episodeNumber: number;
  run: AutopilotRun | null;
}) {
  const { t } = useTranslation();
  const stepLabel = useStepLabel();
  const startAutopilot = useStartEpisodeAutopilot(project);
  const continueAutopilot = useContinueEpisodeAutopilot(project);
  const pauseAutopilot = usePauseEpisodeAutopilot(project);

  const busy =
    startAutopilot.isPending ||
    continueAutopilot.isPending ||
    pauseAutopilot.isPending;

  const stopProp = (e: SyntheticEvent) => {
    e.stopPropagation();
  };

  if (!run || run.status === "cancelled") {
    return (
      <div
        className="flex items-center justify-between gap-2 rounded-[8px] border border-white/[0.06] bg-white/[0.02] px-2 py-1"
        onClick={stopProp}
        onMouseDown={stopProp}
        onKeyDown={stopProp}
      >
        <span className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
          <Sparkles className="size-3 text-primary" />
          {t("episode.autopilot.cardLabel")}
        </span>
        <Button
          type="button"
          variant="ghost"
          size="xs"
          disabled={busy}
          onClick={async (e) => {
            e.stopPropagation();
            try {
              const res = await startAutopilot.mutateAsync({
                episode: episodeNumber,
                body: { mode: "two_stage", start_phase: "phase1" },
              });
              if (res.ok === false) {
                toast.error(backendErrorToastMessage(res.error, t));
                return;
              }
              toast.success(
                t("episode.autopilot.cardStartedToast", {
                  episode: episodeNumber,
                }),
              );
            } catch (err) {
              toast.error(backendErrorToastMessage(err, t));
            }
          }}
          className="h-6 gap-1 rounded-[6px] px-2 text-[11px] text-primary hover:bg-primary/15"
        >
          {startAutopilot.isPending ? (
            <Loader2 className="size-3 animate-spin" />
          ) : (
            <Play className="size-3" />
          )}
          {t("episode.autopilot.cardStart")}
        </Button>
      </div>
    );
  }

  const active = isAutopilotActive(run.status);
  const review = run.status === "awaiting_asset_review";
  const paused = run.status === "paused" || run.status === "failed";
  const completed = run.status === "completed";

  return (
    <div
      className={cn(
        "flex items-center justify-between gap-2 rounded-[8px] border px-2 py-1 text-[11px]",
        review
          ? "border-amber-500/35 bg-amber-500/[0.08] text-amber-300"
          : paused
            ? "border-destructive/30 bg-destructive/[0.07] text-destructive"
            : completed
              ? "border-emerald-500/25 bg-emerald-500/[0.07] text-emerald-300"
              : "border-primary/30 bg-primary/[0.08] text-primary",
      )}
      onClick={stopProp}
      onMouseDown={stopProp}
      onKeyDown={stopProp}
    >
      <span className="flex min-w-0 items-center gap-1.5 truncate">
        {active && <Loader2 className="size-3 shrink-0 animate-spin" />}
        {review && <Sparkles className="size-3 shrink-0" />}
        {paused && <AlertTriangle className="size-3 shrink-0" />}
        {completed && <CheckCircle2 className="size-3 shrink-0" />}
        <span className="truncate">
          {active && stepLabel(run.current_step)}
          {review && t("episode.autopilot.cardReviewStatus")}
          {paused && t("episode.autopilot.cardPausedStatus")}
          {completed && t("episode.autopilot.cardDoneStatus")}
        </span>
      </span>

      {active && (
        <Button
          type="button"
          variant="ghost"
          size="xs"
          disabled={busy}
          onClick={async (e) => {
            e.stopPropagation();
            await pauseAutopilot.mutateAsync(episodeNumber);
          }}
          className="h-6 shrink-0 gap-1 rounded-[6px] px-1.5 text-[11px] text-foreground hover:bg-white/10"
        >
          <Pause className="size-3" />
          {t("episode.autopilot.pause")}
        </Button>
      )}

      {(review || paused) && (
        <Button
          type="button"
          variant="ghost"
          size="xs"
          disabled={busy}
          onClick={async (e) => {
            e.stopPropagation();
            try {
              const res = await continueAutopilot.mutateAsync({
                episode: episodeNumber,
              });
              if (res.ok === false) {
                toast.error(backendErrorToastMessage(res.error, t));
                return;
              }
              toast.success(
                review
                  ? t("episode.autopilot.toastEnteredPhase2", {
                      episode: episodeNumber,
                    })
                  : t("episode.autopilot.toastResumed", {
                      episode: episodeNumber,
                    }),
              );
            } catch (err) {
              toast.error(backendErrorToastMessage(err, t));
            }
          }}
          className="h-6 shrink-0 gap-1 rounded-[6px] bg-primary/15 px-2 text-[11px] text-primary hover:bg-primary/25"
        >
          {continueAutopilot.isPending ? (
            <Loader2 className="size-3 animate-spin" />
          ) : (
            <Play className="size-3" />
          )}
          {review
            ? t("episode.autopilot.cardContinue")
            : t("episode.autopilot.cardResume")}
        </Button>
      )}
    </div>
  );
}

// ─── Batch Concurrent Launch Bar (Episodes List Header) ─────────────────────

export function BatchAutopilotBar({
  project,
  episodes,
  runs,
}: {
  project: string;
  episodes: Episode[];
  runs: AutopilotRun[];
}) {
  const { t } = useTranslation();
  const batchStart = useBatchStartAutopilot(project);
  const [tailAnchor, setTailAnchor] = useState(false);

  const runByEp = useMemo(() => {
    const map = new Map<number, AutopilotRun>();
    for (const r of runs) map.set(r.episode, r);
    return map;
  }, [runs]);

  const startableEpisodes = useMemo(
    () =>
      episodes
        .map((ep) => ep.number)
        .filter((num) => {
          const r = runByEp.get(num);
          return (
            !r ||
            r.status === "cancelled" ||
            r.status === "paused" ||
            r.status === "failed"
          );
        }),
    [episodes, runByEp],
  );

  const runningCount = runs.filter((r) => isAutopilotActive(r.status)).length;
  const reviewCount = runs.filter(
    (r) => r.status === "awaiting_asset_review",
  ).length;

  if (episodes.length === 0) return null;

  const handleBatchStart = async () => {
    if (startableEpisodes.length === 0) {
      toast.info(t("episode.autopilot.batchNoStartable"));
      return;
    }
    try {
      const res = await batchStart.mutateAsync({
        episodes: startableEpisodes,
        mode: "two_stage",
        start_phase: "phase1",
        tail_anchor: tailAnchor,
      });
      if (res.ok === false) {
        toast.error(backendErrorToastMessage(res.error, t));
        return;
      }
      toast.success(
        t("episode.autopilot.batchStartedToast", { count: res.data.count }),
      );
    } catch (err) {
      toast.error(backendErrorToastMessage(err, t));
    }
  };

  return (
    <div className="mt-2 flex flex-wrap items-center justify-between gap-2 rounded-[10px] border border-white/[0.07] bg-white/[0.025] px-3.5 py-2 text-xs">
      <div className="flex flex-wrap items-center gap-2.5 text-muted-foreground">
        <span className="inline-flex items-center gap-1.5 font-medium text-foreground">
          <Sparkles className="size-3.5 text-primary" />
          {t("episode.autopilot.batchTitle")}
        </span>
        {runningCount > 0 && (
          <span className="inline-flex items-center gap-1 rounded-full bg-primary/15 px-2 py-0.5 text-[11px] text-primary">
            <Loader2 className="size-3 animate-spin" />
            {t("episode.autopilot.batchRunningCount", { count: runningCount })}
          </span>
        )}
        {reviewCount > 0 && (
          <span className="inline-flex items-center gap-1 rounded-full bg-amber-500/15 px-2 py-0.5 text-[11px] text-amber-300">
            {t("episode.autopilot.batchReviewCount", { count: reviewCount })}
          </span>
        )}
        <span className="hidden text-[11px] text-muted-foreground/80 sm:inline">
          {t("episode.autopilot.batchIsolationHint")}
        </span>
      </div>

      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={() => setTailAnchor((v) => !v)}
          className={cn(
            "inline-flex h-7 items-center gap-1 rounded-[7px] border px-2 text-[11px] transition-colors",
            tailAnchor
              ? "border-primary/45 bg-primary/12 text-primary"
              : "border-white/10 bg-transparent text-muted-foreground hover:text-foreground",
          )}
          title={t("episode.autopilot.tailAnchorTooltip")}
        >
          <Link2 className="size-3" />
          {t("episode.autopilot.tailAnchor")}
        </button>
        <Button
          type="button"
          size="sm"
          onClick={handleBatchStart}
          disabled={batchStart.isPending || startableEpisodes.length === 0}
          className="h-7 gap-1.5 rounded-[7px] bg-primary px-3 text-[11px] font-medium text-primary-foreground shadow-none hover:bg-primary/85 disabled:opacity-50"
        >
          {batchStart.isPending ? (
            <Loader2 className="size-3 animate-spin" />
          ) : (
            <Play className="size-3" />
          )}
          {t("episode.autopilot.batchStartButton", {
            count: startableEpisodes.length,
          })}
        </Button>
      </div>
    </div>
  );
}
