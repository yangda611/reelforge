// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
/**
 * 画布平移性能探针（诊断工具，不是 CI 测试）。
 *
 * 它打开 `e2e/canvas-perf.html`（一个只挂真实 `<Canvas>` 的最小页），铺满媒体节点，
 * 然后用真实鼠标中键拖拽驱动平移，采集：
 *   - 每帧耗时分布（p50/p90/p99/max、超 33ms 的帧数）；
 *   - Chrome `long-animation-frame`（LoAF）归因：这一帧被哪段脚本、样式布局、
 *     光栅化各吃掉多少毫秒。
 *
 * 为什么需要它：单看 fps 分不清「React 渲染慢」「样式重算慢」还是「合成器跟不上」，
 * 而这三者的修法完全不同。
 *
 * ── 跑法 ────────────────────────────────────────────────────────────────
 * 1) 起 dev server：
 *      cd frontend && pnpm exec vite --port 5199 --host 127.0.0.1
 * 2) 跑探针：
 *      node e2e/canvas-pan-loaf.mjs --nodes=120 --dense=1 --zoom=1
 *
 * 参数：
 *   --nodes=120   节点数（图片/视频/故事板轮转）
 *   --dense=1     网格间距小于节点尺寸，形成「墙」，让视口里同时塞满节点
 *   --zoom=1      初始缩放（低于 0.35 进入低缩放 LOD 档：全量挂载 + 灰壳）
 *   --real=1      用 `public/static/__perf/` 下的 2560x1440 真图（需自己放，见下）
 *
 * 环境变量：
 *   CANVAS_PERF_CHROME    指定 chrome.exe 路径（默认用本机 chrome channel）
 *   CANVAS_PERF_HEADLESS=1 走 headless（rAF 不锁 vsync，只能做趋势对比）
 *   CANVAS_PERF_BASE      自定义站点地址
 *   CANVAS_PERF_ABLATE=1  逐项关闭 CSS 效果做对照
 *
 * ── 关于真图 ────────────────────────────────────────────────────────────
 * 演示图只有几百 KB，测不出「大图重光栅化」。要复现真机负载，把 2560x1440 的
 * PNG 放进 `frontend/public/static/__perf/` 并命名 `img_01.png` … `img_28.png`
 * （该目录已在 .gitignore 里，不入库）。没有这些图时请加 `--real=0`。
 *
 * ── dev 构建的限制 ──────────────────────────────────────────────────────
 * dev server 用 React 开发版，`jsxDEV` 在 CPU profile 里能占到 ~10%，
 * 会显著放大「节点挂载」成本。判定「挂载是否是真凶」时必须以生产构建为准，
 * 否则容易把优化做反（本仓库就踩过：为消除灰壳闪现代价做的改动，在生产口径下
 * 反而把冷启动从 160fps 拉到 98fps，已回退）。
 */
import { chromium } from "@playwright/test";

function readArg(name, fallback) {
  const prefix = `--${name}=`;
  const hit = process.argv.slice(2).find((arg) => arg.startsWith(prefix));
  return hit ? hit.slice(prefix.length) : fallback;
}

const BASE = process.env.CANVAS_PERF_BASE ?? "http://127.0.0.1:5199";
const NODES = readArg("nodes", "120");
const DENSE = readArg("dense", "1");
const ZOOM = readArg("zoom", "1");
const REAL = readArg("real", "1");
const ABLATE = process.env.CANVAS_PERF_ABLATE === "1";

const browser = await chromium.launch({
  executablePath: process.env.CANVAS_PERF_CHROME ?? undefined,
  channel: process.env.CANVAS_PERF_CHROME
    ? undefined
    : (process.env.CANVAS_PERF_CHANNEL ?? "chrome"),
  headless: process.env.CANVAS_PERF_HEADLESS === "1",
  args: ["--enable-gpu-rasterization", "--ignore-gpu-blocklist"],
});
const context = await browser.newContext({ viewport: { width: 1600, height: 900 } });
const page = await context.newPage();

