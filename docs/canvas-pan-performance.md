# 画布平移卡顿：排查记录与诊断工具

本文记录「无限画布拖动时内容层卡顿、点点背景丝滑」这个问题的排查结论、实测数据，
以及在**你的机器**上继续定位的方法。代码层面的结论是：**在当前仓库的代码下，
我们没能复现这个问题**；已确认的两处真实缺陷（详见下文）都已修复或证伪回退。

---

## 1. 已修复：agent 读不到完整剧本，导致不拆分镜头

这个问题与画布性能无关，但和「明明聊完了却没建节点」是同一现场，一并记在这里。

**现象**：用户选中 Workflow Skill 后要求「把剧本拆成多个 clip」，agent 回复
「我这里没有 `skill_view` 工具」或「剧本只返回了被截断的开头，无法核对完整情节」，
然后什么都不做。

**三处断点**（都已在同一批改动里修掉）：

| 断点 | 位置 | 修法 |
|---|---|---|
| 选中的 Workflow Skill 传不到后端 | `frontend/src/features/superchat/superchat-panel.tsx` 序列化成字面量 `/skill-id`；后端 `service.py` 的 `_freezone_native_skill_context` 只查 native markdown 技能表 | 扩展该函数，同时查 `agent_config_store` 的 Workflow Skills，命中后注入 `[FREEZONE_WORKFLOW_SKILL_SELECTED]` 块，明确要求调用一次 `freezone_get_workflow_skill` |
| 模型去调一个不存在的工具 | Freezone profile 的 `enabled_toolsets` 里没有 `skills`，`skill_view` / `skills_list` 从未注册 | 注入块里显式声明「不要调用 `skill_view`/`skills_list`，它们未启用」 |
| 剧本正文被截成 240 字 | `frontend/src/features/freezone/chatNodeReferences.ts` 的 `NODE_REFERENCE_TEXT_PREVIEW_LIMIT` 同时作用于紧凑引用列表**和** `node_detail` | `node_detail` 改为返回完整 `text_content`（上限 64k 字符，超出时置 `text_truncated: true`）；紧凑引用列表仍保持 240 字 |

回归测试：
- `tests/test_native_skill_store.py::test_chat_service_injects_workflow_skill_selection_for_slash_mention`
- `frontend/src/__tests__/features/freezone/canvas-chat-commands.test.ts`（`node_detail` 完整正文断言）

---

## 2. 已证伪并回退：手势中生成灰壳

**假设**：`LodShellNode` 在任何缩放档下，手势期间新进视口的节点都先挂成灰色 LOD 外壳，
而升级泵在手势期间不放行，所以灰壳会一直挂到松手 —— 这看起来像是「内容层跳动」的来源。

**实测**：在 120 节点画布上以 zoom=1 拖拽，59% 的帧里存在灰壳，最多一帧 8/9 个可见节点
全是灰壳，松手后还要约 300ms 才补齐。假设成立。

**改法**：让手势中的灰壳只在低缩放档生效（低缩放档灰壳本来就是最终形态，无可见翻转）。

**结果：性能反而变差，已回退。**

| 场景（生产构建口径） | 原实现（手势中出灰壳） | 改动后（手势中挂完整组件） |
|---|---|---|
| 24 节点 / zoom 1 / 冷启动首拖 | 166.2 fps，p99 16.7ms | 161.1 fps，p99 27.9ms |
| 60 节点 dense / 冷启动首拖 | **160.6 fps，p99 22ms** | **97.8 fps，p99 94.5ms** |

结论：完整组件挂载的单次成本（约 5ms/节点）远大于灰壳，快速平移时一帧挂多个就直接丢帧。
原实现的取舍是正确的，`LodShellNode.tsx` 里那段注释描述的正是这件事。**不要**为了消除灰壳
闪现而改掉它。

---

## 3. 未复现：内容层平移卡顿

在上述机器（Intel Arc B580 独显，硬件加速正常）上，覆盖了以下全部场景，
**没有一档能复现用户描述的卡顿**：

### 3.1 真实应用、真实画布、真实交互

直接启动 API + 前端，走真实用户路径进入 `虾画` 画布，用真实鼠标中键拖拽。

| 场景 | 挂载节点 | 缩放 | 结果 |
|---|---|---|---|
| 真实画布局部（用户截图同款大图） | 7 | 1.22 | 167–174 fps，p99 11–22ms，8 秒长拖 2 帧超 33ms |
| 真实画布全图（Ctrl+滚轮缩到最小） | 35（31 个灰壳） | 0.10 | 176–179 fps，p99 5.8ms，8 秒长拖 **0** 帧超 33ms |

### 3.2 生产构建 + 构造场景

| 场景 | 结果 |
|---|---|
| 200 节点 dense / zoom 1 | 168.8 fps，p99 16.7ms |
| 300 节点 dense / zoom 0.3（全量挂载） | 111.8 fps，p90 22.1ms，p99 33.4ms，3 帧超 33ms |
| 400 节点 / zoom 0.15（全量挂载） | 110.6 fps，p90 16.8ms |
| 100 节点 / zoom 1 / 带 20 条连线 | 160.4 fps，p99 27.7ms |

