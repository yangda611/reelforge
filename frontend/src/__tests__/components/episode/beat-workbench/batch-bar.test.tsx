// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { render, screen } from "@testing-library/react";
import { I18nextProvider, initReactI18next } from "react-i18next";
import i18next from "i18next";
import { beforeAll, describe, expect, it, vi } from "vitest";

import { BatchBar } from "@/components/episode/beat-workbench/batch-bar";

const i18n = i18next.createInstance();

beforeAll(async () => {
  await i18n.use(initReactI18next).init({
    lng: "zh",
    fallbackLng: "zh",
    interpolation: { escapeValue: false },
    resources: {
      zh: {
        translation: {
          common: {
            cancel: "取消",
            confirmExecute: "确认执行",
            error: "错误",
            ok: "OK",
            billingRuleNotConfiguredShort: "需配置",
          },
          episode: {
            workbench: {
              batch: {
                toolbar: "批量操作",
                genAudio: "生成音频",
                genAudioTitle: "生成音频？",
                genAudioDesc: "确认生成音频？",
                videoModel: "视频模型",
              },
              video: {
                ratio: "画幅",
              },
            },
          },
        },
      },
    },
  });
});

vi.mock("@/lib/queries/audio", () => ({
  useAudioBillingQuote: () => ({
    data: {
      ok: true,
      data: {
        beat_numbers: [1, 2, 3],
        quantity: 3,
        unit_cost: 6,
        cost: 18,
        display: "18",
        prereq_errors: [],
      },
    },
    error: null,
  }),
  useGenerateAudio: () => ({
    mutateAsync: vi.fn(),
    isPending: false,
  }),
}));

vi.mock("@/lib/queries/video", () => ({
  useVideoBackends: () => ({
    data: {
      ok: true,
      data: [
        {
          value: "seedance",
          label: "Seedance 1.0",
          is_default: true,
          is_seedance2: false,
          dialogue_only: false,
        },
        {
          value: "huimeng_seedance-2.0-fast",
          label: "Seedance 2.0 Fast",
          is_default: false,
          is_seedance2: true,
          dialogue_only: false,
        },
      ],
    },
  }),
}));

vi.mock("@/hooks/use-task-controller", () => ({
  useTaskController: () => ({
    started: false,
    stopping: false,
    stream: { status: "idle" },
    start: vi.fn(),
    stop: vi.fn(),
  }),
}));

const BEATS = [
  {
    beat_number: 1,
    narration_segment: "n1",
    visual_description: "v1",
    scene_ref: { scene_id: "store" },
    is_manual_shot: true,
  },
  {
    beat_number: 2,
    narration_segment: "n2",
    visual_description: "v2",
    scene_ref: { scene_id: "store" },
    audio_type: "narration",
    is_manual_shot: false,
  },
];

function renderBar(
  props: Partial<Parameters<typeof BatchBar>[0]> = {},
) {
  return render(
    <I18nextProvider i18n={i18n}>
      <BatchBar
        project="demo"
        episode={1}
        beats={BEATS}
        videoBackend="seedance"
        aspectRatio="2:3"
        onAspectRatioChange={vi.fn()}
        {...props}
      />
    </I18nextProvider>,
  );
}

describe("BatchBar", () => {
  it("shows the project aspect ratio select in the toolbar", () => {
    renderBar({ aspectRatio: "16:9" });

    const select = screen.getByRole("combobox", { name: "画幅" });
    expect(select).toHaveTextContent("16:9");
  });

  it("keeps the video model out of the top batch toolbar", () => {
    renderBar();

    expect(screen.queryByText("视频模型")).not.toBeInTheDocument();
  });

  it.each([
    ["narrated" as const, "seedance"],
    ["narrated" as const, "huimeng_seedance-2.0-fast"],
    ["drama" as const, "seedance"],
  ])(
    "hides whole-episode audio from the batch toolbar (%s / %s)",
    (spineTemplate, videoBackend) => {
      renderBar({ spineTemplate, videoBackend });

      expect(screen.queryByRole("button", { name: "生成音频" })).not.toBeInTheDocument();
    },
  );
});