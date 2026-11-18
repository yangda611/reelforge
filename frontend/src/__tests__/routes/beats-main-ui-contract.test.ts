// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

function read(path: string) {
  return readFileSync(path, "utf8");
}

describe("beats workbench contract", () => {
  it("keeps the main branch split shell without grid galleries or plan dialogs", () => {
    const route = read(
      "src/routes/_app/projects.$project/episodes.$episode/beats.lazy.tsx",
    );

    expect(route).toContain("<BatchBar");
    expect(route).toContain("<ViewToggles");
    expect(route).toContain("data-beats-split");
    expect(route).not.toContain("react-resizable-panels");
    expect(route).toContain("<BeatCardGrid");
    expect(route).toContain("<ActionPanel");
    expect(route.indexOf("<ViewToggles")).toBeGreaterThan(
      route.indexOf("data-beats-split"),
    );
    expect(route.indexOf("<BeatCardGrid")).toBeGreaterThan(route.indexOf("<ViewToggles"));
    expect(route.indexOf("<ActionPanel")).toBeGreaterThan(route.indexOf("<BeatCardGrid"));

    expect(route).not.toContain("GridGallery");
    expect(route).not.toContain("PlanDialog");
    expect(route).not.toContain("rebuildPoolIndex");
  });
  it("does not collapse the right action panel when users switch beats", () => {
    const route = read(
      "src/routes/_app/projects.$project/episodes.$episode/beats.lazy.tsx",
    );

    expect(route).toContain("onCardClick={handleCardClick}");
    expect(route).not.toContain("setCardCollapseKey");
    expect(route).not.toContain("collapseKey={");
  });

  it("keeps ViewToggles as a plain selection bar and BatchBar free of removed actions", () => {
    const viewToggles = read("src/components/episode/beat-workbench/view-toggles.tsx");
    const batchBar = read("src/components/episode/beat-workbench/batch-bar.tsx");

    expect(viewToggles).toContain("onSelectAll");
    expect(viewToggles).toContain("onClearSelection");
    expect(viewToggles).not.toContain("onBatchRegen");
    expect(viewToggles).not.toContain("onToggle");

    expect(batchBar).toContain("AspectRatioSelect");
    expect(batchBar).not.toContain("GlobalOptimize");
    expect(batchBar).not.toContain("useDetectIdentities");
    expect(batchBar).not.toContain("useAssignColors");
    expect(batchBar).not.toContain("usePlanFixedCameraLocks");
  });
  it("does not let stale URL beat deep-links overwrite restored workbench state", () => {
    const route = read(
      "src/routes/_app/projects.$project/episodes.$episode/beats.lazy.tsx",
    );
    const deepLinkEffect = route.slice(
      route.indexOf("// Deep-link:"),
      route.indexOf("// Sync URL"),
    );

    expect(deepLinkEffect).toContain('selection.mode !== "none"');
    expect(deepLinkEffect).not.toContain("activeBeat !== null");
    expect(deepLinkEffect).toContain("appliedDeepLinkRef");
  });

  it("keeps the episode-level Freezone entry hidden in the beats workbench", () => {
    const route = read(
      "src/routes/_app/projects.$project/episodes.$episode/beats.lazy.tsx",
    );
    const zh = read("public/locales/zh/translation.json");
    const en = read("public/locales/en/translation.json");

    expect(route).toContain("const SHOW_EPISODE_FREEZONE_ENTRY = false");
    expect(route).toContain("handleOpenEpisodeFreezone");
    expect(route).toContain("openingEpisodeFreezone");
    expect(route).toContain('scope: "episode"');
    expect(route).toContain("SHOW_EPISODE_FREEZONE_ENTRY &&");
    expect(route).not.toContain(">EP Freezone<");
    expect(zh).toContain("episodeFreezone");
    expect(en).toContain("episodeFreezone");
  });

  it("shows the audio media status only for narrated projects", () => {
    const route = read(
      "src/routes/_app/projects.$project/episodes.$episode/beats.lazy.tsx",
    );
    const actionPanel = read("src/components/episode/beat-workbench/action-panel.tsx");
    const singleBeatPanel = read("src/components/episode/beat-workbench/single-beat-panel.tsx");
    const videoPane = read("src/components/episode/beat-workbench/video-pane.tsx");

    expect(route).toContain("showAudioMediaStatus={isNarratedProject}");
    expect(actionPanel).toContain("showAudioMediaStatus");
    expect(singleBeatPanel).toContain("showAudioMediaStatus");
    expect(videoPane).toContain("showAudioMediaStatus &&");
  });

  it("persists the video backend through project config instead of local-only storage", () => {
    const route = read(
      "src/routes/_app/projects.$project/episodes.$episode/beats.lazy.tsx",
    );

    expect(route).toContain("useProject(project)");
    expect(route).toContain("useUpdateProject(project)");
    expect(route).toContain("projectConfigRes.data?.data?.video_backend");
    expect(route).toContain("handleVideoBackendChange");
    expect(route).toContain("video_backend: backend");
    expect(route).not.toContain('"video-backend"');
  });

  it("hydrates and persists project aspect ratio through project config", () => {
    const route = read(
      "src/routes/_app/projects.$project/episodes.$episode/beats.lazy.tsx",
    );
    const projectTypes = read("src/types/project.ts");

    expect(projectTypes).toContain('aspect_ratio?: "2:3" | "9:16" | "16:9"');
    expect(route).toContain("orientationForAspectRatio");
    expect(route).toContain("projectConfigRes.data?.data?.aspect_ratio");
    expect(route).toContain("aspect_ratio: aspectRatioForOrientation");
  });

  it("keeps Director Render video first-frame compatibility off the visible React UI", () => {
    const route = read(
      "src/routes/_app/projects.$project/episodes.$episode/beats.lazy.tsx",
    );
    const batchBar = read("src/components/episode/beat-workbench/batch-bar.tsx");
    const actionPanel = read("src/components/episode/beat-workbench/action-panel.tsx");
    const singleBeatPanel = read("src/components/episode/beat-workbench/single-beat-panel.tsx");
    const videoPane = read("src/components/episode/beat-workbench/video-pane.tsx");
    const videoQuery = read("src/lib/queries/video.ts");
    const projectTypes = read("src/types/project.ts");

    expect(projectTypes).toContain("use_director_render?: boolean");
    expect(videoQuery).toContain("use_director_render?: boolean");

    expect(route).not.toContain("handleUseDirectorRenderChange");
    expect(route).not.toContain("useDirectorRender={useDirectorRender}");
    expect(batchBar).not.toContain("useDirectorRender");
    expect(batchBar).not.toContain("onUseDirectorRenderChange");
    expect(batchBar).not.toContain("episode.workbench.batch.useDirectorRender");
    expect(actionPanel).not.toContain("useDirectorRender");
    expect(singleBeatPanel).not.toContain("useDirectorRender");
    expect(videoPane).not.toContain("useDirectorRender");
  });
});
