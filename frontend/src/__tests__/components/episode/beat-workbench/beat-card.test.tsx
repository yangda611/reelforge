// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { BeatCard } from "@/components/episode/beat-workbench/beat-card";
import type { Beat } from "@/types/episode";

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string, vars?: Record<string, string | number>) => {
      if (key === "episode.beat.select") return "选择 Beat";
      if (key === "episode.beat.deselect") return "取消选择 Beat";
      if (key === "episode.beat.insertAfter") return `在第 ${vars?.n} 个 beat 后插入`;
      if (key === "episode.beat.openFreezone") return "进入虾画 Beat 工作台";
      if (key === "episode.beat.insertBeforeShort") return "前插";
      if (key === "episode.beat.s9Badge") return `${vars?.seconds}s · ${vars?.shots}镜`;
      return key;
    },
  }),
}));

const beat = {
  beat_number: 4,
  narration_segment: "发现密室里的书",
  visual_description: "主角翻开旧书",
} as Beat;

function renderBeatCard(overrides: Partial<Parameters<typeof BeatCard>[0]> = {}) {
  const props = {
    beat,
    displayNumber: beat.beat_number,
    isSelected: false,
    isChecked: false,
    onCardClick: vi.fn(),
    onCheckboxClick: vi.fn(),
    ...overrides,
  };

  render(<BeatCard {...props} />);

  return props;
}

describe("BeatCard", () => {
  it("keeps the multi-select control visible without hover-only opacity over thumbnails", () => {
    renderBeatCard();

    const selectButton = screen.getByRole("button", { name: "选择 Beat" });
    expect(selectButton.className).toContain("size-5");
    expect(selectButton.className).not.toContain("opacity-40");
    expect(selectButton.className).not.toContain("group-hover:opacity-100");
  });

  it("does not reveal the check icon on hover before the card is selected", () => {
    renderBeatCard();

    const selectButton = screen.getByRole("button", { name: "选择 Beat" });
    expect(selectButton.className).toContain("text-transparent");
    expect(selectButton.className).toContain("hover:border-white/[0.30]");
    expect(selectButton.className).toContain("hover:bg-black/50");
    expect(selectButton.className).not.toContain("hover:text-primary");
  });

  it("shows the check icon only after the card is checked", () => {
    renderBeatCard({ isChecked: true });

    const selectButton = screen.getByRole("button", { name: "取消选择 Beat" });
    expect(selectButton.className).toContain("text-primary");
    expect(selectButton.className).not.toContain("text-transparent");
  });

  it("toggles multi-select from the card control without selecting the card body", async () => {
    const user = userEvent.setup();
    const { onCardClick, onCheckboxClick } = renderBeatCard();

    await user.click(screen.getByRole("button", { name: "选择 Beat" }));

    expect(onCheckboxClick).toHaveBeenCalledWith(4);
    expect(onCardClick).not.toHaveBeenCalled();
  });

  it("shows the beat narration and visual description in the card body", () => {
    renderBeatCard();

    expect(screen.getByText("发现密室里的书")).toBeInTheDocument();
    expect(screen.getByText("主角翻开旧书")).toBeInTheDocument();
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
  });

  it("selects the card from its text body", async () => {
    const user = userEvent.setup();
    const { onCardClick } = renderBeatCard();

    await user.click(screen.getByText("发现密室里的书"));

    expect(onCardClick).toHaveBeenCalledWith(4);
  });

  it("opens the beat freezone workbench from the always-visible action without selecting the card", async () => {
    const user = userEvent.setup();
    const onOpenFreezone = vi.fn();
    const { onCardClick } = renderBeatCard({ onOpenFreezone });

    await user.click(screen.getByRole("button", { name: "进入虾画 Beat 工作台" }));

    expect(onOpenFreezone).toHaveBeenCalledWith(4);
    expect(onCardClick).not.toHaveBeenCalled();
    expect(screen.queryByText("进入虾画 Beat 工作台")).not.toBeInTheDocument();
  });

  it("shows insert actions even when the beat has no text or media", async () => {
    const user = userEvent.setup();
    const onInsertBefore = vi.fn();
    renderBeatCard({
      beat: { beat_number: 4, narration_segment: "", visual_description: "" } as Beat,
      onInsertBefore,
    });

    expect(screen.getByText("episode.beat.noNarration")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "episode.beat.insertBefore" }));

    expect(onInsertBefore).toHaveBeenCalledWith(4);
  });
  it("renders s9 planned duration and subshot count badge next to beat badge when present", () => {
    renderBeatCard({
      beat: {
        ...beat,
        segment_title: "大段1 · 便利店夜哨",
        estimated_duration: 18,
        s9_segment_meta: {
          segment_number: 1,
          segment_title: "大段1 · 便利店夜哨",
          planned_duration_seconds: 18,
          max_segment_seconds: 30,
          story_beat: "守夜遇不速之客",
          tail_anchor: false,
          include_first_frame: false,
          segment_asset_slots: [],
        },
        s9_subshots: [
          { shot_number: 1 } as never,
          { shot_number: 2 } as never,
          { shot_number: 3 } as never,
          { shot_number: 4 } as never,
        ],
      } as Beat,
    });

    expect(screen.getByText("episode.beat.badge")).toBeInTheDocument();
    expect(screen.getByText("18s · 4镜")).toBeInTheDocument();
  });
});
