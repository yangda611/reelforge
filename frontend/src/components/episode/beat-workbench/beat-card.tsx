// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { memo } from "react";
import { Check, Loader2, Shrimp, Trash2 } from "lucide-react";
import { useTranslation } from "react-i18next";

import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import type { Beat } from "@/types/episode";

interface BeatCardProps {
  beat: Beat;
  displayNumber: number;
  isSelected: boolean;
  isChecked: boolean;
  onCardClick: (beatNum: number) => void;
  onCheckboxClick: (beatNum: number) => void;
  onInsertBefore?: (beatNum: number) => void;
  onInsertAfter?: (beatNum: number) => void;
  onOpenFreezone?: (beatNum: number) => void;
  onDeleteManual?: (beatNum: number, displayNumber: number) => void;
  isOpeningFreezone?: boolean;
  isDeletingManual?: boolean;
}

function BeatCardImpl({
  beat,
  displayNumber,
  isSelected,
  isChecked,
  onCardClick,
  onCheckboxClick,
  onInsertBefore,
  onInsertAfter,
  onOpenFreezone,
  onDeleteManual,
  isOpeningFreezone = false,
  isDeletingManual = false,
}: BeatCardProps) {
  const { t } = useTranslation();
  const s9SubshotCount = (() => {
    if (Array.isArray(beat.s9_subshots) && beat.s9_subshots.length > 0) {
      return beat.s9_subshots.length;
    }
    if (beat.s9_subshots_json) {
      try {
        const parsed = JSON.parse(beat.s9_subshots_json);
        return Array.isArray(parsed) ? parsed.length : 0;
      } catch {
        return 0;
      }
    }
    return 0;
  })();
  const s9Meta = (() => {
    if (beat.s9_segment_meta && typeof beat.s9_segment_meta === "object") {
      return beat.s9_segment_meta;
    }
    if (beat.s9_segment_meta_json) {
      try {
        const parsed = JSON.parse(beat.s9_segment_meta_json);
        return parsed && typeof parsed === "object" ? parsed : null;
      } catch {
        return null;
      }
    }
    return null;
  })();
  const s9PlannedSeconds =
    typeof s9Meta?.planned_duration_seconds === "number" &&
    s9Meta.planned_duration_seconds > 0
      ? Math.round(s9Meta.planned_duration_seconds * 10) / 10
      : typeof beat.estimated_duration === "number" &&
          beat.estimated_duration > 0 &&
          s9SubshotCount > 0
        ? Math.round(beat.estimated_duration * 10) / 10
        : null;
  const isS9Segment = s9SubshotCount > 0 || s9Meta !== null;

  return (
    <article
      data-beat-number={beat.beat_number}
      className={cn(
        "group relative flex transform-gpu flex-col overflow-hidden rounded-[8px] border bg-white/[0.025] text-left transition-all duration-150 ease-out hover:scale-[1.008]",
        isSelected && "border-primary/65 bg-white/[0.03]",
        isChecked && !isSelected && "border-primary/45 bg-white/[0.03]",
        !isSelected && !isChecked && "border-white/[0.08] hover:border-white/[0.28] hover:bg-white/[0.03]",
        "min-h-[120px]",
      )}
    >
      <div className="absolute left-1.5 top-1.5 z-20 flex max-w-[calc(100%-3.75rem)] flex-wrap items-center gap-1">
        <span
          className={cn(
            "rounded-[4px] border px-2 py-1 font-mono text-[11px] font-medium leading-none tabular-nums backdrop-blur-md",
            isSelected
              ? "border-primary/45 bg-black/55 text-primary/90"
              : "border-white/[0.10] bg-black/45 text-white/78",
          )}
        >
          {t("episode.beat.badge", { n: displayNumber })}
        </span>
        {isS9Segment && s9PlannedSeconds !== null && (
          <span
            className="rounded-[4px] border border-cyan-400/25 bg-black/55 px-1.5 py-1 font-mono text-[10px] font-medium leading-none tabular-nums text-cyan-200 backdrop-blur-md"
            title={beat.segment_title || undefined}
          >
            {s9SubshotCount > 0
              ? t("episode.beat.s9Badge", {
                  seconds: s9PlannedSeconds,
                  shots: s9SubshotCount,
                })
              : `${s9PlannedSeconds}s`}
          </span>
        )}
      </div>

      <div className="absolute right-1.5 top-1.5 z-20 flex items-center gap-1">
        {beat.is_manual_shot && onDeleteManual && (
          <Tooltip>
            <TooltipTrigger
              render={
                <button
                  type="button"
                  onClick={(e) => {
                    e.stopPropagation();
                    onDeleteManual(beat.beat_number, displayNumber);
                  }}
                  disabled={isDeletingManual}
                  className="flex size-5 items-center justify-center rounded-[5px] border border-destructive/25 bg-destructive/[0.08] text-destructive/85 backdrop-blur transition-colors hover:bg-destructive/15 hover:text-destructive focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-destructive disabled:cursor-wait disabled:opacity-60"
                  aria-label={t("episode.beat.deleteManualShot")}
                />
              }
            >
              {isDeletingManual ? (
                <Loader2 className="size-3 animate-spin" />
              ) : (
                <Trash2 className="size-3" />
              )}
            </TooltipTrigger>
            <TooltipContent
              side="top"
              sideOffset={8}
              showArrow={false}
              className="border border-white/10 bg-background/95 text-foreground shadow-none"
            >
              {t("episode.beat.deleteManualShot")}
            </TooltipContent>
          </Tooltip>
        )}
        {/* Multi-select control */}
        <button
          type="button"
          onClick={(e) => {
            e.stopPropagation();
            onCheckboxClick(beat.beat_number);
          }}
          className={cn(
            "flex size-5 items-center justify-center rounded-[5px] border backdrop-blur transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary",
            isChecked
              ? "border-primary/55 bg-black/55 text-primary/90"
              : "border-white/[0.16] bg-black/35 text-transparent hover:border-white/[0.30] hover:bg-black/50",
          )}
          aria-label={isChecked ? t("episode.beat.deselect") : t("episode.beat.select")}
        >
          <Check className="size-3" />
        </button>
      </div>

      {/* Card body — clickable, shows the beat text */}
      <button
        type="button"
        onClick={() => onCardClick(beat.beat_number)}
        className="flex flex-1 flex-col gap-1.5 px-2.5 pb-2 pt-9 text-left"
      >
        <p className="line-clamp-3 text-xs leading-5 text-foreground">
          {beat.narration_segment || (
            <span className="italic text-muted-foreground/60">
              {t("episode.beat.noNarration")}
            </span>
          )}
        </p>
        {beat.visual_description && (
          <p className="line-clamp-3 text-[11px] leading-4 text-muted-foreground">
            {beat.visual_description}
          </p>
        )}
      </button>
      {(onInsertBefore || onInsertAfter || onOpenFreezone) && (
        <div className="flex items-center justify-end gap-1 px-2 pb-2">
          {onInsertBefore && (
            <BeatCardActionButton
              label={t("episode.beat.insertBeforeShort")}
              tooltip={t("episode.beat.insertBefore", { n: displayNumber })}
              onClick={() => onInsertBefore(beat.beat_number)}
            />
          )}
          {onInsertAfter && (
            <BeatCardActionButton
              label={t("episode.beat.insertAfterShort")}
              tooltip={t("episode.beat.insertAfter", { n: displayNumber })}
              onClick={() => onInsertAfter(beat.beat_number)}
            />
          )}
          {onOpenFreezone && (
            <BeatCardActionButton
              label={
                isOpeningFreezone ? (
                  <Loader2 className="size-3 animate-spin" />
                ) : (
                  <Shrimp className="size-3.5" />
                )
              }
              tooltip={t("episode.beat.openFreezoneTip")}
              onClick={() => onOpenFreezone(beat.beat_number)}
              disabled={isOpeningFreezone}
              ariaLabel={t("episode.beat.openFreezone")}
            />
          )}
        </div>
      )}
    </article>
  );
}

