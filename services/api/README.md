# CentaurAI Pocket API

CentaurAI Pocket 的单用户私有数据治理后端。它是独立服务，不导入或复用
RAGFlow、CentaurAI DataHub 或旧 `centaurAI-database` 的运行时代码和数据目录。

## 本地启动

```bash
uv sync --group dev
uv run centaur-pocket-api
```

默认监听 `127.0.0.1:8718`，API 前缀为 `/api/v1`，数据存放在
`~/.local/share/centaurai-pocket`（设置了 `XDG_DATA_HOME` 时使用其下的
`centaurai-pocket`）。没有通过环境变量提供 token 时，首次启动会在数据目录
分别生成 `owner-token` 和 `agent-token`，并尝试设置为 `0600`。

常用环境变量：

- `CENTAURAI_POCKET_DATA_DIR`：覆盖数据根目录。
- `CENTAURAI_POCKET_OWNER_TOKEN`：显式设置 Owner 控制令牌。
- `CENTAURAI_POCKET_AGENT_TOKEN`：显式设置 Agent 只读令牌。
- `CENTAURAI_POCKET_HOST`、`CENTAURAI_POCKET_PORT`：监听地址和端口。
- `CENTAURAI_POCKET_MAX_FILE_BYTES`：单文件扫描上限，默认 20 MiB。
- `CENTAURAI_POCKET_SCHEDULER_POLL_SECONDS`：自动同步调度检查间隔；
  设为 `0` 可关闭。
- `CENTAURAI_POCKET_CORS_ORIGINS`：逗号分隔的精确浏览器 Origin 白名单；
  MCP 请求若带 `Origin` 也使用同一白名单校验。

空字符串 `CENTAURAI_POCKET_DATA_DIR` 会安全回退默认私有目录；不覆盖默认值时
仍建议完全不定义。
API 不会自动加载仓库根 `.env`，环境变量需要由 shell、服务管理器或容器显式提供。

## 数据流

文件夹数据源同步后会按 SHA-256 去重，并为新内容生成待处理治理任务。用户可以
先编辑标题、分类和标签；存在 pending 任务时，必须通过 apply 动作确认后才能进入
`ready`，不能用条目 PATCH 绕过质量门。Agent 检索严格只返回 `ready` 数据；
`needs_review`、`inbox`、`archived` 状态均不可见。

完整成功扫描发现尚未归档的条目失去最后来源时，会生成 `deletion` 治理任务，
不会立即移除私人内容；存在 superseding generation 时除外。服务会先结束该条目
旧的 pending review。apply deletion 只会归档；skip 维持原状态和可见性，只有
原本 `ready` 的条目继续供 Agent 查询。来源重新出现时 pending deletion 自动
标记为 skipped。普通 review 不能借用 `archived` patch 改变上述边界。

MVP 只接收 UTF-8 文本：支持系统识别的 `text/*`，以及内置的 CSV、HTML、INI、
Java、JavaScript/TypeScript、JSON、日志、Markdown、Python、RST、SQL、TOML、
TXT、XML、YAML 等扩展名。PDF、DOC/DOCX、图片、音视频等不支持文件直接计入
`skipped_count`，不会读取正文、计算内容指纹、创建条目/治理任务或进入 Agent
索引。

手机端分享与离线队列可调用 `POST /api/v1/captures`；兼容入口
`POST /api/v1/imports/text` 使用相同请求和幂等语义。请求只接收 JSON 文字/URL，
且二者至少一项非空；当前没有 `/imports/file`、附件上传或导入查询接口。

## 凭据

- Owner 接口接受 `Authorization: Bearer <owner-token>` 或
  `X-Owner-Token: <owner-token>`。
- Agent 查询与 MCP 只接受独立的 `Authorization: Bearer <agent-token>`。
- 两个 token 必须不同，且不能互换权限。

MVP 同时只有一个 Agent token：

- `GET /api/v1/agent/token` 由 Owner 调用，只返回前缀和 `generated` /
  `environment` 管理模式。
- `POST /api/v1/agent/token/rotate` 由 Owner 调用，在自动生成模式立即写入新
  `agent-token`、替换内存值并使旧 token 失效；完整新值随响应返回。
- 环境变量托管模式在线轮换返回 `409`，需修改
  `CENTAURAI_POCKET_AGENT_TOKEN` 并重启。

当前 token 由权限受限的本地文件或环境变量托管，不是数据库 hash、多 Key 列表、
逐 key 撤销或 last-used 审计实现。

## Agent 与 MCP

`POST /api/v1/agent/search` 和 `POST /api/v1/mcp` 都严格只返回 `ready` 条目，
并共用 8718 端口；8720 只是未来 Gateway 的预留端口。

MCP 实现协议 `2025-06-18` 和 `initialize`、`ping`、
`notifications/initialized`、`tools/list`、`tools/call`。唯一工具是
`knowledge_retrieve`，不接受 `dataset_ids`。它是无状态的单消息 HTTP POST，
不提供 GET/SSE、JSON-RPC batch 或会话恢复。`initialize` 后的所有请求都必须带：

```text
MCP-Protocol-Version: 2025-06-18
```

带 `Origin` 的 MCP 请求还必须精确命中 `CENTAURAI_POCKET_CORS_ORIGINS`，
否则返回 `403`；无 `Origin` 不代表匿名访问，Agent Bearer 始终必需。

运行测试：

```bash
uv run pytest
uvx ruff check src tests
```
