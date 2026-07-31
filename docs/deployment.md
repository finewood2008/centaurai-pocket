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

代理必须保留 `Authorization`、`X-Owner-Token`、`Idempotency-Key`、
`MCP-Protocol-Version` 和 `Origin` 请求头，并关闭对这些凭据头和轮换响应正文的
访问日志记录。

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

## 8. 备份和恢复

数据根只有 Pocket 自己使用。最简单的可靠备份方式：

1. 退出 Electron 桌面应用并停止其他 Pocket API，保证 SQLite WAL 和桌面
   profile 都不再写入。
2. 完整复制 `~/.local/share/centaurai-pocket/` 到加密备份位置。
3. 记录应用版本。
4. 恢复时先在隔离目录验证，再替换目标数据根。

不要把备份写入旧 database 或企业 RAGFlow 的数据卷，也不要只复制 `pocket.db` 而遗漏令牌和未来的附件目录。

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
Renderer、LocalStorage 和 DevTools 不会收到它。应用退出时会终止自己管理的
进程，Linux sidecar 还配置了 parent-death signal，主进程崩溃时由内核收尾。
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
