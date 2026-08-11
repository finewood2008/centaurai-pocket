"use strict";

const {
  app,
  BrowserWindow,
  dialog,
  ipcMain,
  Menu,
  protocol,
  session,
  shell,
} = require("electron");
const { spawn } = require("node:child_process");
const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const {
  WECHAT_WEB_URL,
  buildSidecarEnvironment,
  lanCorsOrigins,
  resolveLanAccess,
  contentTypeFor,
  desktopPortalOpenUriArgs,
  isPocketHealth,
  normalizeApiRequest,
  resolveContentPath,
} = require("./runtime.cjs");

const APP_ORIGIN = "centaur-pocket://app";
const API_ORIGIN = "http://127.0.0.1:8718";
const API_PREFIX = "/api/v1";
const CSP = [
  "default-src 'self'",
  "base-uri 'self'",
  "connect-src 'self'",
  "font-src 'self' data:",
  "form-action 'self'",
  "frame-ancestors 'none'",
  "img-src 'self' data: blob:",
  "object-src 'none'",
  "script-src 'self'",
  "style-src 'self' 'unsafe-inline'",
  "worker-src 'self' blob:",
].join("; ");

protocol.registerSchemesAsPrivileged([
  {
    scheme: "centaur-pocket",
    privileges: {
      standard: true,
      secure: true,
      supportFetchAPI: true,
      corsEnabled: true,
      stream: true,
      codeCache: true,
    },
  },
]);

app.setName("CentaurAI Pocket");

let apiChild = null;
let apiLogHandle = null;
let apiOwnerToken = "";
let mainWindow = null;
let quitting = false;

function openExternalWithDesktopPortal(url, timeoutMs = 10000) {
  return new Promise((resolve, reject) => {
    const command = "/usr/bin/gdbus";
    let stderr = "";
    let settled = false;
    let timer = null;
    let child;

    function finish(error) {
      if (settled) return;
      settled = true;
      if (timer) clearTimeout(timer);
      if (error) reject(error);
      else resolve();
    }

    try {
      child = spawn(command, desktopPortalOpenUriArgs(url), {
        stdio: ["ignore", "ignore", "pipe"],
        windowsHide: true,
      });
    } catch (error) {
      finish(error);
      return;
    }

    child.stderr.setEncoding("utf8");
    child.stderr.on("data", (chunk) => {
      stderr = `${stderr}${chunk}`.slice(-4096);
    });
    child.once("error", (error) => finish(error));
    child.once("close", (code, signal) => {
      if (code === 0) {
        finish();
        return;
      }
      const detail = stderr.trim() || `退出码 ${code ?? "未知"}${signal ? `，信号 ${signal}` : ""}`;
      finish(new Error(`系统桌面门户未能打开网页：${detail}`));
    });
    timer = setTimeout(() => {
      try {
        child.kill("SIGTERM");
      } catch {
        // The portal helper already exited.
      }
      finish(new Error("系统桌面门户打开网页超时"));
    }, timeoutMs);
  });
}

async function openWechatWeb() {
  if (process.platform === "linux") {
    try {
      await openExternalWithDesktopPortal(WECHAT_WEB_URL);
      return;
    } catch (portalError) {
      console.warn(
        "CentaurAI Pocket 无法通过桌面门户打开微信网页，尝试系统默认方式。",
        portalError,
      );
    }
  }
  await shell.openExternal(WECHAT_WEB_URL, { activate: true });
}

function dataRoot() {
  const configured = process.env.CENTAURAI_POCKET_DATA_DIR?.trim();
  return configured
    ? path.resolve(configured)
    : path.join(os.homedir(), ".local", "share", "centaurai-pocket");
}

const desktopProfileRoot = path.join(dataRoot(), "desktop-profile");
fs.mkdirSync(desktopProfileRoot, { recursive: true, mode: 0o700 });
app.setPath("userData", desktopProfileRoot);
const approvedSourcePathsFile = path.join(
  desktopProfileRoot,
  "approved-source-paths.json",
);

function normalizeSourcePath(sourcePath) {
  if (typeof sourcePath !== "string" || !path.isAbsolute(sourcePath)) {
    return "";
  }
  try {
    return fs.realpathSync.native(path.resolve(sourcePath));
  } catch {
    return "";
  }
}

