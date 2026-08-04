# CentaurAI 微信网页观察器 Native Host

这是 Firefox 扩展与本机 Pocket API 之间的最小权限桥接进程。它使用 Firefox
Native Messaging 的长度前缀 JSON 协议，并只向 `http://127.0.0.1:<port>` 发送
请求。

## 安装

从仓库根目录运行：

```bash
./tools/native-host/install-native-host.sh
```

脚本只写入当前用户目录，不需要 `sudo`，也不会要求系统密码：

- Host：`~/.local/share/centaurai-pocket/wechat-observer/native_host.py`
- Firefox manifest：`~/.mozilla/native-messaging-hosts/ai.centaur.pocket.wechat_observer.json`
- 首次配对后凭据：`~/.config/centaurai-pocket/wechat-observer.json`（权限 `0600`）

推荐在扩展弹窗输入来源 ID 和一次性配对码。也可使用权限为 `0600` 的文件进行
无界面配置：

```bash
./tools/native-host/install-native-host.sh \
  --source-id src_xxx \
  --pairing-code-file /安全路径/pairing-code
```

配对码只用于一次握手；API 返回的独立 `collector_token` 会原子写入配置并替换
配对码。Host 不接受 Owner token，也不会向扩展返回 collector token。

## 安全约束

- manifest 的 `allowed_extensions` 固定为
  `centaur-pocket-wechat-observer@centaur.ai`。
- Host 只接受已知字段、最多 50 条事件的批次、16,000 字符正文和 256 KiB
  Native Messaging 帧，并限制每分钟请求数和事件数。
- API 地址必须是明确的 IPv4 loopback `127.0.0.1`，拒绝重定向、域名、认证信息、
  查询参数和非 HTTP 地址。
- 配置必须属于当前用户、是普通文件且不能向 group/other 开放权限。
- 标准输出只用于 Native Messaging 帧；错误响应不包含 API 返回正文或秘密。

Flatpak/Snap 版 Firefox 可能无法启动普通宿主机 Native Host。首版支持发行版原生
Firefox；沙箱发行版需要单独评估其宿主通信机制，不能通过放宽文件系统权限规避。

## 卸载

默认保留凭据，便于重装：

```bash
./tools/native-host/uninstall-native-host.sh
```

显式删除凭据：

```bash
./tools/native-host/uninstall-native-host.sh --purge-config
```

## 测试

```bash
python3 -m unittest discover -s tools/native-host/tests -v
```
