import { beforeEach, describe, expect, it } from "vitest";

import { getNodeSize } from "@/features/canvas/application/autoLayout";
import { CANVAS_NODE_TYPES, type CanvasEdge, type CanvasNode } from "@/features/canvas/domain/canvasNodes";
import { buildCanvasOntologyContext } from "@/features/canvas/ontology/canvasOntology";
import {
  applyCanvasChatCommands,
  CANVAS_CHAT_COMMANDS_SCHEMA_VERSION,
  type CanvasChatCommandEnvelope,
} from "@/features/freezone/canvasChatCommands";
import { shouldIncludeCanvasSummary } from "@/features/freezone/chatNodeReferences";
import { validateCanvasChatCommandEnvelopes } from "@/features/freezone/context/canvasCommandValidator";
import { useCanvasStore } from "@/stores/canvasStore";

function makeTextNode(
  id: string,
  position: { x: number; y: number },
  content: string,
  parentId?: string,
): CanvasNode {
  return {
    id,
    type: CANVAS_NODE_TYPES.textAnnotation,
    position,
    ...(parentId ? { parentId } : {}),
    width: 260,
    height: 160,
    data: {
      label: content,
      displayName: content,
      content,
      text: content,
    },
  } as CanvasNode;
}

function makeGroupNode(
  id: string,
  position: { x: number; y: number },
  size: { width: number; height: number },
  label: string,
  extraData: Record<string, unknown> = {},
): CanvasNode {
  return {
    id,
    type: CANVAS_NODE_TYPES.group,
    position,
    width: size.width,
    height: size.height,
    style: { width: size.width, height: size.height },
    data: {
      label,
      displayName: label,
      ...extraData,
    },
  } as CanvasNode;
}

function makeEdge(id: string, source: string, target: string): CanvasEdge {
  return {
    id,
    source,
    target,
    data: { link_type: "reference" },
  } as CanvasEdge;
}

function envelope(commands: CanvasChatCommandEnvelope["commands"]): CanvasChatCommandEnvelope {
  return {
    schema_version: CANVAS_CHAT_COMMANDS_SCHEMA_VERSION,
    project_id: "proj-1",
    canvas_id: "user_canvas",
    commands,
  };
}

function expectNoSiblingOverlap(nodes: CanvasNode[]): void {
  const byParent = new Map<string, CanvasNode[]>();
  for (const node of nodes) {
    const key = node.parentId ?? "__root__";
    const list = byParent.get(key) ?? [];
    list.push(node);
    byParent.set(key, list);
  }
  for (const siblings of byParent.values()) {
    for (let i = 0; i < siblings.length; i += 1) {
      const a = siblings[i];
      const sizeA = getNodeSize(a);
      for (let j = i + 1; j < siblings.length; j += 1) {
        const b = siblings[j];
        const sizeB = getNodeSize(b);
        const overlapsX = a.position.x < b.position.x + sizeB.width && a.position.x + sizeA.width > b.position.x;
        const overlapsY = a.position.y < b.position.y + sizeB.height && a.position.y + sizeA.height > b.position.y;
        expect(overlapsX && overlapsY).toBe(false);
      }
    }
  }
}

