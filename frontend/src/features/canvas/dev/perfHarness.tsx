// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
/**
 * 画布平移性能压测页（`e2e/canvas-perf.html`）：不登录、不起后端，只把真实
 * `<Canvas>` 和它的 store 挂起来，用媒体节点铺满画布，再由 Playwright 脚本化
 * 平移、采集每帧耗时。
 *
 * 没有任何应用代码引用这个文件，所以它不进产物；放在 `src` 下只是为了吃到类型检查。
 *
 * 查询参数：
 *   ?nodes=60        节点数量（按图片 / 视频 / 故事板 轮转生成）
 *   &zoom=1          初始缩放
 *   &media=1         是否给节点挂真实媒体（0 = 只有空壳）
 */
import { useEffect, useMemo, useRef } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ReactFlowProvider, useReactFlow } from "@xyflow/react";

import { ThemeProvider } from "@/components/theme-provider";
import { Canvas } from "@/features/canvas/Canvas";
import { CANVAS_NODE_TYPES } from "@/features/canvas/domain/canvasNodes";
import { useCanvasStore } from "@/stores/canvasStore";

import "@/i18n";
import "@/index.css";

const IMAGE_URL = "/images/camera/sony-venice.png";
const IMAGE_URL_ALT = "/images/camera/red-v-raptor.png";
const VIDEO_URL = "/images/btnmotion.mp4";

/** 真实画布素材：`public/static/__perf/` 下的 2560x1440 大图（本机放，不入库）。 */
const REAL_IMAGES = [
  "img_01.png", "img_02.png", "img_03.png", "img_04.png", "img_05.png",
  "img_06.png", "img_07.png", "img_08.png", "img_09.png", "img_10.png",
  "img_11.png", "img_12.png", "img_13.png", "img_14.png", "img_15.png",
  "img_16.png", "img_17.png", "img_18.png", "img_19.png", "img_20.png",
  "img_21.png", "img_22.png", "img_23.png", "img_24.png", "img_25.png",
  "img_26.png", "img_27.png", "img_28.png",
];

function realImage(index: number): string {
  return `/static/__perf/${REAL_IMAGES[index % REAL_IMAGES.length]}`;
}

type FrameStats = {
  frames: number;
  /** 每帧耗时（ms），按采样顺序。 */
  deltas: number[];
  p50: number;
  p90: number;
  p99: number;
  max: number;
  fps: number;
};

declare global {
  interface Window {
    __canvasPerf?: {
      /** 场景是否已挂载完成（节点数 > 0 且 ReactFlow 已测量）。 */
      ready: () => boolean;
      /** 把视口平移到指定位置（立即生效，无动画）。 */
      setViewport: (x: number, y: number, zoom?: number) => void;
      /** 直接用 store 改写 transform（绕开 d3-zoom，模拟「纯合成」上限）。 */
      rawPanTo: (x: number, y: number) => void;
      /** 以给定步长、步数、每步间隔驱动一次平移，返回帧统计。 */
      panRun: (opts: {
        dx: number;
        dy: number;
        steps: number;
        intervalMs: number;
      }) => Promise<FrameStats>;
      /** 当前节点数 / 边数，用于确认场景。 */
      counts: () => { nodes: number; edges: number };
      /** 注入一段 CSS，用于对照实验（返回移除函数）。 */
      addCss: (css: string) => () => void;
      /** 统计渲染树元素数。 */
      domStats: () => { total: number; nodes: number; videos: number; blurred: number };
    };
  }
}

function readInt(name: string, fallback: number): number {
  const raw = new URLSearchParams(window.location.search).get(name);
  const parsed = Number(raw);
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : fallback;
}

function stats(deltas: number[]): FrameStats {
  const sorted = [...deltas].sort((a, b) => a - b);
  const at = (q: number) =>
    sorted.length === 0 ? 0 : sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * q))];
  const total = deltas.reduce((sum, value) => sum + value, 0);
  return {
    frames: deltas.length,
    deltas,
    p50: Number(at(0.5).toFixed(2)),
    p90: Number(at(0.9).toFixed(2)),
    p99: Number(at(0.99).toFixed(2)),
    max: Number((sorted[sorted.length - 1] ?? 0).toFixed(2)),
    fps: total > 0 ? Number(((deltas.length * 1000) / total).toFixed(1)) : 0,
  };
}

