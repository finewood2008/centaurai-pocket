# CentaurAI Pocket Desktop

CentaurAI Pocket 的 Electron 桌面壳。双击桌面快捷方式后，它会：

1. 以单实例模式启动 Electron。
2. 检查 `127.0.0.1:8718` 是否是可信的 Pocket API。
3. 仅在端口空闲时拉起随包附带的 Python sidecar；不会接管已有监听者。
4. 每次启动生成只驻留于 Main/sidecar 的随机 Owner 会话凭据，不把会话 token
   交给 Renderer；sidecar 独立加载数据目录中的长期 Owner token，供明确配置的
   本机客户端使用，Main 和 Renderer 均不读取它。
5. 用受限 IPC 代理 UI 的 API 请求，并在退出时停止自己启动的 sidecar。

## 安全边界

- 本地 UI 使用 `centaur-pocket://app` 标准安全协议，不使用 `file://`。
- `nodeIntegration: false`、`contextIsolation: true`、`webSecurity: true`；
  BrowserWindow 仍请求 `sandbox: true`。
- preload 只公开启动配置和 Pocket API 两个固定能力；不公开文件系统、进程、
  shell、任意 URL 或原始 `ipcRenderer`。第三个固定能力只打开原生文件夹选择器。
- Main 只代理规范化后的相对 `/api/v1` 路径，强制请求
  `http://127.0.0.1:8718`，再按 method/path 白名单过滤，并在主进程注入本次
  启动的 Owner 会话 token。
- Sidecar 同时接受当前桌面会话 token 与 `owner-token` 文件中的稳定 Owner token。
  前者在桌面重启后立即失效；后者只供用户明确配置的本机或受保护 HTTPS 客户端，
  不进入 Renderer、LocalStorage、DevTools 或桌面 IPC 响应。
- 创建文件夹来源时，路径必须先由用户在 Main 原生目录选择器中明确批准；授权
  的文件系统真实路径以 `0600` 文件保存在桌面 profile，提交时再次解析并匹配，
  Renderer 不能静默指定任意宿主目录或通过重定向软链接绕过授权。
- 自定义协议页面检测到 preload/bootstrap 缺失或格式错误时会 fail closed：
  连接状态保持未配置，写入与离线入队全部拒绝，不会退回普通 Web token 模式。
  桌面初始化还会清理同一 Renderer profile 中旧版本可能遗留的 Web Owner token。
- 导航、新窗口、下载和网页权限默认拒绝，页面响应带 CSP。
- 便携启动器在当前 CentaurOS/Ubuntu 上因 AppArmor 限制而以
  `--no-sandbox` 运行 Chromium，因此 Electron 的进程级 sandbox 实际不生效。
  启动器强制要求 bubblewrap 提供外层文件系统约束：系统其余目录只读，只有
  Pocket 数据目录、会话目录和临时目录可写；它仍共享网络、会话 socket 与设备，
  不等价于 Chromium sandbox。该本地折中不需要 `sudo` 或系统密码。
- Linux sidecar 设置 parent-death signal；Electron 异常退出时，内核会同步终止
  API，避免遗留持有会话 token 的孤儿进程。

## 构建

从仓库根目录运行：

```bash
./scripts/build-desktop.sh
```

构建脚本依次导出 Expo Web、用 PyInstaller `--onedir` 生成独立 API sidecar，
再生成 Electron `linux-unpacked` 应用：

```text
apps/desktop/release/linux-unpacked/centaurai-pocket
```

安装到稳定的当前用户目录，并创建应用菜单和桌面快捷方式：

```bash
./scripts/install-desktop-shortcut.sh
```

应用本体复制到 `~/.local/opt/centaurai-pocket/releases/`，快捷方式通过
`~/.local/opt/centaurai-pocket/current` 指向本次安装。因此移动或清理源码仓库
不会让已经安装的入口失效；旧版本目录暂时保留，便于手工回退。

启动：

```bash
gtk-launch ai.centaur.pocket.desktop
```

日志位于：

```text
~/.local/share/centaurai-pocket/desktop-api.log
```

Pocket 数据不会放进 Electron 安装目录，升级或删除桌面壳不会自动删除数据库。

外部承办人任务执行入口需要由启动 Electron 的父进程显式提供公开 HTTPS origin；
桌面壳会将其原样交给 sidecar，不会猜测、补全或硬编码地址：

```bash
CENTAURAI_POCKET_TASK_EXECUTION_PUBLIC_ORIGIN=https://pocket.example.com \
  ./apps/desktop/launch-portable.sh
```

通过 `scripts/install-desktop-shortcut.sh` 安装桌面入口时若显式提供该变量，安装器会把
规范化后的 origin 以 `0600` 保存为 Pocket 数据根中的
`task-execution-public-origin`，确保从桌面图标启动时仍可用；显式父进程环境优先于该
文件。未设置变量且没有持久配置文件时，桌面壳不会向 sidecar 注入公开 origin。无论
是否设置，长期
`CENTAURAI_POCKET_OWNER_TOKEN` 都会从 sidecar 子进程环境中删除。

## 开发

先确保 API 依赖、移动端依赖和 Web 导出存在：

```bash
cd services/api && uv sync --group dev
cd ../../apps/mobile && npm install && npm run export:web
cd ../desktop && npm install && npm start
```

开发模式会用 `uv run` 启动源码 API；打包模式只使用随包附带的 sidecar，不依赖
系统 Python、Docker 或项目虚拟环境。
