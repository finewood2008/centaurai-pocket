# CentaurAI Pocket Mobile

“半人马随身数据中心”的独立手机端。它是单人数据治理控制台，不复用旧
`centaurAI-database` 的登录、JWT、EAS projectId、包名、端口或本地存储。

## 已实现

- 今日概览：待治理、已就绪、总记录、同步源状态，以及后端 `quality_score` 治理进度
- 治理收件箱：接受、跳过、撤销；普通 review 接受前可修改建议标题、分类和标签；
  deletion 卡明确区分“确认归档”和“继续保留”
- 同步源：手机上登记服务端文件夹，设置手动、每小时或每天同步，并可手动触发
- 系统分享接收：基于 Expo SDK 57 `expo-sharing` 接收文字与网页 URL
- 连接设置：HTTPS 服务地址和服务端单一 Owner token；测试连接会访问受保护
  的 `dashboard`，不会只检查公共健康接口
- 所有写操作通过 `Idempotency-Key` 请求头去重；失败后按策略持久化重试
- 后端不可达时明确展示演示数据，不会把演示状态伪装为真实状态

当前系统分享只支持文字和网页链接。文件、图片和 PDF 不会上传，也不会把文件
URI 误存成文本。

## 本地运行

```bash
npm install
npm run start
```

Web 预览：

```bash
npm run web
```

默认 API 为 `http://127.0.0.1:8718/api/v1`，仅用于同机开发。真机应通过
HTTPS 反向代理访问运行 Pocket 服务的电脑/NAS，并将它限制在可信局域网或私有
VPN 中，例如：

```text
https://pocket.example.com
```

可在构建时设置：

```bash
EXPO_PUBLIC_POCKET_API_URL=https://pocket.example.com
```

非 loopback 的明文 HTTP 默认拒绝连接；Android 模拟器访问宿主机所用的
`10.0.2.2` 是开发例外。确有其他局域网开发需要时，必须在开发构建中显式设置
`EXPO_PUBLIC_ALLOW_INSECURE_HTTP=true`；不要把该开关带入生产包。

Owner token 为必填项。原生端 token 保存到 SecureStore；Web 预览只能使用浏览器
AsyncStorage，因此不应在共享浏览器中填写生产 token。

Electron 封装是例外：其 Main process 每次启动生成随机会话 token，并通过受限
IPC 代理 API，Renderer 不会收到 token，也不会把它写入 AsyncStorage。页面设置
会显示“由 Electron 主进程安全管理”，服务地址和凭据字段不可编辑。

未完成服务地址与 Owner token 配置时，采集、添加来源、治理和同步等真实写入都
不会进入离线队列；分享保存被拒绝时不会清除当前系统分享 payload，配置完成后可
返回继续保存。

Expo Web 从其他 Origin 访问 API 时，服务端还需把该精确 Origin 加入
`CENTAURAI_POCKET_CORS_ORIGINS`；CORS 不替代 Owner token。

## 系统分享接收

`app.config.ts` 使用 `expo-sharing` config plugin：

- Android 注册单项 `text/*` 分享目标
- iOS Share Extension 接受文字、网页 URL 与网页
- `+native-intent.ts` 将 iOS 的 `expo-sharing` deep link 导向
  `/handle-share`
- 根布局的原生 bridge 还会检测 payload，覆盖 Android 冷启动回到根路径的情况
- `/handle-share` 读取 `sharedPayloads`，成功写入离线队列后清除 payload
- Web 不调用原生 payload API；可直接打开 Web 路由，用查询参数预览确认页：

```text
http://localhost:8081/handle-share?title=...&text=...&url=...
```

原生 deep link scheme 是 `centaur-pocket://handle-share`。

config plugin 的改动需要重新构建原生应用，普通 Web 刷新不会注册系统分享目标。
接收分享属于 Expo 当前标记的实验能力，尤其 iOS 的行为仍受系统版本影响。

## 离线队列与隐私

- 原生端队列内容使用 Expo Crypto AES-GCM-256 加密，密钥保存在 SecureStore
- 升级时读到的旧版原生明文队列会立即重写为加密 envelope；没有连接 profile
  的遗留操作进入“需处理”，不会自动发送
- 如果加密队列无法读取，App 会暂停所有新写入以避免覆盖旧操作；设置页先展示
  数据丢失警告，再要求不可逆确认，才允许永久清除无法恢复的队列
- Android 禁用系统备份（`allowBackup: false`）
- Web 端队列因没有等价的原生密钥库，保存在当前浏览器的独立 AsyncStorage
  命名空间；分享内容可能敏感，请勿在公共设备使用
- 每项 mutation 绑定由规范化服务地址和 Owner token 生成的稳定 SHA-256
  connection profile；切换连接后，旧操作绝不会发往新服务
- 改回完全相同的地址与 token 会恢复对应队列；设置页可查看并删除非当前连接队列
- 网络错误、408、429 和 5xx 会指数退避；其他 4xx 进入“需处理”，停止自动重试，
  用户修正后可手动重试
- 数据源同步请求允许最长 120 秒，其余移动端请求保持短超时

## 接口约定

- `GET /api/v1/dashboard`
- `GET /api/v1/governance/tasks?status=pending`
- `POST /api/v1/governance/tasks/:id/apply|skip|undo`
- `GET /api/v1/sources`
- `POST /api/v1/sources`
- `POST /api/v1/sources/:id/sync`
- `POST /api/v1/captures`

新增文件夹来源的请求体：

```json
{
  "kind": "folder",
  "display_name": "家庭 NAS 文档",
  "config": {
    "path": "/srv/personal-docs",
    "recursive": true,
    "include_hidden": false
  },
  "schedule": "hourly",
  "enabled": true
}
```

这里的 `path` 是 API 所在电脑、NAS 或容器可读的绝对路径，不是手机文件路径。

治理接受动作可携带手机上编辑后的 patch：

```json
{
  "action": "apply",
  "patch": {
    "state": "ready",
    "title": "2026 年家庭保险保单",
    "category": "家庭财务",
    "tags": ["保险", "家庭"]
  }
}
```

删除卡使用固定归档 patch，不携带普通编辑字段：

```json
{
  "action": "apply",
  "patch": {"state": "archived"}
}
```

幂等键只通过 `Idempotency-Key` 请求头发送，避免污染启用
`extra="forbid"` 的业务请求体。

## 校验

```bash
npm run typecheck
npm run lint
npm test
npx expo-doctor
npm run export:web
```

这些命令不生成或验证 Android/iOS 原生二进制。当前仓库未配置 EAS
`projectId`、`eas.json`、签名凭据，也未提交 APK/IPA：

- 本地 Android 构建可用 `npx expo run:android`，但必须先安装 Android SDK、
  接受相应许可，并准备设备/模拟器和兼容的 Java/Gradle 环境。
- Linux 不能本地构建 iOS；本地 iOS 需要 macOS 与 Xcode。
- EAS 云构建需要产品所有者的 Expo 登录与签名配置；带开发菜单的 development
  client 还需额外安装 `expo-dev-client` 并配置 build profile。
- `expo-sharing` config plugin 或接收类型变化后必须重建原生二进制。当前 iOS
  Share Extension deployment target 为 iOS 16.4；接收分享仍属于 Expo 标记的
  实验能力，应在 iOS 16.4+ 的目标系统版本上真机验收。
