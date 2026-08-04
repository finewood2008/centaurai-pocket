# CentaurAI Pocket 产品规格

## 1. 产品定义

CentaurAI Pocket（半人马随身数据中心）是一套面向单个用户的私有数据治理产品。它在用户自己的电脑、NAS 或私有服务器上运行数据服务，手机负责配置、查看和完成需要人工判断的小任务。

它解决的不是“保存一个文件”，而是长期保持个人数据可用。当前自动来源包括服务端
本机/挂载文件夹，以及本人主动运行的 Firefox 微信可见 DOM 观察器；手机还可采集
文字和网页 URL：

- 数据从文件夹或手机进入，后续可扩展邮箱、网盘和网页订阅。
- IM 数据按来源权威级别、会话、身份、消息、覆盖缺口和证据链保存；网页观察结果不会
  被描述为官方完整历史。
- 支持的文本文件中，相同字节内容会按 SHA-256 合并；手机采集按文字与 URL 的组合去重。
- 格式、标题、日期、来源、标签逐步规范。
- 同步失败、重复冲突和低质量内容不会悄悄堆积。
- Agent 只读取已经通过质量门的数据，并能返回来源引用。

## 2. 目标用户和使用方式

个人数据治理 Vault 只服务一个 owner，不实现多租户、企业 RBAC 或审批组织。
超级秘书扩展域使用固定单人 `ws_default` 与逻辑成员目录来下达任务；这些成员记录
只是业务路由与展示对象，不是登录账号、组织身份或电子签名主体。

典型使用节奏：

- 首次花几分钟配置数据源。
- 系统在后台自动同步和处理。
- 通勤、排队或休息时，用手机每次处理一到三张治理卡片。
- 日常 Agent 直接查询稳定的 `ready` 数据，不需要理解底层文件位置。

## 3. 核心体验

### 3.1 今日

首页必须在一个请求内回答四个问题：

1. 数据源是否健康。
2. 今天新增或更新了多少内容。
3. 有多少事项需要本人处理。
4. Agent 当前能查询多少条 ready 数据。

首页只提供一个主行动：“处理下一条”。

### 3.2 自动同步

当前 MVP 新增文件夹数据源时只配置：

- 显示名称。
- 服务端可访问的绝对路径。
- 同步频率。
- 是否启用。

未来网络数据源再增加类型、地址和授权配置。

服务端负责调度、重试、内容指纹增量判断和失效来源映射清理。当前文件夹连接器每次扫描可见文件；未来远端连接器再负责自己的增量游标。手机不承担网盘、邮箱等长期拉取任务。

一次完整成功扫描若发现路径消失，会先删除过期来源映射。任何尚未归档、失去
最后一个来源且没有新 generation 正在替代的条目都可进入 deletion；尚未处理的
旧 review 会先标记为 skipped，避免无来源内容之后被误开放。接受 deletion 表示
归档，选择“继续保留”表示维持条目原状态和可见性：原本 `ready` 的仍可供 Agent
查询，原本 `needs_review` 的仍保持私有。内容重新出现时，pending deletion 卡
自动结束。

MVP 直接实现本机/挂载文件夹；下一阶段接入 WebDAV、RSS、通用 REST，随后通过 RAGFlow Engine Adapter 获得 Gmail、Outlook、Google Drive、OneDrive、Dropbox、Notion、S3 等连接器。

个人微信观察器是另一条受限的连续事件通道：它只观察本人已正常登录的
`wx.qq.com` 当前会话中真实渲染的消息，不自动点击或滚动，不读取 Cookie，也不绕过
平台登录和账号限制。企业微信的生产完整性方向使用官方会话内容存档；当前仓库仅提供
官方 SDK 适配骨架。两条通道的实现边界见
[IM 数据源、治理与微信网页观察器](im-data-sources.md)。

### 3.3 自动治理

所有新内容按相同管线处理：

```text
discovered
  → fingerprinted
  → normalized
  → quality_checked
  → needs_review
  → ready（本人确认后）
```

当前 MVP 可以自动完成：

