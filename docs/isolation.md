# 独立性与迁移边界

## 硬隔离要求

CentaurAI Pocket 必须满足：

1. 独立 Git 仓库和依赖锁文件。
2. 不以 Git submodule、Python import 或 Node workspace 方式运行时依赖旧 database。
3. 不读写旧项目的 `watch_folder`、`chroma_data`、`wiki`、`memory`、`gbrain_data` 或 `file_center.db`。
4. 不复用 8618、8620、8443、8080 等旧端口。
5. 不复用 `local-vector-db`、`centaurai-personal-memory.desktop`、`ai.centaur.personalnode` 等服务和应用标识。
6. 不复用浏览器 localStorage、IndexedDB、Keychain 或 Keystore key。
7. 不继承企业 DataHub 的用户、tenant、role、group、grant、review 或 publication 表。
8. 安装、启动、备份、恢复和卸载不要求另外两个产品存在。

## 与旧 database 的关系

旧 database 的职责是个人记忆与知识检索。Pocket 的职责是个人数据同步和治理。

当前仓库没有旧 database 连接器，也不会探测或自动迁移它的数据。未来只允许通过显式、版本化的边界连接：

- `DatabaseSourceAdapter`：用户主动把旧 database 的导出或只读 API 配置为 Pocket 数据源。
- `KnowledgeTargetAdapter`：用户把 Pocket 的 ready 数据推送到旧 database 作为知识材料。

两种适配目前都未实现；未来实现时也必须默认关闭、不共享数据库文件，并记录 Sync Run。删除任一产品不会删除另一产品的数据。

## 与 RAGFlow 的关系

当前 Pocket 不依赖、启动或调用 RAGFlow。未来若接入，只能通过网络 API 或独立 worker 适配，不把企业治理代码复制进 Pocket。

可以复用：

- 数据源连接器和增量游标思想。
- DeepDoc/任务/文档/分块解析链。
- embedding、全文、rerank 和引用结果。
- Connector secret 响应脱敏。
- MCP 请求校验思想。未来若实现多 Access Key，可参考 hash/prefix 模式；当前 MVP 是数据目录文件或环境变量托管的单一 Agent token。

必须剥离：

- workspace、tenant 选择和组织身份。
- role、group、member、resource grant。
- owner/steward、四级 sensitivity。
- draft/in-review/approved/published/retired 审批发布流。
- 每个 Agent 对每个资产单独授权。
- Python 与 Go 两套治理策略判断。

## 导入而非继承

如果未来需要迁移旧数据，使用一次性导入任务：

1. 只读打开导出文件或调用旧服务 API。
2. 对每条内容计算新产品自己的 SHA-256 指纹。
3. 写入 Pocket 的 `needs_review` 状态，不直接进入 ready。
4. 运行相同质量门并生成迁移报告。
5. 用户确认后进入 ready。
6. 保留旧产品数据不变，直到用户明确决定自行清理。

不得通过修改配置把 Pocket 的数据目录指向旧项目目录来“完成迁移”。

## 当前运行身份

- Pocket API 使用 `127.0.0.1:8718`；MCP 与 REST 共用此端口。
- `127.0.0.1:8720` 只为未来 Agent Gateway 预留，MVP 没有进程监听它。
- 默认数据根为 `~/.local/share/centaurai-pocket`，或 `$XDG_DATA_HOME/centaurai-pocket`。
- 移动应用包名为 `ai.centaur.pocket`，URL scheme 为 `centaur-pocket`，本地 key 均使用 `centaur-pocket.*` 命名空间。
- 任何未来适配器都必须由用户显式配置目标 URL/导出文件，不得根据邻近目录或端口自动发现另外两个产品。
