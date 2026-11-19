// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  buildIncrementalPatch,
  INCREMENTAL_PATCH_MIN_NODES,
} from "@/features/freezone/canvasSyncCore";
import {
  readCanvasDraftAsync,
  writeCanvasDraftAsync,
  clearCanvasDraftAsync,
} from "@/features/freezone/canvasDraftStorage";
import {
  registerFreezoneCanvasRuntime,
  refreshRemoteFreezoneCanvasDelta,
} from "@/features/freezone/canvasSyncRuntime";
import { getFreezoneCanvasDelta } from "@/api/canvas";
import { CANVAS_NODE_TYPES } from "@/features/canvas/domain/canvasNodes";
import { useCanvasStore } from "@/stores/canvasStore";

vi.mock("@/api/canvas", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/api/canvas")>();
  return {
    ...actual,
    getFreezoneCanvas: vi.fn(),
    getFreezoneCanvasDelta: vi.fn(),
  };
});

function makeNodes(count: number) {
  return Array.from({ length: count }, (_, i) => ({
    id: `n-${i}`,
    type: CANVAS_NODE_TYPES.upload,
    position: { x: i * 100, y: 0 },
    data: { imageUrl: `/static/projects/p/freezone/_uploads/${i}.png` },
  }));
}

describe("incremental canvas patch & delta sync", () => {
  beforeEach(() => {
    window.localStorage.clear();
    useCanvasStore.getState().setCanvasData([], []);
    vi.mocked(getFreezoneCanvasDelta).mockReset();
  });

  it("buildIncrementalPatch returns compact diff when >= min nodes and partial changes", () => {
    const base = makeNodes(INCREMENTAL_PATCH_MIN_NODES);
    const updated = base.map((n, idx) =>
      idx === 2 ? { ...n, position: { x: 999, y: 888 } } : n,
    );
    updated.push({
      id: "n-new",
      type: CANVAS_NODE_TYPES.upload,
      position: { x: 1200, y: 0 },
      data: { imageUrl: "/static/projects/p/freezone/_uploads/new.png" },
    });
    const withoutFirst = updated.slice(1);

    const patch = buildIncrementalPatch({
      baselineNodes: base,
      baselineEdges: [{ id: "e-1", source: "n-0", target: "n-1" }],
      baselineMetadata: { title: "v1" },
      currentNodes: withoutFirst,
      currentEdges: [],
      currentMetadata: { title: "v1" },
      viewport: { x: 0, y: 0, zoom: 1 },
      baseRevision: 4,
      clientSaveId: "save-1",
      decision: { kind: "send", saveSource: "autosave", allowEmptyOverwrite: false },
    });

    expect(patch).not.toBeNull();
    expect(patch?.base_revision).toBe(4);
    expect(patch?.remove_node_ids).toEqual(["n-0"]);
    expect(patch?.remove_edge_ids).toEqual(["e-1"]);
    expect((patch?.upsert_nodes as Array<{ id: string }>).map((n) => n.id)).toEqual([
      "n-2",
      "n-new",
    ]);
    expect(patch?.has_metadata).toBeUndefined();
  });

  it("buildIncrementalPatch falls back to null (full PUT) when >60% nodes changed or below threshold", () => {
    const small = makeNodes(3);
    expect(
      buildIncrementalPatch({
        baselineNodes: small,
        baselineEdges: [],
        baselineMetadata: null,
        currentNodes: small,
        currentEdges: [],
        currentMetadata: null,
        baseRevision: 1,
        clientSaveId: "s",
        decision: { kind: "send", saveSource: "autosave", allowEmptyOverwrite: false },
      }),
    ).toBeNull();

    const large = makeNodes(10);
    const mostlyChanged = large.map((n) => ({ ...n, position: { x: n.position.x + 1, y: 1 } }));
    expect(
      buildIncrementalPatch({
        baselineNodes: large,
        baselineEdges: [],
        baselineMetadata: null,
        currentNodes: mostlyChanged,
        currentEdges: [],
        currentMetadata: null,
        baseRevision: 1,
        clientSaveId: "s",
        decision: { kind: "send", saveSource: "autosave", allowEmptyOverwrite: false },
      }),
    ).toBeNull();
  });

  it("refreshRemoteFreezoneCanvasDelta merges delta and preserves untouched node references", async () => {
    const initial = makeNodes(3);
    useCanvasStore.getState().setCanvasData(initial, [{ id: "e-1", source: "n-0", target: "n-1" }]);
    const untouchedBefore = useCanvasStore.getState().nodes[0];

    const applySpy = vi.fn((remote) => {
      useCanvasStore.getState().setCanvasData(
        remote.nodes as never,
        remote.edges as never,
      );
      return true;
    });
    const unregister = registerFreezoneCanvasRuntime(
      "proj",
      "default",
      applySpy,
      undefined,
      undefined,
      undefined,
      () => 1,
    );

    vi.mocked(getFreezoneCanvasDelta).mockResolvedValue({
      mode: "delta",
      from_revision: 1,
      revision: 2,
      upsert_nodes: [
        {
          id: "n-2",
          type: CANVAS_NODE_TYPES.upload,
          position: { x: 777, y: 333 },
          data: { imageUrl: "/static/projects/p/freezone/_uploads/2.png" },
        },
      ],
      remove_node_ids: ["n-1"],
      upsert_edges: [],
      remove_edge_ids: ["e-1"],
    });

    const ok = await refreshRemoteFreezoneCanvasDelta("proj", "default", 1, {
      protectUnsavedLocalEdits: true,
    });
    unregister();

    expect(ok).toBe(true);
    expect(applySpy).toHaveBeenCalledTimes(1);
    const after = useCanvasStore.getState().nodes;
    expect(after.map((n) => n.id)).toEqual(["n-0", "n-2"]);
    expect(after[0]).toBe(untouchedBefore);
    expect(after[1]?.position).toEqual({ x: 777, y: 333 });
    expect(useCanvasStore.getState().edges).toEqual([]);
  });

  it("async draft helpers round-trip and clean up cleanly in fallback mode", async () => {
    const ok = await writeCanvasDraftAsync("proj", "default", {
      baseRevision: 2,
      nodes: makeNodes(2) as never,
      edges: [],
      viewport: { x: 1, y: 2, zoom: 1 },
      metadata: null,
      history: { past: [], future: [] },
      mutation: {
        userEditsSinceHydrate: 1,
        lastMutationSource: "user_edit",
        pendingClearIntent: false,
      },
      updatedAt: Date.now(),
    });
    expect(ok).toBe(true);
    const readBack = await readCanvasDraftAsync("proj", "default");
    expect(readBack?.nodes).toHaveLength(2);
    await clearCanvasDraftAsync("proj", "default");
    expect(await readCanvasDraftAsync("proj", "default")).toBeNull();
  });
});
