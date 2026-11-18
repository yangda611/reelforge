// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import {
  Clapperboard,
  FileText,
  Film,
  Mic2,
  ScrollText,
  Video,
  type LucideIcon,
} from "lucide-react";
import type { StageId } from "@/lib/episode-stage-registry";

/**
 * React keeps the current 3-entry aggregation (script/beats/compose) above the
 * legacy 5-stage registry. The stage registry remains the source of truth for
 * asset-readiness derivation (used by `useBeatStates`). This module owns
 * navigation: what routes exist, which tab is active, and how the per-beat
 * workbench exposes its 4 sub-tabs.
 */

export type TopTabId = "script" | "beats" | "compose";
export type BeatSubTabId = "text" | "audio" | "video";

export interface TopTabDef {
  id: TopTabId;
  labelKey: string;
  icon: LucideIcon;
  routeSegment: "/script" | "/beats" | "/compose";
  /**
   * Which legacy stage ids contribute to this tab's health count.
   * `beats` aggregates audio+video; `script` is itself; `compose`
   * is the compose gate.
   */
  stageIds: readonly (Exclude<StageId, "compose">)[];
  /** Whether this tab represents the compose gate (special display). */
  isCompose?: boolean;
}

export interface BeatSubTabDef {
  id: BeatSubTabId;
  labelKey: string;
  icon: LucideIcon;
  /**
   * Legacy stage this sub-tab surfaces. `null` for the 文案 tab which is
   * cross-cutting beat metadata editing.
   */
  stageId: Exclude<StageId, "compose"> | null;
}

export const TOP_TABS: readonly TopTabDef[] = [
  {
    id: "script",
    labelKey: "episode.nav.script",
    icon: ScrollText,
    routeSegment: "/script",
    stageIds: ["script"],
  },
  {
    id: "beats",
    labelKey: "episode.nav.shots",
    icon: Clapperboard,
    routeSegment: "/beats",
    stageIds: ["audio", "video"],
  },
  {
    id: "compose",
    labelKey: "episode.nav.compose",
    icon: Film,
    routeSegment: "/compose",
    stageIds: [],
    isCompose: true,
  },
];

export const BEAT_SUB_TABS: readonly BeatSubTabDef[] = [
  { id: "text", labelKey: "episode.nav.text", icon: FileText, stageId: null },
  { id: "audio", labelKey: "episode.nav.audio", icon: Mic2, stageId: "audio" },
  { id: "video", labelKey: "episode.nav.video", icon: Video, stageId: "video" },
];

const TOP_TAB_PATHS: readonly string[] = TOP_TABS.map((t) => t.routeSegment);

/** Map a URL path to the active top tab id. */
export function topTabForPathname(pathname: string): TopTabId {
  const m = pathname.match(/\/episodes\/\d+(\/[a-z-]+)?/);
  const found = m?.[1] ?? "";
  if (TOP_TAB_PATHS.includes(found)) {
    return TOP_TABS.find((t) => t.routeSegment === found)!.id;
  }
  return "script";
}

/** Parse a URL search `sub` param to a valid BeatSubTabId; default to `text`. */
export function parseSubTabParam(raw: unknown): BeatSubTabId {
  if (typeof raw !== "string") return "text";
  return (BEAT_SUB_TABS.some((s) => s.id === raw) ? raw : "text") as BeatSubTabId;
}

/**
 * Legacy → new sub routing. Old stage tabs (/audio, /video)
 * redirect to /beats with the corresponding sub param.
 */
export const LEGACY_STAGE_TO_SUB: Record<string, BeatSubTabId> = {
  audio: "audio",
  video: "video",
};
