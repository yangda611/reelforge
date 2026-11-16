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
import { resolveMediaUrl } from "@/lib/media-url";
import { resolveImage } from "@/lib/resolve-image";
import { cn } from "@/lib/utils";
import type { Beat } from "@/types/episode";
import type { PoolImage } from "@/lib/queries/sketches";

interface BeatCardProps {
  beat: Beat;
  displayNumber: number;
  showSketch: boolean;
  showRender: boolean;
  images: PoolImage[];
  assignments: Record<string, string>;
  aspectRatio: "portrait" | "landscape";
  isSelected: boolean;
  isChecked: boolean;
  onCardClick: (beatNum: number) => void;
  onCheckboxClick: (beatNum: number) => void;
  onInsertBefore?: (beatNum: number) => void;
  onInsertAfter?: (beatNum: number) => void;
  onOpenFreezone?: (beatNum: number, primarySlot: "sketch" | "frame") => void;
  onDeleteManual?: (beatNum: number, displayNumber: number) => void;
  isOpeningFreezone?: boolean;
  isDeletingManual?: boolean;
}

function BeatCardImpl({
  beat,
  displayNumber,
  showSketch,
  showRender,
  images,
  assignments,
  aspectRatio,
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
  const hasVisibleMedia = showSketch || showRender;
  const dualImage = showSketch && showRender;

  const sketch = showSketch
    ? resolveImage(images, assignments, beat.beat_number, "sketch", beat.sketch_url ?? null)
    : null;
  const render = showRender
    ? resolveImage(images, assignments, beat.beat_number, "render", beat.frame_url ?? null)
    : null;
  const mainImage = dualImage ? (render?.url ? render : sketch) : showRender ? render : sketch;
  const mainAlt = dualImage ? (render?.url ? "render" : "sketch") : showRender ? "render" : "sketch";
  const mainPlaceholder =
    mainAlt === "render" ? t("episode.beat.noRender") : t("episode.beat.noSketch");
  const shouldShowSketchOverlay = dualImage && mainAlt === "render";
  const freezonePrimarySlot = mainAlt === "render" ? "frame" : "sketch";
  const useHorizontalActions = aspectRatio === "landscape";

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
        // Ensure text-only cards have reasonable minimum height
        !hasVisibleMedia && "min-h-[100px]",
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

      {/* Card body — clickable */}
      <button
        type="button"
        onClick={() => onCardClick(beat.beat_number)}
        className="flex flex-1 flex-col text-left"
      >
        {hasVisibleMedia && (
          <div className="relative w-full">
            <ImageSlot
              src={mainImage?.url ?? null}
              alt={mainAlt}
              placeholder={mainPlaceholder}
              className="w-full"
              aspectRatio={aspectRatio}
            />
            {shouldShowSketchOverlay && (
              <div
                className={cn(
                  "absolute bottom-1.5 left-1.5 w-[32%] min-w-10 max-w-16 overflow-hidden rounded-[5px] border border-white/[0.16] bg-black/50 shadow-[0_10px_26px_rgba(0,0,0,0.48)] backdrop-blur",
                  aspectRatio === "landscape" && "w-[24%]",
                )}
              >
                <ImageSlot
                  src={sketch?.url ?? null}
                  alt="sketch"
                  placeholder={t("episode.beat.noSketch")}
                  className="w-full"
                  aspectRatio={aspectRatio}
                />
              </div>
            )}
          </div>
        )}
      </button>
      {(onInsertBefore || onInsertAfter || onOpenFreezone) && hasVisibleMedia && (
        <div
          className={cn(
            "absolute bottom-1.5 right-1.5 z-20 flex items-center gap-1",
            useHorizontalActions ? "flex-row" : "flex-col",
          )}
        >
          {onInsertBefore && (
            <BeatCardActionButton
              label={t("episode.beat.insertBeforeShort")}
              tooltip={t("episode.beat.insertBefore", { n: displayNumber })}
              onClick={() => onInsertBefore(beat.beat_number)}
              tooltipSide={useHorizontalActions ? "top" : "right"}
            />
          )}
          {onInsertAfter && (
            <BeatCardActionButton
              label={t("episode.beat.insertAfterShort")}
              tooltip={t("episode.beat.insertAfter", { n: displayNumber })}
              onClick={() => onInsertAfter(beat.beat_number)}
              tooltipSide={useHorizontalActions ? "top" : "right"}
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
              onClick={() => onOpenFreezone(beat.beat_number, freezonePrimarySlot)}
              disabled={isOpeningFreezone}
              ariaLabel={t("episode.beat.openFreezone")}
              tooltipSide={useHorizontalActions ? "top" : "right"}
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
  tooltipSide = "right",
}: {
  label: React.ReactNode;
  tooltip: string;
  onClick: () => void;
  disabled?: boolean;
  ariaLabel?: string;
  tooltipSide?: "top" | "right";
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
        side={tooltipSide}
        sideOffset={8}
        showArrow={false}
        className="border border-white/10 bg-background/95 text-foreground shadow-none"
      >
        {tooltip}
      </TooltipContent>
    </Tooltip>
  );
}

function ImageSlot({
  src,
  alt,
  placeholder,
  className,
  aspectRatio,
}: {
  src: string | null;
  alt: string;
  placeholder: string;
  className?: string;
  aspectRatio: "portrait" | "landscape";
}) {
  const resolved = src ? resolveMediaUrl(src) : null;
  return (
    <div
      className={cn(
        "relative overflow-hidden bg-black/35",
        aspectRatio === "portrait"
          ? "aspect-[2/3]"
          : "aspect-video",
        className,
      )}
    >
      {resolved ? (
        <img src={resolved} alt={alt} className="h-full w-full object-cover" loading="lazy" decoding="async" />
      ) : (
        <div className="flex h-full items-center justify-center text-[10px] text-muted-foreground/40">
          {placeholder}
        </div>
      )}
    </div>
  );
}
