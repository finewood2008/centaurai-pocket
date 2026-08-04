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
- `CENTAURAI_POCKET_TASK_EXECUTION_PUBLIC_ORIGIN`：任务执行公开工作台的规范 HTTPS
  Origin，例如 `https://tasks.example.com`。默认未启用；空值保持禁用。末尾 `/` 会被
  去除，显式 `:443`、凭据、路径、查询参数或片段会令服务启动失败。
- `CENTAURAI_POCKET_DESKTOP_SESSION_TOKEN`：仅供受管 Electron sidecar 注入的
  短期 Owner 会话凭据；普通部署不要设置。
- `CENTAURAI_POCKET_OUTLOOK_CLIENT_ID`：Microsoft 公共客户端应用 UUID；未设置时
  Outlook 连接不可用。
- `CENTAURAI_POCKET_OUTLOOK_TENANT`：`common`、`organizations`、`consumers` 或租户
  UUID，默认 `common`。

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

## 超级秘书业务工作区

Pocket 现在通过独立的 `/api/v1/workspaces/ws_default/*` 业务域承载超级秘书，
不会复用数据治理用的 `governance_tasks`：

- `bootstrap` 在一致性读事务中返回备忘、业务任务、步骤、变更、日程、会议与当前游标；
- `sync?after=` 按 SQLite 自增 sequence 返回不可变事件和删除 tombstone；
- 创建和动作接口强制 `Idempotency-Key`、`X-Device-ID`，同 key 不同请求返回 `409`；
  签发邀请的创建接口不会落库或幂等重放明文对齐码，同 key 重试也返回 `409`，
  需用新 key 重建；这与后续 `exchange` 对同 key/body 确定性返回同一会话是两个不同操作；
- 资源修改使用 `If-Match` 或请求体中的 `expected_version`，陈旧版本返回 `412`；
- 任务生命周期为 `draft → issued → aligned → in_progress → submitted → accepted`，非正常关闭通过带原因的变更确认完成；
- 执行任务可追加不改写任务版本/正式进度的 check-in；服务端按工作区时区生成
  `report_date`，同步事件为 `task.checkin_recorded`；
- `task-attention` 用服务端时钟即时推导缺计划、待复盘、步骤/日程/任务逾期、阻塞、
  预测延期和 48 小时内低进度风险，不持久化告警；由前端同步主动呈现，而不是后台推送；
- `task-analysis?from=YYYY-MM-DD&to=YYYY-MM-DD` 在一个只读快照中分开返回任务结果事实和
  分动作的人员归属证据；最多 366 天且对任务、事件、业务行和成员各设 50,000 上限，
  A2/A1/A0 权重由服务端按不可变 decision 与 typed execution 绑定校验，历史普通 actor
  不反推身份，不输出绩效总分或排行；
- 当承办人不是下达人时，Owner 只能签发约 10 分钟的一次性对齐邀请，不能直接把
  `issued` 改成 `aligned`；旧 `preview/confirm` 两步页面/端点仍保留兼容，
  v4 绑定的新邀请经兼容 confirm 接受时也会写入不可变决定；
- 新邀请同时冻结 `centaur.task-agreement.v1` 完整协议 revision。承办人也可用路径 +
  短码交换 10 分钟、hash-only、绑定 task/case/device 的 `cp_task_at_`，再执行
  accept/reject/counter；access token 由数据根中独立持久的任务会话 HMAC key 做域分离后
  确定性生成，该 key 与 Owner token 轮换解耦且必须与数据库成对备份恢复，
  在会话 live 且协议 current 时，同 invitation/code/device/key/body 的任意重试或并发
  返回相同 token/session/原 expiry，
  不延寿也不重复审计；库内仅存 token hash。不同 key/body/device 使用正确双凭据
  会撤销现有会话并返回 `409`；没有 refresh 或 DPoP；
- task agreement revision/decision 在 SQLite 中不可 UPDATE/DELETE；pending 与 accepted
  case 均阻止未签署协议字段写入，health 等非协议字段仍可更新；secret-bearing JSON
  请求体上限 8 MiB，单 revision 规范 JSON 最多 3 MiB、单 case 累计最多
  4 MiB/100 revisions（因每个 revision 最多一个 decision，decision 也最多 100 条）；
  验证错误不回显 code、token 或 counter document；
- P1b-B 把 `assignee`、`due_at`、`acceptance_criteria`、`abnormal_close` 四类
  exact change 固化为 `centaur.task-change.v1` 不可变提案。主人自办任务仍须由 Owner
  显式回应；外部任务只能由提案冻结的当前承办人通过双通道 `cp_task_ch_` scoped
  session 接受或拒绝，Owner 不能代答，只能带理由取消自己的提案；
