# API 契约

所有产品接口使用 `/api/v1`。JSON 时间为 UTC ISO 8601，资源 ID 为不透明字符串。

以下操作实现了服务端幂等记录，并接受 `Idempotency-Key` 请求头：创建数据源、手动同步、文字/URL 采集，以及治理任务的 apply/skip/undo。相同操作与相同 key 返回第一次成功结果；key 只应重用于同一次业务意图的重试，不要换 payload 后复用。失败的同步运行不会缓存为幂等成功结果，可用同一 key 在故障恢复后重试。`PATCH` 和 `DELETE` 当前没有幂等记录语义。

Owner 接口使用 `Authorization: Bearer <owner-token>`，并兼容
`X-Owner-Token: <owner-token>`。Agent 接口只接受独立的 Agent Bearer，
两种 token 不能互换。

除健康检查、Agent 查询、MCP，以及本文明确标出的双通道邀请页和 scoped task
agreement/change/execution 接口外，本页接口均要求 Owner 凭据。邮件域和超级秘书
Workspace 也可由已配对、持有有效 Owner Device 会话的设备调用；其 `X-Device-ID`
必须与 Bearer 会话绑定，本身不是认证凭据。Owner token 调用中的该头只表示本次
调用设备。配对设备只能访问默认工作区 `ws_default`。

## 公共健康检查

### `GET /api/v1/health`

