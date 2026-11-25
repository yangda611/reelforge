// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { useState } from "react";
import { Check, Film, Loader2, Pencil, Sparkles, Volume2, VolumeX, X } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";
import type {
  Beat,
  S9EpisodePreviewMeta,
  S9SegmentMeta,
  S9SubShot,
} from "@/types/episode";

export interface ScriptBeatPreviewLabels {
  title: string;
  count: (count: number) => string;
  segmentCountSummary?: (segments: number, shots: number, totalSeconds: number) => string;
  loading: string;
  emptyTitle: string;
  empty: string;
  audioType: (type: string) => string;
  speaker: string;
  noSpeaker: string;
  dialogueLine: string;
  narrationLine: string;
  noNarration: string;
  visualDescription: string;
  noVisualDescription: string;
  durationAssessmentTitle?: string;
  assetManifestTitle?: string;
  plannedDurationBadge?: (seconds: number) => string;
  shotCountBadge?: (shots: number) => string;
  shotBasicInfoLabel?: string;
  shotContentLabel?: string;
  editShot?: string;
  saveShot?: string;
  cancelEdit?: string;
  syncPrompt?: string;
  syncingPrompt?: string;
  assetReady?: string;
  assetPending?: string;
  batchFillMissingPrompts?: (count: number) => string;
  batchRegenPrompts?: string;
  batchGeneratingPrompts?: string;
  promptReady?: string;
  promptPending?: string;
}

interface ScriptBeatPreviewProps {
  beats: Beat[];
  s9PreviewMeta?: S9EpisodePreviewMeta | null;
  labels: ScriptBeatPreviewLabels;
  loading?: boolean;
  savingBeatNumber?: number | null;
  regeneratingBeatNumber?: number | null;
  batchPromptsBusy?: boolean;
  onUpdateSegmentSubshots?: (
    beatNumber: number,
    payload: { segment_title?: string; s9_subshots: S9SubShot[] },
  ) => void | Promise<void>;
  onRegenerateSegmentPrompt?: (beatNumber: number) => void | Promise<void>;
  onBatchGeneratePrompts?: (onlyMissing: boolean) => void | Promise<void>;
  className?: string;
}

const AUDIO_STYLE: Record<string, string> = {
  dialogue: "border-emerald-400/20 bg-emerald-400/[0.08] text-emerald-300",
  narration: "border-cyan-400/20 bg-cyan-400/[0.08] text-cyan-200",
  silence: "border-white/[0.08] bg-white/[0.04] text-muted-foreground",
  action: "border-amber-400/20 bg-amber-400/[0.08] text-amber-200",
};

const ASSET_KIND_LABEL: Record<string, string> = {
  identity: "角色",
  scene: "场景",
  prop: "道具",
  voice: "音色",
  first_frame: "首帧",
};

function formatSec(value: number | undefined): string {
  const num = Number(value ?? 0);
  if (!Number.isFinite(num) || num <= 0) return "0";
  const rounded = Math.round(num * 10) / 10;
  return Number.isInteger(rounded) ? String(rounded) : rounded.toFixed(1);
}

function resolveSegmentSubshots(beat: Beat): S9SubShot[] {
  if (Array.isArray(beat.s9_subshots) && beat.s9_subshots.length > 0) {
    return beat.s9_subshots;
  }
  if (beat.s9_subshots_json) {
    try {
      const parsed = JSON.parse(beat.s9_subshots_json);
      if (Array.isArray(parsed) && parsed.length > 0) return parsed;
    } catch {
      return [];
    }
  }
  return [];
}

function resolveSegmentMeta(beat: Beat): S9SegmentMeta | null {
  if (beat.s9_segment_meta && typeof beat.s9_segment_meta === "object") {
    return beat.s9_segment_meta;
  }
  if (beat.s9_segment_meta_json && beat.s9_segment_meta_json !== "{}") {
    try {
      const parsed = JSON.parse(beat.s9_segment_meta_json);
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
        return parsed as S9SegmentMeta;
      }
    } catch {
      return null;
    }
  }
  return null;
}