/**
 * 铺场景。
 *
 * - `dense`：网格间距小于节点尺寸，形成「墙」，让视口里同时有上百个节点
 *   （复现用户那种铺满整屏的画布，仓促平移时渲染树规模才是真的）。
 * - 默认：宽松网格，平移时不断有节点进出视口。
 * - `realMedia`：用 `public/static/__perf/` 里的 2560x1440 大图（用户真实素材），
 *   代替几百 KB 的演示图。大图解码 + 上传纹理才是真机卡顿的主因。
 */
function seedScene(
  nodeCount: number,
  withMedia: boolean,
  dense: boolean,
  realMedia: boolean,
  withEdges: boolean,
): void {
  const columns = dense ? 10 : 6;
  const colWidth = dense ? 340 : 700;
  const rowHeight = dense ? 260 : 500;
  useCanvasStore.getState().setCanvasData([], []);
  for (let index = 0; index < nodeCount; index += 1) {
    const column = index % columns;
    const row = Math.floor(index / columns);
    const position = { x: column * colWidth, y: row * rowHeight };
    const kind = index % 3;
    const seed = useCanvasStore.getState();
    const imageUrl = withMedia
      ? realMedia
        ? realImage(index)
        : index % 2 === 0
          ? IMAGE_URL
          : IMAGE_URL_ALT
      : null;
    if (kind === 2) {
      seed.addNode(CANVAS_NODE_TYPES.video, position, {
        displayName: `视频${index + 1}`,
        videoUrl: withMedia ? VIDEO_URL : null,
        previewImageUrl: imageUrl,
        durationMs: 4000,
      });
    } else {
      seed.addNode(CANVAS_NODE_TYPES.imageGen, position, {
        displayName: `图片${index + 1}`,
        imageUrl,
        prompt: "一张电影感剧照，暖色灯光，浅景深",
      });
    }
  }
  if (withEdges) {
    // 真实画布上节点是连起来的，边有独立的重绘路径（EdgeRenderer 每帧重算路径
    // 并画进 SVG）。没有边的压测台会低估这一项，所以这里把网格按行串起来。
    const nodes = useCanvasStore.getState().nodes;
    for (let index = 0; index + 1 < nodes.length; index += 1) {
      if (index % columns === columns - 1) continue;
      useCanvasStore.getState().addEdge(nodes[index].id, nodes[index + 1].id);
    }
  }
}

