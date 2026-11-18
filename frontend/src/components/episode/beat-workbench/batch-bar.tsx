// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";
import { Loader2, Mic2 } from "lucide-react";

import { useAudioBillingQuote, useGenerateAudio } from "@/lib/queries/audio";
import { useVideoBackends } from "@/lib/queries/video";
import { useTaskController } from "@/hooks/use-task-controller";
import { queryKeys } from "@/lib/query-keys";
import { TASK_TYPES } from "@/lib/task-types";
import { cn } from "@/lib/utils";
import {
  backendErrorToastMessage,
  BillingRuleNotConfiguredError,
} from "@/lib/api-errors";
import type { ProjectAspectRatio } from "@/lib/aspect-ratio";
import { CreditCostInline } from "@/components/credit-cost-inline";
import { CreditCostPill } from "@/components/credits/credit-visual";
import type { Beat } from "@/types/episode";

import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  WORKBENCH_SELECT_CONTENT_CLASS,
  WORKBENCH_SELECT_ITEM_CLASS,
  WORKBENCH_SELECT_TRIGGER_CLASS,
} from "./toolbar-select-styles";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";

// 产品决定：整集音频入口从工具栏下线，任何 spineTemplate 下都不给。后端路由、
// 账单口径和任务控制都原样留着（其它入口和历史任务仍要用），这里只掐掉入口。
// 恢复时把常量翻回 true，下面原有的 spineTemplate 条件继续生效。
const SHOW_EPISODE_AUDIO_ENTRY = false;

const TOOLBAR_CONTROL_CLASS =
  "h-[26px] gap-1.5 rounded-[6px] border border-white/10 bg-transparent px-2 py-0 text-[11px] font-medium text-foreground/85 transition-colors hover:border-primary/40 hover:bg-white/[0.04] hover:text-foreground focus-visible:ring-2 focus-visible:ring-sidebar-ring disabled:text-muted-foreground/50";

interface BatchBarProps {
  project: string;
  episode: number;
  beats: Beat[];
  videoBackend: string;
  spineTemplate?: "drama" | "narrated";
  aspectRatio: ProjectAspectRatio;
  onAspectRatioChange: (aspectRatio: ProjectAspectRatio) => void;
}

function AspectRatioSelect({
  aspectRatio,
  onAspectRatioChange,
}: {
  aspectRatio: ProjectAspectRatio;
  onAspectRatioChange: (aspectRatio: ProjectAspectRatio) => void;
}) {
  const { t } = useTranslation();
  const label = t("episode.workbench.video.ratio");

  return (
    <div className="flex items-center gap-2.5 text-[11px]">
      <Label className="whitespace-nowrap text-[11px] text-muted-foreground">
        {label}
      </Label>
      <Select
        value={aspectRatio}
        onValueChange={(value) => {
          if (!value) return;
          onAspectRatioChange(value === "16:9" ? "16:9" : "2:3");
        }}
      >
        <SelectTrigger
          aria-label={label}
          className={cn(WORKBENCH_SELECT_TRIGGER_CLASS, "w-[70px]")}
        >
          <SelectValue>{() => aspectRatio}</SelectValue>
        </SelectTrigger>
        <SelectContent
          align="start"
          sideOffset={8}
          alignItemWithTrigger={false}
          className={WORKBENCH_SELECT_CONTENT_CLASS}
        >
          <SelectItem value="2:3" className={WORKBENCH_SELECT_ITEM_CLASS}>
            2:3
          </SelectItem>
          <SelectItem value="16:9" className={WORKBENCH_SELECT_ITEM_CLASS}>
            16:9
          </SelectItem>
        </SelectContent>
      </Select>
    </div>
  );
}

