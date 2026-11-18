// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { I18nextProvider, initReactI18next } from "react-i18next";
import i18next from "i18next";
import { beforeAll, describe, expect, it, vi } from "vitest";

import { ViewToggles } from "@/components/episode/beat-workbench/view-toggles";

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
              view: {
                selectAll: "全选",
                selectedCount: "已选 {{count}}",
                clear: "清除",
                totalShots: "共{{count}}个镜头",
              },
            },
          },
        },
      },
    },
  });
});

describe("ViewToggles", () => {
  it("shows select all for partial selection together with the clear action", async () => {
    const user = userEvent.setup();
    const onSelectAll = vi.fn();
    const onClearSelection = vi.fn();

    render(
      <I18nextProvider i18n={i18n}>
        <ViewToggles
          selection={{ mode: "multi", checked: new Set([1]) }}
          totalBeats={3}
          onSelectAll={onSelectAll}
          onClearSelection={onClearSelection}
        />
      </I18nextProvider>,
    );

    expect(screen.getByText("已选 1")).toBeInTheDocument();
    expect(screen.getByText("共3个镜头")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "全选" }));
    expect(onSelectAll).toHaveBeenCalledTimes(1);

    await user.click(screen.getByRole("button", { name: "清除" }));
    expect(onClearSelection).toHaveBeenCalledTimes(1);
  });

  it("hides select all once every beat is already checked", () => {
    render(
      <I18nextProvider i18n={i18n}>
        <ViewToggles
          selection={{ mode: "multi", checked: new Set([1, 2, 3]) }}
          totalBeats={3}
          onSelectAll={vi.fn()}
          onClearSelection={vi.fn()}
        />
      </I18nextProvider>,
    );

    expect(screen.getByText("已选 3")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "全选" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "清除" })).toBeInTheDocument();
  });

  it("shows only select all when no beat is checked", async () => {
    const user = userEvent.setup();
    const onSelectAll = vi.fn();
    render(
      <I18nextProvider i18n={i18n}>
        <ViewToggles
          selection={{ mode: "none" }}
          totalBeats={3}
          onSelectAll={onSelectAll}
          onClearSelection={vi.fn()}
        />
      </I18nextProvider>,
    );

    await user.click(screen.getByRole("button", { name: "全选" }));
    expect(onSelectAll).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("button", { name: "清除" })).not.toBeInTheDocument();
  });

  it("shows neither selection action for single-beat selection", () => {
    render(
      <I18nextProvider i18n={i18n}>
        <ViewToggles
          selection={{ mode: "single", beatNum: 1 }}
          totalBeats={3}
          onSelectAll={vi.fn()}
          onClearSelection={vi.fn()}
        />
      </I18nextProvider>,
    );

    expect(screen.getByRole("button", { name: "全选" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "清除" })).not.toBeInTheDocument();
  });
});