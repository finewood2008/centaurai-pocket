# 启动、部署与手机构建

Pocket 个人版以 Android/iOS 手机 App 为主要交付物；Web 只用于开发预览，
Electron 仅是本机辅助启动壳。

## 1. 本机开发

要求：

- Python 3.11+
- `uv`
- Node.js 22.13+
- npm

安装：

```bash
cd /home/user/centaurai-pocket/services/api
uv sync --group dev

cd /home/user/centaurai-pocket/apps/mobile
npm install
```

启动 API：

```bash
cd /home/user/centaurai-pocket/services/api
uv run centaur-pocket-api
```

未设置 `XDG_DATA_HOME` 时，首次启动会在 `~/.local/share/centaurai-pocket/` 生成：

- `pocket.db`
- `owner-token`
- `agent-token`

仅在对应 token 没有通过环境变量提供时才会生成文件。服务会尝试把数据目录权限设为 `0700`、令牌文件设为 `0600`；仍应确认底层文件系统支持这些权限。不要把令牌内容粘贴到聊天、提交到 Git 或放进手机截图。

`.env.example` 是配置参考，应用本身不会自动加载仓库根 `.env`。请在启动进程、systemd 或容器中显式提供环境变量。空字符串 `CENTAURAI_POCKET_DATA_DIR` 会安全回退默认私有目录；不需要覆盖时仍建议完全不定义它。

启动手机 Web 预览：

```bash
cd /home/user/centaurai-pocket/apps/mobile
npm run web
```

Web 预览用于检查界面和基本操作。系统分享接收、SecureStore 和原生 AES-GCM 队列需要原生 development/production build 真机验证；当前分享入口只接收文字和网页 URL。

### 可选：Firefox 微信网页观察器

网页观察器只适用于 API 与 Firefox 在同一 Linux 用户会话中的本机试点。安装 Native
Messaging Host 和运行契约测试：

```bash
make observer-native-install
make observer-check
```

安装为当前用户操作，不需要 `sudo` 或系统密码。扩展在开发阶段通过 Firefox
`about:debugging` 临时加载，普通长期安装需要 Mozilla 签名；Flatpak/Snap Firefox
可能无法启动宿主机 Native Host。来源创建、一次性配对、本人扫码登录、卸载和排错见
[IM 数据源、治理与微信网页观察器](im-data-sources.md)。

## 2. 凭据与 Agent token 轮换

读取自动生成的 Owner token：

```bash
POCKET_OWNER_TOKEN="$(tr -d '\n' < ~/.local/share/centaurai-pocket/owner-token)"
```

查看当前 Agent token 的非敏感元数据：

```bash
curl http://127.0.0.1:8718/api/v1/agent/token \
  -H "X-Owner-Token: ${POCKET_OWNER_TOKEN}"
```

自动生成模式可在线轮换：

```bash
curl -X POST http://127.0.0.1:8718/api/v1/agent/token/rotate \
  -H "X-Owner-Token: ${POCKET_OWNER_TOKEN}"
```

响应包含完整新 token；立即保存到 Agent 的安全凭据存储并清理终端历史或日志，旧 token 已经失效。新 token 同时写入数据目录的 `agent-token` 文件。环境变量托管模式会返回 `409`，需改 `CENTAURAI_POCKET_AGENT_TOKEN` 并重启。

MVP 没有 Owner 在线配对/轮换端点。Owner token 若由文件托管，应停服后替换 `owner-token`、保持 `0600`，再启动；若由环境变量托管，则修改变量并重启。

首次升级到带外部任务执行会话的版本时，必须先保留旧 Owner token 启动一次新版本，
确认数据目录已生成普通文件 `task-session-hmac-key`、权限为 `0600` 且服务正常启动，
再轮换 Owner token。该文件在同目录完整写入并 `fsync` 后才原子发布；如果发布前中断，
最终文件不会出现，保留旧 Owner token 重新启动即可。应把它和数据库作为同一恢复单元
做加密备份。签发过任何任务对齐、变更或执行 scoped 会话后，若该文件损坏或丢失，
必须连同 `pocket.db` 恢复同一备份集，不能生成新 key；否则既有凭据无法验证。
只有确认从未签发任何上述 scoped 会话、并仍持有升级时的
旧 Owner token 时，才可移走损坏文件并用旧 token 重新生成。不要先轮换 Owner token
再做首次新版本启动，否则无法复现兼容旧会话所需的初始派生 key。不得只复制
数据库、从另一实例复制 key，或通过手工“撤销”会话规避完整性校验。
发布前至少执行一次“保留数据库和 key、替换 Owner token、重启、验证旧 scoped
token 仍按原状态受控”的恢复演练。