function hasSegmentVideoPrompt(beat: Beat): boolean {
  if (!beat.seedance2_config_json) return false;
  try {
    const parsed = JSON.parse(beat.seedance2_config_json) as {
      final_prompt?: unknown;
    };
    return (
      typeof parsed?.final_prompt === "string" &&
      parsed.final_prompt.trim().length > 0
    );
  } catch {
    return false;
  }
}

export function ScriptBeatPreview({
  beats,
  s9PreviewMeta,
  labels,
  loading = false,
  savingBeatNumber = null,
  regeneratingBeatNumber = null,
  batchPromptsBusy = false,
  onUpdateSegmentSubshots,
  onRegenerateSegmentPrompt,
  onBatchGeneratePrompts,
  className,
}: ScriptBeatPreviewProps) {
  const hasS9Segments = beats.some((b) => resolveSegmentSubshots(b).length > 0);
  const missingPromptCount = hasS9Segments
    ? beats.filter((b) => !hasSegmentVideoPrompt(b)).length
    : 0;
  const totalShots = beats.reduce(
    (sum, b) => sum + resolveSegmentSubshots(b).length,
    0,
  );
  const totalSeconds = beats.reduce((sum, b) => {
    const meta = resolveSegmentMeta(b);
    const dur = Number(
      meta?.planned_duration_seconds || b.duration_seconds || b.estimated_duration || 0,
    );
    return sum + (Number.isFinite(dur) ? dur : 0);
  }, 0);

  return (
    <section className={cn("min-w-0", className)}>
      <div className="mb-2 flex min-h-7 flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <h2 className="text-sm font-semibold tracking-tight text-foreground">
            {labels.title}
          </h2>
          <span className="text-[11px] text-muted-foreground/70">
            (
            {hasS9Segments && labels.segmentCountSummary
              ? labels.segmentCountSummary(beats.length, totalShots, Math.round(totalSeconds))
              : labels.count(beats.length)}
            )
          </span>
        </div>
        {hasS9Segments && onBatchGeneratePrompts && (
          <div className="flex items-center gap-1.5">
            {missingPromptCount > 0 && (
              <Button
                type="button"
                variant="outline"
                size="sm"
                disabled={batchPromptsBusy}
                onClick={() => void onBatchGeneratePrompts(true)}
                className="h-6 gap-1 rounded-full border-amber-400/25 bg-amber-400/[0.08] px-2.5 text-[11px] font-medium text-amber-300 hover:bg-amber-400/[0.15] hover:text-amber-200"
              >
                {batchPromptsBusy ? (
                  <Loader2 className="size-3 animate-spin" />
                ) : (
                  <Sparkles className="size-3" />
                )}
                {labels.batchFillMissingPrompts
                  ? labels.batchFillMissingPrompts(missingPromptCount)
                  : `补全缺失提示词 (${missingPromptCount})`}
              </Button>
            )}
            <Button
              type="button"
              variant="ghost"
              size="sm"
              disabled={batchPromptsBusy}
              onClick={() => void onBatchGeneratePrompts(false)}
              className="h-6 gap-1 rounded-full border border-white/[0.08] bg-[#0f1117] px-2.5 text-[11px] font-medium text-foreground/82 hover:bg-[#1f2a3f] hover:text-[#5ba0ff]"
            >
              {batchPromptsBusy ? (
                <Loader2 className="size-3 animate-spin" />
              ) : (
                <Sparkles className="size-3" />
              )}
              {batchPromptsBusy
                ? (labels.batchGeneratingPrompts ?? "批量更新中...")
                : (labels.batchRegenPrompts ?? "批量更新视频提示词")}
            </Button>
          </div>
        )}
      </div>

      {loading ? (
        <div className="space-y-3" aria-label={labels.loading}>
          {Array.from({ length: 4 }, (_, index) => (
            <Skeleton key={index} className="h-24 w-full rounded-[10px]" />
          ))}
        </div>
      ) : beats.length === 0 ? (
        <div className="flex min-h-[168px] flex-col items-center justify-center rounded-[10px] border border-white/[0.06] bg-white/[0.025] px-4 py-8 text-center">
          <p className="text-sm font-medium text-foreground/82">{labels.emptyTitle}</p>
          <p className="mt-1.5 text-xs text-muted-foreground/70">{labels.empty}</p>
        </div>
      ) : hasS9Segments ? (
        <div className="space-y-3">
          {s9PreviewMeta?.duration_assessment && (
            <div className="rounded-[12px] border border-white/[0.07] bg-[#13141b] p-3">
              <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
                <span className="text-xs font-semibold text-foreground">
                  {labels.durationAssessmentTitle ?? "【时长评估】"}
                </span>
                <Badge
                  variant="outline"
                  className="h-5 rounded-full border-emerald-400/25 bg-emerald-400/10 px-2 text-[11px] font-medium text-emerald-300"
                >
                  {s9PreviewMeta.duration_assessment.conclusion || "满足约束"}
                </Badge>
              </div>
              <div className="grid grid-cols-2 gap-x-4 gap-y-1.5 text-xs md:grid-cols-4">
                <div>
                  <span className="text-muted-foreground/65">语言：</span>
                  <span className="text-foreground/90">
                    {s9PreviewMeta.duration_assessment.language || "普通话"}
                  </span>
                </div>
                <div>
                  <span className="text-muted-foreground/65">分段约束：</span>
                  <span className="text-foreground/90">
                    {s9PreviewMeta.duration_assessment.constraint_summary ||
                      `单段上限${s9PreviewMeta.max_segment_seconds || 30}秒`}
                  </span>
                </div>
                <div>
                  <span className="text-muted-foreground/65">大段 / 总长：</span>
                  <span className="font-mono text-cyan-300">
                    {s9PreviewMeta.duration_assessment.segment_count || beats.length} 大段 /{" "}
                    {s9PreviewMeta.duration_assessment.estimated_total_seconds ||
                      Math.round(totalSeconds)}
                    秒
                  </span>
                </div>
                <div>
                  <span className="text-muted-foreground/65">画风 / 比例：</span>
                  <span className="text-foreground/90">
                    {s9PreviewMeta.visual_style_label || s9PreviewMeta.visual_style_id || "默认"} ·{" "}
                    {s9PreviewMeta.aspect_ratio || "9:16"}
                  </span>
                </div>
              </div>
              {s9PreviewMeta.duration_assessment.core_chain_seconds && (
                <p className="mt-1.5 text-[11px] text-muted-foreground/78">
                  {s9PreviewMeta.duration_assessment.core_chain_seconds}
                </p>
              )}
            </div>
          )}

          {s9PreviewMeta?.asset_manifest && s9PreviewMeta.asset_manifest.length > 0 && (
            <div className="rounded-[12px] border border-white/[0.07] bg-[#13141b] p-3">
              <div className="mb-2 text-xs font-semibold text-foreground">
                {labels.assetManifestTitle ?? "【素材准备清单】"}
              </div>
              <div className="flex flex-wrap gap-1.5">
                {s9PreviewMeta.asset_manifest.map((item, idx) => {
                  const isReady = item.status_label === "已就绪";
                  return (
                    <span
                      key={`${item.slot_label}-${idx}`}
                      className="inline-flex items-center gap-1.5 rounded-full border border-white/[0.08] bg-[#101118] px-2.5 py-0.5 text-[11px] text-foreground/86"
                    >
                      <span className="font-mono text-cyan-300">{item.slot_label}</span>
                      <span className="text-muted-foreground/70">
                        ({ASSET_KIND_LABEL[item.asset_kind] ?? item.asset_kind}: {item.name})
                      </span>
                      <span
                        className={cn(
                          "rounded-full px-1.5 py-0 text-[10px]",
                          isReady
                            ? "bg-emerald-400/15 text-emerald-300"
                            : "bg-amber-400/15 text-amber-300",
                        )}
                      >
                        {isReady
                          ? (labels.assetReady ?? "已就绪")
                          : (labels.assetPending ?? "待绑定")}
                      </span>
                    </span>
                  );
                })}
              </div>
            </div>
          )}

          {beats.map((beat) => (
            <S9SegmentPreviewCard
              key={beat.beat_number}
              beat={beat}
              subshots={resolveSegmentSubshots(beat)}
              meta={resolveSegmentMeta(beat)}
              labels={labels}
              saving={savingBeatNumber === beat.beat_number}
              regenerating={regeneratingBeatNumber === beat.beat_number}
              onUpdateSegmentSubshots={onUpdateSegmentSubshots}
              onRegenerateSegmentPrompt={onRegenerateSegmentPrompt}
            />
          ))}
        </div>
      ) : (
        <div className="space-y-3">
          {beats.map((beat) => (
            <ScriptBeatPreviewRow key={beat.beat_number} beat={beat} labels={labels} />
          ))}
        </div>
      )}
    </section>
  );
}