- 任务变更回应同时绑定 change/task version、proposal digest、`If-Match`、设备、
  `Idempotency-Key` 和 `client_mutation_id`。接受才在同一事务应用 patch；承办人变更
  接受后任务回到 `issued`，必须针对新承办人重新完成 P1b-A；
- schema v7 外部执行邀请交换为 10 分钟 `cp_task_ex_` access 与可旋转
  `cp_task_er_` refresh。最小投影支持 start、check-in、本人步骤和 submit；pending
  change 期间只保留 check-in，Owner 负责带理由退回返工或最终验收。换人 epoch、终态、
  删除或成员停用会撤销整个 execution family；可选 HTTPS BFF 使用无脚本同源表单、
  path-scoped 安全 Cookie 和签名 CSRF，且不开放 CORS；
- 业务表、事件和幂等响应在同一事务提交。

P1c-A 备忘落地只提供两个显式 Owner 命令：

- `POST /api/v1/workspaces/{workspace_id}/memos/{memo_id}/task`；
- `POST /api/v1/workspaces/{workspace_id}/memos/{memo_id}/calendar`。

Owner token 或已配对、Bearer 会话与 `X-Device-ID` 匹配的 Owner Device 可调用；设备
仅能访问 `ws_default`。两条路由不接受 query/尾随斜线，严格要求
`Idempotency-Key`、`X-Device-ID`、`If-Match` 和 body 中相同的
`expected_memo_version`。服务端派生任务的 memo/source/domain/issuer/验收人/初始状态，
或日程的 memo/domain/scheduled/空参与人及空外部关联；客户端不能注入这些字段。

只有 live、active、已确认且未物化的 memo 可执行。任务与日程共享一个一次性槽位；
成功严格返回 `{memo,task}` 或 `{memo,calendar_entry}`，ETag 是转换后 memo 的
`expected_memo_version + 1`。事务先创建目标并追加 `task.created` 或
`calendar.created`，再追加 `memo.updated`，同时提交 converted memo、v5 账本与幂等
响应。建议逻辑只在客户端解释候选，不会自动调用这两个端点。Pocket 日程是内部安排，
不发送外部日历或参会邀请。

个人 memo 委派给非 Owner 时必须严格确认 disclosure；该确认只授权任务表单字段进入
后续对齐，不授权承办人读取原始 memo 正文、source ref 或 excerpt。Owner 侧保留受控
来源供追溯，scoped agreement 不返回 `source`/`origin_memo_id`。

workspace schema v5 的 `secretary_memo_materializations` 是不可更新/删除的二选一账本；
触发器同时锁定 converted memo 和目标关联，通用 `/tasks` 的 `origin_memo_id` 与通用
`/calendar` 的 `memo_id` 均被拒绝。旧版缺少完整原子事件和 disclosure 证据，升级不会
回填任何历史 task/calendar 链接；发现任一旧 memo 链接或任一无账本的 converted memo
即诊断并 fail closed，须人工处置后重试。只有清洁库才创建空账本，不能用关联列补造
审计证明。

当前最高 workspace schema 为 v7。对齐邀请表与 schema v4 的
case/revision/decision/session 表由
`WorkspaceService.initialize()` 幂等升级；已有 SQLite 不重建任务表、不清空业务数据，
重复启动安全，并在测试中执行 `foreign_key_check` 验证兼容性。v3 时期已经
confirm/aligned 的历史记录不会回填协议修订或决定，不能因升级而宣称获得了
追溯证明。上文的 v5 指其引入不可变 memo materialization 账本的历史版本，不表示
当前 schema 仍停留在 v5。

schema v6 新增 task-change proposal/invitation/session/decision 表、索引与不可变绑定
触发器。启动时会校验对象类型、关键列、主外键、索引、触发器及已有记录的 digest、
角色和会话绑定；发现无迁移标记的同名弱对象、只有 v6 marker 却缺协议对象，或存在
无法补造双方确认依据的历史 `status=proposed` 变更时整体 fail closed。v6 不为升级前
已关闭的历史变更补造 P1b-B proposal/decision；这些记录保留原业务状态，但不获得新的
不可变确认凭据。

