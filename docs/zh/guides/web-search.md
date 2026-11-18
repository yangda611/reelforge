# 虾导联网搜索

虾导（默认虾导与虾画里的虾导）可以联网检索公开网页与图片，并能把找到的图片
导入画布。本文说明配置方式、工具行为和安全边界。

## 配置

联网搜索使用[博查 Web Search API](https://open.bochaai.com)。到博查开放平台
注册并在「API KEY 管理」创建 Key，然后写进 `.env`：

```bash
BOCHA_API_KEY=sk-xxxx
```

可选项：

| 变量 | 默认 | 说明 |
|---|---|---|
| `BOCHA_API_KEY` | 空 | 博查 API Key；留空时联网搜索工具明确提示未配置，其余功能不受影响 |
| `WEB_SEARCH_BASE_URL` | `https://api.bochaai.com` | 兼容博查 `/v1/web-search` 契约的自建/中转地址 |
| `WEB_SEARCH_TIMEOUT_SECONDS` | `20` | 搜索请求超时 |

搜索凭证只在 API 进程里使用：两个 Agent 后端（Codex MCP、Hermes 插件）都通过
本轮的 agent token 调 `/api/v1/web/*` 路由，插件与 MCP 子进程本身不持有 Key，
也不需要额外出网配置。

## 工具

Agent 拿到三个工具（Codex 与 Hermes 同名同参）：

| 工具 | 用途 |
|---|---|
| `web_search` | 联网检索网页与图片，返回标题、链接、摘要、发布时间、图片直链 |
| `web_page_read` | 读取一个公网页面的正文纯文本（默认最多 8000 字），用于摘要不够时深入核对 |
| `web_image_import` | 把一张公网图片下载进当前项目 `freezone/_uploads/`，返回本项目静态地址 |

典型用法：

- 「查一下 XX 的最新消息」→ `web_search`，回答里转述检索结论并附来源链接。
- 「这条新闻的原文怎么说」→ `web_page_read`。
- 「帮我找几张赛博朋克城市的参考图放进画布」→ `web_search`（用 `images`），
  对选中的 `image_url` 调 `web_image_import`，再把返回的 `url` 写进画布
  图片/上传节点的 `imageUrl`。

为什么必须先导入再放画布：画布节点存的是项目内静态地址，跨项目/外部直链会被
画布媒体作用域守卫拒绝（其他成员打开时也会 403）。`web_image_import` 由服务端
代为下载，节点持久化且对协作者可见。

## 安全边界

- **只允许公网地址**。`web_page_read` 与图片导入复用下载层的公网目的地校验：
  解析、固定 IP、重定向逐跳复查，内网/保留地址（含 `127.0.0.1`、`169.254.x`、
  `10.x` 等）一律拒绝。
- **只读网页文本**。非文本响应（二进制）会被拒绝；正文抽取丢弃脚本与样式，
  单页字节与返回字符数均有上限。
- **不新增业务写入边界**。联网搜索是独立 MCP server / 插件，不进
  `dramaclaw` 工具集；唯一落盘的 `web_image_import` 走既有鉴权路由，写进
  当前项目自己的 `_uploads`，文件名做清洗、内容做图片签名嗅探。
- **凭证不进 Agent 进程**。`BOCHA_API_KEY` 只在 API 进程读取；子进程环境由
  `build_model_child_env` 洗过后下发，即使配错也不会把搜索 Key 带进模型网关
  环境。

## 本机外部 Agent（可选）

让本机 Codex/Claude 通过 MCP 使用联网搜索时，加一个 server：

```json
{
  "mcpServers": {
    "reelforge-web": {
      "command": "/path/to/reelforge/.venv/bin/python",
      "args": ["-m", "novelvideo.chat.web_mcp"],
      "cwd": "/path/to/reelforge",
      "env": {
        "DRAMACLAW_API_URL": "http://127.0.0.1:8780",
        "DRAMACLAW_CE_OWNER": "1"
      }
    }
  }
}
```

`DRAMACLAW_CE_OWNER=1` 与 `dramaclaw` MCP 的约定一致：只允许回环地址；非回环
地址需要 `DRAMACLAW_CE_OWNER_ALLOW_REMOTE=1`（不安全）或改用 agent token。
