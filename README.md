# CentaurAI Pocket

> 半人马随身数据中心：把分散的个人数据持续同步、轻量治理，并安全地提供给个人 Agent。

CentaurAI Pocket 是一个以 Android/iOS 手机 App 为主要交付形态的全新、独立
单人私有数据中心。它不是企业 DataHub 的缩小版，也不是旧
`centaurAI-database` 的第二个客户端。它聚焦一个完整闭环：

1. 设定个人数据源，服务端按计划自动同步。
2. 自动完成内容指纹、去重、标准化和基础质量检查。
3. 把新内容的确认任务送到手机治理收件箱，用碎片时间快速处理。
4. 治理完成的数据进入 `ready` 区，供个人 Agent 通过只读接口调用。

## 当前实现范围

- 独立 FastAPI 服务、SQLite 数据库和 FTS5 全文检索。
- 文件夹数据源扫描、增量同步、SHA-256 去重和同步运行记录；MVP 只接收列出的 UTF-8 文本，PDF/Office、图片、音视频等不支持文件计为 skipped，不建条目。
- 个人微信 Firefox 可见 DOM 观察器：一次性配对、最小权限 Native Messaging
  Host、心跳与覆盖缺口、会话/消息/身份/证据模型，以及手机来源状态界面。它只记录
  `wx.qq.com` 当前真实渲染且可可靠识别的消息，不是完整聊天历史。
- 企业微信官方会话内容存档适配骨架：通过部署方提供的官方 C SDK 分页取得事件，按
  公钥版本注入企业私钥解密器后再调用 SDK 解密；Source 注册、持久游标、媒体下载和
  后台调度仍需生产集成，不能宣称已经全量同步。
- 从明确措辞保守生成决定、承诺和任务候选，默认保持待确认；每个新 IM 会话默认禁止
  Agent 使用，并保留消息级证据与来源权威标记。
- 单人治理收件箱：接受、跳过、撤销、修改条目元数据，并确认来源消失后的归档决定。
- 超级秘书业务任务支持 P1b-A 不可变对齐协议：完整修订经规范化
  SHA-256 摘要串联，当前回应方可接受、拒绝或提出完整反提案；接受会在
  同一事务中把精确修订落到任务并进入 `aligned`。链接与验证码交换的
  10 分钟任务会话只能读写该任务协议，不能访问 Workspace、邮件或文档。
- 超级秘书 P1b-B 任务变更协议冻结 `assignee`、`due_at`、
  `acceptance_criteria`、`abnormal_close` 四类 exact change 的当前值、patch、原因、
  任务版本、提议方与回应方，并以规范化 SHA-256 摘要绑定不可变提案和决定。
  主人自办任务也必须由 Owner 显式接受或拒绝；外部任务只能由提案冻结的当前承办人
  通过双通道 scoped session 回应，Owner 不能代答，只能带理由取消自己的提案。
  承办人变更被接受后，任务回到 `issued`，并须针对新承办人重新完成 P1b-A 对齐。
- workspace schema v7 提供外部承办任务执行：10 分钟 access、可旋转 refresh、最小
  执行投影以及 start/check-in/本人步骤/submit；Owner 负责退回返工或最终验收。可选
  HTTPS 工作台完全无脚本、同源、无 CORS，并使用 path-scoped 安全 Cookie 与签名 CSRF。
- P1b-A/P1b-B/外部执行的双通道流程只证明“同时持有分享链接与验证码”的能力；Owner
  在创建邀请时本来就同时看到两段凭据，因而它不证明自然人、企业身份、授权代理或法律签名。
  WebAuthn/设备持有证明与组织 IdP 是未来增强，不在当前保证范围。
- 超级秘书支持 append-only 任务复盘和按服务端时钟即时推导的任务关注清单；自报进度
  不覆盖正式进度，风险由前端同步主动呈现，而不是后台推送。
- Agent 只检索通过相应治理门的数据：文件条目必须为 `ready`；IM 会话必须由 Owner
  明确允许，其中知识候选还必须是 `confirmed`。
- 独立的单一 Agent 只读 token，可查看前缀并立即轮换；无租户、角色、群组和审批流。
- Expo 原生手机 App（主产品）：Android/iOS 今日概览、治理卡片、同步源、连接
  设置、原生端加密离线队列，以及文字/网页 URL 的系统分享入口。UI 使用当前
  CentaurAI“暖米”品牌体系；Web 只作为开发预览。
- Electron 桌面辅助壳：用于本机开发演示和自动启动 API，不是个人版的主要交付
  入口。
- Outlook 单账户邮件连接器：Device Code 登录、Inbox 元数据增量同步、正文/附件按需
  读取、任务候选确认，以及向原邮件 Sender 新发纯文本邮件的两次确认发送；网络歧义
  只读核验，绝不自动重发。当前不读取 Reply-To，也不保证 Outlook 线程回复语义。

## 产品边界

| 产品 | 核心职责 | 与 Pocket 的关系 |
| --- | --- | --- |
| CentaurAI 企业 DataHub（现有 RAGFlow 改造） | 企业数据资产、组织权限、审批发布、审计 | 完全独立；只参考其质量门和 Agent 安全思想 |
| `centaurAI-database` | 个人记忆、知识检索、Wiki/MCP | 完全独立；以后可作为主动配置的数据源或下游知识目标 |
| RAGFlow 上游 | 连接器、复杂解析、向量索引和检索引擎 | 可选 HTTP 适配器与投影映射已落地；自动投影任务尚未接通，且它始终只是可重建检索投影 |
| CentaurAI Pocket | 单人自动同步、碎片化治理、Agent 数据服务 | 本仓库 |