schema v7 在 task 上增加单调 `assignment_epoch`，并新增 execution invitation、access
session、refresh family、一次性 refresh token 以及绑定/撤销索引和触发器。启动不仅
检查 marker，还校验精确 DDL 摘要、列约束、主外键、唯一性与已有 token 链；弱同名对象、
可空 epoch 或绑定异常均 fail closed。v7 不为历史任务补造执行会话、事件或身份保证。

当前 `X-Device-ID` 用于会话绑定、游标和审计归属，不是独立认证凭据；跨公网或多人使用前，
仍应由 Owner 引导签发可撤销的设备 Token。外部任务对齐已经提供高熵分享路径、
hash-only 短码、尝试上限、不可变摘要和明确确认；它只证明相应能力凭据被持有和使用。
Owner 在创建邀请的响应中本来就同时看到分享路径和验证码，且只要省略
Owner header 就能作为匿名能力持有者交换会话；因而 P1b-A/P1b-B/external execution
的外部双通道流程
不证明自然人、企业身份、授权代理或法律签名。更强保证需要未来的
WebAuthn/设备持有证明或组织 IdP。
执行事件中的 `on_behalf_of_member_id`、task `updated_by` 和 check-in `created_by`
只是业务逻辑归属，不是不可抵赖认证；任务与人员分析必须按 event-level
`assurance_method` 加权。
与会者纪要的独立凭据仍属后续范围。

### P1b-A 任务协议端点

- Owner 按任务发现当前协议：
  `GET /api/v1/workspaces/{workspace_id}/tasks/{task_id}/agreement`；
- Owner/受管 Owner 设备或已交换的任务会话读取指定协议：
  `GET /api/v1/task-agreements/{case_id}`；
- 链接+验证码交换任务会话：`POST /api/v1/task-alignments/exchange`；
- 当前回应方接受、拒绝或反提案：
  `POST /api/v1/task-agreements/{case_id}/responses`。

协议 GET 返回 `ETag`；回应同时要求 `If-Match`、body 中相同的
`expected_agreement_version`、`revision_id`和 `expected_digest`，并强制
`Idempotency-Key` 与 `X-Device-ID`。协议待回应或已接受时，任何绕过协议直接
修改协议字段的路径均 fail closed；字段或成员绑定漂移会把待回应协议标记为
`stale` 并撤销邀请/任务会话。任务会话没有 refresh，也不能用于其他任务、
Workspace 通用 API、邮件或文档。
会话已 revoked/closed 后的 exchange 只返回无副作用 `409`，不覆写终态 reason；
首次观察到未撤销会话已过期时，服务端只把该会话原子标记为 `expired`，不改写
邀请。确定性 token 的重算 hash 若与库内 hash 不符，完整性校验 fail closed 并要求新邀请。

旧 `preview/confirm` 端点保留兼容；v4 绑定的新邀请经旧 confirm 接受时会写入
不可变决定。但 v3 时期已经 confirm/aligned 的历史数据不回填修订或决定，
因而不获得追溯性证明；未绑定的旧邀请也不能交换 `cp_task_at_` 会话。

### P1b-B 任务变更协议端点

- Owner 提出四类 exact change：
  `POST /api/v1/workspaces/{workspace_id}/tasks/{task_id}/changes`；
- Owner 为外部回应方创建只返回一次明文 code 的邀请：
  `POST /api/v1/workspaces/{workspace_id}/changes/{change_id}/invitations`；
- 提议方取消尚未决定的提案：
  `POST /api/v1/workspaces/{workspace_id}/changes/{change_id}/decision`；
- 双通道凭据交换 task-change session：`POST /api/v1/task-changes/exchange`；
- Owner/受管 Owner 设备或绑定的 task-change session 读取冻结协议：
  `GET /api/v1/task-changes/{change_id}`；
- 当前回应方接受或拒绝：
  `POST /api/v1/task-changes/{change_id}/decisions`；
- 无脚本公开页使用
  `GET /api/v1/task-change-invitations/{invitation_id}`，并通过同前缀的
  `/preview`、`/decide` 表单完成相同的 exchange/decision 语义。

提案文档固定为 schema `centaur.task-change.v1` 和 13 个顶层字段：
`schema/workspace_id/task_id/change_id/change_type/base_task_version/proposer_role/
proposer_member_id/responder_role/responder_member_id/before/patch/reason`。四类 patch
分别且只能为 `{"assignee_member_id": ...}`、`{"due_at": ...}`、
`{"acceptance_criteria": [...]}`、`{"abnormal_close_reason": ...}`。服务端执行 Unicode
NFC、CRLF/CR → LF 和 UTC 秒精度规范化，拒绝额外字段、错误类型和不匹配 patch，再对
排序 key 的紧凑 UTF-8 JSON 计算 `sha256:<64 lowercase hex>`。