function S9SegmentPreviewCard({
  beat,
  subshots,
  meta,
  labels,
  saving,
  regenerating,
  onUpdateSegmentSubshots,
  onRegenerateSegmentPrompt,
}: {
  beat: Beat;
  subshots: S9SubShot[];
  meta: S9SegmentMeta | null;
  labels: ScriptBeatPreviewLabels;
  saving: boolean;
  regenerating: boolean;
  onUpdateSegmentSubshots?: (
    beatNumber: number,
    payload: { segment_title?: string; s9_subshots: S9SubShot[] },
  ) => void | Promise<void>;
  onRegenerateSegmentPrompt?: (beatNumber: number) => void | Promise<void>;
}) {
  const [editingShotNum, setEditingShotNum] = useState<number | null>(null);
  const [draftShot, setDraftShot] = useState<S9SubShot | null>(null);

  const plannedDuration = Math.round(
    Number(meta?.planned_duration_seconds || beat.duration_seconds || beat.estimated_duration || 15),
  );
  const promptReady = hasSegmentVideoPrompt(beat);
  const rawSegmentTitle = String(
    beat.segment_title || meta?.segment_title || "",
  ).trim();
  const segmentHeader = rawSegmentTitle
    ? rawSegmentTitle.startsWith(`大段${beat.beat_number}`)
      ? rawSegmentTitle
      : `大段${beat.beat_number} ｜ ${rawSegmentTitle}`
    : `大段${beat.beat_number}`;

  const beginEdit = (shot: S9SubShot) => {
    setEditingShotNum(shot.shot_number);
    setDraftShot({ ...shot });
  };

  const cancelEdit = () => {
    setEditingShotNum(null);
    setDraftShot(null);
  };

  const commitEdit = async () => {
    if (!draftShot || !onUpdateSegmentSubshots) return;
    const nextShots = subshots.map((item) =>
      item.shot_number === draftShot.shot_number ? draftShot : item,
    );
    await onUpdateSegmentSubshots(beat.beat_number, {
      segment_title: beat.segment_title,
      s9_subshots: nextShots,
    });
    setEditingShotNum(null);
    setDraftShot(null);
  };

  return (
    <div className="rounded-[12px] border border-white/[0.08] bg-[#13141b] px-3.5 py-3">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-white/[0.06] pb-2">
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          <Badge
            variant="outline"
            className="h-5 shrink-0 rounded-full border-cyan-400/25 bg-cyan-400/10 px-2 font-mono text-[11px] font-semibold text-cyan-300"
          >
            {segmentHeader}
          </Badge>
          <span className="rounded-full bg-[#101118] px-2 py-0.5 text-[11px] text-muted-foreground">
            {labels.shotCountBadge
              ? labels.shotCountBadge(subshots.length)
              : `${subshots.length} 个分镜`}
          </span>
          <span className="rounded-full bg-[#23334d] px-2.5 py-0.5 font-mono text-[11px] font-medium text-[#5ba0ff]">
            {labels.plannedDurationBadge
              ? labels.plannedDurationBadge(plannedDuration)
              : `计划段长 ${plannedDuration}秒`}
          </span>
          <Badge
            variant="outline"
            className={cn(
              "h-5 rounded-full px-2 text-[10px] font-medium",
              promptReady
                ? "border-emerald-400/25 bg-emerald-400/[0.08] text-emerald-300"
                : "border-amber-400/25 bg-amber-400/[0.08] text-amber-300",
            )}
          >
            {promptReady
              ? (labels.promptReady ?? "提示词已就绪")
              : (labels.promptPending ?? "提示词待生成")}
          </Badge>
        </div>

        {onRegenerateSegmentPrompt && (
          <Button
            type="button"
            variant="ghost"
            size="sm"
            disabled={regenerating || saving}
            onClick={() => void onRegenerateSegmentPrompt(beat.beat_number)}
            className="h-6 gap-1 rounded-full border border-white/[0.08] bg-[#0f1117] px-2.5 text-[11px] font-medium text-foreground/82 hover:bg-[#1f2a3f] hover:text-[#5ba0ff]"
          >
            {regenerating ? (
              <Loader2 className="size-3 animate-spin" />
            ) : (
              <Sparkles className="size-3" />
            )}
            {regenerating
              ? (labels.syncingPrompt ?? "更新中...")
              : (labels.syncPrompt ?? "更新视频提示词")}
          </Button>
        )}
      </div>

      {meta?.story_beat && (
        <p className="mt-2 text-xs leading-relaxed text-muted-foreground/85">
          {meta.story_beat}
        </p>
      )}

      <div className="mt-2.5 space-y-2">
        {subshots.map((shot) => {
          const isEditing = editingShotNum === shot.shot_number && draftShot !== null;
          const badgeTone = AUDIO_STYLE[shot.audio_type] ?? AUDIO_STYLE.silence;
          const shotTitle = shot.label || `大段${beat.beat_number}-分镜${shot.shot_number}`;
          return (
            <div
              key={shot.shot_number}
              className="rounded-[10px] border border-white/[0.055] bg-[#15161d] px-3 py-2.5"
            >
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="flex min-w-0 flex-wrap items-center gap-2">
                  <span className="text-xs font-semibold text-foreground/92">
                    {shotTitle}
                    {shot.title ? ` ｜ ${shot.title}` : ""}
                  </span>
                  <Badge
                    variant="outline"
                    className={cn(
                      "h-4.5 rounded-full px-1.5 text-[10px] font-medium",
                      badgeTone,
                    )}
                  >
                    {labels.audioType(shot.audio_type)}
                  </Badge>
                  {shot.speaker && (
                    <span className="rounded-full border border-white/[0.08] bg-white/[0.03] px-1.5 py-0 text-[10px] text-muted-foreground">
                      {shot.speaker}
                    </span>
                  )}
                </div>

                {onUpdateSegmentSubshots && !isEditing && (
                  <button
                    type="button"
                    onClick={() => beginEdit(shot)}
                    className="inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] text-muted-foreground/75 transition-colors hover:bg-white/[0.06] hover:text-foreground"
                  >
                    <Pencil className="size-3" />
                    {labels.editShot ?? "编辑"}
                  </button>
                )}
              </div>

              {isEditing && draftShot ? (
                <div className="mt-2 space-y-2 rounded-[8px] border border-white/[0.08] bg-[#0d0e14] p-2.5">
                  <div className="grid grid-cols-2 gap-2 md:grid-cols-5">
                    <label className="space-y-1 text-[11px] text-muted-foreground">
                      <span>小标题</span>
                      <Input
                        value={draftShot.title}
                        onChange={(e) =>
                          setDraftShot({ ...draftShot, title: e.target.value })
                        }
                        className="h-7 text-xs"
                      />
                    </label>
                    <label className="space-y-1 text-[11px] text-muted-foreground">
                      <span>计划秒数</span>
                      <Input
                        type="number"
                        step={0.5}
                        min={1}
                        max={30}
                        value={draftShot.planned_duration_seconds}
                        onChange={(e) =>
                          setDraftShot({
                            ...draftShot,
                            planned_duration_seconds: Math.max(
                              1,
                              Number(e.target.value || 1),
                            ),
                          })
                        }
                        className="h-7 font-mono text-xs"
                      />
                    </label>
                    <label className="space-y-1 text-[11px] text-muted-foreground">
                      <span>景别</span>
                      <Input
                        value={draftShot.shot_scale_movement}
                        onChange={(e) =>
                          setDraftShot({
                            ...draftShot,
                            shot_scale_movement: e.target.value,
                          })
                        }
                        className="h-7 text-xs"
                      />
                    </label>
                    <label className="space-y-1 text-[11px] text-muted-foreground">
                      <span>视角</span>
                      <Input
                        value={draftShot.camera_angle}
                        onChange={(e) =>
                          setDraftShot({ ...draftShot, camera_angle: e.target.value })
                        }
                        className="h-7 text-xs"
                      />
                    </label>
                    <label className="space-y-1 text-[11px] text-muted-foreground">
                      <span>运镜</span>
                      <Input
                        value={draftShot.camera_motion}
                        onChange={(e) =>
                          setDraftShot({ ...draftShot, camera_motion: e.target.value })
                        }
                        className="h-7 text-xs"
                      />
                    </label>
                  </div>
                  <label className="block space-y-1 text-[11px] text-muted-foreground">
                    <span>分镜内容</span>
                    <textarea
                      rows={3}
                      value={draftShot.content}
                      onChange={(e) =>
                        setDraftShot({ ...draftShot, content: e.target.value })
                      }
                      className="w-full rounded-[8px] border border-white/[0.1] bg-[#13141b] px-2.5 py-1.5 text-xs leading-relaxed text-foreground outline-none focus:border-[var(--accent)]/60"
                    />
                  </label>
                  <label className="block space-y-1 text-[11px] text-muted-foreground">
                    <span>台词 / 内心独白（原句保留）</span>
                    <Input
                      value={draftShot.spoken_text}
                      onChange={(e) =>
                        setDraftShot({ ...draftShot, spoken_text: e.target.value })
                      }
                      className="h-7 text-xs"
                    />
                  </label>
                  <div className="flex justify-end gap-1.5">
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      disabled={saving}
                      onClick={cancelEdit}
                      className="h-7 gap-1 px-2.5 text-xs"
                    >
                      <X className="size-3" />
                      {labels.cancelEdit ?? "取消"}
                    </Button>
                    <Button
                      type="button"
                      size="sm"
                      disabled={saving}
                      onClick={() => void commitEdit()}
                      className="h-7 gap-1 px-2.5 text-xs"
                    >
                      {saving ? (
                        <Loader2 className="size-3 animate-spin" />
                      ) : (
                        <Check className="size-3" />
                      )}
                      {labels.saveShot ?? "保存分镜"}
                    </Button>
                  </div>
                </div>
              ) : (
                <div className="mt-1.5 space-y-1.5 text-xs">
                  <div className="flex flex-wrap items-center gap-1.5 text-foreground/82">
                    <span className="font-medium text-muted-foreground/80">
                      {labels.shotBasicInfoLabel ?? "镜头基础信息："}
                    </span>
                    <span className="font-mono text-cyan-300">
                      计划{formatSec(shot.planned_duration_seconds)}秒
                    </span>
                    <span className="text-muted-foreground/45">｜</span>
                    <span>{shot.shot_scale_movement || "中景"}</span>
                    <span className="text-muted-foreground/45">｜</span>
                    <span>{shot.camera_angle || "平视"}</span>
                    <span className="text-muted-foreground/45">｜</span>
                    <span>{shot.camera_motion || "固定机位"}</span>
                    <span className="ml-auto font-mono text-[10px] text-muted-foreground/60">
                      {formatSec(shot.start_seconds)}s–{formatSec(shot.end_seconds)}s
                    </span>
                  </div>
                  <div className="leading-relaxed text-foreground/88">
                    <span className="font-medium text-muted-foreground/80">
                      {labels.shotContentLabel ?? "分镜内容："}
                    </span>
                    {shot.content}
                    {shot.spoken_text && (
                      <span className="ml-1.5 text-emerald-300/95">
                        「{shot.speaker ? `${shot.speaker}：` : ""}
                        {shot.spoken_text}」
                      </span>
                    )}
                  </div>
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

function ScriptBeatPreviewRow({
  beat,
  labels,
}: {
  beat: Beat;
  labels: ScriptBeatPreviewLabels;
}) {
  const audioType = beat.audio_type || "silence";
  const isSilent =
    audioType === "silence" || audioType === "action" || Boolean(beat.is_manual_shot);
  const badgeTone = AUDIO_STYLE[audioType] ?? AUDIO_STYLE.silence;

  return (
    <div className="rounded-[10px] border border-white/[0.06] bg-white/[0.025] px-3.5 py-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-xs font-semibold tabular-nums text-foreground/85">
          #{beat.beat_number}
        </span>
        <Badge
          variant="outline"
          className={cn(
            "h-5 gap-1 rounded-md px-1.5 text-[11px] font-medium shadow-none",
            badgeTone,
          )}
        >
          {isSilent ? <VolumeX className="size-3" /> : <Volume2 className="size-3" />}
          {labels.audioType(audioType)}
        </Badge>
        {!isSilent && (
          <span className="rounded-md border border-white/[0.07] bg-white/[0.03] px-1.5 py-0.5 text-[11px] text-muted-foreground">
            {beat.speaker?.trim() || labels.noSpeaker}
          </span>
        )}
      </div>

      <div
        className={cn(
          "mt-2.5 grid gap-2.5",
          isSilent ? "grid-cols-1" : "sm:grid-cols-2",
        )}
      >
        {!isSilent && (
          <div className="rounded-[8px] border border-cyan-400/12 bg-cyan-400/[0.03] px-3 py-2">
            <p className="mb-1 flex items-center gap-1.5 text-[11px] font-medium text-cyan-200/80">
              <Volume2 className="size-3 shrink-0" />
              {audioType === "dialogue" ? labels.dialogueLine : labels.narrationLine}
            </p>
            <p className="text-xs leading-relaxed text-foreground/86 whitespace-pre-wrap">
              {beat.narration_segment?.trim() || (
                <span className="text-muted-foreground/60">{labels.noNarration}</span>
              )}
            </p>
          </div>
        )}

        <div className="rounded-[8px] border border-white/[0.05] bg-white/[0.02] px-3 py-2">
          <p className="mb-1 flex items-center gap-1.5 text-[11px] font-medium text-muted-foreground/80">
            <Film className="size-3 shrink-0" />
            {labels.visualDescription}
          </p>
          <p className="text-xs leading-relaxed text-foreground/78 whitespace-pre-wrap">
            {beat.visual_description?.trim() || (
              <span className="text-muted-foreground/60">{labels.noVisualDescription}</span>
            )}
          </p>
        </div>
      </div>
    </div>
  );
}