- SHA-256 内容指纹与精确去重。
- 文件名转标题、来源路径保留、更新时间规范化。
- 对支持的文本类型执行 UTF-8/UTF-8-SIG 解码，保留原始来源信息。
- 从文件路径和扩展名推断基础分类与标签。
- 记录精确重复、跳过项、同步错误和每次运行计数。

文件夹扫描只接收系统识别为 `text/*` 的文件，以及内置的 CSV、HTML、INI、Java、JavaScript/TypeScript、JSON、日志、Markdown、Python、RST、SQL、TOML、纯文本、XML、YAML 等文本扩展名。支持的文件采用 UTF-8-SIG/UTF-8 解码，错误字节会替换，再计算指纹并进入治理。PDF、DOC/DOCX、图片、音视频和其他不支持的二进制文件本轮计入 `skipped_count`，不会创建条目、治理任务或 Agent 索引。

当前 MVP 的每条新内容都会生成一张 review 卡片，由用户确认标题、分类和标签后进入 `ready`。以下更复杂规则属于后续治理能力：

- 两个相似但不完全相同的内容是否合并。
- 标题或分类无法可靠推断。
- 来源内容与本人修订发生冲突。
- 覆盖或不可逆合并。

### 3.4 碎片化治理

治理收件箱每次展示一张卡片：

- 问题是什么。
- 系统为什么这样判断。
- 建议操作以及操作后的效果。
- 原内容与建议结果的最小必要对比。

用户可以接受、编辑后接受、跳过、撤销。普通 review 卡接受后进入 `ready`；
deletion 卡接受后进入 `archived`，选择“继续保留”则跳过删除任务并维持当前
状态和可见性；只有原本 `ready` 的条目会继续供 Agent 查询。
操作必须幂等；断网操作先进入手机离线队列，恢复连接后自动提交。

### 3.5 Agent 数据服务

Agent 默认查询整个个人 vault 中的 ready 文件，不要求用户选择 dataset id。IM 使用
会话级显式开关：新会话默认关闭；启用后可检索该会话消息，知识结果仍只返回
`confirmed` 候选。该个人版不实现企业 RBAC 或逐资产授权。

返回结果至少包含：

- 命中的文本片段。
- 标题、来源、更新时间。
- 相关度分数。
- 可追溯的 item id 或引用。

MVP 使用一个固定为只读能力的 Agent token：

- 首次启动生成到私有数据目录，或由 `CENTAURAI_POCKET_AGENT_TOKEN` 注入。
- Owner 可查看 token 前缀和管理模式，但元数据接口不会回传完整 token。
- 对自动生成的 token，Owner 可立即轮换；完整新 token 只出现在轮换响应和受保护的 `agent-token` 文件中，旧 token 立即失效。
- 环境变量托管的 token 不支持在线轮换，需修改环境变量并重启。

多 Access Key、逐 key 撤销和 last-used 记录属于后续路线，不是当前 MVP。

### 3.6 超级秘书 P1b-A 任务协议

P1b-A 解决“Owner 下达、独立承办人回应的初始任务对齐”。它本身不承载对齐后的
任务变更；四类受控变更由下节 P1b-B 处理，对齐后的外部执行由 3.8 处理。外部日历
协作、第三方验收人、主动通知或承办人原生 App 仍不在当前范围。P1b-A 要求 Owner 同时是下达人和验收人，
任务仍处于 `issued`，并由不同成员承办。

每次提案保存一份完整协议，其中包括标题、目的、目标、策略、关键点、验收标准、
期限与双方/任务绑定。文档经 NFC/LF/UTC 秒级规范化后计算 SHA-256，后续修订通过
`parent_digest` 串联；revision 和 decision 只追加。当前回应方可：

- `accept`：原子应用精确修订，任务 `issued → aligned`；
- `reject`：记录理由并关闭协议，任务保持 `issued`；
- `counter`：提交完整的 N+1 修订与理由，当前回应方翻转。

反提案可改变标题、目的、目标、策略、关键点、验收标准与期限；issuer、assignee、
acceptance owner、domain、tier、priority 和初始 task version 在同一 case 中固定。协议
待回应或已接受时，绕过协议直接修改这些协议字段必须 fail closed；真实字段或
成员漂移会使 pending case 变为 `stale` 并撤销能力。