### Outlook 应用注册

Outlook 连接器需要 Microsoft Entra 公共客户端应用，并启用 Device Code Flow。只配置
委托权限 `Mail.ReadWrite` 和 `Mail.Send`；服务发送的授权范围固定为
`offline_access Mail.ReadWrite Mail.Send`，不需要 `User.Read`，也不要配置或注入
client secret：

```bash
export CENTAURAI_POCKET_OUTLOOK_CLIENT_ID='<Microsoft application/client UUID>'
export CENTAURAI_POCKET_OUTLOOK_TENANT='common'
```

tenant 可使用 `common`、`organizations`、`consumers` 或租户 UUID；修改后重启 API。
当前只允许一个活动邮箱账户。OAuth 状态、token 和 Inbox delta 游标由数据目录中的
私有 AES-GCM 密钥加密；密钥文件与附件密文必须随整个数据根一起做加密备份，缺失密钥
时不能恢复这些数据。不要单独复制 token、游标或归档附件，也不要把它们写入日志。

邮件写操作要求认证凭据、`Idempotency-Key` 和 `X-Device-ID`，修改已有资源还要求
版本匹配的 `If-Match`。反向代理必须保留这些头，并禁止缓存 `/api/v1/mail/*` 响应。
发送歧义只能由只读 reconcile 核验，运维脚本不得自动重试 Graph `/send`。
本轮发送是给同步时记录的原邮件 Graph `Sender` 地址新发纯文本邮件并加 `Re:` 主题，
不读取 `Reply-To`，也不保证 Outlook 会话线程语义；确认页必须展示已核验的实际 From
地址。`ready` 当前没有显式 cancel/abandon 入口。
持久的 `prepare_uncertain` / `send_uncertain` 只锁定该回复和账户 disconnect，不影响
Inbox 等读取。用户须先在真实 Outlook/Sent Items 人工核验；当前没有安全自助关闭，
不要直接修改 SQLite。未来应通过高摩擦、可审计的 `close-unresolved` 流程处理。

## 3. 添加第一个自动同步文件夹

添加数据源：

```bash
curl -X POST http://127.0.0.1:8718/api/v1/sources \
  -H "X-Owner-Token: ${POCKET_OWNER_TOKEN}" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: first-personal-folder" \
  -d '{
    "kind": "folder",
    "display_name": "个人文档",
    "config": {
      "path": "/绝对路径/个人文档",
      "recursive": true,
      "include_hidden": false
    },
    "schedule": "hourly",
    "enabled": true
  }'
```

手动立即同步：

```bash
curl -X POST http://127.0.0.1:8718/api/v1/sources/<SOURCE_ID>/sync \
  -H "X-Owner-Token: ${POCKET_OWNER_TOKEN}" \
  -H "Idempotency-Key: first-sync"
```

手机“新增来源”中的路径同样是服务端路径，不是 Android/iOS 本机路径。

## 4. 真机连接

`127.0.0.1` 在手机上指手机本身，不能访问电脑。推荐两种方式：

### 私有 VPN

让手机和电脑进入同一个私有 VPN，仍通过该私有网络中的 HTTPS 地址访问服务。这种方式不需要向公网开放 API。

### 局域网反向代理

1. API 仍只监听回环地址。
2. Caddy、Nginx 或其他反向代理监听局域网地址。
3. 代理终止 HTTPS，再转发到 `127.0.0.1:8718`。
4. 防火墙只允许可信网段。

代理必须保留 `Authorization`、`X-Owner-Token`、`X-Device-ID`、`Idempotency-Key`、
`If-Match`、`MCP-Protocol-Version` 和 `Origin` 请求头，并关闭对这些凭据头和
一次性/会话 token 响应正文的访问日志记录。

只有明确理解风险时才把 `CENTAURAI_POCKET_HOST` 改为 `0.0.0.0`；不要把 8718 直接映射到公网。移动端默认拒绝非 loopback 的明文 HTTP，Android 模拟器访问宿主机所用的 `10.0.2.2` 是开发例外。其他受控开发环境可设置 `EXPO_PUBLIC_ALLOW_INSECURE_HTTP=true`，生产包不要启用。

手机设置需要：