```json
{
  "status": "ok",
  "service": "centaurai-pocket",
  "version": "0.3.0"
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

当前支持 `kind: "folder"` 和个人微信可见 DOM 观察器。`config.path` 是运行 API 的电脑、NAS 或容器所能访问的绝对路径，不是手机本地路径；可选 `extensions` 用于限制扩展名。

扫描器只接收系统识别的 `text/*`，以及内置的 CSV、HTML、INI、Java、JavaScript/TypeScript、JSON、日志、Markdown、Python、RST、SQL、TOML、TXT、XML、YAML 等 UTF-8 文本扩展名；支持的文件才读取、计算 SHA-256 并入库。PDF、DOC/DOCX、图片、音视频等不支持文件计入 `skipped_count`，不创建条目、治理任务或 Agent 索引。RAGFlow/DeepDoc 复杂解析仍是路线图。

暂停的数据源或同一来源已有未超时运行任务时，手动同步返回 `409`。运行内部失败会先把 Sync Run 落为 `failed`，再由 HTTP 返回 `502` 和 `sync_run`；失败不会推进来源的 `last_sync_at`。单文件超过 `CENTAURAI_POCKET_MAX_FILE_BYTES` 时计入 `skipped_count`。

只有完整成功扫描才清理已消失的来源路径。任何尚未归档且失去最后来源的条目
都可创建 `kind: "deletion"` 的 pending 治理任务，有未归档 superseding
generation 时除外。若条目仍有 pending 普通 review，服务会先把旧 review 标记为
skipped，再创建 deletion。

微信网页观察器来源示例：

```json
{
  "kind": "wechat_visible_web",
  "display_name": "本人微信网页观察器",
  "config": {"capture_mode": "visible_dom"},
  "schedule": "continuous",
  "enabled": true
}
```

该来源不使用 `/sync`，而由本机 Collector 持续提交当前已渲染的消息。Owner 管理接口：

- `POST /api/v1/sources/{source_id}/pairings`：创建约 10 分钟有效、只返回一次明文的配对码；
- `DELETE /api/v1/sources/{source_id}/pairings/{pairing_id}`：撤销配对记录；已经换取
  Collector token 后，该操作不等于撤销 token；
- `POST /api/v1/sources/{source_id}/pause`；
- `POST /api/v1/sources/{source_id}/resume`；
- `GET /api/v1/sources/{source_id}/observer-status`；
- `GET /api/v1/sources/{source_id}/coverage-gaps?limit=50&offset=0`。

Collector 使用配对码在
`POST /api/v1/collectors/v1/sources/{source_id}/handshake` 换取来源专用 Bearer，
之后向同一前缀的 `/heartbeat` 和 `/events` 提交心跳与事件。这三个端点供受约束的
Native Host 使用，不接受 Owner token 代替 Collector 凭据。批次要求先有对应浏览器
session 的心跳，并按 `batch_id` 和 provider 消息 ID 幂等。

网页观察器不是完整历史接口。它不自动点击或滚动会话，不读取 Cookie 或网络协议，
只观察 `wx.qq.com` 当前 `#chatArea` 中实际渲染且能可靠识别的节点。完整安装、字段语义、
覆盖缺口和安全边界见 [IM 数据源、治理与微信网页观察器](im-data-sources.md)。

## IM 会话与知识候选

以下 Owner 接口用于查看已观察的会话和原始消息：

- `GET /api/v1/im/conversations?source_id={source_id}&limit=50&offset=0`；
- `GET /api/v1/im/conversations/{conversation_id}/messages?limit=100&offset=0`；
- `PATCH /api/v1/im/conversations/{conversation_id}/policy`。

新会话策略固定默认 `agent_enabled: false`、`retention_days: 365`。策略更新示例：

```json
{
  "agent_enabled": true,
  "retention_days": 180
}
```

`retention_days` 范围为 1–3650。修改该字段不会立即删除消息；Owner 可先预览，再用
固定确认值显式执行本机清理：

- `GET /api/v1/maintenance/retention-preview`；
- `POST /api/v1/maintenance/retention-apply`，body 为
  `{"confirm":"delete_expired_messages"}`。

清理以消息的 `sent_at`（缺失时用 `observed_at`）和会话策略计算，删除过期消息、级联
版本/附件/FTS、无证据的非 confirmed 候选、孤立采集事件，以及已没有消息支撑的成员与
身份记录。仍被 `confirmed` 知识引用的消息会保留并计入
`protected_evidence_count`。当前没有自动清理调度器，也不会清理备份或外部系统，因此
接口结果不能单独作为企业合规删除证明。

明确措辞可能产生 `decision`、`commitment` 或 `task` 候选。候选默认
`provisional`，并返回消息级 evidence：

- `GET /api/v1/knowledge/candidates?status=provisional&conversation_id={conversation_id}`；
- `POST /api/v1/knowledge/candidates/{candidate_id}/confirm`；
- `POST /api/v1/knowledge/candidates/{candidate_id}/dismiss`。

候选状态为 `provisional`、`confirmed`、`dismissed` 或 `superseded`。确认只改变知识
候选状态，不改写原消息，也不自动打开会话的 Agent 权限。原始聊天入库不等于 Agent
可见，检索层仍必须同时校验会话策略、治理状态和证据。

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

## Outlook 邮件

邮件连接器使用 `/api/v1/mail`，只允许 Owner 或已配对 Device。所有响应使用
`Cache-Control: no-store`；不得把 Graph token、远端 ID、验证 marker、本地密文路径或
原始 HTML 暴露给客户端。所有 `POST`、`PATCH` 等写操作必须同时携带
`Idempotency-Key` 和 `X-Device-ID`；修改已有资源还必须携带 `If-Match`，其版本须与
请求体 `expected_version` 一致。配对设备的 `X-Device-ID` 与 Bearer 会话绑定，Owner
请求中则只是调用设备标识；两者都会进入幂等 payload，但都不替代认证。当前不提供
邮件级持久设备审计或可归属到具体设备的发送审计，企业审计属于后续范围。

账户与设备码登录：

- `GET /api/v1/mail/accounts`
- `POST /api/v1/mail/outlook/device-authorizations`
- `GET /api/v1/mail/outlook/device-authorizations/{authorization_id}`
- `POST /api/v1/mail/outlook/device-authorizations/{authorization_id}/poll`
- `POST /api/v1/mail/outlook/device-authorizations/{authorization_id}/cancel`
- `POST /api/v1/mail/accounts/{account_id}/disconnect`

OAuth 使用公共客户端 Device Code Flow，授权范围固定为
`offline_access Mail.ReadWrite Mail.Send`，不请求 `User.Read`，也不使用 client secret。
当前只允许一个活动邮箱账户。OAuth 状态、访问/刷新 token 和 Inbox delta 游标均用本机
私有密钥进行 AES-GCM 加密后持久化；连接器只接受 Microsoft 登录地址和受限的 Microsoft
Graph URL。

收件箱与附件：

- `POST /api/v1/mail/accounts/{account_id}/inbox/delta`
- `GET /api/v1/mail/accounts/{account_id}/inbox?limit=50&offset=0`
- `GET /api/v1/mail/messages/{message_id}`
- `GET /api/v1/mail/messages/{message_id}/body`
- `GET /api/v1/mail/messages/{message_id}/attachments`
- `POST /api/v1/mail/messages/{message_id}/attachments/{attachment_id}/archive`

增量同步只落收件箱元数据；纯文本正文与附件仅在用户打开或明确归档时按需读取。归档只
接受非内嵌的 PDF、UTF-8 纯文本和无宏 OOXML，并继续检查类型、扩展名、压缩包结构和
危险 PDF 标记；通过检查的附件以 AES-GCM 加密保存为 Owner-only 文档。其他格式、宏、
加密/异常压缩包和内嵌附件均拒绝。

任务候选：

- `GET /api/v1/mail/accounts/{account_id}/task-candidates?status=pending&limit=50`
- `POST /api/v1/mail/task-candidates/{candidate_id}/confirm`
- `POST /api/v1/mail/task-candidates/{candidate_id}/dismiss`

候选确认后才创建 Workspace 备忘；扫描到的邮件措辞本身不会直接下达任务。

给原邮件发件人新建邮件与发送：

- `POST /api/v1/mail/messages/{message_id}/reply-drafts`
- `GET /api/v1/mail/drafts/{draft_id}`
- `PATCH /api/v1/mail/drafts/{draft_id}`
- `POST /api/v1/mail/drafts/{draft_id}/prepare`
- `GET /api/v1/mail/send-intents/{intent_id}`
- `POST /api/v1/mail/send-intents/{intent_id}/confirm`
- `POST /api/v1/mail/send-intents/{intent_id}/reconcile`

本轮并不实现 Outlook 的线程回复语义：服务端给增量同步时记录的原邮件 Graph
`Sender` 地址新发一封纯文本邮件，主题加 `Re:`；不读取 `Reply-To`，也不保证
Conversation、`In-Reply-To` 或 `References` 线程关联。客户端只能编辑纯文本正文，
因此它不能被描述为完整的“回复邮件”闭环。发送采用两次明确确认：先 `prepare` 创建并
核验远端草稿，再由用户对最终预览调用 `confirm`；确认 body
固定为 `expected_version`、`preview_hash` 和
`confirmation: "send_exact_preview"`。任何 prepare/send 网络歧义都进入待核验状态，
此后只允许 `GET` 或只读 `reconcile` 查询远端结果，绝不自动重发。

发送意图的 `preview.from_address` 为已核验的 Graph `from`/`sender` 地址，并与
`from_label` 一起展示和参与 `preview_hash`；在尚未取得快照的 `prepare_uncertain`
中它可以为 `null`，从 `ready` 起必须是非空字符串。`ready` 当前没有显式 cancel/abandon
入口，属于已知限制，不能据此宣称完整的发送闭环。

持久的 `prepare_uncertain` 或 `send_uncertain` 只锁定该封回复，并阻止对应账户执行
`disconnect`；Inbox 同步、列表、正文和附件等读取仍可使用。系统不会自动重发，也不会
自动关闭歧义。用户必须先在真实 Outlook 客户端和 Sent Items 中人工核验；未来只能由
高摩擦、可审计的 `close-unresolved` 能力处理。目前没有安全的自助关闭入口，也不支持
或建议直接修改 SQLite 绕过锁定。

所谓“精确预览核验”只覆盖 UI 展示的信封字段（包括已核验 `from_address`）、纯文本正文、唯一 marker、无附件、
`replyTo` 为空、送达/已读回执均为 false，以及 `importance=normal`。成功后还要求在
Sent Items 中找到唯一 marker 和相同完整内容；服务已取得初始远端 ID 的正常确认发送
还必须匹配同一不可变消息 ID。若 prepare 响应丢失而没有初始 ID，恢复只能证明唯一
marker 与上述限定内容，邮件也可能由其他 Outlook 客户端手工发送，因此
`sent_items_verified` 不代表一定由本服务执行了发送。它不保证 Microsoft
Graph 的读取与 `/send` 原子化，也不声称枚举或固定全部 Exchange/MAPI 属性；Graph
发送前最后一次 GET 与 POST `/send` 之间存在不可避免的 TOCTOU 窗口，同邮箱的其他
客户端、Exchange 传输规则及隐藏 MAPI 属性仍是残余风险。`sensitivity` 不是本轮所用
Graph message 的顶层核验字段，因此不纳入本轮承诺。

## Agent

### `POST /api/v1/agent/search`

请求头：

```text
Authorization: Bearer cp_live_<secret>
```

请求：

```json
{
  "query": "最终选择哪套报价？",
  "limit": 8,
  "filters": {
    "source_ids": ["src_..."],
    "conversation_ids": ["conv_..."],
    "participant_ids": ["wxid_..."],
    "sent_from": "2026-07-01T00:00:00+08:00",
    "sent_to": "2026-07-31T23:59:59+08:00",
    "item_kinds": ["knowledge"]
  }
}
```

响应还包含 `query`、`count` 和可见性说明。知识结果带回逐条消息引用：

```json
{
  "query": "最终选择哪套报价？",
  "results": [
    {
      "kind": "knowledge",
      "candidate_id": "claim_...",
      "title": "已确认决策",
      "snippet": "最终选择 A 套报价",
      "source": "本人微信网页观察器/采购讨论群",
      "source_id": "src_...",
      "conversation_id": "conv_...",
      "conversation_name": "采购讨论群",
      "claim_type": "decision",
      "speaker": "张三",
      "authority": "observed",
      "status": "confirmed",
      "score": 0.91,
      "citations": [
        {
          "type": "im_message",
          "message_id": "msg_...",
          "provider_msgid": "123456",
          "source_id": "src_...",
          "conversation_id": "conv_...",
          "speaker": "张三",
          "sent_at": null,
          "observed_at": "2026-07-30T08:00:00Z",
          "authority": "observed",
          "role": "primary"
        }
      ]
    }
  ],
  "count": 1,
  "visibility": "ready_and_opted_in_im"
}
```

结果 `kind` 是 `document`、`im_message` 或 `knowledge`：

- `document` 始终隐式过滤 `state = ready`；
- `im_message` 只来自 `agent_enabled=true` 的会话，并附消息级 citation；
- `knowledge` 还要求候选 `status=confirmed`，并附完整 evidence citations。

`visibility` 在结果包含已允许的 IM 时为 `ready_and_opted_in_im`，否则为
`ready_only`。客户端不能通过筛选绕过这些隐式门。

所有过滤项都可省略。`item_kinds` 可包含上述三种值；空列表表示全部。
`source_ids` 适用于文件与 IM；`conversation_ids`、`participant_ids`、`sent_from` 和
`sent_to` 是 IM 专用筛选，提供时不返回文件结果。IM 时间筛选优先使用 `sent_at`，缺失
时使用 `observed_at`。`tags` 和 `category` 只适用于文件结果；提供多个 tags 时，结果
必须同时包含全部指定标签。每个 ID 列表最多 50 项，`limit` 范围为 1–50，默认 8。

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
的所有 ready 文件以及 Owner 已明确允许的 IM；知识候选仍必须是 `confirmed`。
MCP 当前没有 REST 的会话、参与者、时间和 `item_kinds` 精细筛选。HTTP 使用与
`/agent/search` 相同的 Agent Bearer；
`initialize` 请求可以不带协议头，之后的所有请求必须携带
`MCP-Protocol-Version: 2025-06-18`。错误或不支持的协议版本返回 `400`。

如果请求携带 `Origin`，其去除末尾 `/` 后必须与
`CENTAURAI_POCKET_CORS_ORIGINS` 中某个精确 Origin 相同，否则返回 `403`。
这项端点校验与浏览器 CORS 中间件并存，用于降低 DNS rebinding 风险；没有
`Origin` 的非浏览器客户端仍必须通过 Agent Bearer 认证。

MCP 与 REST Agent 查询都在 API 的 8718 端口；8720 只是未来独立 Gateway 的预留端口。

## 超级秘书 Workspace

业务任务与 `governance_tasks` 完全独立。首个单用户工作区固定为 `ws_default`，
Owner 接口使用现有 Owner Bearer；所有 Owner 写操作还必须携带：

```text
Idempotency-Key: <至少 8 字符的稳定请求键>
X-Device-ID: <本设备稳定 ID>
```

同一操作下重复 key + 相同规范请求会返回原响应；重复 key + 不同请求返回 `409`。
签发一次性任务对齐码是例外：仍强制请求键，但服务端绝不持久化或重放明文码；
同一请求键重试返回 `409`。如果首次响应丢失，Owner 必须用新的请求键重建邀请，
新邀请会撤销该任务此前未完成的邀请。这条只适用于 Owner “签发邀请”；不适用于后文
`exchange` 对同 key/body 确定性返回同一任务会话。
PATCH 使用 `If-Match: "<version>"`；动作请求在 body 中使用 `expected_version`，
陈旧版本返回 `412`，缺少 PATCH 前置条件返回 `428`。

同步：

- `GET /api/v1/workspaces/ws_default/bootstrap`
- `GET /api/v1/workspaces/ws_default/sync?after=0&limit=200`
- `PUT /api/v1/workspaces/ws_default/sync/cursor`
- `GET /api/v1/workspaces/ws_default/audit`

工作成员目录：

- `GET /api/v1/workspaces/{workspace_id}` 的 `members` 返回成员的
  `id/workspace_id/kind/role/display_name/contact_ref/active/version/created_at/updated_at`；
- `POST /api/v1/workspaces/{workspace_id}/members` 创建工作成员，返回 `201` 和完整成员。

成员创建只允许 Owner 或已配对设备，不接受 query，不使用 `If-Match`，但必须携带
`X-Device-ID` 和 `Idempotency-Key`。严格请求体为：

```json
{
  "kind": "person",
  "role": "member",
  "display_name": "交付负责人",
  "contact_ref": "wecom://delivery-owner",
  "client_mutation_id": "member-create-local-001"
}
```

`kind` 只能是 `person|team|external`，`role` 只能是 `member|viewer`；该接口禁止创建
第二个 Owner。`display_name` 去除首尾空白后必须为 1–500 字符；`contact_ref` 可为
`null`，非空时最多 2000 字符。相同 key 和相同规范请求精确重放第一次的成员，不重复
成员或事件；同 key 换请求返回 `409`。成功事务追加
`aggregate_type=workspace_member`、`event_type=workspace.member_created` 的版本 1 审计
事件。

业务资源：

- `/members`：工作成员目录创建；
- `/memos`：规范备忘创建、修改和 tombstone 删除；
- `/tasks`：业务任务、步骤状态、生命周期 transition；
- `/tasks/{id}/alignment-invitations`：Owner 为外部承办人签发一次性对齐邀请；
- `/tasks/{id}/execution-invitations`：Owner 为已对齐的外部承办人签发 v7 执行能力；
- `/tasks/{id}/changes` 与 `/changes/{id}/decision`：任务关键字段变更；
- `/calendar`：带时区的日程创建、修改和取消；
- `/meetings`、`/meetings/{id}/minutes`、`/minutes/{id}/decision`：会议、纪要修订与确认。
- `/documents`：文档元数据、单篇正文、合同/工作汇报审阅、模板生成、归档和受控片段。

### P1c-A 备忘落地为任务或 Pocket 日程

P1c-A 只在主人明确确认后把一条服务端备忘落地一次。客户端可按期限、紧急程度、
任务候选类型和固定行动措辞展示可解释建议，但建议不是命令，服务端不会根据建议、
IM 扫描结果或定时任务自动执行物化。专用端点为：

- `POST /api/v1/workspaces/{workspace_id}/memos/{memo_id}/task`；
- `POST /api/v1/workspaces/{workspace_id}/memos/{memo_id}/calendar`。

两条端点只允许 Owner token 或已配对且 Bearer 会话与 `X-Device-ID` 匹配的 Owner
Device；后者只能使用 `ws_default`。路径 ID 必须是安全的不透明标识，不接受 query 或
尾随斜线。请求必须同时携带：

```text
If-Match: "<memo.version>"
Idempotency-Key: <至少 8 字符的稳定请求键>
X-Device-ID: <本设备稳定 ID>
```

body 的严格正整数 `expected_memo_version` 必须等于 `If-Match`；缺少前置条件返回
`428`，陈旧或不一致返回 `412`。相同 actor、端点、memo、key 和规范 body 的成功重试
精确重放第一次 `201` 复合响应；同 key 换 body 返回 `409`。只允许未删除、`active`、
`confirmation_status=not_required|confirmed` 且尚无物化账本的备忘；任务与日程竞争同一
个一次性槽位，并发时最多一个事务成功。

任务请求体只接受以下字段，不能由客户端注入 `source`、`domain`、下达人、验收人、
阶段或来源备忘 ID：

```json
{
  "expected_memo_version": 1,
  "title": "完成合同复核",
  "purpose": "降低合同执行风险",
  "objective": "形成可验收的复核结论",
  "strategy": "逐条核对并记录依据",
  "key_points": ["退出条款", "违约责任"],
  "acceptance_criteria": ["关键条款均有结论"],
  "assignee_member_id": "member_owner",
  "due_at": "2026-08-05T18:00:00+08:00",
  "priority": "high",
  "tier": "standard",
  "confirm_personal_disclosure": false,
  "client_mutation_id": "task-from-memo-local-001"
}
```

服务端固定派生 `origin_memo_id`、备忘 `domain` 与受控 `source`，并把 Owner 设为
issuer 和 acceptance owner；新任务固定为 `draft`、`on_track`、`progress=0`、无
`start_at`/步骤，`summary=purpose`，是否需要对齐由承办人是否为 Owner 决定。承办人
必须是活动的 `owner|member`，不能是 viewer。

个人域备忘委派给非 Owner 时，`confirm_personal_disclosure` 必须严格为 `true`；其他
情况必须为 `false`。该确认只授权把主人填写的任务协议字段交给后续承办人对齐，不授权
披露原始备忘正文、IM 来源引用或摘录。Owner 侧任务仍保留受控来源以供追溯，但外部
承办人的 scoped agreement/exchange 视图不含 `source` 或 `origin_memo_id`。

日程请求体只接受：

```json
{
  "expected_memo_version": 1,
  "title": "合同复核专注时间",
  "description": "集中核对合同条款并记录结论",
  "start_at": "2026-08-04T09:00:00+08:00",
  "end_at": "2026-08-04T10:30:00+08:00",
  "timezone": "Asia/Shanghai",
  "all_day": false,
  "kind": "focus",
  "client_mutation_id": "calendar-from-memo-local-001"
}
```

`description` 必须非空，起止时间必须带时区且结束晚于开始，`timezone` 必须是有效 IANA
时区。服务端派生 `memo_id` 和备忘 `domain`，固定 `task_id/step_id=null`、
`status=scheduled`、`attendees=[]`、`external_provider/external_id=null`。这是 Pocket
内部日程，不创建 Outlook/Google/系统日历事件，也不发送邀请、通知或 RSVP。

成功响应严格为 `{memo, task}` 或 `{memo, calendar_entry}`；顶层 HTTP `ETag` 对应
复合响应中 `status=converted` 的 memo 版本，并且必须是
`expected_memo_version + 1`，不是新目标的版本。目标、来源摘要账本、memo 状态、幂等
响应和事件在同一事务提交；同步事件顺序固定为 `task.created` 或
`calendar.created` 在前，`memo.updated` 在后。

Workspace schema v5 新增 append-only `secretary_memo_materializations`：每个已物化 memo
恰好绑定一个 task 或 calendar entry，保存转换前 memo 版本及只含来源类型/引用、权限
和 SHA-256 摘要的最小来源快照，不保存正文或 excerpt。触发器禁止更新/删除账本、已
物化 memo 及目标链接；converted memo 的 API 修改与删除也返回 `409`。通用
`POST /tasks` 携带非空 `origin_memo_id`、通用 `POST /calendar` 携带非空 `memo_id`
均返回 `409`，不能绕过专用事务。

v5 升级不自动回填任何历史 task 或 calendar 链接：旧版没有完整的原子事件与个人信息
披露证据，不能仅凭关联列补造不可变审计证明。迁移前只要发现任一历史 memo 链接，或
任一没有 v5 账本支撑的 `converted` memo，就会输出诊断并 fail closed，要求人工处置后
重试；只有不存在这两类历史状态的清洁数据库才创建空账本并启用新约束。

文档正文采用按需读取边界：`bootstrap` 不包含 `documents`；
`GET /documents` 只返回不含正文、存档位置、审阅详情和片段正文的元数据摘要，
`GET /documents/{id}` 才返回该文档的完整 Owner/已绑定设备视图。文档同步事件同样
只携带摘要，客户端看到版本变化后按需重新读取单篇文档，不应把完整正文写入秘书
Workspace 的通用持久缓存。

文档写 API：

- `POST /documents`：登记普通文档、合同、工作汇报或模板；
- `PATCH /documents/{id}`：使用 `If-Match` 修改内容、来源或可看范围；正文变化会使
  合同/工作汇报重新进入 `review_pending`，正文或权限变化会撤销旧受控片段；
- `POST /documents/{id}/reviews`：使用 `expected_version` 固化合同或工作汇报的审阅
  摘要、结论、风险发现和建议；
- `POST /documents/{template_id}/generate`：按模板版本严格替换 `{{变量}}`，缺失或
  多余变量均拒绝，并保存模板 ID、模板版本和使用的变量；
- `POST /documents/{id}/excerpts`：按 JavaScript/React Native 的 UTF-16 code-unit
  偏移由服务端截取正文，拒绝切开代理对，只允许把片段授权给文档可看范围内的活跃成员；
- `GET /documents/{id}/excerpts?viewer_member_id=...`：以指定成员的逻辑视角返回仍
  有效的可看片段；该接口仍是 Owner/已绑定设备管理接口，不替代成员身份认证；
- `POST /documents/{id}/archive`：版本化归档并撤销现有片段；归档文档不可继续修改、
  审阅、生成或创建片段。

文档 `access_scope` 为 `owner_only`、`workspace` 或 `restricted`。`restricted` 必须
显式提供活跃的 `viewer_member_ids`；Owner 始终保留管理权限。所有文档写操作均要求
稳定 `Idempotency-Key` 和与手机会话绑定的 `X-Device-ID`，并产生单调 Workspace
事件；同步事件不包含正文等敏感负载。由于个人 Vault 的 `ready` 文件默认可被 Agent
全库检索，服务端拒绝把这类 `source_item_id` 关联到 `owner_only` 或 `restricted`
文档，防止秘书文档 ACL 被 Agent 搜索旁路；只有 `workspace` 文档可直接关联 ready
文件，其他范围需使用尚未开放给 Agent 的来源状态或独立正文。

任务生命周期为：

```text
draft → issued → aligned → in_progress → submitted → accepted
```

`abnormal_closed` 是终态，必须经带原因的任务变更确认；`submitted` 只表示承办人
提交成果，不等于验收。事件以单调 `sequence` 排序，删除通过 `operation=delete`
tombstone 同步。客户端成功落地后再确认游标，游标不得后退。

### 主人自办任务的执行计划

当前步骤编排只覆盖 `issuer_member_id`、`assignee_member_id` 和
`acceptance_owner_id` 均为 `member_owner` 的主人自办任务。外部承办任务必须走包含
完整执行路径与拟排期的独立对齐/变更确认；Owner 不能用本节接口把自己的催办提醒
伪装成承办人的执行时段。Pocket 日程也不等于 Outlook 邀请、参会人 RSVP 或外部日历
已经同步。

主人自办任务提供以下任务聚合命令：

- `POST /tasks/{task_id}/steps`：向末尾新增关键结果、里程碑或行动；
- `PATCH /tasks/{task_id}/steps/{step_id}`：修改步骤结构、描述、期限、成功度量和依赖；
- `POST /tasks/{task_id}/steps/reorder`：一次提交全部步骤 ID，归一化为连续顺序；
- `POST /tasks/{task_id}/steps/{step_id}`：修改步骤真实状态；
- `PUT /tasks/{task_id}/steps/{step_id}/schedule`：创建或改排 leaf action 的 Pocket
  执行时段；
- `POST /tasks/{task_id}/steps/{step_id}/schedule/status`：把执行时段标为
  `completed` 或 `canceled`，但不自动把步骤标为 done。

这些写操作不接受 query，均要求 `X-Device-ID`、`Idempotency-Key` 和
`If-Match: "<task.version>"`。带 `expected_version` 的命令还要求 body 版本与
`If-Match` 完全一致；旧版本返回 `412`。步骤不是独立同步聚合：每次成功写入都只把
父任务版本递增一次，响应及 task 事件包含完整步骤图。相同 key 和相同规范请求精确
重放，相同 key 换请求返回 `409`，失败事务不得留下步骤、依赖、日程、事件或幂等缓存。
新增步骤请求可带 1–200 字符的 `client_mutation_id`；Pocket 将它保存在步骤记录中并
纳入幂等请求散列，供本地客户端变更审计使用。该内部审计字段不进入步骤响应 DTO，
客户端仍以服务端步骤 ID 和父任务版本作为同步事实。

`depends_on_step_ids` 必须属于同一任务；父子图和依赖图均拒绝自环、后代环及跨任务
引用。关键结果必须有非空 `success_metric`。步骤顺序在活动步骤内唯一，服务端迁移会
先按旧 `(position, created_at, id)` 归一化，再建立部分唯一索引。

`calendar.step_id` 是步骤排期的唯一数据库事实源。同一步骤最多一条未删除且
`status=scheduled` 的活动日程；步骤 DTO 的 `schedule_id` 由活动日程即时派生，旧
`secretary_task_steps.schedule_id` 裸列会被清空且不再写入。公共 `/calendar` 创建不
接受 `step_id`，公共 PATCH/cancel 也不能修改已有步骤日程，防止绕过任务版本和原子
事件。步骤排期事务先写 `calendar.created`/`calendar.updated`，再写完整
`task.step_scheduled`；取消或完成同理保留历史，不做静默删除。

### 任务复盘与主动预警

执行阶段的任务可追加复盘记录：

- `POST /api/v1/workspaces/ws_default/tasks/{task_id}/check-ins`
- `GET /api/v1/workspaces/ws_default/tasks/{task_id}/check-ins`
- `GET /api/v1/workspaces/ws_default/task-attention`

这三个接口不接受任何 query 参数，且成功与错误响应都携带
`Cache-Control: no-store, max-age=0`、`Pragma: no-cache`、
`Referrer-Policy: no-referrer` 和 `X-Content-Type-Options: nosniff`。
它们只允许 Owner 或已配对设备访问，不能用 Agent token 调用。

创建复盘必须同时携带 `Idempotency-Key`、`X-Device-ID` 和
`If-Match: "<task-version>"`；`If-Match` 必须与 body 的
`expected_version` 相同。请求示例：

```json
{
  "expected_version": 4,
  "summary": "方案已评审，等待客户确认上线窗口。",
  "reported_progress": 70,
  "risks": ["窗口可能调整"],
  "blockers": ["等待客户确认"],
  "next_actions": ["今天发送上线清单"],
  "forecast_at": "2026-08-06T10:00:00Z",
  "client_mutation_id": "mobile-checkin-001"
}
```

`summary` 为 1–4000 字符；三组清单各最多 50 条、每条 1–2000 字符；
`reported_progress` 为 0–100；`forecast_at` 可省略或为 `null`，非空时必须包含时区。
复盘是 append-only 事实，不修改任务的 stage、health、正式 progress 或 version。
`report_date` 由服务端时钟按 Workspace 的 IANA 时区生成，客户端不能传入或覆盖。
`accepted` 和 `abnormal_closed` 任务拒绝新的复盘；同 key、同 body 的重试精确重放首次
`201` 结果，同 key、不同 body 返回 `409`。成功结果的 `ETag` 固定对应复盘自身
`version=1`，并追加 `event_type=task.checkin_recorded`、
`aggregate_type=task_checkin` 的 Workspace 事件；API DTO 和事件 payload 都不暴露
`device_id`。列表固定返回最新 100 条以及未截断的 `total`：

```json
{
  "items": [{
    "id": "checkin_...",
    "workspace_id": "ws_default",
    "task_id": "task_...",
    "task_version": 4,
    "report_date": "2026-08-02",
    "summary": "方案已评审，等待客户确认上线窗口。",
    "reported_progress": 70,
    "risks": ["窗口可能调整"],
    "blockers": ["等待客户确认"],
    "next_actions": ["今天发送上线清单"],
    "forecast_at": "2026-08-06T10:00:00Z",
    "created_by": "member_owner",
    "version": 1,
    "client_mutation_id": "mobile-checkin-001",
    "created_at": "2026-08-02T02:00:00Z"
  }],
  "total": 1
}
```

`task-attention` 使用服务端当前时钟和 Workspace 时区即时推导，不持久化预警，也不
改变任务。HTTP 客户端不能注入 `as_of`。只评估 `aligned` 和 `in_progress` 任务，
排除下达前、待对齐和所有提交/终态。返回每个需关注任务的正式任务版本与进度、最近
复盘时间/自报进度、最高严重度和原因：

- `plan_missing`：没有未取消的任务步骤；
- `review_due`：执行开始的本地日期早于今天，且今天尚无复盘；
- `step_overdue`：未完成且未取消的步骤已经过期；
- `schedule_missed`：任务或未完成步骤关联的 scheduled 日程已经结束；
- `task_overdue`：任务期限已过；
- `blocked`：任务 health、任一步骤或最新复盘仍显示阻塞；最新复盘的空 blockers 会
  清除更早复盘带来的阻塞，但不会清除当前任务/步骤阻塞，响应最多带 3 条 blocker；
- `forecast_slip`：最新复盘预计时间晚于任务期限；
- `due_soon`：距离期限 0–48 小时且任务正式 `progress < 80`。

原因严重度只有 `warning` 和 `critical`。`reported_progress` 仅供对照，不能覆盖正式
progress，也不能据此清除 `due_soon`。该列表由前端同步主动呈现，而不是后台推送；
当前没有静默后台通知、定时推送或持久化告警状态。

### 任务结果与人员归属分析

Owner 或已配对 Owner 设备可读取一个不落库的期间分析：

`GET /api/v1/workspaces/ws_default/task-analysis?from=2026-08-01&to=2026-08-31`

`from`、`to` 是 Workspace IANA 时区中的含首含尾本地日期，服务端规范化为
`[start_at, end_exclusive_at)`；两项都必须且只能出现一次，不接受其他 query，单次最多
366 天。所选期间超过 50,000 条任务、任务事件或任一类受核对业务行，或工作区超过
50,000 名成员时返回 `413`，要求缩短范围或治理工作区，不静默截断。
Agent 和所有 `cp_task_*` scoped token 均不能调用。成功与错误响应沿用 Workspace 的
`no-store`、`no-referrer`、`nosniff` 边界。

响应 schema 为 `centaur.task-analysis.v1`，只返回有界聚合：

- `period`：规范日期、UTC 边界、Workspace 时区，以及
  `current_assignment_overlapping_period` 任务口径；
- `task_facts`：任务结果事实，包括期间任务总体、验收/按期/逾期/无期限、异常关闭、
  当前风险、返工事件和启动至验收周期样本；这些事实不归功于某个自然人；
- `assignees[].current_assignment_snapshot`：生成时的逻辑承办任务和当前阶段分布，不是
  历史身份归因或“本人完成率”；
- `tasks[].attribution_evidence` 与 `assignees[].attribution_evidence`：分别按
  `start|checkin|step_status|submit|agreement_response|change_response` 动作返回原始事件数、
  已分类数、assignment epoch 数、保证等级分布和由服务端预计算的整数 basis points；
- `coverage`：进入分析、typed 解析、A0、完整性不匹配、排除的安全事件及缺事件业务行
  数量，并固定声明当前不支持强成员身份；
- `assurance_policy`：版本化权重与结论边界。v1 的 A2 Owner control 为 10000 bp、A1
  scoped capability 为 5000 bp、A0 unknown 为 0 bp。该数字是证据展示折扣，不是身份
  概率、工作质量或绩效分。

A2 只来自不可变 agreement/change decision 中明确且绑定正确的
`owner_token|owner_device_session`；历史普通 Owner/member 事件只有 actor/业务字段时仍是
A0，不能事后猜测。A1 execution 事件必须把 session、refresh family、task、member、
device、assignment epoch、有效时间和 `dual_channel_task_execution` 全部匹配；agreement/
change 回应必须匹配对应不可变 decision。错绑、缺字段和未知方法降为 A0 并增加完整性
覆盖计数。邀请、会话签发、refresh 和 security revoke 只计入 coverage，不进入工作动作。
同一不可变 decision 在期间被多个事件引用时整组 fail closed 为 A0；不会任选一条保留为
强证据。execution 撤销不追溯：撤销前的合法事件保留原等级，发生于撤销时点或之后、或
恰好位于 expiry 边界的事件降为 A0。旧 `task.step_updated` 和日程传播事件语义含混，不
算作执行；只有明确的 `task.step_status_updated` 或 typed execution step 事件进入
`step_status`。

该接口不返回 `contact_ref`、原始 event payload、session/family/device ID、check-in 正文、
来源摘录或秘密；客户端不得扫描 `/sync` 重新推算、跨动作合成总分、生成排行，或把分析
持久化为本机人员档案。

### 外部承办人任务对齐

当 `issuer_member_id != assignee_member_id` 时，任务的 `requires_alignment=true`。
Owner 可以把任务从 `draft` 下达到 `issued`，但不能调用普通 transition 冒充承办人
把它改成 `aligned`；该请求固定返回 `409`。最小安全闭环是：

1. Owner 调用
   `POST /api/v1/workspaces/ws_default/tasks/{task_id}/alignment-invitations`，
   body 为 `{"expected_version": 2}`，并携带 `Idempotency-Key` 与 `X-Device-ID`。
2. 首次 `201` 响应包含高熵 `invitation_id`、只返回一次的 12 位 Crockford Base32
   `code`、`expires_at` 和不含 code 的 `confirmation_path`。Owner 只分享路径，并经
   独立通道把 code 交给承办人；code 不得放入 URL、聊天链接参数或日志。
3. 拿到两段凭据的一方打开 `GET /api/v1/task-alignments/{invitation_id}`。页面本身不展示任务
   正文，只通过 form body 接收 code。
4. JSON 客户端可调用 `POST /api/v1/task-alignments/preview`，body 为
   `{"invitation_id":"...","code":"....-....-...."}`。验证成功后 code 立即
   一次性失效，响应返回完整只读对齐包
   `title/purpose/objective/strategy/key_points/acceptance_criteria/due_at`，以及只
   返回一次、最长约 5 分钟有效的 `confirmation_token`。
5. 凭据持有方核对后第二次明确点击；JSON 客户端调用
   `POST /api/v1/task-alignments/confirm`，body 为
   `{"invitation_id":"...","confirmation_token":"..."}`。只有这一步会把
   `issued → aligned`。

preview/confirm 不需要也不接受 Owner Authorization；HTML 页使用同一 JSON 语义的
两步服务端表单。邀请约 10 分钟有效，code 错误 5 次即锁定；code 和 confirmation
token 在 SQLite 中都只保存 SHA-256 hash。旧版邀请、短会话都绑定 task、task_version
和 assignee，过期、重放、任务内容/版本/承办人变化均拒绝。成功事务把
`updated_by` 写为承办人，并追加 `actor_type=member`、
`event_type=task.aligned_by_assignee` 的审计事件。

确认页与 JSON 响应均为 `Cache-Control: no-store`；HTML 还设置 nonce CSP、
`Referrer-Policy: no-referrer`、禁止 frame、外部资源、脚本、摄像头、麦克风和定位。
签发 code 的 Owner 响应同样使用 `no-store` 与 `no-referrer`。
`invitation_id` 为不可枚举的 128-bit 随机标识，但它不是确认凭据。当前最小模型以
“同时持有高熵分享路径和另行传递的短码”提供 A1 级能力持有证明。系统只能按邀请
映射到承办人记录；这不证明现实身份、授权代理、电子签名或不可抵赖意图。Owner
在创建邀请的首次响应中本来就同时看到路径和短码，只要省略 Owner header，也能以
匿名能力持有者完成交换；因此不得把本流程描述为承办人亲自或实名确认。如需更强
保证，必须在后续版本接入 WebAuthn/设备持有证明、组织 IdP 或符合目标法域的签名基础设施。

#### P1b-A 不可变任务协议

新建邀请会创建或复用该任务的 pending 协议 case，并把邀请绑定到当时的不可变完整
revision；旧 v3 未绑定邀请继续走上述兼容路径，不补造历史证明。Owner 可通过
`GET /api/v1/workspaces/{workspace_id}/tasks/{task_id}/agreement` 发现当前/最近协议；
明确 case 读取为 `GET /api/v1/task-agreements/{case_id}`。DTO 只含 case、不可变
revisions/decisions 与协议文档，不含成员联系方式、标签、证据、原始 canonical 字符串、
短码或 token hash。

case DTO 的顶层 key 为 `id/workspace_id/task_id/issuer_member_id/
assignee_member_id/status/current_revision_no/accepted_revision_no/version/created_at/
updated_at/closed_at/current_revision/revisions/decisions`。revision 为
`id/case_id/revision_no/parent_revision_id/base_task_version/schema_version/
proposed_by_role/proposed_by_member_id/required_responder_role/
required_responder_member_id/digest/document/reason/created_at`；decision 为
`id/case_id/revision_id/revision_digest/action/actor_role/actor_member_id/
actor_session_id/assurance_method/reason/counter_revision_id/version/created_at`。
case `status` 只能为 `pending|accepted|rejected|canceled|stale`；只有
`accepted` 的 `accepted_revision_no` 非 null。

协议文档固定 schema `centaur.task-agreement.v1` 和以下精确 key 集合：

```text
schema, workspace_id, task_id, agreement_id, revision_no, parent_digest,
proposer_role, proposer_member_id, responder_role, responder_member_id,
issuer_member_id, assignee_member_id, acceptance_owner_id, domain, tier,
priority, title, purpose, objective, strategy, key_points,
acceptance_criteria, due_at
```

服务端递归执行 Unicode NFC、CRLF/CR → LF、时间转 UTC
秒精度 `Z`，拒绝浮点和额外字段，数组顺序与重复项原样保留，再以 UTF-8、排序 key、
紧凑分隔符序列化；公开摘要
格式严格为 `sha256:<64 个小写十六进制字符>`。共享 golden vectors 位于 API 测试
fixture，并与 Owner 前端向量保持一致。

拿到两段凭据的能力持有方客户端可调用 `POST /api/v1/task-alignments/exchange`：

```json
{
  "invitation_id": "align_...",
  "code": "....-....-....",
  "client_device_id": "assignee-device-1"
}
```

请求必须带 `Idempotency-Key`。服务端使用数据根中独立持久的任务会话 HMAC key，对
session ID、请求键摘要与规范请求摘要做域分离 HMAC，生成 `cp_task_at_` token。该 key
首次升级时可由当时的 Owner secret 受控引导一次，之后不再随 Owner token 轮换，必须
与 SQLite 数据库作为同一备份和恢复集。
成功返回该 scoped access token、首次交换起 10 分钟的绝对过期时间、safe session 投影和
协议。响应 key 为 `token_type/access_token/expires_at/session/agreement`，其中 session 只含
`id/task_id/agreement_id/assignee_member_id/client_device_id/assurance_method/expires_at`。
明文 token 不落库，SQLite 只存 SHA-256 hash。在会话 live 且协议仍 current 时，
同 invitation、code、device、`Idempotency-Key` 和 body 的任意重试/并发都确定性返回
完全相同的 token、session 和原
expiry，不延长会话、不旋转 token hash，也不重复写审计事件。不同 key/body/device 即使 code
正确也会 fail closed：撤销会话并返回 `409`，需新邀请。错误 code 永远不能撤销现有
会话。会话已 closed/revoked 后的任何 exchange 都无副作用地返回 `409`，不改写终态
`revoke_reason` 或邀请状态。对于 `revoked_at` 仍为 null 但已过绝对期限的会话，首次
exchange 只原子填入 `revoked_at/revoke_reason=expired`，不改写邀请，并返回 `409`；
之后的重试无副作用。重算的确定性 token hash 与库内 hash 不符时完整性校验
fail closed，撤销会话/邀请并要求创建新邀请。该 token 没有 refresh、DPoP、跨任务权限或身份声明。

`GET /api/v1/task-agreements/{case_id}` 和
`POST /api/v1/task-agreements/{case_id}/responses` 使用专用鉴权：只接受 case issuer 的
Owner token、配对 Owner-device session，或绑定同 case/task/assignee 的 `cp_task_at_`；
scoped token 不会回退成 Owner/mobile token。task session 的每次 GET/POST 都必须提供
匹配的 `X-Device-ID`。两个 GET 都返回与 case `version` 相同的 `ETag`。POST 对任何
认证方式都必须提供 `X-Device-ID`、`If-Match`、`Idempotency-Key`，body 包含
`expected_agreement_version/revision_id/expected_digest/action/reason/
counter_document/client_mutation_id`。`reject|counter` 必须有 reason，`accept` 的 reason
必须为 null。`If-Match` 与 `expected_agreement_version` 必须一致，客户端也必须核对
响应 `ETag` 与响应体的 `agreement.version`；三种成功回应都使 case version 精确增加 1，
因而两者应同时等于 `expected_agreement_version + 1`。缺少 `If-Match` 返回 `428`，
header/body 版本不一致或已陈旧返回 `412`，revision ID/digest 不一致返回 `409`。
反提案是完整文档，只能改变
title、purpose、objective、strategy、
key_points、acceptance_criteria 和 due_at；身份、domain、tier、priority 与
base_task_version 固定，回应方翻转。

accept 在单一事务中写不可变 decision、关闭 case、把精确 current revision 应用到 task、
执行 `issued → aligned` 且只增加一次当前 task version，并撤销邀请/会话；reject 保持
task 为 issued；counter 写 decision + revision N+1，task 不变。revision/decision 受
数据库 UPDATE/DELETE 拒绝触发器保护。旧版已绑定 preview/confirm 成功也原子写
`dual_channel_capability` decision；未绑定 v3 邀请不补造 decision。审计保证值为
`owner_token|owner_device_session|dual_channel_capability|task_session`，其中后两者仍不
代表实名身份。
回应 DTO 固定为 `agreement/decision/task`；只有 accept 的 `task` 非 null，且只含
`id/stage/version/updated_at`。

pending case 禁止直接 PATCH 协议字段，也禁止 task-change 绕过；health 等非协议字段
可更新且不使协议 stale。协议字段、成员或 assignee 的真实漂移会原子标记 stale、撤销
能力并拒绝继续。accepted P1b-A case 的协议字段同样禁止直接 PATCH；其中承办人、期限、
验收标准与非正常关闭必须进入下节 P1b-B 新提案，其余尚未纳入 P1b-B 的协议字段继续
fail closed。单个 revision 的 canonical UTF-8 JSON 不得超过 3 MiB；单 case 所有 revision
的 canonical JSON 累计不得超过 4 MiB，且最多 100 个 revisions。由于每个 revision 最多
一个 decision，API DTO 中 decisions 也最多 100 条。上述 secret-bearing JSON 请求体流式限制为
8 MiB（包括 chunked），该上限
覆盖合法 200,000 字符 strategy 即使采用 JSON Unicode 转义的最坏编码；超限
返回不回显的 `413`；验证失败统一返回不回显输入的 `422`。所有响应均 `no-store`、
`no-referrer`、`nosniff`。

回应按 actor + case + `Idempotency-Key` 缓存规范请求：同 key/同 body 精确重试返回
原决定和原响应，同 key/不同 body 返回 `409`。accept/reject 关闭协议并撤销任务会话后，
该会话仅可精确重放已缓存的原 response；任何新 key/body 均返回 `401`，不会创建第二个决定。

#### P1b-B 不可变任务变更协议

P1b-B 只接受四类 exact change，`change_type` 与 patch key 必须一一对应：

| `change_type` | exact patch | 接受后的任务语义 |
| --- | --- | --- |
| `assignee` | `{"assignee_member_id":"member_..."}` | 更换承办人，任务回到 `issued`、`requires_alignment=true`，必须为新承办人重新完成 P1b-A |
| `due_at` | `{"due_at":"2026-08-30T10:00:00Z"}` | 只更新完成期限 |
| `acceptance_criteria` | `{"acceptance_criteria":["标准一"]}` | 只替换完整验收标准数组 |
| `abnormal_close` | `{"abnormal_close_reason":"外部条件不具备"}` | 进入终态 `abnormal_closed` 并保存原因 |

Owner 提案端点为：

```text
POST /api/v1/workspaces/{workspace_id}/tasks/{task_id}/changes
```

请求必须携带 `Idempotency-Key`、`X-Device-ID`，body 精确为：

```json
{
  "change_type": "due_at",
  "base_version": 6,
  "reason": "等待客户确认上线窗口",
  "patch": {"due_at": "2026-08-30T10:00:00Z"},
  "client_mutation_id": "task-change-proposal-001"
}
```

`base_version` 是当前 task version。创建只写 `status=proposed`，不自动应用 patch；同一
任务同时最多一个 P1b-B pending change。响应返回强 `ETag`、`proposal_digest`、
`responder_member_id` 和 `protocol_version=1`，客户端必须核对响应的 task/change 绑定、
类型、规范 patch、原因、mutation ID、版本与请求一致。

不可变提案文档 schema 固定为 `centaur.task-change.v1`，顶层 key 必须精确等于：

```text
schema, workspace_id, task_id, change_id, change_type, base_task_version,
proposer_role, proposer_member_id, responder_role, responder_member_id,
before, patch, reason
```

服务端递归执行 Unicode NFC、CRLF/CR → LF、时间转 UTC 秒精度 `Z`，拒绝浮点、额外
字段、错误类型和不匹配的 patch；数组顺序与重复项保持不变，再用 UTF-8、排序 key、
紧凑分隔符序列化并计算 `sha256:<64 个小写十六进制字符>`。proposal canonical JSON
和 digest 每次读取时重新验证；proposal/decision 受数据库 UPDATE/DELETE 触发器及
change/status/digest/角色/会话绑定触发器共同保护。

提议方固定为任务 issuer，回应方冻结为提案时的当前 assignee。若二者同为 Owner，
主人自办任务也必须由 Owner 显式调用 protocol decision 接受或拒绝；若回应方不是
Owner，Owner token 和 Owner-device 都不能代替承办人 accept/reject。Owner 只能创建
外部邀请，或经兼容 workspace decision 路由带理由取消自己的提案：

```text
POST /api/v1/workspaces/{workspace_id}/changes/{change_id}/invitations
POST /api/v1/workspaces/{workspace_id}/changes/{change_id}/decision
```

邀请创建 body 精确为 `expected_change_version/expected_task_version`，并要求
`If-Match: "<change-version>"`、`Idempotency-Key` 与 `X-Device-ID`。首次 `201` 才返回
高熵 `invitation_id`、只返回一次的 code、`expires_at`、responder label 和不含 code 的
`confirmation_path=/api/v1/task-change-invitations/{invitation_id}`；同创建 key 重试返回
`409`，不会重放明文 code。workspace `decision` 兼容 DTO 虽保留
`accept|reject|cancel`，但对 v6 protocol-bound change 只允许提议方使用
`decision=cancel` 并提供非空 reason，不能作为 Owner accept/reject 旁路。

链接与 code 必须通过两个独立渠道传递。公开无脚本流程为：

```text
GET  /api/v1/task-change-invitations/{invitation_id}
POST /api/v1/task-change-invitations/{invitation_id}/preview
POST /api/v1/task-change-invitations/{invitation_id}/decide
```

GET 页在 code 验证前只显示到期时间与输入框，不显示任务或变更内容；`preview` 和
`decide` 拒绝混入 Owner 凭据。code 错误最多 5 次，明文不落库，只保存 hash。JSON
客户端使用相同能力调用：

```text
POST /api/v1/task-changes/exchange
GET  /api/v1/task-changes/{change_id}
POST /api/v1/task-changes/{change_id}/decisions
```

exchange body 为 `invitation_id/code/client_device_id` 并要求稳定 `Idempotency-Key`；
Owner context 被拒绝。成功返回 `cp_task_ch_` Bearer、原始绝对 expiry、安全 session
投影和 change protocol。token 最长 10 分钟、无 refresh，只绑定单一
change/task/responder/device，库内仅存 token hash；认证前缀优先分派，错误 scope 不会
回退为 `cp_task_at_`、Owner 或 mobile session。会话 live 且提案仍 current 时，同
invitation/code/device/key/body 的重试或并发确定性返回同 token/session/原 expiry，
不延寿、不重复事件；不同 key/body/device 使用正确双凭据会 fail closed 并要求新邀请。

协议 GET 允许 issuer 的 Owner token、绑定 Owner-device，或绑定同
change/task/responder/device 的 `cp_task_ch_`，返回与 change version 相同的强
`ETag`。DTO 顶层固定为
`id/workspace_id/task_id/change_type/base_task_version/status/version/proposer_member_id/
responder_member_id/proposal/decision/actionable/task/created_at/updated_at/closed_at`。
scoped 投影只展示任务标题、最小任务状态和冻结的 before/patch/reason/digest，不包含
memo 正文、`origin_memo_id`、source ref、excerpt、成员联系方式、邮件或文档。

接受/拒绝 body 精确包含：

```json
{
  "expected_change_version": 1,
  "expected_task_version": 6,
  "proposal_digest": "sha256:...",
  "decision": "accept",
  "reason": null,
  "client_mutation_id": "task-change-decision-001"
}
```

POST 还必须提供 `If-Match: "<change-version>"`、`Idempotency-Key` 和与凭据绑定的
`X-Device-ID`。accept 的 reason 必须为 null，reject 必须提供 reason。服务端同时验证
change/task version、proposal digest、当前回应方、任务/成员/session/device 绑定和唯一
client mutation ID；任一漂移均 fail closed。相同 actor/key/body 精确重放首次响应，
同 key 不同 body 返回 `409`。accept 原子写一条 append-only decision、应用 exact patch、
任务版本增加一次、change 关闭并撤销邀请/会话；reject 只关闭 change，不改任务；cancel
同样撤销能力。事务在事件或幂等响应写入失败时整体回滚。

所有 task-change 协议、exchange、邀请和决定响应都使用 `no-store`、`no-referrer`、
`nosniff`；secret-bearing JSON 在模型验证前执行 8 MiB 流式上限，错误不回显 code、token
或请求内容。外部双通道只证明两段能力凭据被持有和使用。Owner 创建邀请时原本就同时
看到链接和 code，因此不得据此宣称指定自然人或企业亲自回应、授权代理、电子签名或
法律不可抵赖。更强保证需要 WebAuthn/设备持有证明、组织 IdP 或目标法域的签名设施。

workspace schema v6 不为升级前已经 accepted/rejected/canceled 的历史 task change 补造
proposal/decision；读取此类历史记录会明确表示没有 P1b-B 协议，不能把旧业务状态解释
为新的不可变确认凭据。v5 中仍为 `proposed` 的旧变更因无法补造双方确认依据而阻止
v6 迁移，必须先在旧版本明确处理。

### 外部承办人任务执行（workspace schema v7）

P1b-A 被接受后，Owner 可为当前外部承办人签发执行能力。它与协议回应、任务变更使用
不同的 token 前缀和 scope；Owner 不能用普通任务 transition 冒充外部承办人启动或
提交任务。公开执行工作台默认关闭，只有非空且合法的
`CENTAURAI_POCKET_TASK_EXECUTION_PUBLIC_ORIGIN` 才启用签发入口与浏览器 BFF。

#### 邀请、access 与 refresh

Owner 创建执行邀请：

```text
POST /api/v1/workspaces/{workspace_id}/tasks/{task_id}/execution-invitations
```

请求 body 精确为 `{"expected_task_version":6}`，并要求
`If-Match: "6"`、`Idempotency-Key` 和 `X-Device-ID`。任务必须由 Owner 下达、当前
承办人必须是活跃 external member、当前双方 P1b-A 已接受，且任务处于 `aligned` 或
`in_progress`。公开工作台未启用时固定返回 `503`，不创建邀请或死链接。首次 `201`
响应返回以下字段，确认码只出现一次；相同创建 key 重试返回 `409`：

```text
invitation_id, task_id, task_version, assignment_epoch,
assignee_member_id, expires_at, capability_expires_at,
code, assignee_label, confirmation_path
```

邀请约 10 分钟有效，code 最多失败 5 次且库内只存 hash。`capability_expires_at` 不晚于
签发后 7 天，也不晚于任务 `due_at + 7 天`；期限宽限已过时拒绝签发。邀请绑定已接受的
agreement、当前 task version、assignee 和单调 `assignment_epoch`。

无 Owner 上下文的 JSON 客户端用双通道凭据交换会话：

```text
POST /api/v1/task-executions/exchange
```

body 为 `invitation_id/code/client_device_id`，并要求稳定 `Idempotency-Key`。成功返回
`cp_task_ex_` access、`cp_task_er_` refresh、两者到期时间和不含 secret 的 session
投影。access 最长 10 分钟；refresh family 绝对最长 7 天，活动 refresh token 有 24
小时 idle 窗口，并继续受 `due_at + 7 天` 限制。明文 code/access/refresh 均不落库，
服务端只存 hash。live 首次会话的相同 invitation/code/device/key/body 重试确定性返回
相同 token 和原 expiry；不同 key/body/device 的重放 fail closed。用新邀请成功交换会
撤销同任务旧的 live execution family。

短 access 到期后使用：

```text
POST /api/v1/task-executions/refresh
```

body 精确为 `refresh_token/client_device_id`，并要求 `Idempotency-Key`，不接受 Owner
Authorization。成功原子旋转 access 与一次性 refresh、撤销旧 access，并随响应返回当前
最小任务投影和新的执行视图 `ETag`。同 key/body 在受控的 30 秒重放窗口内返回同一旋转
结果；旧 refresh 的其他重用、设备不符或完整性异常会撤销整个 family，并写入带原因的
security event。refresh 不延长 family 的绝对期限。

#### 最小投影与执行命令

携带 `Authorization: Bearer cp_task_ex_...` 和绑定的 `X-Device-ID` 读取：

```text
GET /api/v1/task-executions/{task_id}
```

顶层投影 key 固定为：

```text
id, title, purpose, objective, strategy, key_points, acceptance_criteria,
start_at, due_at, stage, health, priority, progress, change_pending,
own_checkins, steps, version, updated_at
```

`own_checkins` 只返回当前逻辑承办人最新 10 条执行回报。step key 固定为：

```text
id, parent_step_id, step_type, title, description, status, position, due_at,
success_metric, depends_on_step_ids, completed_at, version, editable
```

投影不包含 Workspace ID、成员 ID/联系方式、issuer/acceptance owner、memo/source/
excerpt/evidence、邮件、文档或事件。`editable=true` 只表示任务正在执行、步骤属于当前
承办人、步骤未完成/取消且没有 pending task change；它是 UI 能力提示，服务端写入仍会
重新校验全部绑定。

执行视图的强 ETag 为
`"task-execution-v1-<64 位小写 SHA-256>"`，覆盖 task version、assignment epoch、
step versions、check-in cursor 和 pending changes。所有执行写入均要求该 `If-Match`、
body 中的 `expected_task_version`、稳定 `Idempotency-Key`、绑定的 `X-Device-ID` 与唯一
`client_mutation_id`；步骤写入还要求 `expected_step_version`。该 mutation ID 按
task/assignee/assignment epoch 跨 access 轮换保持唯一，相同 actor/key/body 精确重放，
换 key 或 token 不能制造第二次业务动作。

支持的 JSON 命令为：

```text
POST /api/v1/task-executions/{task_id}/start
POST /api/v1/task-executions/{task_id}/check-ins
PUT  /api/v1/task-executions/{task_id}/steps/{step_id}/status
POST /api/v1/task-executions/{task_id}/submit
```

- `start` 只允许当前承办人把 `aligned → in_progress`；
- `check-ins` 只在 `in_progress` 追加本人回报，使用服务端 Workspace 时区生成
  `report_date`，不修改 task version 或正式 progress，但会改变执行视图 ETag；
- step status 只允许修改本人负责的步骤为
  `pending|in_progress|blocked|done`，遵守 step version、状态机和依赖完成约束；成功后
  重算正式 progress，并使 task version 增加一次；
- `submit` 只在本人负责的所有 leaf action 都为 `done|canceled` 后执行
  `in_progress → submitted`，把正式 progress 设为 100；提交不等于 Owner 验收。

存在 `status=proposed` 的 P1b-B change 时，`change_pending=true`：start、step 写入和
submit 均返回 `409`，所有 step 的 `editable` 为 false；执行中的承办人仍可追加
check-in 说明现状。Owner 对 `submitted` 任务可以调用
`POST /api/v1/workspaces/{workspace_id}/tasks/{task_id}/transitions` 执行 `accepted`，
或携带非空 note 退回 `in_progress`；返工会清空 `submitted_at`，原绑定仍 current 时
承办人可继续并再次提交。进入 `accepted|abnormal_closed`、任务删除、承办人停用或
承办人/assignment epoch 改变都会撤销邀请、access 与 refresh family。

所有 execution JSON 成功、错误和 validation 响应均使用 `no-store`、`no-referrer`、
`nosniff`，敏感请求错误不回显 code/token/body。access 认证按 `cp_task_ex_` 前缀
fail closed，refresh 只接受 `cp_task_er_` body 凭据，不会回退为 Owner、mobile、
P1b-A 或 P1b-B 会话。

#### 无脚本浏览器 BFF

配置的公开 Origin 必须是规范 HTTPS Origin，例如 `https://tasks.example.com`；末尾
`/` 会被去除，显式 `:443`、凭据、路径、query、fragment 或非 HTTPS 均使 API 启动
失败。BFF 使用以下同源、服务端渲染的无脚本流程：

```text
GET  /api/v1/task-execution-invitations/{invitation_id}
POST /api/v1/task-execution-invitations/{invitation_id}/exchange
GET  /api/v1/task-execution-invitations/{invitation_id}/workbench
POST /api/v1/task-execution-invitations/{invitation_id}/workbench/start
POST /api/v1/task-execution-invitations/{invitation_id}/workbench/check-ins
POST /api/v1/task-execution-invitations/{invitation_id}/workbench/steps/{step_id}/status
POST /api/v1/task-execution-invitations/{invitation_id}/workbench/submit
GET  /api/v1/task-execution-invitations/{invitation_id}/session/continue
POST /api/v1/task-execution-invitations/{invitation_id}/session/refresh
```

页面 CSP 为 `default-src 'none'`，不加载或执行 JavaScript；只允许带 nonce 的内联样式和
同源 form。boot、access、refresh secret 分别放入 path-scoped 的
`__Secure-cp_task_exec_boot`、`__Secure-cp_task_exec_at`、
`__Secure-cp_task_exec_rt` Cookie，三者均为 `Secure`、`HttpOnly`、
`SameSite=Strict`；refresh Cookie 的 path 不能随普通 workbench 请求发送。每个写表单都携带服务端 HMAC 签名 CSRF，绑定
invitation/boot 或 family/task/assignment epoch/credential generation/action/执行视图
ETag/task 与 step version/独立幂等键/到期时间。POST 还必须精确匹配配置 Origin，并满足
`Sec-Fetch-Site: same-origin`、`Sec-Fetch-Mode: navigate`、
`Sec-Fetch-Dest: document`；拒绝 Owner/API
Authorization、query、重复或额外表单字段及超过 32 KiB 的表单。

access Cookie 到期后，workbench 只显示“继续安全会话”链接；用户显式进入
`session/continue` 后再提交 refresh 表单，不运行脚本、不静默刷新。pending change 时
BFF 保留 check-in 表单，隐藏 start、step 和 submit 控件。该命名空间刻意不提供 CORS：
边界中间件位于通用 CORS 外层，移除所有 `Access-Control-*` 响应头，`OPTIONS` 固定
`405`。所有页面、重定向和错误响应都使用 `no-store`、`no-referrer`、`nosniff`、
HSTS、frame deny、same-origin opener/resource policy 和禁用摄像头/麦克风/定位的策略。

#### 归属、保证等级与 v7 迁移

execution event 记录 `actor_subject_type=task_execution_capability`、session/family、
`on_behalf_of_member_id`、`assurance_method` 和 `assignment_epoch`。task 的
`updated_by`、check-in 的 `created_by` 以及 event 的 `on_behalf_of_member_id` 只是业务
路由上的逻辑归属：它们表示“绑定到该成员的能力完成了操作”，不证明该自然人亲自操作，
也不是不可抵赖证据。分析承办人表现或任务过程时必须保留并按 execution event 的
`assurance_method` 加权，不能把 A1 能力事件与更强身份认证事件等价合并。

与 P1b-A/P1b-B 一样，路径+独立 code 的 external execution 只达到 A1 能力持有保证。
Owner 在签发时同时看到两段凭据，因此系统不证明指定自然人/企业身份、授权代理、电子
签名或法律不可抵赖；更强结论需要 WebAuthn/设备持有证明、组织 IdP 或目标法域签名设施。

workspace schema v7 在 task 上增加单调 `assignment_epoch`，并新增 execution
invitation/session/refresh-family/refresh-token 表、绑定/不可变/唯一性索引与任务终态、
换人、删除、成员停用时的撤销触发器。迁移会校验精确对象类型、列约束、外键、索引、
触发器规范摘要与已有 token 绑定；只有 v7 marker 而对象较弱或被篡改仍会 fail closed。
升级不为历史任务补造邀请、会话、执行事件或身份保证。`cp_task_at_`、`cp_task_ch_`、
`cp_task_ex_`、`cp_task_er_` 的确定性签名依赖数据根中的独立持久
`task-session-hmac-key`；它与数据库必须成对备份、成对恢复。

## 官方 RSS/Atom 可靠信源

首版只实现主人确认后的官方 RSS/Atom feed，不是通用网页爬虫。路径位于 API 顶层，
不带 Workspace 前缀：

- `GET /api/v1/reliable-sources`
- `GET /api/v1/reliable-source-candidates?status=pending`
- `POST /api/v1/reliable-source-candidates`
- `POST /api/v1/reliable-source-candidates/{id}/confirm`
- `POST /api/v1/reliable-source-candidates/{id}/dismiss`
- `GET|PATCH /api/v1/reliable-sources/{id}/collection-plan`
- `POST /api/v1/reliable-sources/{id}/collect`
- `GET /api/v1/reliable-sources/{id}/entries?limit=50`

所有 GET 允许 Owner 或已配对设备，Agent token 和 Collector token 均不能读取。所有
写操作必须再带稳定 `X-Device-ID` 和至少 8 字符的 `Idempotency-Key`。同 actor、操作
和 key 的相同规范请求重放原结果；不同 payload 返回 `409`。采集的 `403/502` 终态
错误也会以不含 feed、header 或 IP 的错误哨兵缓存；同 key 重试不会再次解析 DNS、
访问远端或重复写失败快照。

候选创建请求为：

```json
{
  "display_name": "机构官方发布",
  "organization_origin": "该机构官方网站",
  "feed_url": "https://news.example.com/feed.xml",
  "trust_reason": "官网明确列出的官方 RSS",
  "scope": "政策与公告",
  "review_due_at": "2026-12-31T12:00:00+08:00"
}
```

`scope`、`organization_origin` 都是非空 string，不是 URL 或枚举。候选 DTO 固定为
`id/display_name/organization_origin/feed_url/trust_reason/scope/review_due_at/status/
version/created_at/updated_at/confirmed_at/dismissed_at/dismiss_reason/
reliable_source_id`，其中 `status` 为 `pending|confirmed|dismissed`。创建候选只做
本地校验和持久化，绝不解析 DNS 或发起网络请求，响应为 `201`、`ETag: "1"`。

确认请求同时要求 `If-Match: "<candidate version>"` 和 body：

```json
{"expected_version": 1, "schedule": "manual"}
```

两处版本和当前版本必须完全一致；缺少 `If-Match` 返回 `428`，陈旧或不一致返回
`412`。确认在一个事务中把候选改为 `confirmed`，并创建内部 `sources(kind=rss)`、
可靠信源注册项和采集计划，响应为
`{candidate,reliable_source,collection_plan}`，ETag 表示候选的新版本。驳回 body 为
`{expected_version,reason}`，使用同样版本语义，响应为候选 DTO。

可靠信源 DTO 固定为：

```text
id, source_id, display_name, organization_origin, feed_url,
trust_reason, scope, status="active", version,
created_at, updated_at, last_collected_at
```

计划 DTO 固定为：

```text
id, reliable_source_id, schedule="manual|daily", enabled,
review_due_at, version, last_collected_at, next_run_at,
created_at, updated_at
```

PATCH 计划只接受 `schedule/enabled/review_due_at`，必须携带计划的 `If-Match`；不能
通过该接口修改 endpoint。手工 collect body 为 `{expected_version}`，并要求同值的
source `If-Match`。成功采集使 source version 加一，返回
`{source,collection_plan,snapshot}`，ETag 表示新 source version。公开 snapshot 仅有：

```text
id, reliable_source_id, status="completed|not_modified", request_url,
http_status, content_hash, etag, last_modified, byte_count,
entry_count, new_entry_count, changed_entry_count, duplicate_entry_count,
collected_at
```

不公开 `resolved_ip`、失败内部码或原始响应。`304` 使用上次 snapshot hash，计数均为
零；后续请求会发送受长度和控制字符约束的 `If-None-Match/If-Modified-Since`。daily
采集失败从 1 小时开始指数退避、上限 24 小时；调度器逐信源隔离异常，一个失败不会
阻止后续信源执行。公开 `last_collected_at` 仅表示最近成功或 `304`，当前 API 不提供
失败详情，客户端不能据此虚构成功或错误原因。

条目列表返回 `{items,total,limit}`。每项固定为：

```text
id, identity_key, title, summary, url, url_trust, publisher,
published_at, collected_at, current_version, snapshot_hash, state,
item_id, governance_task_id, evidence
```

`publisher` 始终是主人确认的 `display_name`，不会采用远端 feed 的 author/channel title
冒充机构。`url_trust` 为 `feed_claimed_unverified`、
`feed_url_fallback_missing` 或 `feed_url_fallback_invalid`；合法的跨 host HTTPS 原文
地址会保留，但 Pocket 永不抓取条目链接。客户端不得把未验证地址自动打开或当作受信
导航。每个 evidence 固定为：

```json
{
  "snapshot_id": "rsnap_...",
  "snapshot_hash": "sha256...",
  "field": "summary",
  "start_offset": 0,
  "end_offset": 12,
  "offset_unit": "unicode_code_points",
  "excerpt": "可回查的短摘录"
}
```

摘要由 feed 字段确定性抽取，远端 markup 转成纯文本。新条目生成
`items.state=needs_review` 和 `governance_tasks.kind=news_summary`，在 Owner 接受治理
任务前 Agent 搜不到。接受后 Agent `/agent/search` 仍以 `kind=document` 返回，并增加
`content_type=news`；其唯一 citation 的稳定结构为：

```json
{
  "type": "web_snapshot",
  "entry_id": "rentry_...",
  "reliable_source_id": "rsrc_...",
  "publisher": "机构官方发布",
  "url": "https://news.example.com/releases/1",
  "url_trust": "feed_claimed_unverified",
  "published_at": "2026-07-01T08:30:00Z",
  "collected_at": "2026-08-02T10:00:00Z",
  "snapshot_id": "rsnap_...",
  "snapshot_hash": "sha256...",
  "evidence": []
}
```

相同条目标识/URL 与内容 hash 重试不重复；内容变化创建新版本和新的待治理 item，旧
ready 版本在新版本被接受前仍保持可检索，接受新版本后才归档旧版本。

网络边界为公开域名 HTTPS、默认 443、无 userinfo/IP literal/fragment/原始空白或控制
字符。DNS 的所有结果都必须是公网地址；私网、回环、link-local、reserved、multicast、
metadata，以及 IPv4-mapped、6to4、Teredo 和 NAT64 中嵌入的非公网 IPv4 均拒绝。
实际 TCP 固定到已验证 IP，同时 Host、TLS SNI 与证书校验仍使用原域名；不用环境代理、
不跟随重定向、`Accept-Encoding: identity`，并限制绝对请求时限、响应字节和 header。
只接受限定 MIME 的 UTF-8 RSS/Atom XML；拒绝 NUL、非 UTF-8 declaration、
DOCTYPE/ENTITY/XXE、HTML fallback、压缩响应、过深/过多元素、过多条目和过长文本。
原始 feed body 从不持久化，也不出现在列表、Agent citation 或同步事件中。

## CORS

`CENTAURAI_POCKET_CORS_ORIGINS` 是逗号分隔的精确 Origin 列表，启动时会去掉每项末尾的 `/`。Origin 只包括 scheme、host 和可选 port，不要写路径，也不要使用 `*` 与凭据模式组合。

默认值是：

```text
http://localhost:8081
http://127.0.0.1:8081
http://localhost:19006
http://127.0.0.1:19006
http://127.0.0.1:17818
```

`127.0.0.1:17818` 是“半人马 AI 超级秘书”Electron 壳的固定本机 Origin；它仍需
有效 Owner 认证，且不会放宽到任意回环端口。

生产 Web 前端或反向代理使用其他 Origin 时必须显式配置。CORS 不是认证机制，Owner/Agent token 仍然必需。