function loadApprovedSourcePaths() {
  try {
    const parsed = JSON.parse(fs.readFileSync(approvedSourcePathsFile, "utf8"));
    if (!Array.isArray(parsed)) return new Set();
    return new Set(
      parsed
        .map((candidate) => normalizeSourcePath(candidate))
        .filter(Boolean),
    );
  } catch {
    return new Set();
  }
}

const approvedSourcePaths = loadApprovedSourcePaths();

function rememberApprovedSourcePath(sourcePath) {
  const normalized = normalizeSourcePath(sourcePath);
  if (!normalized) throw new Error("目录选择器返回了无效路径");
  approvedSourcePaths.add(normalized);
  const temporaryPath = `${approvedSourcePathsFile}.tmp-${process.pid}`;
  fs.writeFileSync(
    temporaryPath,
    `${JSON.stringify([...approvedSourcePaths].sort())}\n`,
    { encoding: "utf8", mode: 0o600 },
  );
  fs.renameSync(temporaryPath, approvedSourcePathsFile);
  try {
    fs.chmodSync(approvedSourcePathsFile, 0o600);
  } catch {
    // Some mounted filesystems do not support POSIX modes.
  }
  return normalized;
}

function assertSourcePathApproved(request) {
  if (request.method !== "POST" || request.path !== "/sources") return;
  const body = request.body;
  if (body?.kind === "wechat_visible_web") return;
  const sourcePath = normalizeSourcePath(body?.config?.path);
  if (
    body?.kind !== "folder" ||
    !sourcePath ||
    !approvedSourcePaths.has(sourcePath)
  ) {
    throw new Error("请先使用桌面目录选择器授权该文件夹");
  }
}

function repositoryRoot() {
  return path.resolve(__dirname, "..", "..");
}

function webRoot() {
  return app.isPackaged
    ? path.join(process.resourcesPath, "web")
    : path.join(repositoryRoot(), "apps", "mobile", "dist");
}

function desktopAssetsRoot() {
  return path.join(__dirname, "assets");
}

function iconPath() {
  return app.isPackaged
    ? path.join(process.resourcesPath, "assets", "icon.png")
    : path.join(repositoryRoot(), "apps", "mobile", "assets", "icon.png");
}

function backendCommand() {
  if (app.isPackaged) {
    return {
      executable: path.join(
        process.resourcesPath,
        "backend",
        "centaur-pocket-api",
      ),
      args: [],
      cwd: path.join(process.resourcesPath, "backend"),
    };
  }

  const apiRoot = path.join(repositoryRoot(), "services", "api");
  const preferredPython = path.join(apiRoot, ".venv", "bin", "python");
  if (fs.existsSync(preferredPython)) {
    return {
      executable: preferredPython,
      args: [path.join(apiRoot, "desktop_entry.py")],
      cwd: apiRoot,
    };
  }
  const preferredUv = path.join(os.homedir(), ".local", "bin", "uv");
  return {
    executable: fs.existsSync(preferredUv) ? preferredUv : "uv",
    args: [
      "run",
      "--project",
      apiRoot,
      "python",
      path.join(apiRoot, "desktop_entry.py"),
    ],
    cwd: apiRoot,
  };
}

function assertTrustedSender(event) {
  const senderUrl = event.senderFrame?.url ?? event.sender.getURL();
  if (!senderUrl.startsWith(`${APP_ORIGIN}/`)) {
    throw new Error("拒绝来自非本地页面的桌面请求");
  }
}

