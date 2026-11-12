// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { CANVAS_NODE_TYPES } from "@/features/canvas/domain/canvasNodes";
import { useCanvasStore } from "@/stores/canvasStore";

/**
 * 空画布「从模板开始」（P1-6）与新内容自动适配（P2-6）的共享逻辑。
 *
 * 模板摆的是「剧本 → 脚本生成器」节点组：一个剧本节点 + 一个脚本生成器
 * （storyboardGen），连上线、包进组，落在当前视口中央，用户点进来就能
 * 直接写剧本、点生成分镜。空白画布不摆任何东西。
 */
export type CanvasTemplateKind = "novel" | "original" | "blank";

const TEMPLATE_SEEDS: Record<
  Exclude<CanvasTemplateKind, "blank">,
  { displayName: string; prompt: string }
> = {
  // i18n-exempt —— 模板预填的示例文案，用户可直接改写
  novel: {
    displayName: "小说改编 · 剧本",
    prompt:
      "把导入的小说第一章改编成短剧第一集：保留原著的核心冲突，拆成 10 个分镜，每个分镜一句话交代画面、人物和台词。",
  },
  original: {
    displayName: "原创剧本",
    prompt:
      "写一个 1 分钟短剧剧本：雨夜、旧车站、一封迟到的信。先写故事梗概，再拆成 10 个分镜。",
  },
};

export function templatePickerDismissedKey(
  canvasId: string | null | undefined,
): string {
  return `reelforge:template-picker-dismissed:${canvasId ?? "default"}`;
}

export function readTemplatePickerDismissed(
  canvasId: string | null | undefined,
): boolean {
  if (typeof window === "undefined") return false;
  try {
    return (
      window.localStorage.getItem(templatePickerDismissedKey(canvasId)) === "1"
    );
  } catch {
    return false;
  }
}

export function markTemplatePickerDismissed(
  canvasId: string | null | undefined,
): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(templatePickerDismissedKey(canvasId), "1");
  } catch {
    // ignore
  }
}

/** 在视口中央摆好模板节点组，返回新节点 id（空白模板返回空数组）。 */
export function applyCanvasTemplate(
  kind: CanvasTemplateKind,
  center: { x: number; y: number },
): string[] {
  if (kind === "blank") return [];
  const state = useCanvasStore.getState();
  const seed = TEMPLATE_SEEDS[kind];
  const scriptId = state.addNode(
    CANVAS_NODE_TYPES.script,
    { x: center.x - 500, y: center.y - 150 },
    { displayName: seed.displayName, prompt: seed.prompt },
  );
  const genId = state.addNode(
    CANVAS_NODE_TYPES.storyboardGen,
    { x: center.x + 40, y: center.y - 150 },
    { displayName: "脚本生成器" },
  );
  state.addEdge(scriptId, genId);
  state.autoGroupSpawn(scriptId, [genId], { label: "剧本 → 脚本生成器" });
  return [scriptId, genId];
}

/** 新节点/组落点后请画布适配（P2-6）：Canvas 监听此事件做实际 fit。 */
export const FIT_NEW_NODES_EVENT = "reelforge:fit-new-nodes";

export function requestFitToNewNodes(nodeIds: string[]): void {
  if (typeof window === "undefined" || nodeIds.length === 0) return;
  window.dispatchEvent(
    new CustomEvent(FIT_NEW_NODES_EVENT, { detail: { nodeIds } }),
  );
}