export function BatchBar({
  project,
  episode,
  beats,
  videoBackend,
  spineTemplate = "drama",
  aspectRatio,
  onAspectRatioChange,
}: BatchBarProps) {
  const { t } = useTranslation();

  const generateAudio = useGenerateAudio(project, episode);
  const episodeAudioRevision = useMemo(
    () =>
      beats
        .map((beat) =>
          [
            beat.beat_number,
            beat.audio_type,
            beat.speaker,
            beat.audio_url,
            beat.narration_segment,
          ].join(":"),
        )
        .join(","),
    [beats],
  );
  const episodeAudioBillingQuote = useAudioBillingQuote(
    project,
    episode,
    { mode: "sync_changed" },
    episodeAudioRevision,
  );
  const videoBackends = useVideoBackends(project);

  const [errorDialog, setErrorDialog] = useState<{
    title: string;
    description: string;
  } | null>(null);
  const showError = (title: string, description: string) => {
    setErrorDialog({ title, description });
  };
  const audioTask = useTaskController({
    key: { taskType: TASK_TYPES.AUDIO_GENERATION_INDEXTTS2, project, episode },
    alsoReconcile: [TASK_TYPES.AUDIO_GENERATION],
    invalidateKeys: [
      queryKeys.beats(project, episode),
      queryKeys.pipelineStatus(project),
    ],
  });
  const selectedVideoBackend = videoBackends.data?.data.find(
    (option) => option.value === videoBackend,
  );
  const audioUnavailableForVideoBackend = selectedVideoBackend?.is_seedance2 === true;
  const episodeAudioCostDisplay =
    episodeAudioBillingQuote.data?.data.display ??
    (episodeAudioBillingQuote.error instanceof BillingRuleNotConfiguredError
      ? t("common.billingRuleNotConfiguredShort")
      : "");
  const episodeAudioPrereqErrors =
    episodeAudioBillingQuote.data?.data.prereq_errors ?? [];

  const [confirm, setConfirm] = useState<{
    title: string;
    description: string;
    onConfirm: () => void;
  } | null>(null);

  const askConfirm = (
    title: string,
    description: string,
    onConfirm: () => void,
  ) => {
    setConfirm({ title, description, onConfirm });
  };
  const confirmCostDisplay = episodeAudioCostDisplay;

  const handleGenAllAudio = async () => {
    try {
      const res = await generateAudio.mutateAsync(undefined);
      if (res.ok === false) {
        showError(t("episode.workbench.batch.genAudioTitle"), res.error || t("common.error"));
        return;
      }
      audioTask.start({ scope: res.scope, taskId: res.task_id });
    } catch (error) {
      toast.error(backendErrorToastMessage(error, t));
    }
  };

  return (
    <div className="flex h-full w-full items-center px-3">
      <div
        role="toolbar"
        aria-label={t("episode.workbench.batch.toolbar")}
        className="flex w-full min-w-0 flex-wrap items-center justify-center gap-x-7 gap-y-1.5"
      >
        <div className="flex min-w-0 flex-wrap items-center gap-5">
          <AspectRatioSelect
            aspectRatio={aspectRatio}
            onAspectRatioChange={onAspectRatioChange}
          />
        </div>

        <span className="hidden h-5 w-px shrink-0 bg-border/75 lg:block" aria-hidden />

        <div className="flex min-w-0 flex-wrap items-center gap-4">
          {/* 精品剧 (spine_template === "drama") 把解说烘进渲染视频，没有独立音频阶段 —— 隐藏「生成全集音频」 */}
          {SHOW_EPISODE_AUDIO_ENTRY && spineTemplate !== "drama" && (
          <Tooltip>
            <TooltipTrigger
              delay={150}
              closeDelay={150}
              render={
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => {
                    if (audioUnavailableForVideoBackend) return;
                    if (episodeAudioPrereqErrors.length > 0) {
                      showError(
                        t("episode.workbench.batch.genAudioTitle"),
                        episodeAudioPrereqErrors.join("\n"),
                      );
                      return;
                    }
                    askConfirm(
                      t("episode.workbench.batch.genAudioTitle"),
                      t("episode.workbench.batch.genAudioDesc"),
                      handleGenAllAudio,
                    );
                  }}
                  disabled={!audioUnavailableForVideoBackend && (audioTask.started || generateAudio.isPending)}
                  aria-disabled={audioUnavailableForVideoBackend}
                  className={cn(
                    TOOLBAR_CONTROL_CLASS,
                    audioUnavailableForVideoBackend &&
                      "cursor-not-allowed border-white/[0.06] text-muted-foreground/45 hover:border-white/[0.06] hover:bg-transparent hover:text-muted-foreground/45",
                  )}
                />
              }
            >
              {generateAudio.isPending || audioTask.started ? (
                <Loader2 className="size-3 animate-spin" />
              ) : (
                <Mic2 className="size-3.5" />
              )}
              {t("episode.workbench.batch.genAudio")}
              <span aria-hidden="true" className="inline-flex min-w-7 justify-start">
                {episodeAudioPrereqErrors.length === 0 && (
                  <CreditCostPill
                    display={episodeAudioCostDisplay}
                    promotion={episodeAudioBillingQuote.data?.data.promotion}
                    disabled={audioUnavailableForVideoBackend}
                    className="h-4 bg-transparent px-0 text-[11px]"
                  />
                )}
              </span>
            </TooltipTrigger>
            {audioUnavailableForVideoBackend && (
              <TooltipContent
                side="bottom"
                sideOffset={8}
                showArrow={false}
                className="border border-white/10 bg-background/95 text-foreground shadow-none"
              >
                {t("episode.workbench.batch.genAudioUnavailableForVideoModel")}
              </TooltipContent>
            )}
          </Tooltip>
          )}
        </div>
      </div>

      {/* Confirmation dialog for episode-level actions */}
      <AlertDialog open={confirm !== null} onOpenChange={(v) => !v && setConfirm(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{confirm?.title}</AlertDialogTitle>
            <AlertDialogDescription>{confirm?.description}</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{t("common.cancel")}</AlertDialogCancel>
            <AlertDialogAction
              variant={confirmCostDisplay ? "outline" : undefined}
              onClick={() => {
                confirm?.onConfirm();
                setConfirm(null);
              }}
              className={cn(
                confirmCostDisplay &&
                  "relative border-[3px] border-[#007A87] bg-transparent pr-9 transition-transform hover:border-[#007A87] hover:bg-transparent active:scale-95 dark:border-[#007A87] dark:hover:border-[#007A87]",
              )}
            >
              {t("common.confirmExecute")}
              <CreditCostInline
                display={confirmCostDisplay}
                promotion={episodeAudioBillingQuote.data?.data.promotion}
              />
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Error dialog — prerequisite / validation failures from the BE */}
      <AlertDialog
        open={errorDialog !== null}
        onOpenChange={(v) => !v && setErrorDialog(null)}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{errorDialog?.title}</AlertDialogTitle>
            <AlertDialogDescription className="whitespace-pre-line">
              {errorDialog?.description}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogAction onClick={() => setErrorDialog(null)}>
              {t("common.ok", "OK")}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

    </div>
  );
}