await page.goto(
  `${BASE}/e2e/canvas-perf.html?nodes=${NODES}&zoom=${ZOOM}&media=1&dense=${DENSE}&real=${REAL}`,
  { waitUntil: "load" },
);
await page.waitForFunction(() => window.__canvasPerf?.ready(), null, { timeout: 60_000 });
// 大图（2560x1440 / 4-5MB）解码很慢，必须等它们全部 decode 完再测，
// 否则测到的是「首次加载」而不是「稳态平移」。
await page.waitForTimeout(3000);
await page.evaluate(async () => {
  const images = [...document.querySelectorAll(".react-flow__viewport img")];
  await Promise.all(
    images.map(async (img) => {
      try {
        await img.decode();
      } catch {
        /* 解码失败不阻塞压测 */
      }
    }),
  );
});
await page.waitForTimeout(1500);

const scene = await page.evaluate(() => {
  const viewport = document.querySelector(".react-flow__viewport");
  return {
    nodes: viewport ? viewport.querySelectorAll(".react-flow__node").length : 0,
    shells: viewport ? viewport.querySelectorAll(".dc-lod-shell").length : 0,
    elements: viewport ? viewport.querySelectorAll("*").length : 0,
    transform: viewport ? getComputedStyle(viewport).transform : null,
  };
});
console.log(
  `scene: ${scene.nodes} mounted nodes, ${scene.shells} LOD shells, ` +
    `${scene.elements} elements (zoom=${ZOOM}, dense=${DENSE}, real=${REAL})`,
);
console.log(`transform: ${scene.transform}\n`);

async function dragPan({ steps, stepPx, holdMs }) {
  await page.evaluate(() => {
    window.__loaf = [];
    window.__frameDeltas = [];
    let last = performance.now();
    window.__sampling = true;
    const tick = (now) => {
      window.__frameDeltas.push(now - last);
      last = now;
      if (window.__sampling) requestAnimationFrame(tick);
    };
    requestAnimationFrame((now) => {
      last = now;
      requestAnimationFrame(tick);
    });
    try {
      new PerformanceObserver((list) => {
        for (const entry of list.getEntries()) {
          window.__loaf.push({
            duration: entry.duration,
            renderStart:
              entry.renderStart != null ? entry.renderStart - entry.startTime : null,
            styleAndLayout:
              entry.styleAndLayoutStart != null && entry.renderStart != null
                ? entry.styleAndLayoutStart - entry.renderStart
                : null,
            scripts: (entry.scripts ?? []).map((script) => ({
              duration: script.duration,
              invoker: script.invoker,
              sourceFunctionName: script.sourceFunctionName,
              sourceURL: (script.sourceURL ?? "").replace(/^.*\/(src|node_modules)\//, "$1/"),
            })),
          });
        }
      }).observe({ type: "long-animation-frame" });
    } catch {
      /* 老版本 Chrome 不支持 LoAF */
    }
  });

  const box = await page.locator(".react-flow__pane").boundingBox();
  const startX = box.x + box.width * 0.62;
  const startY = box.y + box.height * 0.58;
  await page.mouse.move(startX, startY);
  await page.mouse.down({ button: "middle" });
  let x = startX;
  let y = startY;
  for (let step = 0; step < steps; step += 1) {
    x -= stepPx;
    y -= stepPx * 0.45;
    await page.mouse.move(x, y);
    await page.waitForTimeout(holdMs);
  }
  await page.mouse.up({ button: "middle" });
  await page.waitForTimeout(250);

  return page.evaluate(() => {
    window.__sampling = false;
    const deltas = window.__frameDeltas.slice(2);
    const sorted = [...deltas].sort((a, b) => a - b);
    const at = (q) => sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * q))] ?? 0;
    const total = deltas.reduce((sum, value) => sum + value, 0);
    const loaf = window.__loaf;
    const byInvoker = new Map();
    for (const entry of loaf) {
      for (const script of entry.scripts) {
        const key = `${script.invoker || "?"} :: ${script.sourceFunctionName || "?"} (${script.sourceURL || "?"})`;
        const current = byInvoker.get(key) ?? { count: 0, ms: 0, max: 0 };
        current.count += 1;
        current.ms += script.duration;
        current.max = Math.max(current.max, script.duration);
        byInvoker.set(key, current);
      }
    }
    return {
      frames: deltas.length,
      fps: Number(((deltas.length * 1000) / total).toFixed(1)),
      p50: Number(at(0.5).toFixed(2)),
      p90: Number(at(0.9).toFixed(2)),
      p99: Number(at(0.99).toFixed(2)),
      max: Number((sorted.at(-1) ?? 0).toFixed(2)),
      over33: deltas.filter((value) => value > 33.4).length,
      loafCount: loaf.length,
      loafMax: Number(loaf.reduce((max, entry) => Math.max(max, entry.duration), 0).toFixed(1)),
      loafTop: loaf
        .slice()
        .sort((a, b) => b.duration - a.duration)
        .slice(0, 6)
        .map((entry) => ({
          duration: Number(entry.duration.toFixed(1)),
          script: entry.renderStart != null ? Number(entry.renderStart.toFixed(1)) : null,
          styleAndLayout: entry.styleAndLayout != null ? Number(entry.styleAndLayout.toFixed(1)) : null,
          scripts: entry.scripts.map((s) => `${s.sourceFunctionName || "?"}(${s.duration.toFixed(1)}ms)`),
        })),
      byInvoker: [...byInvoker.entries()]
        .map(([key, value]) => ({
          key,
          count: value.count,
          ms: Number(value.ms.toFixed(1)),
          max: Number(value.max.toFixed(1)),
        }))
        .sort((a, b) => b.ms - a.ms)
        .slice(0, 10),
    };
  });
}