Owner 创建邀请后同时获得分享链接和验证码。两者可交换为最长 10 分钟、无刷新、
仅绑定单一 task/case/device 的会话，该会话不能读取 Workspace 其他数据、邮件或文档。
所有回应还必须通过 ETag/version CAS、revision ID/digest 校验、稳定幂等键和设备绑定。
任务 token 由数据根中独立持久的任务会话 HMAC key 做域分离后确定性生成，明文不落库；
该 key 首次升级可由当时的 Owner secret 引导一次，之后与 Owner token 轮换解耦，并与
数据库作为同一备份恢复集。会话 live 且协议 current 时，同一
invitation/code/device/key/body 的任意重试或并发都返回完全相同的 token/session/
原 expiry，不延寿也不重复审计。不同 key/body/device 使用正确凭据则 fail closed 并撤销
已有会话；错误 code 不能撤销已有会话。单 revision/case 分别限制为 3 MiB 和
4 MiB/100 revisions/100 decisions，敏感 JSON 请求体最多 8 MiB。

这一保证级别只证明“链接+验证码”的能力持有。由于 Owner 一开始就同时看到两段
凭据，系统不能据此证明响应者是指定自然人或企业，也不构成授权代理、电子签名或
法律意义上的不可抵赖证明。WebAuthn/设备持有证明和组织 IdP 是未来增强。旧 v3
confirm/aligned 历史不回填为新协议决定，不获得追溯证明。

### 3.7 超级秘书 P1b-B 任务变更协议

P1b-B 在任务完成初始对齐后，为以下四类 exact change 建立新的不可变确认边界：

- `assignee`：patch 只能是 `assignee_member_id`；
- `due_at`：patch 只能是带时区并规范化到 UTC 秒精度的 `due_at`；
- `acceptance_criteria`：patch 只能是一组非空验收标准；
- `abnormal_close`：patch 只能是非空 `abnormal_close_reason`。

创建提案只产生 `proposed` 记录，不自动改变任务。每份 `centaur.task-change.v1` 文档精确
冻结 `schema/workspace_id/task_id/change_id/change_type/base_task_version/proposer_role/
proposer_member_id/responder_role/responder_member_id/before/patch/reason` 13 个字段；客户端
和服务端执行相同的 NFC、LF、UTC 秒级规范化与 SHA-256 校验，拒绝额外字段或类型不匹配
的 patch。proposal 与 decision 只追加，并通过 digest、change/task version、成员和会话
绑定，不能把另一项任务、另一名承办人或另一份提案的决定移用到当前变更。

提议方固定为任务 issuer，回应方冻结为提案时的当前 assignee。主人自办任务中两者虽为
同一 Owner，仍须显式接受或拒绝；外部任务只能由当前承办人用“邀请链接 + 独立渠道
验证码”交换的 `cp_task_ch_` scoped session 接受或拒绝，Owner token/Owner-device
不能代答。Owner 只能带理由取消自己的待处理提案。接受才在一个事务中写不可变决定、
应用 exact patch、更新任务并撤销邀请/会话；拒绝不改变任务。`assignee` 被接受后任务
回到 `issued`，清除旧执行/提交终态字段，并须针对新承办人重新完成 P1b-A 对齐。

所有回应要求 change ETag/`If-Match`、相同的 change/task expected version、proposal
digest、设备 ID、稳定幂等键和唯一 client mutation ID；陈旧版本、摘要不符、角色或任务
绑定漂移全部 fail closed。相同 actor/key/body 的精确重试只重放第一次结果，不会创建
第二个决定或再次更新任务。任务变更是在线关键动作，不进入离线 Outbox，也不做本地
乐观接受。