function Harness({ nodeCount, withMedia, zoom, dense, realMedia, withEdges }: {
  nodeCount: number;
  withMedia: boolean;
  zoom: number;
  dense: boolean;
  realMedia: boolean;
  withEdges: boolean;
}) {
  const reactFlow = useReactFlow();
  const seededRef = useRef(false);

  useEffect(() => {
    if (seededRef.current) return;
    seededRef.current = true;
    seedScene(nodeCount, withMedia, dense, realMedia, withEdges);
    useCanvasStore.setState({ currentViewport: { x: 0, y: 0, zoom } });
  }, [nodeCount, withMedia, zoom, dense, realMedia, withEdges]);

  // Canvas 的 defaultViewport 只在首帧读一次（见 Canvas.tsx 的 initialViewportRef），
  // 所以把初始缩放写进 store 并不会让 ReactFlow 真的缩放到那里。压测关心的是
  // 「在某个缩放档下平移」，必须在挂载后用 setViewport 显式落到目标档位，
  // 否则 zoom=0.3 这类用例其实全在 zoom=1 下跑，结论完全失真。
  useEffect(() => {
    const timer = window.setTimeout(() => {
      reactFlow.setViewport({ x: 0, y: 0, zoom }, { duration: 0 });
    }, 60);
    return () => window.clearTimeout(timer);
  }, [reactFlow, zoom]);

  const api = useMemo(
    () => ({
      ready: () => useCanvasStore.getState().nodes.length > 0,
      setViewport: (x: number, y: number, nextZoom?: number) => {
        reactFlow.setViewport({ x, y, zoom: nextZoom ?? zoom }, { duration: 0 });
      },
      rawPanTo: (x: number, y: number) => {
        // 直改 store 的 transform：模拟「只有合成、没有 React 往返」的理论上限。
        const rf = reactFlow as unknown as {
          setViewport: (vp: { x: number; y: number; zoom: number }, opts: { duration: number }) => void;
        };
        rf.setViewport({ x, y, zoom }, { duration: 0 });
      },
      counts: () => {
        const state = useCanvasStore.getState();
        return { nodes: state.nodes.length, edges: state.edges.length };
      },
      addCss: (css: string) => {
        const element = document.createElement("style");
        element.textContent = css;
        document.head.appendChild(element);
        return () => element.remove();
      },
      domStats: () => {
        const viewport = document.querySelector(".react-flow__viewport");
        return {
          total: viewport ? viewport.querySelectorAll("*").length : 0,
          nodes: viewport ? viewport.querySelectorAll(".react-flow__node").length : 0,
          videos: viewport ? viewport.querySelectorAll("video").length : 0,
          blurred: viewport
            ? viewport.querySelectorAll('[class*="backdrop-blur"]').length
            : 0,
        };
      },
      panRun: async ({
        dx,
        dy,
        steps,
        intervalMs,
      }: {
        dx: number;
        dy: number;
        steps: number;
        intervalMs: number;
      }): Promise<FrameStats> => {
        const deltas: number[] = [];
        let last = performance.now();
        let sampling = true;
        const sample = (now: number) => {
          deltas.push(now - last);
          last = now;
          if (sampling) requestAnimationFrame(sample);
        };
        requestAnimationFrame((now) => {
          last = now;
          requestAnimationFrame(sample);
        });
        const start = useCanvasStore.getState().currentViewport;
        for (let step = 1; step <= steps; step += 1) {
          reactFlow.setViewport(
            { x: start.x + dx * step, y: start.y + dy * step, zoom },
            { duration: 0 },
          );
          await new Promise((resolve) => setTimeout(resolve, intervalMs));
        }
        await new Promise((resolve) => setTimeout(resolve, 200));
        sampling = false;
        // 丢掉采样起始那一帧（包含启动开销）。
        return stats(deltas.slice(1));
      },
    }),
    [reactFlow, zoom],
  );

  useEffect(() => {
    window.__canvasPerf = api;
    return () => {
      delete window.__canvasPerf;
    };
  }, [api]);

  return <Canvas />;
}

function App() {
  const queryClient = useMemo(
    () =>
      // 与 src/main.tsx 的生产默认值对齐：staleTime 必须一致，否则每次节点重挂载
      // 都会触发一轮 refetch，压测测到的就成了网络回调而不是画布本身的开销。
      new QueryClient({
        defaultOptions: {
          queries: { retry: false, refetchOnWindowFocus: false, staleTime: 30_000 },
        },
      }),
    [],
  );
  const nodeCount = readInt("nodes", 60);
  const withMedia = readInt("media", 1) === 1;
  const dense = readInt("dense", 0) === 1;
  const realMedia = readInt("real", 0) === 1;
  const withEdges = readInt("edges", 0) === 1;
  const zoom = Number(new URLSearchParams(window.location.search).get("zoom") ?? 1) || 1;

  return (
    <QueryClientProvider client={queryClient}>
      <ThemeProvider>
        <div style={{ position: "fixed", inset: 0 }}>
          <ReactFlowProvider>
            <Harness
              nodeCount={nodeCount}
              withMedia={withMedia}
              zoom={zoom}
              dense={dense}
              realMedia={realMedia}
              withEdges={withEdges}
            />
          </ReactFlowProvider>
        </div>
      </ThemeProvider>
    </QueryClientProvider>
  );
}

createRoot(document.getElementById("root")!).render(<App />);