async function readJsonResponse(response) {
  const text = await response.text();
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

async function probeApi(timeoutMs = 1000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(`${API_ORIGIN}${API_PREFIX}/health`, {
      headers: { Accept: "application/json" },
      signal: controller.signal,
    });
    const payload = await readJsonResponse(response);
    if (!response.ok || !isPocketHealth(payload)) {
      return { state: "conflict", payload };
    }
    return { state: "ready", payload };
  } catch (error) {
    const code = error?.cause?.code;
    if (
      code === "ECONNREFUSED" ||
      code === "ECONNRESET" ||
      error?.name === "AbortError"
    ) {
      return { state: "offline", payload: null };
    }
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

async function validateOwnerToken(token) {
  const response = await fetch(`${API_ORIGIN}${API_PREFIX}/dashboard`, {
    headers: {
      Accept: "application/json",
      Authorization: `Bearer ${token}`,
      "X-Owner-Token": token,
    },
  });
  return response.ok;
}

async function waitForApi() {
  const deadline = Date.now() + 30000;
  while (Date.now() < deadline) {
    const probe = await probeApi(1200);
    if (probe.state === "ready") return probe.payload;
    if (probe.state === "conflict") {
      throw new Error(
        "8718 端口已被其他服务占用。请关闭冲突服务后重新启动。",
      );
    }
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
  throw new Error("本地数据服务在 30 秒内未能就绪");
}

function startApiChild(sessionOwnerToken) {
  const root = dataRoot();
  fs.mkdirSync(root, { recursive: true, mode: 0o700 });
  try {
    fs.chmodSync(root, 0o700);
  } catch {
    // Some mounted filesystems do not support POSIX modes.
  }

  const logPath = path.join(root, "desktop-api.log");
  apiLogHandle = fs.openSync(logPath, "a", 0o600);
  try {
    fs.chmodSync(logPath, 0o600);
  } catch {
    // Keep running on filesystems without chmod support.
  }

  const command = backendCommand();
  if (app.isPackaged && !fs.existsSync(command.executable)) {
    throw new Error(`桌面数据服务不存在：${command.executable}`);
  }

  const readyNonce = crypto.randomBytes(32).toString("base64url");
  // 局域网访问是显式开关：lan-access 文件不合规直接抛错终止启动（fail closed）
  const lanAccess = resolveLanAccess(root);
  const environment = buildSidecarEnvironment(process.env, {
    dataRoot: root,
    sessionOwnerToken,
    readyNonce,
    lan: lanAccess.enabled
      ? { enabled: true, corsOrigins: lanCorsOrigins(os.networkInterfaces()) }
      : undefined,
  });
  if (lanAccess.enabled) {
    console.log(
      "[pocket-desktop] 局域网访问已开启：API 绑定 0.0.0.0:8718，" +
        "CORS 已扩展本机私网 origin。",
    );
  }

  apiChild = spawn(command.executable, command.args, {
    cwd: command.cwd,
    detached: process.platform !== "win32",
    env: environment,
    shell: false,
    stdio: ["ignore", apiLogHandle, apiLogHandle, "pipe"],
    windowsHide: true,
  });
  const child = apiChild;
  const readyStream = child.stdio[3];
  return new Promise((resolve, reject) => {
    let buffer = "";
    let bound = false;
    const timeout = setTimeout(() => {
      if (!bound) reject(new Error("桌面数据服务没有返回私有就绪信号"));
    }, 10000);

    function failBeforeReady(error) {
      if (bound) return;
      clearTimeout(timeout);
      reject(error);
    }

    readyStream.setEncoding("utf8");
    readyStream.on("data", (chunk) => {
      if (bound) return;
      buffer += chunk;
      if (buffer.length > 4096) {
        failBeforeReady(new Error("桌面数据服务返回了异常的就绪信息"));
        return;
      }
      const newline = buffer.indexOf("\n");
      if (newline < 0) return;
      try {
        const descriptor = JSON.parse(buffer.slice(0, newline));
        if (
          descriptor?.nonce !== readyNonce ||
          descriptor?.pid !== child.pid ||
          descriptor?.port !== 8718
        ) {
          throw new Error("就绪描述符不匹配");
        }
        bound = true;
        clearTimeout(timeout);
        resolve();
      } catch {
        failBeforeReady(new Error("桌面数据服务就绪信息验证失败"));
      }
    });
    readyStream.once("error", (error) => {
      failBeforeReady(
        new Error(`读取桌面数据服务就绪信号失败：${error.message}`),
      );
    });
    child.once("error", (error) => {
      if (!bound) {
        failBeforeReady(new Error(`无法启动本地数据服务：${error.message}`));
      } else if (!quitting) {
        void showFatalError(`本地数据服务发生错误：${error.message}`);
      }
    });
    child.once("exit", (code, signal) => {
      const wasManaged = apiChild === child;
      if (wasManaged) apiChild = null;
      if (!bound) {
        failBeforeReady(
          new Error(
            `本地数据服务未能绑定端口（${signal ?? `退出码 ${code ?? "未知"}`}）`,
          ),
        );
      } else if (wasManaged && !quitting && mainWindow) {
        void showFatalError(
          `本地数据服务意外停止（${signal ?? `退出码 ${code ?? "未知"}`}）。`,
        );
      }
    });
  });
}

async function ensureApi() {
  const initial = await probeApi();
  if (initial.state === "ready") {
    throw new Error(
      "检测到 8718 上已有 CentaurAI Pocket API。为避免把桌面会话凭据交给非受管进程，桌面版不会接管已有服务；请先关闭该 API 后重试。",
    );
  }
  if (initial.state === "conflict") {
    throw new Error(
      "8718 端口已被非 CentaurAI Pocket 服务占用。请先关闭该服务。",
    );
  }
  apiOwnerToken = `cp_desktop_${crypto.randomBytes(32).toString("base64url")}`;
  await startApiChild(apiOwnerToken);
  const health = await waitForApi();
  if (health.version !== app.getVersion()) {
    throw new Error(
      `桌面数据服务版本不匹配（UI ${app.getVersion()} / API ${health.version}）`,
    );
  }
  if (!apiChild || apiChild.exitCode !== null) {
    throw new Error("桌面数据服务进程未能保持运行");
  }
  if (!(await validateOwnerToken(apiOwnerToken))) {
    throw new Error("桌面数据服务没有接受本次启动的会话凭据");
  }
}

async function proxyApiRequest(input) {
  if (!apiOwnerToken) throw new Error("本地数据服务尚未就绪");
  const request = normalizeApiRequest(input);
  assertSourcePathApproved(request);
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), request.timeoutMs);
  try {
    const response = await fetch(
      `${API_ORIGIN}${API_PREFIX}${request.path}`,
      {
        method: request.method,
        headers: {
          Accept: "application/json",
          ...(request.body === undefined
            ? {}
            : { "Content-Type": "application/json" }),
          Authorization: `Bearer ${apiOwnerToken}`,
          "X-Owner-Token": apiOwnerToken,
          ...(request.idempotencyKey
            ? { "Idempotency-Key": request.idempotencyKey }
            : {}),
        },
        body:
          request.body === undefined ? undefined : JSON.stringify(request.body),
        signal: controller.signal,
      },
    );
    return {
      ok: response.ok,
      status: response.status,
      payload: await readJsonResponse(response),
    };
  } catch (error) {
    if (error?.name === "AbortError") {
      return {
        ok: false,
        status: null,
        payload: { detail: "连接本地数据服务超时" },
      };
    }
    return {
      ok: false,
      status: null,
      payload: {
        detail:
          error instanceof Error
            ? `无法连接本地数据服务：${error.message}`
            : "无法连接本地数据服务",
      },
    };
  } finally {
    clearTimeout(timer);
  }
}

function stopApi() {
  if (!apiChild) return;
  const child = apiChild;
  apiChild = null;
  try {
    if (process.platform === "win32") {
      child.kill("SIGTERM");
    } else {
      process.kill(-child.pid, "SIGTERM");
    }
  } catch {
    try {
      child.kill("SIGTERM");
    } catch {
      // The child already exited.
    }
  }
}

async function showFatalError(message) {
  if (quitting) return;
  await dialog.showMessageBox(mainWindow ?? undefined, {
    type: "error",
    title: "CentaurAI Pocket 启动失败",
    message: "桌面应用未能启动",
    detail: `${message}\n\n日志：${path.join(dataRoot(), "desktop-api.log")}`,
    buttons: ["关闭"],
    defaultId: 0,
    noLink: true,
  });
  app.quit();
}

function registerContentProtocol() {
  protocol.handle("centaur-pocket", async (request) => {
    const filePath = resolveContentPath(
      request.url,
      webRoot(),
      desktopAssetsRoot(),
    );
    if (!filePath) {
      return new Response("Not found", {
        status: 404,
        headers: { "Content-Type": "text/plain; charset=utf-8" },
      });
    }

    const body = await fs.promises.readFile(filePath);
    const isHtml = path.extname(filePath).toLowerCase() === ".html";
    return new Response(body, {
      status: 200,
      headers: {
        "Content-Type": contentTypeFor(filePath),
        "Content-Security-Policy": CSP,
        "Cache-Control": isHtml
          ? "no-store"
          : "public, max-age=31536000, immutable",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
      },
    });
  });
}

function registerIpc() {
  ipcMain.handle("centaur-pocket:get-bootstrap-settings", (event) => {
    assertTrustedSender(event);
    return {
      managedByDesktop: true,
      serverUrl: API_ORIGIN,
    };
  });
  ipcMain.handle("centaur-pocket:api-request", (event, input) => {
    assertTrustedSender(event);
    return proxyApiRequest(input);
  });
  ipcMain.handle("centaur-pocket:select-folder", async (event) => {
    assertTrustedSender(event);
    const result = await dialog.showOpenDialog(mainWindow ?? undefined, {
      title: "选择允许 CentaurAI Pocket 读取的文件夹",
      buttonLabel: "授权此文件夹",
      properties: ["openDirectory", "dontAddToRecent"],
    });
    if (result.canceled || result.filePaths.length !== 1) return null;
    return rememberApprovedSourcePath(result.filePaths[0]);
  });
  ipcMain.handle("centaur-pocket:open-wechat-web", async (event) => {
    assertTrustedSender(event);
    await openWechatWeb();
    return true;
  });
}

function createApplicationMenu() {
  const template = [
    {
      label: "CentaurAI Pocket",
      submenu: [
        {
          label: "重新载入",
          accelerator: "CmdOrCtrl+R",
          click: () => mainWindow?.reload(),
        },
        { type: "separator" },
        { role: "quit", label: "退出" },
      ],
    },
    {
      label: "编辑",
      submenu: [
        { role: "undo", label: "撤销" },
        { role: "redo", label: "重做" },
        { type: "separator" },
        { role: "cut", label: "剪切" },
        { role: "copy", label: "复制" },
        { role: "paste", label: "粘贴" },
        { role: "selectAll", label: "全选" },
      ],
    },
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1280,
    height: 840,
    minWidth: 940,
    minHeight: 640,
    show: false,
    backgroundColor: "#070B14",
    title: "CentaurAI Pocket · 半人马随身数据中心",
    icon: iconPath(),
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      nodeIntegration: false,
      contextIsolation: true,
      sandbox: true,
      webSecurity: true,
      devTools: !app.isPackaged,
      webviewTag: false,
      spellcheck: false,
    },
  });

  mainWindow.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
  mainWindow.webContents.on("will-navigate", (event, targetUrl) => {
    if (!targetUrl.startsWith(`${APP_ORIGIN}/`)) event.preventDefault();
  });
  mainWindow.once("ready-to-show", () => mainWindow?.show());
  mainWindow.on("closed", () => {
    mainWindow = null;
  });
  void mainWindow.loadURL(`${APP_ORIGIN}/loading`);
}