export const BeatCard = memo(BeatCardImpl);

function BeatCardActionButton({
  label,
  tooltip,
  onClick,
  disabled = false,
  ariaLabel,
}: {
  label: React.ReactNode;
  tooltip: string;
  onClick: () => void;
  disabled?: boolean;
  ariaLabel?: string;
}) {
  return (
    <Tooltip>
      <TooltipTrigger
        delay={150}
        closeDelay={150}
        render={
          <button
            type="button"
            onClick={(e) => {
              e.stopPropagation();
              onClick();
            }}
            disabled={disabled}
            className="flex size-6 items-center justify-center rounded-[6px] border border-white/[0.18] bg-black/60 text-[12px] font-medium leading-none text-white/82 shadow-[0_8px_18px_rgba(0,0,0,0.32)] backdrop-blur transition-colors duration-100 hover:border-primary/45 hover:bg-black/72 hover:text-primary/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary disabled:cursor-wait disabled:opacity-65 [&_svg]:size-3"
            aria-label={ariaLabel ?? tooltip}
          />
        }
      >
        {label}
      </TooltipTrigger>
      <TooltipContent
        side="top"
        sideOffset={8}
        showArrow={false}
        className="border border-white/10 bg-background/95 text-foreground shadow-none"
      >
        {tooltip}
      </TooltipContent>
    </Tooltip>
  );
}