- 服务 URL。
- `owner-token` 的内容。

Agent 使用另一个 `agent-token`，不要把 Agent token 填进手机 owner token 字段。

### 浏览器 CORS 与 MCP Origin

原生 App 通常不受浏览器 CORS 限制；Expo Web 和带 `Origin` 的 MCP 客户端需要精确白名单。例如：

```bash
export CENTAURAI_POCKET_CORS_ORIGINS='https://pocket.example.com'
```

多项用英文逗号分隔。每项必须是 `scheme://host[:port]`，不要带路径、不要用通配符。修改后重启 API。MCP 端点在收到 `Origin` 时会复用同一白名单进行额外校验；无 `Origin` 的客户端仍必须携带 Agent Bearer。

默认白名单还包含 `http://127.0.0.1:17818`，专供本机“半人马 AI 超级秘书”
Electron 壳；没有 Owner 凭据的其他本机网页仍不能读取或修改数据。

### 任务协议公网关

任务执行公开工作台必须显式启用，并使用反向代理对外提供的唯一 HTTPS Origin：

```bash
export CENTAURAI_POCKET_TASK_EXECUTION_PUBLIC_ORIGIN='https://tasks.example.com'
```

未设置或设置为空值时，公开工作台路由不会安装，Owner 创建执行邀请会返回 `503`，
且不会写入邀请记录。该值必须是规范 Origin；末尾 `/` 会被规范化去除，但不得包含
显式 `:443`、凭据、额外路径、查询参数或片段，配置不合法时 API 会拒绝启动。公网
代理启用后应转发 `/api/v1/task-execution-invitations/*` 公开工作台以及任务执行所需的
`/api/v1/task-executions/*` JSON 端点。公开工作台边界会拒绝浏览器预检请求并移除
CORS 响应头；不要在代理层补回跨域访问头。

承办方设备必须能信任并访问反向代理的 HTTPS 地址。公网关只需转发现有
`/api/v1/task-alignments/*` 和 `/api/v1/task-agreements/*` 端点，不应为前者自动注入
Owner token；`exchange` 会显式拒绝携带 Owner header 的请求。但这只是防止认证
上下文混用，不是身份证明：Owner 在创建邀请时已同时获得链接和验证码，
省略 Owner header 后也可交换会话。运维/验收话术不得把这个 A1 能力持有结果宣称为
自然人、企业身份或法律签名；这些保证需要未来接入 WebAuthn/设备持有证明或组织 IdP。

对 `/api/v1/task-alignments/*`、`/api/v1/task-agreements/*` 和 Owner discovery
`/api/v1/workspaces/{workspace_id}/tasks/{task_id}/agreement` 必须禁止共享缓存，并保留
`Cache-Control: no-store`、`Pragma: no-cache`、`Referrer-Policy: no-referrer` 和
`X-Content-Type-Options: nosniff`。不要记录邀请码、`cp_task_at_` token、协议请求体或
响应正文。任务 token 只有最长 10 分钟的绝对有效期，无 refresh；运维层不得尝试
延长或把它交换为 Owner/设备会话。
网关若对丢失响应做自动重试，必须原样保留 invitation、body、
`Idempotency-Key` 和设备 ID；会话 live 且协议 current 时，同 key/body 会由域分离
HMAC 确定性返回同 token/session/
原 expiry。网关不得自动生成新 key 或改写 body，因为不同 key/body 使用正确双凭据
会 fail closed 并撤销已有会话。
单 revision 规范 JSON 上限为 3 MiB，单 case 累计上限为 4 MiB/100 revisions/
100 decisions，敏感 JSON 请求体上限为 8 MiB。反向代理的 body/header 限制不得低于
合法契约，但仍应保留 API 自身限额作为权威边界。

## 5. Docker Compose

根目录的 Compose 配置只把 API 映射到宿主机回环地址，并以非 root 用户运行容器：

```bash
cd /home/user/centaurai-pocket
docker compose up --build -d
docker compose ps
curl http://127.0.0.1:8718/api/v1/health
```

- 数据位于命名卷 `centaurai-pocket-data` 的 `/data`。
- 仓库 `imports/` 以只读方式挂载为容器 `/imports`；容器数据源必须使用 `/imports/...` 路径，并且宿主文件权限要允许容器 UID 10001 读取。
- Compose 文件当前只显式传入调度间隔。需要自定义 token、CORS 或其他变量时，应在 `compose.yaml` 的 `environment` 中显式传入；根目录 `.env` 不会自动成为容器环境。
- `docker compose down` 不删除命名卷；不要使用 `down -v`，除非已经备份并明确要删除 Pocket 数据和 token。

