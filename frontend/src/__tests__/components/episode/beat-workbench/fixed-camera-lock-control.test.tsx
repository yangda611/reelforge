// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { I18nextProvider, initReactI18next } from "react-i18next";
import i18next from "i18next";
import { beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { FixedCameraLockControl } from "@/components/episode/beat-workbench/fixed-camera-lock-control";
import type { Beat } from "@/types/episode";

const mutateAsyncMock = vi.fn();

vi.mock("@/lib/queries/scripts", () => ({
  useScript: () => ({
    data: {
      ok: true,
      data: {
        beats: [
          { beat_number: 1, narration_segment: "", visual_description: "" },
          { beat_number: 2, narration_segment: "", visual_description: "" },
        ],
      },
    },
  }),
  useUpdateBeat: () => ({
    mutateAsync: mutateAsyncMock,
    isPending: false,
  }),
}));

const i18n = i18next.createInstance();

beforeAll(async () => {
  await i18n.use(initReactI18next).init({
    lng: "zh",
    fallbackLng: "zh",
    interpolation: { escapeValue: false },
    resources: {
      zh: {
        translation: {
          episode: {
            workbench: {
              spatialLock: {
                label: "固定机位空间锁定",
                idleHint: "未锁定：重绘/渲染与相邻镜自由运镜",
                activeHint: "锚定 #B{{n}} 底图与地面站位，生成视频保持稳定三脚架机位",
                modeOff: "不锁定",
                modeFullBody: "锁定机位·全身",
                modeUpperBody: "锁定机位·仅改上半身",
                anchorLabel: "锚定源镜头",
                anchorItem: "锚定 #B{{n}}",
                lockedToast: "已锁定到 #B{{n}} 机位与底图空间",
                unlockedToast: "已取消固定机位锁定",
                saveFailed: "保存固定机位设置失败",
              },
            },
          },
        },
      },
    },
  });
});

beforeEach(() => {
  mutateAsyncMock.mockReset();
  mutateAsyncMock.mockResolvedValue({ ok: true, data: {} });
});

describe("FixedCameraLockControl", () => {
  it("hides on Beat #1 when unlocked and renders on Beat #2+", () => {
    const beat1: Beat = {
      beat_number: 1,
      narration_segment: "",
      visual_description: "",
      scene_ref: { scene_id: "store" },
    };
    const { container, rerender } = render(
      <I18nextProvider i18n={i18n}>
        <FixedCameraLockControl beat={beat1} project="demo" episode={1} />
      </I18nextProvider>,
    );
    expect(container.firstChild).toBeNull();

    const beat2: Beat = {
      beat_number: 2,
      narration_segment: "",
      visual_description: "",
      scene_ref: { scene_id: "store" },
    };
    rerender(
      <I18nextProvider i18n={i18n}>
        <FixedCameraLockControl beat={beat2} project="demo" episode={1} />
      </I18nextProvider>,
    );
    expect(screen.getByText("固定机位空间锁定")).toBeTruthy();
    expect(screen.getByText("不锁定")).toBeTruthy();
  });

  it("persists lock_camera_beat and lock_spatial_mode cleanly and clears on unlock", async () => {
    const beat2Locked: Beat = {
      beat_number: 2,
      narration_segment: "",
      visual_description: "",
      scene_ref: {
        scene_id: "store",
        render_anchor_id: "selected_background",
        lock_camera_beat: 1,
        lock_spatial_mode: "upper_body",
      },
    };
    render(
      <I18nextProvider i18n={i18n}>
        <FixedCameraLockControl beat={beat2Locked} project="demo" episode={1} />
      </I18nextProvider>,
    );

    expect(screen.getByText("锁定机位·仅改上半身")).toBeTruthy();
    expect(screen.getByText("锚定 #B1")).toBeTruthy();

    const user = userEvent.setup();
    await user.click(screen.getByRole("combobox", { name: "固定机位空间锁定" }));
    await user.click(await screen.findByRole("option", { name: "不锁定" }));

    await waitFor(() => {
      expect(mutateAsyncMock).toHaveBeenCalledWith({
        beatNum: 2,
        data: {
          scene_ref: {
            scene_id: "store",
            render_anchor_id: "selected_background",
            lock_spatial_mode: "",
          },
        },
      });
    });
  });
});
