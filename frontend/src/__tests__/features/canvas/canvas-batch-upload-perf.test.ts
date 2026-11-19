// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { beforeEach, describe, expect, it, vi } from "vitest";
import { CANVAS_NODE_TYPES } from "@/features/canvas/domain/canvasNodes";
import { useCanvasStore } from "@/stores/canvasStore";

describe("canvas batch upload performance primitives", () => {
  beforeEach(() => {
    useCanvasStore.getState().setCanvasData([], []);
  });

  it("addNodes creates multiple nodes in a single store commit and a single history step", () => {
    const ids = useCanvasStore.getState().addNodes(
      Array.from({ length: 5 }, (_, index) => ({
        type: CANVAS_NODE_TYPES.upload,
        position: { x: index * 356, y: 0 },
        data: { user_spawned: true },
      })),
    );

    expect(ids).toHaveLength(5);
    const state = useCanvasStore.getState();
    expect(state.nodes).toHaveLength(5);
    expect(state.history.past).toHaveLength(1);
    expect(state.history.past[0]?.nodes).toHaveLength(0);
    expect(state.userEditsSinceHydrate).toBe(1);
  });

  it("setCanvasData preserves object references for unchanged unselected nodes", () => {
    useCanvasStore.getState().addNodes([
      { type: CANVAS_NODE_TYPES.upload, position: { x: 0, y: 0 }, data: { imageUrl: "/static/projects/p/a.png" } },
      { type: CANVAS_NODE_TYPES.upload, position: { x: 400, y: 0 }, data: { imageUrl: "/static/projects/p/b.png" } },
    ]);
    const before = useCanvasStore.getState().nodes;
    const firstNodeBefore = before[0];
    const secondNodeBefore = before[1];

    const updatedPayload = [
      JSON.parse(JSON.stringify(firstNodeBefore)),
      {
        ...JSON.parse(JSON.stringify(secondNodeBefore)),
        position: { x: 500, y: 100 },
      },
    ];
    useCanvasStore.getState().setCanvasData(updatedPayload, []);

    const after = useCanvasStore.getState().nodes;
    expect(after[0]).toBe(firstNodeBefore);
    expect(after[1]).not.toBe(secondNodeBefore);
    expect(after[1]?.position).toEqual({ x: 500, y: 100 });
  });

  it("uploadFreezoneImage caps concurrent uploads to avoid exhausting browser connections", async () => {
    let inFlight = 0;
    let maxInFlight = 0;
    const resolvers: Array<() => void> = [];

    vi.resetModules();
    vi.doMock("@/api/client", () => ({
      apiCall: vi.fn(),
      apiCallEnvelope: vi.fn(),
      apiClient: vi.fn((path: string) => {
        if (path.endsWith("/freezone/upload/check")) {
          return {
            json: async () => ({ ok: true, data: { found: false } }),
          };
        }
        return {
          json: () =>
            new Promise((resolve) => {
              inFlight += 1;
              maxInFlight = Math.max(maxInFlight, inFlight);
              resolvers.push(() => {
                inFlight -= 1;
                resolve({
                  ok: true,
                  data: { url: "/static/projects/p/freezone/_uploads/a.png", filename: "a.png", size: 10 },
                });
              });
            }),
        };
      }),
    }));

    const { uploadFreezoneImage } = await import("@/api/ops");
    const blobs = Array.from({ length: 10 }, () => new Blob(["x"], { type: "image/png" }));
    const promises = blobs.map((blob, i) => uploadFreezoneImage("proj", blob, `${i}.png`));

    await vi.waitFor(() => {
      expect(inFlight).toBe(4);
    });
    expect(maxInFlight).toBe(4);

    while (resolvers.length > 0 || inFlight > 0) {
      const next = resolvers.shift();
      if (next) next();
      await new Promise((r) => setTimeout(r, 0));
    }

    const results = await Promise.all(promises);
    expect(results).toHaveLength(10);
    expect(maxInFlight).toBe(4);
    vi.doUnmock("@/api/client");
  });

  it("uploadFreezoneImage short-circuits via SHA-256 dedup check without uploading multipart bytes", async () => {
    vi.resetModules();
    const apiClientMock = vi.fn((path: string) => {
      if (path.endsWith("/freezone/upload/check")) {
        return {
          json: async () => ({
            ok: true,
            data: {
              found: true,
              url: "/static/projects/proj/freezone/_uploads/existing.png",
              filename: "existing.png",
              size: 12,
              width: 1920,
              height: 1080,
              deduplicated: true,
            },
          }),
        };
      }
      throw new Error(`unexpected multipart upload call: ${path}`);
    });
    vi.doMock("@/api/client", () => ({
      apiCall: vi.fn(),
      apiCallEnvelope: vi.fn(),
      apiClient: apiClientMock,
    }));

    const { uploadFreezoneImage } = await import("@/api/ops");
    const blob = new Blob(["hello-image!"], { type: "image/png" });
    const res = await uploadFreezoneImage("proj", blob, "dup.png");

    expect(res.deduplicated).toBe(true);
    expect(res.url).toBe("/static/projects/proj/freezone/_uploads/existing.png");
    expect(res.width).toBe(1920);
    expect(res.height).toBe(1080);
    expect(apiClientMock).toHaveBeenCalledTimes(1);
    vi.doUnmock("@/api/client");
  });
});
