# API 契约

所有产品接口使用 `/api/v1`。JSON 时间为 UTC ISO 8601，资源 ID 为不透明字符串。

以下操作实现了服务端幂等记录，并接受 `Idempotency-Key` 请求头：创建数据源、手动同步、文字/URL 采集，以及治理任务的 apply/skip/undo。相同操作与相同 key 返回第一次成功结果；key 只应重用于同一次业务意图的重试，不要换 payload 后复用。失败的同步运行不会缓存为幂等成功结果，可用同一 key 在故障恢复后重试。`PATCH` 和 `DELETE` 当前没有幂等记录语义。

Owner 接口使用 `Authorization: Bearer <owner-token>`，并兼容
`X-Owner-Token: <owner-token>`。Agent 接口只接受独立的 Agent Bearer，
两种 token 不能互换。

除健康检查、Agent 查询和 MCP 外，本页接口均要求 Owner 凭据。

## 公共健康检查

### `GET /api/v1/health`

```json
{
  "status": "ok",
  "service": "centaurai-pocket",
  "version": "0.1.0"
}
```

## 今日概览

### `GET /api/v1/dashboard`

返回手机首页所需全部数据：

```json
{
  "items": {"total": 120, "ready": 108, "needs_review": 4},
  "sources": {"total": 3, "healthy": 2, "attention": 1},
  "sync": {"discovered_today": 6, "deduplicated_today": 2},
  "pending_tasks": 4,
  "ready_items": 108,
  "quality_score": 96,
  "processed_today": 3,
  "last_sync_at": "2026-07-30T08:00:00Z",
  "next_task": null,
  "recent_activity": []
}
```

`discovered_today` 统计当天生成的新内容代际；`deduplicated_today`
统计命中已有内容指纹并复用条目的来源记录，不把它误称为“更新数”。

## 数据源

- `GET /api/v1/sources`
- `POST /api/v1/sources`
- `GET /api/v1/sources/{source_id}`
- `PATCH /api/v1/sources/{source_id}`
- `DELETE /api/v1/sources/{source_id}`
- `POST /api/v1/sources/{source_id}/sync`
- `GET /api/v1/sync-runs?source_id={source_id}`
- `GET /api/v1/sync-runs/{run_id}`

文件夹数据源示例：

```json
{
  "kind": "folder",
  "display_name": "个人文档",
  "config": {
    "path": "/data/personal-docs",
    "recursive": true,
    "include_hidden": false
  },
  "schedule": "manual",
  "enabled": true
}
```

当前只实现 `kind: "folder"`。`config.path` 是运行 API 的电脑、NAS 或容器所能访问的绝对路径，不是手机本地路径；可选 `extensions` 用于限制扩展名。

扫描器只接收系统识别的 `text/*`，以及内置的 CSV、HTML、INI、Java、JavaScript/TypeScript、JSON、日志、Markdown、Python、RST、SQL、TOML、TXT、XML、YAML 等 UTF-8 文本扩展名；支持的文件才读取、计算 SHA-256 并入库。PDF、DOC/DOCX、图片、音视频等不支持文件计入 `skipped_count`，不创建条目、治理任务或 Agent 索引。RAGFlow/DeepDoc 复杂解析仍是路线图。

暂停的数据源或同一来源已有未超时运行任务时，手动同步返回 `409`。运行内部失败会先把 Sync Run 落为 `failed`，再由 HTTP 返回 `502` 和 `sync_run`；失败不会推进来源的 `last_sync_at`。单文件超过 `CENTAURAI_POCKET_MAX_FILE_BYTES` 时计入 `skipped_count`。

只有完整成功扫描才清理已消失的来源路径。任何尚未归档且失去最后来源的条目
都可创建 `kind: "deletion"` 的 pending 治理任务，有未归档 superseding
generation 时除外。若条目仍有 pending 普通 review，服务会先把旧 review 标记为
skipped，再创建 deletion。

## 条目

- `GET /api/v1/items?state=ready&query=合同`
- `GET /api/v1/items/{item_id}`
- `PATCH /api/v1/items/{item_id}`

可编辑字段仅包括 `title`、`category`、`tags` 和 `state`。存在 pending 治理任务时，不能用条目 PATCH 绕过任务直接进入 `ready`。
`state` 的有效值是 `inbox`、`needs_review`、`ready`、`archived`；没有
`rejected` 状态。

## 治理任务

- `GET /api/v1/governance/tasks?status=pending&limit=20`
- `GET /api/v1/governance/tasks/{task_id}`
- `POST /api/v1/governance/tasks/{task_id}/apply`
- `POST /api/v1/governance/tasks/{task_id}/skip`
- `POST /api/v1/governance/tasks/{task_id}/undo`

任务状态只有 `pending`、`applied`、`skipped`。`undo` 会把最近一次 apply/skip
恢复为 pending。普通 review 的 apply 必须让条目最终进入 `ready`；`kind:
"deletion"` 的 apply 无论通用客户端传什么 state 都只会进入 `archived`。跳过
deletion 只维持条目的当前状态和可见性：原本 `ready` 才继续供 Agent 查询，
`needs_review` 仍不可见。

