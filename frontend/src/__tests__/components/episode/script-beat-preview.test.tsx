// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ScriptBeatPreview } from "@/components/episode/script-beat-preview";
import type { Beat } from "@/types/episode";

const labels = {
  title: "脚本预览",
  count: (count: number) => `${count} 个 Beat`,
  loading: "加载脚本中...",
  emptyTitle: "暂无脚本",
  empty: "暂无脚本 beat",
  audioType: (type: string) =>
    ({
      narration: "旁白",
      dialogue: "对白",
      silence: "无声镜头",
      action: "动作镜头",
    })[type] ?? type,
  speaker: "说话人",
  noSpeaker: "无说话人",
  dialogueLine: "对白台词",
  narrationLine: "旁白台词",
  noNarration: "暂无台词",
  visualDescription: "画面描述",
  noVisualDescription: "暂无画面描述",
  segmentCountSummary: (segments: number, shots: number, totalSeconds: number) =>
    `${segments} 大段 · ${shots} 镜 · 约 ${totalSeconds}s`,
  durationAssessmentTitle: "时长评估",
  assetManifestTitle: "素材准备清单",
  plannedDurationBadge: (seconds: number) => `计划段长 ${seconds} 秒`,
  shotCountBadge: (shots: number) => `${shots} 个分镜`,
  shotBasicInfoLabel: "镜头基础信息：",
  shotContentLabel: "分镜内容：",
  editShot: "编辑分镜",
  saveShot: "保存分镜",
  cancelEdit: "取消",
  syncPrompt: "更新视频提示词",
  syncingPrompt: "更新中...",
  assetReady: "已就绪",
  assetPending: "待绑定",
};

function makeBeat(overrides: Partial<Beat>): Beat {
  return {
    beat_number: 1,
    narration_segment: "旁白文案",
    visual_description: "画面一",
    audio_type: "narration",
    speaker: "",
    ...overrides,
  };
}