唯一明显掉帧的是 **300+ 节点、低缩放档、全量挂载**这一档。但真实画布只有 35 个节点，
离这个量级还有 10 倍差距。

### 3.3 排除的环境因素

- **高 DPI**（`deviceScaleFactor: 2`）：无显著差异。
- **右侧停靠面板的 `backdrop-filter`**（模拟 420px、blur(24px) 覆盖层）：无显著差异。
- **聊天气泡流式输出**（每 40ms 追加文本触发重排）：无显著差异。
- **软件合成**（`--disable-gpu`，SwiftShader）：从 180fps 降到 173.9fps，p99 仍为 5.7ms。

### 3.4 测到的真实开销（供参考）

- **`backdrop-filter` 是最大的单项**：300 节点 dense 场景下关闭它，从 88.4fps 回到 158.4fps。
  节点里共 41 处 `backdrop-blur`（`VideoNode` 8 处、`ImageGenNode` 6 处、`Pano360ViewerNode` 5 处、
  `GroupNode` 4 处等）。手势期间的 `.dc-canvas--panning` 已经会关掉它。
- **手势开始那一下的类翻转本身有成本**：给容器加 `.dc-canvas--panning` 会销毁几十个
  `backdrop-filter` 合成层并重算样式，实测 17–25ms，最差一帧 1354ms。这是「一拖就跳一下」的
  候选解释之一，但在真实画布（7 节点）上未观察到可感知的影响。
- **dev 构建会显著放大挂载成本**：CPU profile 里 `jsxDEV` 占 9.9%。判定「挂载是不是真凶」
  必须以生产构建为准，否则容易把优化做反（第 2 节就是例子）。

---

## 4. 在你的机器上继续定位

代码侧已无可改之处，需要拿到真实读数。按以下步骤操作，把结果发回来即可。

### 4.1 打开画布内置的 FPS 计量

画布右上角有一个**活动图标**（`Activity` 图标）按钮，点击后显示实时 FPS（绿/黄/红三档着色）。
拖动画布时观察这个数字。

- 稳定 ≥50（绿）：主线程没问题，卡顿来自合成/显示层。
- 30–50（黄）：主线程有压力。
- <30（红）：主线程严重阻塞。

### 4.2 确认是否走了硬件加速

在应用页面按 F12 打开 DevTools，Console 里执行：

```js
const c = document.createElement("canvas");
const gl = c.getContext("webgl");
const d = gl.getExtension("WEBGL_debug_renderer_info");
console.log(gl.getParameter(d.UNMASKED_RENDERER_WEBGL));
```

- 输出含真实显卡型号（如 `ANGLE (Intel, Intel(R) Arc(TM) B580 Graphics ...)`）→ 硬件加速正常。
- 输出含 `SwiftShader` → **走了软件渲染**，这是卡顿的直接原因。常见诱因：
  远程桌面会话、虚拟显示器（本机装有 Todesk / GameViewer 虚拟显示适配器）、
  显卡驱动回退、Chrome 的「使用硬件加速」被关闭。

### 4.3 用探针拿归因（需要本机能跑 Node）

```bash
# 1) 起 dev server（必须先起，探针连的是 5199）
cd frontend && pnpm exec vite --port 5199 --host 127.0.0.1

# 2) 另开一个终端跑探针
cd frontend
CANVAS_PERF_CHROME="<你的 chrome.exe 路径>" \
  node e2e/canvas-pan-loaf.mjs --nodes=120 --dense=1 --zoom=1 --real=0
```

输出会给出 `p50/p90/p99/max` 帧时、超 33ms 的帧数，以及最长的几个
`long-animation-frame` 分别被**脚本 / 样式布局 / 光栅化**吃掉了多少毫秒。
`canvas-pan-loaf.mjs` 文件头有完整的参数与环境变量说明。

若要复现「大图重光栅化」这一项，把 2560×1440 的 PNG 放进
`frontend/public/static/__perf/img_01.png` … `img_28.png`（该目录已 gitignore），
然后加 `--real=1`。

---

## 5. 相关文件

| 文件 | 作用 |
|---|---|
| `frontend/e2e/canvas-perf.html` | 只挂真实 `<Canvas>` 的最小压测页（不进生产产物） |
| `frontend/src/features/canvas/dev/perfHarness.tsx` | 压测页逻辑：铺节点、暴露 `window.__canvasPerf` 接口 |
| `frontend/e2e/canvas-pan-loaf.mjs` | 诊断探针：真实拖拽 + 帧时分布 + LoAF 归因 |
| `frontend/src/features/canvas/ui/CanvasFpsMeter.tsx` | 应用内 FPS 计量（画布右上角活动图标） |
| `frontend/src/features/canvas/application/canvasLod.ts` | 手势降级规则的模块级信号与阈值 |
| `frontend/src/features/canvas/nodes/LodShellNode.tsx` | 低缩放档轻量外壳，含「为什么不能在手势中用完整组件」的实测注释 |
