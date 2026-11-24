# 剧本全链路拆解与工作流直建规范

当用户提供剧本/小说/剧情大纲，并要求：
- 提取人物、道具、场景并分别创建节点、生成生图提示词；
- 按剧情节奏拆分为多个 **不超过 30 秒（≤30s）的 Clip**；
- 为每个 Clip 衍生创建对应的视频生成节点与视频提示词；
- 将每个 Clip 所依赖的人物、道具、场景资产节点全部正确连线；
- 对全画布节点按类别分组整理并消除重叠。

按下述 **零试错单趟规范** 执行。不要先用 `freezone_emit_canvas_command` 混建 `imageGenNode + videoNode`（插件会因 `_looks_like_handwritten_workflow_batch` 直接拒收），也不要走 10 题生成参数问卷。

## 1. 两类请求的精确选路

- **A. 仅建纯文本/分镜文案卡或整理重排已有画布**（不新建 `imageGenNode + videoNode` 混合流水线）：
  - 直接用 `freezone_organize_canvas` / `freezone_layout_nodes(mode="auto")` / `freezone_emit_canvas_command`（仅 `textAnnotationNode` + `create_edge` + `group_nodes` + `layout_nodes`）。
- **B. 剧本拆解 + 生图资产节点 + ≤30s Clip + `videoNode` 视频节点**：
  - 直接走一次通过的 `freezone_workflow_plan.v1` 通道（禁止调 `freezone_emit_canvas_command` 混写生图与视频节点）：
    1. 调 1 次 `freezone_get_workflow_skill`：`{"skill_id": "text-to-image-video"}`，拿到 `version`。
    2. 调 1 次 `freezone_begin_agent_product_generation`：`{"product_kind": "workflow_result", "generation_session_id": "<本轮唯一会话串>", "artifact_id": "text-to-image-video@<version>", "skill_id": "text-to-image-video", "skill_version": "<version>", "normalized_inputs": {"user_goal": "<简要目标>"}}`。
    3. 调 1 次 `freezone_prepare_workflow_plan_draft`：`{"canvas_id": "<canvas_id>", "operation_id": "<operation_id>", "run_after_create": false, "plan": {...}}`——**完全省略 `generation_answers`、`expected_node_count`、`expected_node_counts`**！
    4. 若当前是 `auto_execute` 模式或用户明确要求直接创建不要停顿确认，拿到 `draft_id` 与 `revision` 后**立即在同一轮调用 `freezone_confirm_workflow_draft(draft_id=..., revision=...)` 落画布**。

## 2. `freezone_workflow_plan.v1` 精确 JSON 模板（严格照抄字段名与大小写）

```json
{
  "schema_version": "freezone_workflow_plan.v1",
  "skill": {"id": "text-to-image-video", "version": "<version>"},
  "inputs": {"user_goal": "<简要目标>", "video_generation_mode": "allReference"},
  "nodes": [
    {
      "id": "outline",
      "node_type": "textAnnotationNode",
      "data": {
        "stage": "planning",
        "displayName": "剧本策划大纲",
        "content": "...",
        "workflowCatalog": {"recipeId": "video-creative-outline"}
      }
    },
    {
      "id": "char_1",
      "node_type": "imageGenNode",
      "data": {
        "displayName": "人物：...",
        "prompt": "...",
        "model": "recommended",
        "workflowCatalog": {"recipeId": "general-image"}
      }
    },
    {
      "id": "clip1_vid",
      "node_type": "videoNode",
      "data": {
        "displayName": "Clip1 视频(20s)",
        "prompt": "...",
        "model": "recommended",
        "genMode": "allReference",
        "durationSec": 20,
        "workflowCatalog": {"recipeId": "general-video"}
      }
    }
  ],
  "edges": [
    {"source": "outline", "target": "char_1", "link_type": "prompt_for"},
    {"source": "char_1", "target": "clip1_vid", "link_type": "media_input_for"}
  ],
  "groups": [
    {"label": "人物资产", "node_ids": ["char_1", "..."]}
  ]
}
```