describe("canvas ontology, summary trigger, auto layout, and organize_canvas", () => {
  beforeEach(() => {
    useCanvasStore.getState().setCanvasData([], []);
  });

  it("preserves large canvases (80+ nodes) with parent_id, size, and text_preview", () => {
    const group = makeGroupNode("grp-main", { x: 40, y: 40 }, { width: 800, height: 600 }, "主组");
    const nodes: CanvasNode[] = [group];
    for (let i = 0; i < 80; i += 1) {
      nodes.push(
        makeTextNode(
          `n-${i}`,
          { x: (i % 10) * 100, y: Math.floor(i / 10) * 100 },
          `第${i}个角色设定说明`,
          i < 5 ? "grp-main" : undefined,
        ),
      );
    }

    const context = buildCanvasOntologyContext(nodes, [], { canvasId: "user_canvas" });
    expect(context.objects).toHaveLength(81);
    const child = context.objects.find((obj) => obj.node_id === "n-0");
    expect(child?.parent_id).toBe("grp-main");
    expect(child?.size).toEqual({ width: 260, height: 160 });
    expect(child?.text_preview).toContain("第0个角色设定说明");
  });

  it("includes canvas summary for full-canvas organization prompts while keeping pure smalltalk excluded", () => {
    expect(shouldIncludeCanvasSummary("帮我整理画布上的所有节点并消除重叠")).toBe(true);
    expect(shouldIncludeCanvasSummary("整理所有节点")).toBe(true);
    expect(shouldIncludeCanvasSummary("你好")).toBe(false);
  });

  it("supports group_nodes client_id across validation and execution", () => {
    const n1 = makeTextNode("n-1", { x: 0, y: 0 }, "角色A");
    const n2 = makeTextNode("n-2", { x: 40, y: 40 }, "角色B");
    useCanvasStore.getState().setCanvasData([n1, n2], []);

    const env = envelope([
      { type: "group_nodes", node_ids: ["n-1", "n-2"], label: "角色区", client_id: "grp-roles" },
      { type: "layout_nodes", node_ids: ["grp-roles", "n-1", "n-2"], mode: "auto" },
    ]);

    const validation = validateCanvasChatCommandEnvelopes(
      [env],
      useCanvasStore.getState().nodes,
      useCanvasStore.getState().edges,
    );
    expect(validation.ok).toBe(true);

    const applied = applyCanvasChatCommands([env]);
    expect(applied.errors).toEqual([]);
    expect(applied.createdNodeIds).toHaveLength(1);
    expectNoSiblingOverlap(useCanvasStore.getState().nodes);
  });

  it("runs hierarchical group-aware DAG auto layout with zero overlap and tightened group size", () => {
    const g1 = makeGroupNode("grp-1", { x: 0, y: 0 }, { width: 1200, height: 900 }, "旧大组");
    const a1 = makeTextNode("a1", { x: 20, y: 34 }, "上一步", "grp-1");
    const a2 = makeTextNode("a2", { x: 25, y: 40 }, "下一步", "grp-1");
    const b1 = makeTextNode("b1", { x: 10, y: 10 }, "下游汇总");
    const edges = [makeEdge("e1", "a1", "a2"), makeEdge("e2", "a2", "b1")];
    useCanvasStore.getState().setCanvasData([g1, a1, a2, b1], edges);

    const env = envelope([{ type: "layout_nodes", mode: "auto" }]);
    const applied = applyCanvasChatCommands([env]);
    expect(applied.errors).toEqual([]);

    const state = useCanvasStore.getState();
    const updatedGroup = state.nodes.find((n) => n.id === "grp-1")!;
    const updatedA1 = state.nodes.find((n) => n.id === "a1")!;
    const updatedA2 = state.nodes.find((n) => n.id === "a2")!;
    const updatedB1 = state.nodes.find((n) => n.id === "b1")!;

    expect(updatedA2.position.x).toBeGreaterThan(updatedA1.position.x);
    expect(updatedB1.position.x).toBeGreaterThan(updatedGroup.position.x);
    expect(updatedGroup.width!).toBeLessThan(1200);
    expectNoSiblingOverlap(state.nodes);
  });

  it("organizes scattered nodes into ordered categories (including 1-node categories), prunes empty groups, and avoids overlap", () => {
    const oldGroup = makeGroupNode("grp-old", { x: 0, y: 0 }, { width: 500, height: 400 }, "杂乱旧组");
    const protectedProjection = makeGroupNode(
      "grp-mainline",
      { x: 0, y: 0 },
      { width: 400, height: 220 },
      "第1集主线投影",
      { projection_key: "ep1", systemManaged: true },
    );
    const protectedChild = makeTextNode("m1", { x: 20, y: 34 }, "主线分镜1", "grp-mainline");
    const n1 = makeTextNode("n1", { x: 10, y: 10 }, "角色卡1", "grp-old");
    const n2 = makeTextNode("n2", { x: 15, y: 15 }, "角色卡2", "grp-old");
    const n3 = makeTextNode("n3", { x: 12, y: 12 }, "孤立单节点分镜");
    const n4 = makeTextNode("n4", { x: 20, y: 20 }, "自由节点");
    useCanvasStore.getState().setCanvasData([protectedProjection, protectedChild, oldGroup, n1, n2, n3, n4], []);

    const env = envelope([
      {
        type: "organize_canvas",
        categories: [
          { label: "角色设定", node_ids: ["n1", "n2"] },
          { label: "分镜脚本", node_ids: ["n3"] },
        ],
        layout_mode: "auto",
        prune_empty_groups: true,
      },
    ]);

    const validation = validateCanvasChatCommandEnvelopes(
      [env],
      useCanvasStore.getState().nodes,
      useCanvasStore.getState().edges,
    );
    expect(validation.ok).toBe(true);

    const applied = applyCanvasChatCommands([env]);
    expect(applied.errors).toEqual([]);
    expect(applied.createdNodeIds).toHaveLength(2);

    const state = useCanvasStore.getState();
    // Old ordinary group became empty and was pruned.
    expect(state.nodes.some((n) => n.id === "grp-old")).toBe(false);
    // Protected projection group was preserved intact.
    const preservedProjection = state.nodes.find((n) => n.data?.projection_key === "ep1");
    expect(preservedProjection).toBeDefined();
    expect(preservedProjection!.position).toEqual({ x: 0, y: 0 });

    const rolesGroup = state.nodes.find((n) => n.data?.label === "角色设定");
    const storyboardGroup = state.nodes.find((n) => n.data?.label === "分镜脚本");
    expect(rolesGroup).toBeDefined();
    expect(storyboardGroup).toBeDefined();
    expect(state.nodes.find((n) => n.id === "n1")?.parentId).toBe(rolesGroup!.id);
    expect(state.nodes.find((n) => n.id === "n2")?.parentId).toBe(rolesGroup!.id);
    expect(state.nodes.find((n) => n.id === "n3")?.parentId).toBe(storyboardGroup!.id);

    // Categories preserve caller order left-to-right and stay clear of protected group.
    expect(storyboardGroup!.position.x).toBeGreaterThan(rolesGroup!.position.x);
    expectNoSiblingOverlap(state.nodes);
  });
});