const hasInstanceLock = app.requestSingleInstanceLock();
if (!hasInstanceLock) {
  app.quit();
} else {
  app.on("second-instance", () => {
    if (!mainWindow) return;
    if (mainWindow.isMinimized()) mainWindow.restore();
    mainWindow.show();
    mainWindow.focus();
  });

  app.whenReady().then(async () => {
    registerContentProtocol();
    registerIpc();
    createApplicationMenu();

    session.defaultSession.setPermissionCheckHandler(() => false);
    session.defaultSession.setPermissionRequestHandler(
      (_webContents, _permission, callback) => callback(false),
    );
    session.defaultSession.on("will-download", (event) =>
      event.preventDefault(),
    );

    createWindow();
    try {
      await ensureApi();
      await mainWindow?.loadURL(`${APP_ORIGIN}/`);
    } catch (error) {
      await showFatalError(
        error instanceof Error ? error.message : "未知启动错误",
      );
    }
  });
}

app.on("before-quit", () => {
  quitting = true;
  stopApi();
  if (apiLogHandle !== null) {
    try {
      fs.closeSync(apiLogHandle);
    } catch {
      // The descriptor may already be closed during process shutdown.
    }
    apiLogHandle = null;
  }
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});

app.on("activate", () => {
  if (BrowserWindow.getAllWindows().length === 0) createWindow();
});