外部邀请页在 code 验证前不展示任务或变更内容。code 与会话 token 明文不落库，
`cp_task_ch_` 最长 10 分钟、无 refresh，只能访问绑定的 change/task/responder/device；
scoped 投影不包含 memo 正文、source ref、excerpt、联系方式、邮件或文档。由于 Owner
创建邀请时本来就同时看到链接与 code，这一流程只证明双通道能力持有，不证明指定
自然人或企业身份，也不构成授权代理、电子签名或不可抵赖证明。schema v6 不为升级前
已关闭的历史变更补造 proposal/decision，旧记录不能被描述为具有 P1b-B 追溯证明。

### 3.8 外部承办任务执行

当前双方完成 P1b-A 后，Owner 可为处于 `aligned|in_progress` 的外部承办任务签发一次
执行邀请。邀请链接与独立渠道验证码交换为绑定 task、assignee、assignment epoch 和
device 的短 access 与可旋转 refresh；它不开放 Workspace 通用数据、memo/source/
evidence、邮件、文档或成员联系方式。access 最长 10 分钟，refresh 每次使用后旋转，
24 小时不活动失效且绝对不超过 7 天或任务期限后 7 天。明文 code/token 不落库；旧
refresh 非精确短窗口重放会撤销整个 family。

承办人看到的最小执行投影只包含任务目的/目标/策略/关键点/验收标准、阶段/进度/期限、
本人最近执行回报和步骤图。能力闭环为：

1. 承办人把已对齐任务启动为执行中；
2. 执行中可追加本人 check-in，记录自报进度、风险、阻塞、下一步和预测时间；
3. 只可改变本人负责步骤的状态，服务端继续校验依赖、step version 和完整执行视图；
4. 本人 leaf action 完成后提交验收；
5. Owner 按验收标准接受，或填写原因退回返工；返工后原绑定仍有效时可继续并重新提交。

check-in 是 append-only 执行事实，不直接改变正式 progress 或 task version。步骤完成会
重算正式 progress；submit 把 progress 设为 100，但不等于验收。存在待确认 P1b-B change
时，承办人仍可写 check-in 说明情况，start/步骤修改/submit 全部停用，UI 也不显示相应
控制。Owner 不能用普通 transition 冒充承办人启动或提交。

浏览器工作台默认关闭；配置规范 HTTPS public Origin 后才可签发。页面完全服务端渲染、
不运行脚本，使用同源表单、path-scoped Secure/HttpOnly/SameSite=Strict Cookie、签名
CSRF、精确 Origin 与 Fetch Metadata 校验。短 access 过期时必须由用户显式进入 continue
页面再提交 refresh，不静默续期。该 BFF 不开放 CORS、拒绝 OPTIONS，并对所有成功和
错误响应设置 no-store、严格 CSP、HSTS、no-referrer、frame deny 等边界头。

换人使单调 `assignment_epoch` 增加并撤销旧执行能力；任务终态/删除、成员停用、绑定或
期限失效同样 fail closed。执行事件以 capability session 为实际 subject，同时记录
`on_behalf_of_member_id`、`assurance_method` 和 epoch。task `updated_by`、check-in
`created_by` 只是“该成员绑定的能力代为执行”的逻辑字段，不是自然人亲自操作或不可
抵赖证明。人员和任务分析必须按 execution event assurance 加权。

双通道执行仍只有 A1 能力持有保证：Owner 签发时同时看到路径和验证码，因此系统不
证明指定自然人/企业身份、授权代理、电子签名或法律不可抵赖。更高保证仍需 WebAuthn、
设备持有证明、组织 IdP 或目标法域签名设施。

### 3.9 任务结果与保证等级归属分析

Owner 可按 Workspace 本地日期读取最多 366 天的任务分析。首版不新增分析表，也不回填
历史认证结论：服务端在一个一致性只读快照中组合当前任务结果、不可变协议/变更决定和
typed execution 事件，页面退出后不持久化结果。

产品把两类结论永久分栏：任务结果只陈述任务 stage、health、期限、验收、异常关闭、
返工与周期样本；人员视图只陈述当前逻辑承办快照和分动作的归属证据。不得输出人员排行、
绩效总分或“某自然人本人完成”的无保留结论。