创建提案只生成 `proposed` 记录，不自动应用。提议方固定为任务 issuer；回应方冻结为
提案时的当前 assignee。二者同为 Owner 时也必须经 protocol decision 显式接受或拒绝；
二者不同时，Owner token/Owner-device 不能代替外部承办人接受或拒绝，只能由绑定
change/task/responder/device 的 `cp_task_ch_` session 回应。旧 workspace decision
兼容路由对 v6 protocol-bound change 只允许提议方带 reason 执行 `cancel`。

协议 GET 返回与 change version 相同的强 `ETag`。回应必须同时提供
`If-Match`、相同的 `expected_change_version`、`expected_task_version`、冻结
`proposal_digest`、稳定 `Idempotency-Key`、`X-Device-ID` 和唯一
`client_mutation_id`；任一版本、摘要、任务或回应方绑定漂移均 fail closed。精确同
actor/key/body 重放首次结果，不产生第二个决定或第二次任务更新；accept 才原子写入
append-only decision、应用 patch、更新任务和关闭 change，reject 不修改任务，cancel
撤销活动邀请与会话。

邀请路径和 code 必须经两个独立渠道传递。公开页在 code 校验前不显示任务或变更内容；
code 只返回一次且库内只存 hash，交换所得 `cp_task_ch_` token 最长 10 分钟、无 refresh，
库内同样只存 token hash。scoped 协议只暴露任务标题、最小任务投影和冻结的变更事实，
不返回 memo 正文、source ref、excerpt、成员联系方式、邮件或文档。Owner 最初仍同时
看到链接与 code，所以这只证明双通道能力被持有和使用，不证明指定自然人或企业身份，
也不构成授权代理、电子签名或不可抵赖证明。

## Outlook 邮件连接器

邮件 API 位于 `/api/v1/mail`，只允许 Owner 或已配对 Device。配对设备请求的
`X-Device-ID` 必须与 Bearer 会话绑定；Owner 请求中它只是调用设备标识。该字段会进入
幂等 payload，但不是认证凭据，也不代表已实现邮件级持久设备审计或可归属到设备的
发送审计；企业审计属于后续范围。所有写操作都要求 `Idempotency-Key` 与
`X-Device-ID`，修改已有资源还要求 `If-Match` 与 body 的 `expected_version` 一致。

OAuth 使用无 client secret 的 Device Code Flow，scope 固定为
`offline_access Mail.ReadWrite Mail.Send`。OAuth 状态/token 和 Inbox delta 游标以
AES-GCM 加密；增量同步只持久化收件箱元数据，正文和附件按需读取。附件归档仅接受
非内嵌 PDF、UTF-8 纯文本和无宏 OOXML，并加密保存。

本轮不是 Outlook 线程回复：服务端给同步时记录的原邮件 Graph `Sender` 地址新发纯文本
邮件并加 `Re:` 主题，不读取 `Reply-To`，也不保证 Conversation、`In-Reply-To` 或
`References` 关联。客户端只编辑正文。`prepare` 创建并核验远端草稿，第二次
`confirm` 才发送；预览同时展示自定义 `from_label` 和已核验 `from_address`，后者在
快照前可为 `null`，从 `ready` 起必须非空并参与 `preview_hash`。任何网络歧义之后都只允许读取或调用只读
`reconcile`，绝不自动重发。核验只覆盖 UI 信封、纯文本正文、唯一 marker、无附件、
空 `replyTo`、关闭回执和 normal importance，不声称 Graph GET 与 `/send` 原子化或
枚举全部 MAPI 属性。二者之间的 TOCTOU、同邮箱其他客户端、Exchange 规则和隐藏 MAPI
属性仍是残余风险；`sensitivity` 不属于本轮顶层核验字段。

持久 `prepare_uncertain` / `send_uncertain` 只锁该回复和 account disconnect，Inbox 等
读取仍可用；系统不会自动重发或关闭。用户须先在真实 Outlook/Sent Items 人工核验。
当前没有安全自助关闭，也不得直接改 SQLite；未来由高摩擦、可审计的
`close-unresolved` 能力处理。`ready` 也没有显式 cancel/abandon 入口；当前能力不能称为
完整的回复或发送闭环。

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

后端回归基线为当前完整 `pytest` 套件与 `ruff` 静态检查全部通过；测试数量随契约覆盖
增长，以当次收集和 CI 结果为准，不在文档中固化旧计数。