const format = (stats) =>
  `frames=${String(stats.frames).padStart(4)} fps=${String(stats.fps).padStart(6)} ` +
  `p50=${String(stats.p50).padStart(6)} p90=${String(stats.p90).padStart(6)} ` +
  `p99=${String(stats.p99).padStart(7)} max=${String(stats.max).padStart(7)} ` +
  `>33ms=${String(stats.over33).padStart(3)} LoAF=${String(stats.loafCount).padStart(3)}/${String(stats.loafMax).padStart(7)}ms`;

if (ABLATE) {
  const cases = {
    "baseline": "",
    "no backdrop-filter": `.react-flow__viewport *{backdrop-filter:none!important;-webkit-backdrop-filter:none!important}`,
    "no box-shadow/filter": `.react-flow__viewport *{box-shadow:none!important;filter:none!important}`,
    "hide <video>": `.react-flow__viewport video{display:none!important}`,
    "viewport will-change": `.react-flow__viewport{will-change:transform}`,
    "node contain:layout paint": `.react-flow__node{contain:layout paint}`,
  };
  console.log("ablation (single run each):");
  for (const [label, css] of Object.entries(cases)) {
    await page.evaluate((rules) => {
      document.getElementById("__ablation")?.remove();
      if (!rules) return;
      const style = document.createElement("style");
      style.id = "__ablation";
      style.textContent = rules;
      document.head.appendChild(style);
    }, css);
    await page.waitForTimeout(400);
    const stats = await dragPan({ steps: 90, stepPx: 12, holdMs: 12 });
    console.log(`  ${label.padEnd(28)} ${format(stats)}`);
  }
} else {
  // 前两轮是预热（首轮含冷启动挂载），只报告第三轮稳态值。
  for (let run = 1; run <= 3; run += 1) {
    const stats = await dragPan({ steps: 110, stepPx: 12, holdMs: 12 });
    const tag = run < 3 ? `warmup ${run}` : "steady";
    console.log(`${tag.padEnd(9)} ${format(stats)}`);
    if (run === 3) {
      if (stats.loafTop.length > 0) {
        console.log("\ntop long animation frames:");
        for (const entry of stats.loafTop) {
          console.log(
            `  ${String(entry.duration).padStart(7)}ms  script=${String(entry.script).padStart(7)} ` +
              `style+layout=${String(entry.styleAndLayout).padStart(7)}  ${entry.scripts.join(", ")}`,
          );
        }
      }
      if (stats.byInvoker.length > 0) {
        console.log("\nscript time by invoker:");
        for (const item of stats.byInvoker) {
          console.log(
            `  ${String(item.ms).padStart(8)}ms  x${String(item.count).padStart(3)}  ` +
              `max ${String(item.max).padStart(6)}ms  ${item.key}`,
          );
        }
      }
    }
  }
}

await browser.close();