保证策略 v1 使用整数 basis points：明确且绑定正确的 Owner control 为 A2/10000，任务
范围能力为 A1/5000，未知、旧格式、系统或完整性不匹配为 A0/0。权重只是证据量的展示
折扣，不是身份概率。外部执行 A1 必须验证 session/family/task/member/device/assignment
epoch；agreement/change 必须验证不可变 decision。技术性的邀请、session、refresh 与
撤销事件不进入工作动作。只要人员视图含 A1，客户端持续显示“能力持有不等于本人、授权
代理、电子签名或不可抵赖”的警示。

### 3.10 超级秘书 P1c-A 备忘落地

P1c-A 把“记录下来”与“开始落地”明确分开。客户端可以根据任务候选类型、期限、
紧急程度和固定行动措辞解释为什么建议“转为任务”或“排入日程”，但建议绝不自动
执行；IM 扫描得到的事项仍须先完成主人确认，主人还须在落地表单上显式提交。

可落地对象必须是 Pocket 服务端中未删除、`active`、已确认或无需确认且从未物化的
备忘。同一备忘只能二选一创建一个任务或一个 Pocket 内部日程；转换完成后备忘变为
`converted` 并锁定，只能跳转查看关联目标，不能编辑、删除或再次转换。客户端离线时
不能乐观物化。

任务表单承接标题、目的、目标、策略、关键点、验收标准、承办人、期限和优先级；
服务端继承备忘的工作/个人域及来源，并固定 Owner 为下达人和验收人。个人备忘委派给
非 Owner 必须得到清晰披露确认；确认范围只是任务表单中主人决定交付的内容，原始备忘
正文、IM 引用和摘录不会提供给承办人的 scoped 对齐协议。

日程表单承接标题、说明、明确起止时间、IANA 时区、全天标志和类型。结果只是 Pocket
内部的 `scheduled` 执行安排，无参与人和外部 provider，不发送 Outlook、Google、系统
日历或 IM 邀请，也不声称参与人已经收到通知。

两个操作都使用 memo 版本的 Header/body 双 CAS、稳定幂等键和设备绑定；成功原子返回
converted memo 与唯一目标，HTTP ETag 对应转换后 memo。服务端 v5 不可变账本、数据库
触发器和通用创建入口旁路拒绝共同保证只落地一次。旧版缺少完整原子事件和披露证据，
升级不回填任何历史 task/calendar 链接；任一旧链接或无账本 converted memo 都会
fail closed，不能靠推断补造历史原子性或审计证明。

## 4. 数据状态

条目状态保持简单：

| 状态 | 含义 | Agent 可见 |
| --- | --- | --- |
| `inbox` | 预留的导入暂存状态；当前文件夹/手机采集直接进入 `needs_review` | 否 |
| `needs_review` | 需要本人判断 | 否 |
| `ready` | 已通过质量门 | 是 |
| `archived` | 用户归档或被新 generation 替代 | 否 |

同步过程中不撤回上一代已经 ready 的内容。新版本完成处理后再原子切换，避免 Agent 在频繁同步时反复失去数据。

## 5. 离线队列

手机端所有写操作都使用同一持久化状态机；发送中的瞬时状态不单独落盘：

```text
pending ──成功确认──> 从队列移除
   ├──可重试失败──> pending（更新 nextAttemptAt）
   └──永久 4xx──> needs-attention ──手动重试──> pending
```

规则：

- 每条操作都有持久化 idempotency key。
- App 启动、回到前台、每 20 秒周期检查和手动重试都会尝试清空队列。
- 网络错误、408、429 和 5xx 采用带抖动的退避；其他 4xx 停在“需处理”，避免永久错误无限重放。
- 不自动丢弃用户采集的文字或 URL。
- 每项操作绑定由服务地址和 Owner token 派生的连接配置标识，不会误发到后来切换的服务。
- 原生端队列用 AES-GCM-256 加密，密钥保存在 SecureStore；Web 预览只能降级为浏览器本地明文存储。
- 未配置 Owner token 时不允许创建任何真实写入队列；分享内容在保存被拒绝时不会被清除。
- 加密队列无法读取时暂停所有新写入，避免覆盖旧操作；只有用户经过不可逆确认后才能永久清除并重新开始。

