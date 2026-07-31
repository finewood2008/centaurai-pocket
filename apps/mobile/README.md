# CentaurAI Pocket Mobile

“半人马随身数据中心”的独立手机端。它是单人数据治理控制台，不复用旧
`centaurAI-database` 的登录、JWT、EAS projectId、包名、端口或本地存储。

Android application ID 与 iOS bundle identifier 均为
`ai.centaur.pocket`。它与旧个人节点 App 的 `ai.centaur.personalnode` 是两个
不同应用，不能互相覆盖升级；正式签名或上架后不要再随意修改这个标识。

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

### 新手机首次连接

`127.0.0.1` 在真机上指向手机自己，并不会自动连接运行在电脑上的 Pocket
服务。新手机首次使用时：

1. 先在电脑或 NAS 上启动 Pocket API，并通过可信 HTTPS 域名或私有 VPN 暴露服务；
2. 从服务端安全取得首次启动生成的 Owner token，不使用旧项目账号、密码或 JWT；
3. 在 App「设置」中填写服务地址与 Owner token，先点「测试连接」，成功后再保存；
4. 回到「今日」下拉刷新，确认显示的不是离线演示数据；
5. 从浏览器或备忘录向 App 分享一段测试文字，确认冷启动、热启动和离线重试均正常。

不要通过群聊、普通邮件或截图传递生产 Owner token。添加文件夹来源时填写的是
API 所在电脑、NAS 或容器能够读取的服务端绝对路径，不是手机文件路径。

## 系统分享接收

`app.config.ts` 使用 `expo-sharing` config plugin：

- Android 注册单项或多项 `text/*` 分享目标
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

## 手机端校验

在仓库根目录运行：

```bash
./scripts/build-mobile.sh
```

不带参数时只做本地校验，不会登录 EAS、创建项目、申请签名或发起付费云构建。
它依次验证 EAS JSON、Expo 配置、TypeScript、ESLint、单元测试、Expo Doctor
和 Web 导出。

也可以在本目录手动执行：

```bash
npm run typecheck
npm run lint
npm test
npx expo-doctor
npm run export:web
```

以上命令均不生成或验证 Android/iOS 原生二进制。只有成功完成原生构建、安装和
真机用例后，才能声称手机 App 已通过验收。

## EAS 云构建

`eas.json` 提供三套 profile：

| Profile | Android 输出 | iOS 输出 | 用途 |
| --- | --- | --- | --- |
| `development` | APK，内部发布 | 内部分发 IPA | 研发设备安装；当前是不带开发菜单的独立包 |
| `preview` | APK，内部发布 | 内部分发 IPA | 产品和真机验收 |
| `production` | AAB | App Store/TestFlight 构建 | 商店正式发布 |

当前没有安装 `expo-dev-client`，因此 `development` 没有开启
`developmentClient`。如果未来确实需要开发菜单，应先加入匹配当前 Expo SDK 的
`expo-dev-client`，再显式调整 profile，不能只打开配置开关。

构建示例：

```bash
# 内部 Android APK
./scripts/build-mobile.sh android preview

# Google Play AAB
./scripts/build-mobile.sh android production

# iOS 内部包或正式包
./scripts/build-mobile.sh ios preview
./scripts/build-mobile.sh ios production
```

每次云构建前脚本都会先运行完整本地校验，然后调用仓库锁定的
`eas-cli@21.4.0`。仓库有意不虚构或提交 EAS `projectId`、Expo 账号、
Android keystore、Apple Team、证书或 provisioning profile。第一次构建前必须由
产品所有者执行并确认：

```bash
cd apps/mobile
npx eas-cli@21.4.0 login
npx eas-cli@21.4.0 init
```

`eas init` 会把项目关联到当前登录账号，并可能把真实 `projectId` 写入 Expo
配置；应确认组织归属后再提交。签名凭据由 EAS 后续流程提示生成或选择，不能使用
仓库中原生预构建目录里的 debug keystore 作为生产签名。

版本来源固定为 `local`。发布新版本前必须在源码中明确递增应用版本，以及
Android `versionCode` 和 iOS `buildNumber`；同一商店版本号不能重复上传。

### Android 本地构建

具备 Android Studio/SDK、ADB、兼容 JDK 和已连接设备或模拟器后，可以执行：

```bash
cd apps/mobile
npx expo run:android --device
```

这会生成原生目录并安装调试包。当前仓库忽略生成的 `android/`、`ios/` 和安装包，
原生配置的来源仍是 `app.config.ts` 与 config plugins。

### iOS 构建限制

Linux 不能本地运行 Xcode、iOS Simulator 或安装 IPA；在 Linux 上调用构建脚本时，
iOS 只能交给 EAS 的 macOS 云构建机。iOS 真机或 TestFlight 仍需要产品所有者的
Apple Developer 账号、App ID、App Group、证书与 provisioning profile。本地
iOS 调试必须在 macOS/Xcode 上执行：

```bash
cd apps/mobile
npx expo run:ios --device
```

iOS 分享接收依赖 Share Extension 与 `group.ai.centaur.pocket` App Group，当前
deployment target 为 iOS 16.4。主 App 和 Extension 必须由同一 Apple Team 正确
签名后再做真机测试。

## 真机验收边界

Android preview APK 至少需要验证：

- 首次安装、覆盖升级、完全退出后重启，以及设备重启后的连接设置；
- 使用真实 HTTPS/私有 VPN 地址访问受保护的 dashboard；
- 从浏览器、备忘录等真实 App 分享文字与 URL，分别覆盖冷启动和热启动；
- 断网保存、恢复网络后的离线队列重试，以及切换连接后旧队列不会误发；
- 小屏、刘海/挖孔、系统大字体、软键盘和底部手势区域；
- 最终 APK/AAB 的包名为 `ai.centaur.pocket`、生产签名正确且无多余敏感权限。

连接 Android 设备后可辅助验证路由与文字分享：

```bash
adb install -r centaurai-pocket-preview.apk
adb shell am start -W -a android.intent.action.VIEW \
  -d 'centaur-pocket://handle-share'
adb shell am start -W \
  -a android.intent.action.SEND \
  -t text/plain \
  --es android.intent.extra.TEXT 'CENTAUR_SHARE_CANARY' \
  -n ai.centaur.pocket/.MainActivity
```

iOS 需要在 iOS 16.4+ 真机上确认分享面板能看到半人马 App、Share Extension
可以唤起主 App、App Group 能传递文字/URL，并覆盖未启动、后台和前台三种状态。
Expo Go、Web 预览和模拟 deep link 都不能替代这项验收。

当前边界：

- 本地 Android 构建可用 `npx expo run:android`，但必须先安装 Android SDK、
  接受相应许可，并准备设备/模拟器和兼容的 Java/Gradle 环境。
- Linux 不能本地构建 iOS；本地 iOS 需要 macOS 与 Xcode。
- EAS 云构建需要产品所有者的 Expo 登录、真实项目关联与签名配置。
- `expo-sharing` config plugin 或接收类型变化后必须重建原生二进制。当前 iOS
  Share Extension deployment target 为 iOS 16.4；接收分享仍属于 Expo 标记的
  实验能力，应在 iOS 16.4+ 的目标系统版本上真机验收。