首次启动后可在本机终端读取容器生成的 Owner token，再立即把终端内容清理掉：

```bash
docker compose exec pocket-api sh -c 'tr -d "\n" </data/owner-token'
```

停止服务：

```bash
docker compose down
```

## 6. Android / iOS 原生构建

仓库已包含 `apps/mobile/eas.json` 与可复现的 development、preview、production
构建 profile，但不包含 Expo EAS `projectId`、账号、签名凭据或已签名 APK/IPA。
`npm run export:web` 也不会验证原生分享扩展。

本地 Android 构建需要已安装 Android SDK、接受相应 SDK 许可、可用的设备或模拟器，以及与 Expo SDK 57 兼容的 Java/Gradle 环境：

```bash
cd /home/user/centaurai-pocket/apps/mobile
npx expo run:android
```

Linux 不能本地构建 iOS；本地 iOS 需要 macOS、Xcode 和 Apple 开发环境。

也可以由产品所有者使用预置 profile 发起 EAS 云构建。需要 Expo 账号登录并先把
项目关联到正确组织；iOS 还需要 Apple Developer 账号和签名配置：

```bash
cd /home/user/centaurai-pocket/apps/mobile
npx eas-cli@21.4.0 login
npx eas-cli@21.4.0 init

cd /home/user/centaurai-pocket
./scripts/build-mobile.sh android preview      # 内部测试 APK
./scripts/build-mobile.sh android production   # Google Play AAB
./scripts/build-mobile.sh ios preview           # iOS 内部测试包
```

若要生成带 Expo 开发菜单的 development client，还需先安装 `expo-dev-client`，
再显式为 development profile 设置 `developmentClient`；当前 development
profile 已预置为不带开发菜单的内部安装包。

系统分享接收依赖 `expo-sharing` 原生配置，修改插件设置后必须重新生成原生二进制，普通 JavaScript 热更新不会改变 iOS Share Extension 或 Android intent filter。SDK 57 插件生成的 iOS Share Extension deployment target 是 iOS 16.4，因此 iOS 16.4+ 才在支持范围内。当前只注册文字/网页 URL；文件、图片和 PDF 不会上传。Expo 仍把 iOS 接收分享标记为实验能力，因此必须在目标系统版本上做真机验收。

## 7. 测试

后端：

```bash
cd services/api
uv run pytest
```

手机端：

```bash
cd apps/mobile
npm run typecheck
npm run lint
npm test
npm run export:web
```

也可以从仓库根目录执行 `./scripts/verify.sh`。这会验证后端、TypeScript、lint、移动端单元测试、Expo Doctor 和 Web 导出。再执行 `./scripts/smoke.sh`（或 `make smoke`）可启动隔离的临时 API，实际走通文件夹同步、治理、Agent REST、手机采集幂等和 MCP 调用。两者都不替代 Android/iOS 原生构建与真机分享测试。

完整端到端测试还应覆盖：

1. 同内容不同文件名只入库一次。
2. 治理前 Agent 查不到，接受后能查到。
3. 手机断网操作在重启后仍存在。
4. 恢复网络后相同 idempotency key 只执行一次。
5. Agent token 与 owner token 不能互换权限。
6. MCP `initialize` 后缺少/错误协议头会被拒绝，非白名单 `Origin` 返回 `403`。
7. 轮换自动生成的 Agent token 后，旧 token 立即返回 `401`。
8. 任务协议 accept/reject/counter 在正确 ETag/digest 下只执行一次，旧 ETag、错误
   digest 和同 key/不同 body 被拒绝。
9. `cp_task_at_` 只能读写绑定 case；Workspace、其他任务、邮件和文档均拒绝。
10. P1b-B 的 `assignee`、`due_at`、`acceptance_criteria`、`abnormal_close` 四类 exact
    change 均覆盖主人自办显式回应和外部承办人双通道回应；Owner 不能代替外部 assignee
    accept/reject，取消必须有理由，scoped 投影不泄露 memo/source/excerpt 或联系方式。
11. task-change decision 在正确 change/task version、ETag、proposal digest、设备和
    idempotency key 下并发执行只写一个决定、只更新一次任务；旧版本、错误 digest、
    cross-task/session、同 key 不同 body 和 renderer/offline 旁路全部拒绝。