## 6. MVP 范围

必须交付：

- 独立安装与独立数据目录。
- 文件夹数据源、增量扫描和运行历史。
- 个人微信 Firefox 可见 DOM 观察器、本机 Native Host、配对、心跳和覆盖缺口。
- IM 会话/身份/消息/版本/附件引用/证据模型，以及默认待确认的知识候选。
- 新 IM 会话默认禁止 Agent 使用。
- SQLite/FTS5 存储与全文检索。
- 自动去重、基础质量门和治理任务。
- 手机今日页、治理页、来源页、连接设置。
- 离线治理操作队列。
- 系统文字与网页 URL 分享接收入口。
- Agent 只读检索、单一 token 元数据和立即轮换。
- 超级秘书 P1b-A 不可变任务协议、任务范围会话与 accept/reject/counter。
- 超级秘书 P1b-B 四类 exact task change、角色绑定回应、双通道 scoped session 与
  append-only proposal/decision。
- 外部承办人的 v7 执行邀请、access/refresh 轮换、最小投影、start/check-in/本人步骤/
  submit、Owner 返工/验收与无脚本 HTTPS 工作台。
- 超级秘书 P1c-A 主人确认后的备忘转任务或 Pocket 内部日程，以及不可变物化账本。
- 自动化测试与一键启动说明。

暂不进入 MVP：

- 企业 RBAC、SSO、租户、群组、审批、发布中心。
- 手机上的邮箱/网盘常驻后台拉取。
- Apple Notes、短信、通讯录和照片全量原生连接器。
- 文件、图片和 PDF 的手机分享上传与解析。
- 多 Agent Access Key、逐 key 撤销和 last-used 审计。
- 企业级合规审计与多副本容灾。
- 把 RAGFlow 或旧 database 的数据目录直接挂载为本产品数据库。

## 7. 验收场景

1. 新增一个测试文件夹，放入两个内容相同、文件名不同的文本文件；同步后只保留一个有效内容，并产生可解释的重复记录或任务。
2. 一条内容在治理确认前无法通过 Agent API 查到，确认后可以查到且返回来源。
3. 手机断网接受一张治理卡片，重启 App 后操作仍在；恢复网络后只提交一次。
4. 修改源文件后再次同步产生新版本，不让 Agent 读到半处理状态。
5. 旧 database、企业 DataHub 和 Pocket 可以同时启动，端口与数据互不冲突。
6. 轮换自动生成的 Agent token 后，旧 token 立即不能查询，新 token 可以查询。
7. 条目失去最后来源后出现 deletion 卡；接受后归档。选择继续保留时，原
   `ready` 条目仍可见，原 `needs_review` 条目仍不向 Agent 开放。
8. 外部承办任务的一次 accept/reject/counter 只留下一个不可变决定；旧版本、
   错误 digest、越权 token 和协议字段绕过写入全部失败，精确幂等重试不产生第二次副作用。
9. 同一条已确认备忘并发请求转任务和排日程时只有一个成功；成功响应同时返回
   converted memo 与唯一目标，重复重试不产生第二个目标，通用创建接口不能伪造关联。
10. 对四类 P1b-B 变更分别验证 self-managed 显式回应和 external 双通道回应；Owner
    不能代替外部 assignee 接受或拒绝，精确重试/并发只写一个决定。承办人变更接受后
    任务回到 `issued`，并须为新承办人重新完成 P1b-A；旧历史记录不获得补造的证明。
11. 外部承办人使用 v7 execution capability 完成 start、check-in、本人步骤和 submit；
    pending change 期间仅 check-in 保留，Owner 可带理由退回返工并最终验收，Owner 不能
    代替承办人启动或提交。
12. 无脚本 HTTPS 工作台的 Cookie path、CSRF/Origin/Fetch Metadata、无 CORS 和显式
    session continue 生效；refresh reuse、换人 epoch、终态和成员停用均撤销旧能力，
    分析输出不会把 A1 `on_behalf` 逻辑归属宣称为真实身份或签名。