`apply` 可带用户编辑后的 patch：

```json
{
  "patch": {"title": "2026 年家庭保险保单", "tags": ["保险", "家庭"]}
}
```

成功响应返回当前任务以及 `next_task`，减少手机端往返。

另有兼容聚合入口：

- `POST /api/v1/governance/tasks/{task_id}/actions`

请求体使用 `{"action": "apply|skip|undo", "patch": {...}}`。独立动作端点和聚合端点都兼容请求体中的 `idempotency_key`，但移动端统一使用请求头。

## 文字与 URL 采集

- `POST /api/v1/captures`
- `POST /api/v1/imports/text`（同一实现的兼容别名）

```json
{
  "title": "来自手机的链接",
  "text": "稍后阅读",
  "url": "https://example.com/article",
  "mimeType": "text/uri-list",
  "origin": "mobile-share"
}
```

`text` 与 `url` 至少有一项非空。推荐携带稳定的 `Idempotency-Key`；相同 key 的重试返回第一次成功结果，不重复生成条目。请求体中的 `idempotency_key` 仅为兼容输入。

当前没有 `/imports/file`、`GET /imports/{id}`、附件上传或二进制解析接口。手机系统分享只接收文字和网页 URL；文件、图片和 PDF 上传属于路线图。

## Agent

### `POST /api/v1/agent/search`

请求头：

```text
Authorization: Bearer cp_live_<secret>
```

请求：

```json
{
  "query": "我的医疗保险报销上限是多少？",
  "limit": 8,
  "filters": {"tags": ["保险"]}
}
```

响应还包含 `query`、`count` 和固定值 `visibility: "ready_only"`：

```json
{
  "query": "我的医疗保险报销上限是多少？",
  "results": [
    {
      "item_id": "item_...",
      "title": "家庭医疗保险",
      "snippet": "……",
      "source": "个人文档/insurance.md",
      "score": 0.91,
      "updated_at": "2026-07-30T08:00:00Z"
    }
  ],
  "count": 1,
  "visibility": "ready_only"
}
```

查询始终隐式附加 `state = ready`，客户端不能绕过。
`filters.tags` 和 `filters.category` 都是可选项；提供多个 tags 时，结果必须同时
包含全部指定标签。`limit` 范围为 1–50，默认 8。

MVP 同时只有一个 Agent token。未设置 `CENTAURAI_POCKET_AGENT_TOKEN` 时，首次启动会在数据目录生成 `agent-token`；设置环境变量时则直接使用环境变量值。

Owner 管理接口：

- `GET /api/v1/agent/token`：只返回 `prefix` 与 `mode`（`generated` 或 `environment`）。
- `POST /api/v1/agent/token/rotate`：仅适用于 `generated` 模式，立即替换内存和文件中的 token，返回一次完整新 `token` 与 `prefix`，旧 token 立即失效。

`environment` 模式调用轮换端点返回 `409`；应修改环境变量并重启服务。多 Access Key、逐 key 撤销和 last-used 记录是下一阶段。

### MCP

`POST /api/v1/mcp` 已实现 MCP `2025-06-18` JSON-RPC：

- `initialize`
- `ping`
- `notifications/initialized`
- `tools/list`
- `tools/call`

这是最小、无状态的单消息 HTTP POST 实现；不支持 JSON-RPC batch、GET/SSE
传输、会话恢复或资源/提示词端点。

唯一工具为 `knowledge_retrieve`，输入同样是 `query`、`limit` 和可选
`filters.tags/category`。`dataset_ids` 不需要也不接受，默认检索个人 vault
的所有 ready 数据。HTTP 使用与 `/agent/search` 相同的 Agent Bearer；
`initialize` 请求可以不带协议头，之后的所有请求必须携带
`MCP-Protocol-Version: 2025-06-18`。错误或不支持的协议版本返回 `400`。

如果请求携带 `Origin`，其去除末尾 `/` 后必须与
`CENTAURAI_POCKET_CORS_ORIGINS` 中某个精确 Origin 相同，否则返回 `403`。
这项端点校验与浏览器 CORS 中间件并存，用于降低 DNS rebinding 风险；没有
`Origin` 的非浏览器客户端仍必须通过 Agent Bearer 认证。

MCP 与 REST Agent 查询都在 API 的 8718 端口；8720 只是未来独立 Gateway 的预留端口。

## CORS

`CENTAURAI_POCKET_CORS_ORIGINS` 是逗号分隔的精确 Origin 列表，启动时会去掉每项末尾的 `/`。Origin 只包括 scheme、host 和可选 port，不要写路径，也不要使用 `*` 与凭据模式组合。

默认值是：

```text
http://localhost:8081
http://127.0.0.1:8081
http://localhost:19006
http://127.0.0.1:19006
```

生产 Web 前端或反向代理使用其他 Origin 时必须显式配置。CORS 不是认证机制，Owner/Agent token 仍然必需。