12. `assignee` accept 后任务回到 `issued` 并要求新承办人重新完成 P1b-A；升级前的
    已关闭 task change 不获得补造的 P1b-B proposal/decision。

## 8. 备份和恢复

数据根只有 Pocket 自己使用。最简单的可靠备份方式：

1. 退出 Electron 桌面应用并停止其他 Pocket API，保证 SQLite WAL 和桌面
   profile 都不再写入。
2. 完整复制 `~/.local/share/centaurai-pocket/` 到加密备份位置。
3. 记录应用版本。
4. 恢复时先在隔离目录验证，再替换目标数据根。

不要把备份写入旧 database 或企业 RAGFlow 的数据卷，也不要只复制 `pocket.db` 而遗漏令牌和未来的附件目录。

当前最高 workspace schema 为 v7：v4 引入 P1b-A task agreement
case/revision/decision/session，v5 引入不可变 memo materialization 账本，v6 引入 P1b-B
task-change proposal/invitation/session/decision，v7 引入 task `assignment_epoch`、external
execution invitation/access session/refresh family/refresh token、绑定索引与撤销触发器。
v4/v5/v6 是正确的历史引入版本，不能把对应说明机械改写为“v7 引入”。

从任何早于 v7 的版本升级生产数据前，必须先停写并对整个数据根做可恢复的加密备份，再把备份复制到
独立临时目录。只对克隆副本启动新二进制，不能直接拿生产数据库做首次迁移演练。启动
前先检查不存在会阻断 v5→v6 的、无法补造双方确认依据的历史 pending 变更：

```sql
SELECT id, task_id, change_type, base_version
FROM secretary_task_changes
WHERE status = 'proposed'
ORDER BY id;
```

查询必须无行。若有结果，应继续运行旧版本，由 Owner 按旧业务语义明确处理或取消后，
重新停止写入并制作新克隆；不得直接改 SQLite、伪造 proposal/decision 或手工插入
v6/v7 marker。v6 会拒绝无迁移标记的 task-change 弱对象；v7 同样拒绝 execution
同名对象碰撞，并对“有 marker 但对象缺失、DDL 摘要或绑定被篡改”的数据库 fail closed。

在克隆副本启动新版本后确认：

```sql
SELECT version FROM secretary_workspace_schema_migrations ORDER BY version;
PRAGMA integrity_check;
PRAGMA foreign_key_check;
```

预期迁移标记精确包含 `1,2,3,4,5,6,7`，`integrity_check` 返回 `ok`，
`foreign_key_check` 无行。还必须：

1. 对比迁移前后 workspace、member、memo、task、change、calendar、meeting、document、
   event 和 idempotency 等核心表行数；v7 只给 task 增加默认 epoch 并新增 execution
   协议对象，不应静默增删业务记录。
2. 抽查 v6 proposal canonical JSON/digest 和 decision/change/status/actor/session 绑定；
   对新建的四类 exact change 分别执行 self-managed 与 external 测试。
3. 抽查 v7 invitation/session/refresh family/token 的 task、assignee、device、epoch 与
   generation 绑定，以及 access/refresh hash、单活动对象约束和精确 DDL 摘要；验证
   start、check-in、本人步骤、submit、pending change、Owner 返工/验收与 epoch 撤销。
4. 若启用公开工作台，验证 HTTPS、无脚本 Cookie/CSRF/Origin/Fetch Metadata、显式
   session continue，并确认 BFF `OPTIONS` 返回 `405` 且没有 `Access-Control-*`。
5. 在同一克隆副本上再次启动新版本，确认 migration timestamp、对象定义和业务行数不变，
   以验证初始化幂等。
6. 运行当次版本收集到的完整后端测试与静态检查，特别是外部双通道、refresh reuse、
   assignment epoch、跨 access 轮换 exactly-once、审计失败原子回滚、schema 对象碰撞和
   marker 缺失用例；通过标准是当前套件全部通过，不能沿用文档中的历史 passed 数。

v4 不会为 v3 时期已 confirm/aligned 的历史任务伪造 revision 或 decision；v6 同样不会
为升级前已 accepted/rejected/canceled 的历史 task change 伪造 P1b-B proposal/decision。
v7 不为历史任务补造 execution invitation/session/check-in 或 capability event。这些记录
保留原业务语义，但不获得新的不可变追溯、执行主体或身份保证。

