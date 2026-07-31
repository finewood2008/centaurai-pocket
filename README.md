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
- 单人治理收件箱：接受、跳过、撤销、修改条目元数据，并确认来源消失后的归档决定。
- 只有 `ready` 数据可被 Agent 检索。
- 独立的单一 Agent 只读 token，可查看前缀并立即轮换；无租户、角色、群组和审批流。
- Expo 原生手机 App（主产品）：Android/iOS 今日概览、治理卡片、同步源、连接
  设置、原生端加密离线队列，以及文字/网页 URL 的系统分享入口。UI 使用当前
  CentaurAI“暖米”品牌体系；Web 只作为开发预览。
- Electron 桌面辅助壳：用于本机开发演示和自动启动 API，不是个人版的主要交付
  入口。

## 产品边界

| 产品 | 核心职责 | 与 Pocket 的关系 |
| --- | --- | --- |
| CentaurAI 企业 DataHub（现有 RAGFlow 改造） | 企业数据资产、组织权限、审批发布、审计 | 完全独立；只参考其质量门和 Agent 安全思想 |
| `centaurAI-database` | 个人记忆、知识检索、Wiki/MCP | 完全独立；以后可作为主动配置的数据源或下游知识目标 |
| RAGFlow 上游 | 连接器、复杂解析、向量索引和检索引擎 | 未来可插拔引擎；当前仓库没有运行时依赖 |
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
├── services/api/         # FastAPI、SQLite、同步与 Agent API
├── docs/                 # 产品、架构、接口与隔离说明
└── scripts/              # 本地开发和验证脚本
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
服务占用，桌面版会明确拒绝接管。

完整静态验证和真实 HTTP/MCP 冒烟测试：

```bash
make test
make smoke
```

## 安全模型

“单人、无复杂权限”表示没有 RBAC 和多租户，不表示把私人数据裸露到网络。Pocket 保留两种最小凭据：

- Owner token：手机配置、治理和同步操作使用；由环境变量提供，或首次启动写入私有数据目录。
- Agent token：仅能查询已经进入 `ready` 状态的数据。MVP 同时只有一个；轮换后旧 token 立即失效。

服务默认只监听回环地址。需要跨设备访问时，应放在 HTTPS 反向代理后，并限制在可信局域网或私有 VPN 内；不要把 8718 直接暴露到公网。浏览器 CORS 与 MCP `Origin` 使用精确来源白名单，详见部署说明。

详细设计见：

- [产品规格](docs/product-spec.md)
- [系统架构](docs/architecture.md)
- [API 契约](docs/api-contract.md)
- [独立性与迁移边界](docs/isolation.md)
- [启动、部署与手机构建](docs/deployment.md)
