// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string) =>
      ({
        "app.versionUpdate.introTitle": "Welcome to ReelForge",
        "app.versionUpdate.introBody": "Turn a novel or an idea into a short drama.",
        "app.versionUpdate.introStep1": "Import a novel or write a script",
        "app.versionUpdate.introStep2": "Generate the storyboard",
        "app.versionUpdate.introStep3": "Render and share the short drama",
        "app.versionUpdate.introStart": "Start creating",
      })[key] ?? key,
    i18n: {
      language: "en",
      resolvedLanguage: "en",
    },
  }),
}));

vi.mock("@/components/ui/dialog", () => ({
  Dialog: ({ open, children }: React.PropsWithChildren<{ open: boolean }>) =>
    open ? <div role="dialog">{children}</div> : null,
  DialogContent: ({ children }: React.PropsWithChildren) => <div>{children}</div>,
  DialogTitle: ({ children }: React.PropsWithChildren) => <h2>{children}</h2>,
}));

vi.mock("@/components/ui/button", () => ({
  Button: ({ children, ...props }: React.ComponentProps<"button">) => (
    <button type="button" {...props}>
      {children}
    </button>
  ),
}));

import { VersionUpdateDialog } from "@/features/version-update/VersionUpdateDialog";

describe("VersionUpdateDialog product intro behavior", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  // P2-1：首次运行弹的是产品简介，看完一次后不再打扰。
  it("shows the product intro on first run and stays hidden afterward", async () => {
    const first = render(<VersionUpdateDialog />);

    expect(await screen.findByText("Welcome to ReelForge")).toBeInTheDocument();

    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Start creating" }));
    expect(localStorage.getItem("reelforge:product-intro-seen")).toBe("seen");

    await waitFor(() => {
      expect(screen.queryByText("Welcome to ReelForge")).not.toBeInTheDocument();
    });

    first.unmount();
    render(<VersionUpdateDialog />);

    await waitFor(() => {
      expect(screen.queryByText("Welcome to ReelForge")).not.toBeInTheDocument();
    });
  });
});