只有克隆演练和业务验收全部通过后才可安排生产迁移。回滚必须成对恢复“旧二进制 +
升级前完整数据根”；不能让旧二进制继续写入已迁移的 v7 数据库，也不能只回退数据库
或只回退二进制。生产迁移后再次执行同样的 schema、完整性、外键和核心行数检查。

微信网页观察器的 Collector token 位于
`~/.config/centaurai-pocket/wechat-observer.json`，不在 Pocket 默认数据根。若业务要求
重装后继续使用，可把该 `0600` 文件单独放入加密凭据备份；若不需要恢复，重装时重新
配对并让旧 token 失效。不要把该配置作为普通文档同步到网盘或 Git。

如果 token 由环境变量托管，它不会写入数据目录；应在独立的加密 secret 管理系统中
备份并在恢复时重新注入。切换到环境变量模式后，数据目录里可能仍存在旧 token
文件，但运行服务会忽略它，不应误当作当前凭据。

Docker 部署的数据在 `centaurai-pocket-data` 命名卷中，不在宿主机默认数据根。应停服后对整个卷做加密备份，并同时验证恢复；不要只导出 SQLite 文件。

## 9. Electron 桌面封装

Linux 桌面版采用 Electron UI 与 PyInstaller `--onedir` API sidecar。构建过程
不会安装系统软件，也不需要 root：

```bash
cd /home/user/centaurai-pocket
./scripts/build-desktop.sh
./scripts/install-desktop-shortcut.sh
```

产物位于：

```text
apps/desktop/release/linux-unpacked/
```

应用本体复制到版本化目录
`~/.local/opt/centaurai-pocket/releases/<version-build>/`，原子更新的
`~/.local/opt/centaurai-pocket/current` 指向当前版本。快捷方式安装到当前 XDG
桌面目录，应用菜单安装到
`~/.local/share/applications/ai.centaur.pocket.desktop`。这些内容都只属于
当前用户，不会触发系统级 `.deb` 安装；源码仓库被移动后入口仍然有效。

启动时 Electron 要求 8718 端口空闲；即使监听者声称自己是 Pocket API，也不会
向它发送任何长期或会话 Owner 凭据，而是明确要求先停止已有服务。随后 Electron
为本次启动生成随机会话 token，启动随包 sidecar 并固定绑定回环地址。该会话
token 不写入数据库或浏览器存储，只在 Main process 与 sidecar 内存中存在；
Renderer、LocalStorage 和 DevTools 不会收到它。Sidecar 还会从私有数据目录加载
稳定 Owner token，供明确配置的秘书客户端访问；Main 不读取该长期凭据。应用退出时
会终止自己管理的进程，Linux sidecar 还配置了 parent-death signal，主进程崩溃时
由内核收尾。
新增文件夹来源时必须使用桌面原生目录选择器；获准路径记录在
`desktop-profile/approved-source-paths.json`（`0600`），Main 会拒绝 Renderer
提交未经选择器批准的路径，并通过文件系统真实路径复核防止软链接改向。

当前 CentaurOS/Ubuntu 的 AppArmor 策略限制便携 Chromium 的 user namespace，
而用户目录中的 `chrome-sandbox` 也不是 `root:root 4755`，所以便携启动器使用
`--no-sandbox`，Electron/Chromium 的进程级 sandbox 实际不生效。启动器强制
要求已安装的 bubblewrap 提供外层文件系统约束，让系统其余目录只读，只放开
Pocket 数据、会话运行目录与临时目录；但它仍共享网络、会话 socket 和 `/dev`，
不能视为 Chromium sandbox 的等价替代。这一已知本地折中不需要 `sudo`、setuid
安装或反复输入系统密码。没有 bubblewrap 时启动器会拒绝启动；正式跨机器分发
应改为已签名的系统安装包，并正确配置 Chromium sandbox。

验证入口：

```bash
desktop-file-validate \
  ~/.local/share/applications/ai.centaur.pocket.desktop \
  "$(xdg-user-dir DESKTOP)/CentaurAI-Pocket.desktop"
gtk-launch ai.centaur.pocket.desktop
curl http://127.0.0.1:8718/api/v1/health
```

桌面 API 日志位于
`~/.local/share/centaurai-pocket/desktop-api.log`，Electron/bubblewrap 启动
日志位于同目录的 `desktop-launcher.log`；两者都按 `0600` 创建。桌面封装不会
改变默认数据库位置；删除快捷方式或 Electron 产物不会删除用户数据。