describe("ScriptBeatPreview", () => {
  it("renders each beat with audio type, speaker, dialogue/narration line, and visual description", () => {
    render(
      <ScriptBeatPreview
        labels={labels}
        beats={[
          makeBeat({ beat_number: 1 }),
          makeBeat({
            beat_number: 2,
            narration_segment: "对白文案",
            visual_description: "画面二",
            audio_type: "dialogue",
            speaker: "陈锋_和尚",
          }),
        ]}
      />,
    );

    expect(screen.getByText("脚本预览")).toBeInTheDocument();
    expect(screen.getByText("(2 个 Beat)")).toBeInTheDocument();
    expect(screen.getByText("#1")).toBeInTheDocument();
    expect(screen.getByText("#2")).toBeInTheDocument();
    expect(screen.getByText("旁白")).toBeInTheDocument();
    expect(screen.getByText("对白")).toBeInTheDocument();
    expect(screen.getByText("无说话人")).toBeInTheDocument();
    expect(screen.getByText("陈锋_和尚")).toBeInTheDocument();
    // Narration beat shows the 旁白台词 label; dialogue beat shows 对白台词.
    expect(screen.getByText("旁白台词")).toBeInTheDocument();
    expect(screen.getByText("对白台词")).toBeInTheDocument();
    expect(screen.getByText("旁白文案")).toBeInTheDocument();
    expect(screen.getByText("对白文案")).toBeInTheDocument();
    expect(screen.getAllByText("画面描述")).toHaveLength(2);
    expect(screen.getByText("画面一")).toBeInTheDocument();
    expect(screen.getByText("画面二")).toBeInTheDocument();
  });

  it("shows an empty state when no beats exist", () => {
    render(<ScriptBeatPreview labels={labels} beats={[]} />);

    expect(screen.getByText("脚本预览")).toBeInTheDocument();
    expect(screen.getByText("(0 个 Beat)")).toBeInTheDocument();
    expect(screen.getByText("暂无脚本 beat")).toBeInTheDocument();
  });

  it("hides narration for silence, action, and manual beats like NiceGUI", () => {
    render(
      <ScriptBeatPreview
        labels={labels}
        beats={[
          makeBeat({
            beat_number: 1,
            audio_type: "silence",
            narration_segment: "静音不显示",
          }),
          makeBeat({
            beat_number: 2,
            audio_type: "action",
            narration_segment: "动作不显示",
          }),
          makeBeat({
            beat_number: 3,
            is_manual_shot: true,
            narration_segment: "手工镜头不显示",
          }),
        ]}
      />,
    );

    expect(screen.queryByText("静音不显示")).not.toBeInTheDocument();
    expect(screen.queryByText("动作不显示")).not.toBeInTheDocument();
    expect(screen.queryByText("手工镜头不显示")).not.toBeInTheDocument();
    // No spoken-line column (and no speaker badge) for silent/action/manual beats.
    expect(screen.queryAllByText("对白台词")).toHaveLength(0);
    expect(screen.queryAllByText("旁白台词")).toHaveLength(0);
  });

  it("renders s9 Stage 1 duration assessment, asset manifest, segment cards, and inline sub-shot edit + prompt sync", async () => {
    const onUpdateSegmentSubshots = vi.fn().mockResolvedValue(undefined);
    const onRegenerateSegmentPrompt = vi.fn().mockResolvedValue(undefined);
    render(
      <ScriptBeatPreview
        labels={labels}
        s9PreviewMeta={{
          version: 1,
          generation_mode: "s9",
          max_segment_seconds: 30,
          tail_anchor: false,
          visual_style_id: "chinese_period_drama",
          visual_style_label: "暗黑悬疑",
          aspect_ratio: "9:16",
          duration_assessment: {
            language: "普通话",
            constraint_summary: "单段上限30秒",
            core_chain_seconds: "约 18 秒",
            estimated_total_seconds: 18,
            segment_count: 1,
            conclusion: "满足约束",
          },
          asset_manifest: [
            {
              slot_label: "@图片1",
              media_type: "image",
              asset_kind: "identity",
              name: "林默_青年",
              asset_id: "林默_青年",
              status_label: "已就绪",
            },
            {
              slot_label: "@图片2",
              media_type: "image",
              asset_kind: "scene",
              name: "深夜便利店",
              asset_id: "scene_store",
              status_label: "待绑定",
            },
          ],
        }}
        beats={[
          makeBeat({
            beat_number: 1,
            segment_title: "便利店夜哨",
            estimated_duration: 18,
            s9_segment_meta: {
              segment_number: 1,
              segment_title: "便利店夜哨",
              planned_duration_seconds: 18,
              max_segment_seconds: 30,
              story_beat: "林默收银台敲代码遇黑衣人推门",
              tail_anchor: false,
              include_first_frame: false,
              segment_asset_slots: [],
            },
            s9_subshots: [
              {
                shot_number: 1,
                label: "大段1-分镜1",
                title: "雨夜便利店与代码",
                planned_duration_seconds: 6,
                start_seconds: 0,
                end_seconds: 6,
                shot_scale_movement: "中景转特写",
                camera_angle: "平视",
                camera_motion: "缓慢推近",
                content: "玻璃门外霓虹闪烁，绿码在显示器飞速滚动。",
                audio_type: "silence",
                speaker: "",
                spoken_text: "",
                identity_ids: ["林默_青年"],
                scene_id: "scene_store",
                scene_variant_id: "",
                prop_ids: [],
              },
              {
                shot_number: 2,
                label: "大段1-分镜2",
                title: "不速之客",
                planned_duration_seconds: 12,
                start_seconds: 6,
                end_seconds: 18,
                shot_scale_movement: "中全景",
                camera_angle: "平视",
                camera_motion: "固定机位",
                content: "黑衣男人压低帽檐走进收银台前。",
                audio_type: "dialogue",
                speaker: "黑衣男人",
                spoken_text: "一包薄荷烟。",
                identity_ids: [],
                scene_id: "scene_store",
                scene_variant_id: "",
                prop_ids: [],
              },
            ],
          }),
        ]}
        onUpdateSegmentSubshots={onUpdateSegmentSubshots}
        onRegenerateSegmentPrompt={onRegenerateSegmentPrompt}
      />,
    );

    expect(screen.getByText(/时长评估/)).toBeInTheDocument();
    expect(screen.getByText(/素材准备清单/)).toBeInTheDocument();
    expect(screen.getByText(/@图片1/)).toBeInTheDocument();
    expect(screen.getByText("已就绪")).toBeInTheDocument();
    expect(screen.getByText("待绑定")).toBeInTheDocument();
    expect(screen.getByText("大段1 ｜ 便利店夜哨")).toBeInTheDocument();
    expect(screen.getByText("计划段长 18 秒")).toBeInTheDocument();
    expect(screen.getByText("2 个分镜")).toBeInTheDocument();
    expect(screen.getByText(/大段1-分镜1/)).toBeInTheDocument();

    fireEvent.click(screen.getAllByRole("button", { name: "编辑分镜" })[0]);
    const contentBox = screen.getByDisplayValue("玻璃门外霓虹闪烁，绿码在显示器飞速滚动。");
    fireEvent.change(contentBox, {
      target: { value: "玻璃门外暴雨倾盆，霓虹冷光映在显示器上。" },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存分镜" }));

    await waitFor(() => expect(onUpdateSegmentSubshots).toHaveBeenCalledTimes(1));
    expect(onUpdateSegmentSubshots.mock.calls[0][0]).toBe(1);
    expect(onUpdateSegmentSubshots.mock.calls[0][1].s9_subshots[0].content).toBe(
      "玻璃门外暴雨倾盆，霓虹冷光映在显示器上。",
    );

    fireEvent.click(screen.getByRole("button", { name: "更新视频提示词" }));
    await waitFor(() => expect(onRegenerateSegmentPrompt).toHaveBeenCalledWith(1));
  });

  it("displays prompt readiness badges and triggers batch fill missing prompts", async () => {
    const onBatchGeneratePrompts = vi.fn();
    const makeSegmentBeat = (num: number, finalPrompt: string): Beat => ({
      beat_number: num,
      narration_segment: "",
      visual_description: `大段 ${num}`,
      segment_title: `大段${num} · 测试`,
      estimated_duration: 10,
      duration_seconds: 10,
      seedance2_config_json: JSON.stringify({ final_prompt: finalPrompt }),
      s9_subshots: [
        {
          shot_number: 1,
          label: `大段${num}-分镜1`,
          title: "镜头",
          planned_duration_seconds: 10,
          start_seconds: 0,
          end_seconds: 10,
          shot_scale_movement: "中景",
          camera_angle: "平视",
          camera_motion: "固定机位",
          content: "镜头推进",
          audio_type: "silence",
          speaker: "",
          spoken_text: "",
          identity_ids: [],
          scene_id: "scene_store",
          scene_variant_id: "",
          prop_ids: [],
        },
      ],
    });

    render(
      <ScriptBeatPreview
        labels={{
          ...labels,
          batchFillMissingPrompts: (count) => `补全缺失提示词 (${count})`,
          batchRegenPrompts: "批量更新视频提示词",
          promptReady: "提示词已就绪",
          promptPending: "提示词待生成",
        }}
        beats={[
          makeSegmentBeat(1, "完整视频提示词"),
          makeSegmentBeat(2, ""),
        ]}
        onBatchGeneratePrompts={onBatchGeneratePrompts}
      />,
    );

    expect(screen.getByText("提示词已就绪")).toBeInTheDocument();
    expect(screen.getByText("提示词待生成")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /补全缺失提示词 \(1\)/ }));
    await waitFor(() => expect(onBatchGeneratePrompts).toHaveBeenCalledWith(true));

    fireEvent.click(screen.getByRole("button", { name: "批量更新视频提示词" }));
    await waitFor(() => expect(onBatchGeneratePrompts).toHaveBeenCalledWith(false));
  });
});
