// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { useMemo } from "react";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";
import { Lock, Unlock } from "lucide-react";

import { useScript, useUpdateBeat } from "@/lib/queries/scripts";
import { cn } from "@/lib/utils";
import type { Beat, SceneRef } from "@/types/episode";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
} from "@/components/ui/select";

type LockModeValue = "off" | "full_body" | "upper_body";

interface FixedCameraLockControlProps {
  beat: Beat;
  project: string;
  episode: number;
  className?: string;
}

export function FixedCameraLockControl({
  beat,
  project,
  episode,
  className,
}: FixedCameraLockControlProps) {
  const { t } = useTranslation();
  const scriptQuery = useScript(project, episode);
  const updateBeat = useUpdateBeat(project, episode);

  const anchorCandidates = useMemo(() => {
    const beats = scriptQuery.data?.data?.beats ?? [];
    const earlier = beats
      .filter((b) => b.beat_number < beat.beat_number)
      .sort((a, b) => b.beat_number - a.beat_number);
    if (earlier.length > 0) {
      return earlier.map((b) => b.beat_number);
    }
    if (beat.beat_number > 1) {
      return [beat.beat_number - 1];
    }
    return [];
  }, [scriptQuery.data, beat.beat_number]);

  const rawLockBeat = beat.scene_ref?.lock_camera_beat;
  const isLocked =
    typeof rawLockBeat === "number" &&
    Number.isFinite(rawLockBeat) &&
    rawLockBeat > 0 &&
    rawLockBeat !== beat.beat_number;
  const activeAnchorBeat = isLocked
    ? rawLockBeat
    : (anchorCandidates[0] ?? Math.max(1, beat.beat_number - 1));
  const modeValue: LockModeValue = !isLocked
    ? "off"
    : beat.scene_ref?.lock_spatial_mode === "upper_body"
      ? "upper_body"
      : "full_body";

  const anchorOptions = useMemo(() => {
    if (isLocked && !anchorCandidates.includes(rawLockBeat)) {
      return [rawLockBeat, ...anchorCandidates];
    }
    return anchorCandidates;
  }, [anchorCandidates, isLocked, rawLockBeat]);

  if (beat.beat_number <= 1 && !isLocked) {
    return null;
  }

  const persistSceneRef = async (nextMode: LockModeValue, nextAnchor: number) => {
    const prevRef: Partial<SceneRef> = beat.scene_ref ?? {};
    let payloadRef: SceneRef;
    if (nextMode === "off") {
      const { lock_camera_beat: _removed, ...rest } = prevRef;
      payloadRef = {
        ...rest,
        scene_id: rest.scene_id ?? "",
        lock_spatial_mode: "",
      };
    } else {
      payloadRef = {
        ...prevRef,
        scene_id: prevRef.scene_id ?? "",
        lock_camera_beat: nextAnchor,
        lock_spatial_mode: nextMode,
      };
    }

    try {
      await updateBeat.mutateAsync({
        beatNum: beat.beat_number,
        data: { scene_ref: payloadRef },
      });
      toast.success(
        nextMode === "off"
          ? t("episode.workbench.spatialLock.unlockedToast")
          : t("episode.workbench.spatialLock.lockedToast", { n: nextAnchor }),
      );
    } catch {
      toast.error(t("episode.workbench.spatialLock.saveFailed"));
    }
  };

  const modeLabel =
    modeValue === "full_body"
      ? t("episode.workbench.spatialLock.modeFullBody")
      : modeValue === "upper_body"
        ? t("episode.workbench.spatialLock.modeUpperBody")
        : t("episode.workbench.spatialLock.modeOff");

  return (
    <div
      className={cn(
        "flex flex-wrap items-center justify-between gap-2 rounded-[8px] border px-2.5 py-1.5 text-xs transition-colors",
        isLocked
          ? "border-primary/35 bg-primary/10 text-foreground"
          : "border-white/[0.08] bg-white/[0.02] text-muted-foreground",
        className,
      )}
    >
      <div className="flex items-center gap-2">
        {isLocked ? (
          <Lock className="size-3.5 shrink-0 text-primary" />
        ) : (
          <Unlock className="size-3.5 shrink-0 text-muted-foreground/75" />
        )}
        <span className="font-medium text-foreground/90">
          {t("episode.workbench.spatialLock.label")}
        </span>
        <span className="hidden text-[11px] text-muted-foreground sm:inline">
          {isLocked
            ? t("episode.workbench.spatialLock.activeHint", { n: activeAnchorBeat })
            : t("episode.workbench.spatialLock.idleHint")}
        </span>
      </div>

      <div className="flex flex-wrap items-center gap-1.5">
        <Select
          value={modeValue}
          onValueChange={(value) => {
            if (!value) return;
            const nextMode = value as LockModeValue;
            void persistSceneRef(nextMode, activeAnchorBeat);
          }}
          disabled={updateBeat.isPending}
        >
          <SelectTrigger
            aria-label={t("episode.workbench.spatialLock.label")}
            className="!h-7 min-w-[146px] rounded-[6px] border-white/[0.12] bg-background/60 px-2 text-xs shadow-none"
          >
            <span data-slot="select-value" className="flex flex-1 items-center text-left">
              {modeLabel}
            </span>
          </SelectTrigger>
          <SelectContent alignItemWithTrigger={false}>
            <SelectItem value="off">
              {t("episode.workbench.spatialLock.modeOff")}
            </SelectItem>
            <SelectItem value="full_body">
              {t("episode.workbench.spatialLock.modeFullBody")}
            </SelectItem>
            <SelectItem value="upper_body">
              {t("episode.workbench.spatialLock.modeUpperBody")}
            </SelectItem>
          </SelectContent>
        </Select>

        {isLocked && anchorOptions.length > 0 && (
          <Select
            value={String(activeAnchorBeat)}
            onValueChange={(value) => {
              if (!value) return;
              const parsed = Number.parseInt(value, 10);
              if (Number.isFinite(parsed) && parsed > 0) {
                void persistSceneRef(modeValue, parsed);
              }
            }}
            disabled={updateBeat.isPending}
          >
            <SelectTrigger
              aria-label={t("episode.workbench.spatialLock.anchorLabel")}
              className="!h-7 min-w-[96px] rounded-[6px] border-primary/30 bg-background/70 px-2 text-xs shadow-none"
            >
              <span data-slot="select-value" className="flex flex-1 items-center text-left">
                {t("episode.workbench.spatialLock.anchorItem", { n: activeAnchorBeat })}
              </span>
            </SelectTrigger>
            <SelectContent alignItemWithTrigger={false}>
              {anchorOptions.map((anchorNum) => (
                <SelectItem key={anchorNum} value={String(anchorNum)}>
                  {t("episode.workbench.spatialLock.anchorItem", { n: anchorNum })}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        )}
      </div>
    </div>
  );
}