硬约束清单：
1. **字段名严格精确**：节点必须用 `node_type`（不是 `type`）和 `data.workflowCatalog.recipeId`（不是顶层 `recipeId`）；边必须用 `source` / `target` / `link_type`；分组放在顶层 `groups` 数组且每个组 `node_ids` 至少 2 个节点（只有 1 个节点单独放画布，不要塞进 `<2` 成员的 `groups`）。
2. **`video_generation_mode` 严格用驼峰 `"allReference"`**（不是 `"all_reference"`），且与每个 `videoNode` 的 `data.genMode: "allReference"` 完全一致；`durationSec` 严格 `<= 30`。
3. **不要传 `generation_answers: {}`，也不要传 `expected_node_count` / `expected_node_counts`**。
4. **零孤立资产自检**：提交前逐一核对每一个人物、道具、场景节点：必须至少有一条出边（`media_input_for`）连向真正使用它的 Clip 关键帧或 `videoNode`，绝不漏掉任何一个道具。

## 3. 选中自定义/导入 Skill 与大画布（20–50+ 节点）分批扩建铁律

1. **原样对齐所选 Skill 的版本、必填输入与可用 Recipe**：
   - 先调 1 次 `freezone_get_workflow_skill(skill_id=<目标Skill>)`，把返回的 `skill.version` **原样一字不改**（如 `"1.0.0"`，绝不简写成 `"1"`）同时填入 `freezone_begin_agent_product_generation` 和 `plan.skill.version`。
   - 检查 `input_contract.required`：若包含额外必填项（如 `narrative_goal`、`emotional_goal`），必须一并写入 `plan.inputs`。
   - 每个节点的 `data.workflowCatalog.recipeId` 必须取自该 Skill 的 `available_recipes[*].id`（如导入分镜 Skill 常用 `storyboard-shot-plan-text` / `general-image` / `storyboard-image-to-video-motion-prompt`，不得跨 Skill 混用 `operation_id`）。
2. **连线 `link_type` 精确矩阵（零容错）**：
   - `textAnnotationNode -> textAnnotationNode`：`"context_for"`
   - `textAnnotationNode -> imageGenNode / videoNode / audioNode`：**`"prompt_for"`**（严禁用 `"context_for"`！）
   - `imageGenNode / videoNode / audioNode -> imageGenNode / videoNode`：**`"media_input_for"`**（满足 `requires_source_media` 的唯一边类型；严禁用 `"composition_input_for"`！）
   - `videoNode -> videoComposeNode`：`"composition_input_for"`
3. **大画布分批提交与防 300s 超时硬规则（新增 ≥20 节点必守）**：
   - **硬性上限**：单次 `freezone_prepare_workflow_plan_draft` 严禁超过 **`18` 个节点或 `35` 条边**！实测单次生成 25–35 个节点的 JSON 会导致模型流式静默缓冲超过 **300 秒** 并直接报「等待模型响应超时」失败，绝不要自作主张“一次性全部写完更简单”！
   - 新增 **≥20 个节点**（如扩展全画布至 50+ 节点）时，必须严格拆分为 **2 个连续小批次**（每批 `15–18` 个节点、`≤ 30` 条边：先完整跑完第 1 批 `begin -> prepare -> confirm` 落画布，再跑第 2 批 `begin(新generation_session_id) -> prepare -> confirm`，第 2 批用独立节点/分组前缀避免重名）。
   - 每节点 `prompt`/`content` 保持 **60–120 字** 的紧凑关键词句，不写冗长散文，仅保留真实资产与对应 Clip 的必要连接。
   - 若某次校验失败，只修报错路径并做一次精简重提；严禁反复重发超大 JSON。
   - 全部新增批次落画布后，再调 1 次 `freezone_organize_canvas(categories=..., prune_empty_groups=true, layout_mode="auto")` 完成全画布分组、清退空组与零重叠排版。