## 独立身份

- 仓库：`centaurai-pocket`
- API：`127.0.0.1:8718`
- Agent Gateway 预留：`127.0.0.1:8720`（MVP 不监听；REST 与 MCP 均由 8718 提供）
- API 前缀：`/api/v1`
- 默认数据目录：`~/.local/share/centaurai-pocket`
- 移动 App ID：`ai.centaur.pocket`
- Electron Desktop App ID：`ai.centaur.pocket.desktop`
- URL Scheme：`centaur-pocket`
- 本地存储前缀：`centaur-pocket-*`

这些值都不复用旧产品的端口、目录、包名、数据库或登录状态，因此三个产品可以分别安装、启动、备份和卸载。

## 仓库结构

```text
centaurai-pocket/
├── apps/mobile/          # 主产品：Expo Android / iOS 手机 App
├── apps/desktop/         # 辅助：Electron 本机预览与 sidecar 管理
├── apps/wechat-observer-extension/ # Firefox 微信可见 DOM 观察器
├── services/api/         # FastAPI、SQLite、同步与 Agent API
├── docs/                 # 产品、架构、接口与隔离说明
├── scripts/              # 本地开发和验证脚本
└── tools/native-host/    # Firefox Native Messaging 本机桥接
```

## 快速开始

后端：

```bash
cd services/api
uv sync --group dev
uv run uvicorn centaur_pocket.main:app --host 127.0.0.1 --port 8718 --reload
```

手机端（另一个终端）：

```bash
cd apps/mobile
npm install
npm run start
```

默认手机端连接 `http://127.0.0.1:8718`，只适合同机开发。真机使用时，在“设置”中填写电脑或 NAS 的 HTTPS 地址；即使通过私有 VPN 连接，移动端生产配置也应使用 HTTPS。

手机 App 完整校验与 Android/iOS 云构建入口：

```bash
./scripts/build-mobile.sh
./scripts/build-mobile.sh android preview   # EAS 内部测试 APK
./scripts/build-mobile.sh android production # Google Play AAB
./scripts/build-mobile.sh ios preview        # EAS iOS 内部测试包
```

EAS 构建需要产品所有者登录 Expo 账号并确认签名；仓库不会虚构或提交账号、
证书、keystore 与 Apple Team。

也可以在仓库根目录运行：

```bash
./scripts/bootstrap.sh
./scripts/dev.sh
```

Electron 桌面辅助壳构建与快捷方式：

```bash
make desktop
make desktop-install
gtk-launch ai.centaur.pocket.desktop
```

桌面版自动连接 `127.0.0.1:8718`，不需要在页面里复制 Owner token；关闭窗口时
会停止由它启动的 API。为避免把会话凭据交给非受管进程，启动前若 8718 已被任何
服务占用，桌面版会明确拒绝接管。受管 sidecar 同时接受数据目录中 `0600` 保存的
稳定 Owner token，供明确配置的本机秘书客户端使用；Electron Main 和 Renderer
始终只使用本次进程的随机会话凭据。

完整静态验证和真实 HTTP/MCP 冒烟测试：

```bash
make test
make smoke
```

本机微信网页观察器的检查与 Native Host 安装：

```bash
make observer-check
make observer-native-install
```

Firefox 临时加载、配对、覆盖语义、卸载和排错请按
[IM 数据源与微信网页观察器](docs/im-data-sources.md)操作。该通道要求本人正常扫码并在
手机确认登录，不读取 Cookie、不会自动轮询会话，也不会绕过平台对网页版的限制。

## 安全模型

“单人、无复杂权限”表示没有 RBAC 和多租户，不表示把私人数据裸露到网络。Pocket 保留以下最小凭据类别：

- Owner token：手机配置、治理和同步操作使用；由环境变量提供，或首次启动写入私有数据目录。
- Agent token：只能查询通过治理门且策略允许的数据。MVP 同时只有一个；轮换后旧
  token 立即失效。
- Collector token：只允许绑定的本机观察器向对应来源提交心跳和事件。一次性配对后由
  Native Host 保存，不能当 Owner 或 Agent token 使用。
- 外部任务 scoped token：按协议、变更或 execution family 绑定单一任务、成员和设备，
  不能当 Owner/Agent token 使用。明文不落库；确定性 token 使用数据根中的独立持久
  `task-session-hmac-key`，该文件必须与 SQLite 数据库成对备份恢复。

服务默认只监听回环地址。需要跨设备访问时，应放在 HTTPS 反向代理后，并限制在可信局域网或私有 VPN 内；不要把 8718 直接暴露到公网。浏览器 CORS 与 MCP `Origin` 使用精确来源白名单，详见部署说明。

Outlook 授权固定为 `offline_access Mail.ReadWrite Mail.Send`。OAuth token、登录状态和
Inbox delta 游标使用本机 AES-GCM 加密；同步只保存邮件元数据，正文与附件按需取得，
归档附件还需通过格式白名单并加密落盘。给原邮件 Sender 新发的纯文本邮件在 prepare
与最终 confirm 两次明确确认后才发送；预览显示已核验的实际 From 地址。Graph 最后一次
核验 GET 与 `/send` 之间仍有无法消除的 TOCTOU，同邮箱其他
客户端、Exchange 规则和隐藏 MAPI 属性属于残余风险。

详细设计见：

- [产品规格](docs/product-spec.md)
- [系统架构](docs/architecture.md)
- [API 契约](docs/api-contract.md)
- [独立性与迁移边界](docs/isolation.md)
- [启动、部署与手机构建](docs/deployment.md)
- [IM 数据源、治理与微信网页观察器](docs/im-data-sources.md)
