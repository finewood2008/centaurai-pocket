"use strict";

const fs = require("node:fs");
const path = require("node:path");

const API_METHODS = new Set(["GET", "POST", "PATCH", "PUT", "DELETE"]);
const API_PATH_PATTERN = /^\/[A-Za-z0-9._~!$&'()*+,;=:@%/?-]*$/;
const API_ROUTE_RULES = [
  ["GET", /^\/health$/],
  ["GET", /^\/dashboard$/],
  ["POST", /^\/mobile\/pairings$/],
  ["GET", /^\/mobile\/devices$/],
  ["DELETE", /^\/mobile\/devices\/[^/?]+$/],
  ["GET", /^\/governance\/tasks(?:\?[A-Za-z0-9._~!$&'()*+,;=:@%/?-]*)?$/],
  ["POST", /^\/governance\/tasks\/[^/?]+\/(?:apply|skip|undo)$/],
  ["GET", /^\/sources$/],
  ["POST", /^\/sources$/],
  ["POST", /^\/sources\/[^/?]+\/sync$/],
  ["GET", /^\/sources\/[^/?]+\/observer-status$/],
  ["GET", /^\/sources\/[^/?]+\/coverage-gaps(?:\?limit=\d+)?$/],
  ["POST", /^\/sources\/[^/?]+\/pairings$/],
  ["DELETE", /^\/sources\/[^/?]+\/pairings\/[^/?]+$/],
  ["POST", /^\/sources\/[^/?]+\/(?:pause|resume)$/],
  ["POST", /^\/captures$/],
];
const WECHAT_WEB_URL = "https://wx.qq.com/";
const DESKTOP_PORTAL_DESTINATION = "org.freedesktop.portal.Desktop";
const DESKTOP_PORTAL_OBJECT_PATH = "/org/freedesktop/portal/desktop";
const DESKTOP_PORTAL_OPEN_URI_METHOD =
  "org.freedesktop.portal.OpenURI.OpenURI";
const TASK_EXECUTION_PUBLIC_ORIGIN_ENV =
  "CENTAURAI_POCKET_TASK_EXECUTION_PUBLIC_ORIGIN";
const CONTENT_TYPES = new Map([
  [".css", "text/css; charset=utf-8"],
  [".html", "text/html; charset=utf-8"],
  [".ico", "image/x-icon"],
  [".jpeg", "image/jpeg"],
  [".jpg", "image/jpeg"],
  [".js", "text/javascript; charset=utf-8"],
  [".json", "application/json; charset=utf-8"],
  [".map", "application/json; charset=utf-8"],
  [".png", "image/png"],
  [".svg", "image/svg+xml; charset=utf-8"],
  [".ttf", "font/ttf"],
  [".webp", "image/webp"],
  [".woff", "font/woff"],
  [".woff2", "font/woff2"],
]);

/**
 * 局域网访问（显式开启）。
 *
 * 默认受管 API 只绑 127.0.0.1。数据目录下存在 `lan-access` 文件
 * （当前用户所有、0600 普通文件、首个有效行是字面 `enabled`）时，
 * 视为主人显式开启局域网访问：API 改绑 0.0.0.0，并把本机私网网卡
 * 的秘书/开发端口 origin 追加进 CORS 白名单。文件不合规直接失败，
 * 绝不静默降级成"部分开启"。
 */
const LAN_ACCESS_FILE = "lan-access";
const LAN_CLIENT_PORTS = [17818, 8081, 19006];
const DEFAULT_LOCAL_CORS_ORIGINS = [
  "http://localhost:8081",
  "http://127.0.0.1:8081",
  "http://localhost:19006",
  "http://127.0.0.1:19006",
  "http://127.0.0.1:17818",
];

function isPrivateIpv4(address) {
  const parts = address.split(".").map(Number);
  if (parts.length !== 4 || parts.some((part) => !Number.isInteger(part))) {
    return false;
  }
  if (parts[0] === 10) return true;
  if (parts[0] === 172 && parts[1] >= 16 && parts[1] <= 31) return true;
  if (parts[0] === 192 && parts[1] === 168) return true;
  return false;
}

function resolveLanAccess(
  dataRoot,
  {
    lstatSync = fs.lstatSync,
    readFileSync = fs.readFileSync,
    getuid = process.getuid,
  } = {},
) {
  const filePath = path.join(dataRoot, LAN_ACCESS_FILE);
  let stat;
  try {
    stat = lstatSync(filePath);
  } catch {
    return { enabled: false };
  }
  const uid = typeof getuid === "function" ? getuid() : undefined;
  if (
    !stat.isFile() ||
    stat.isSymbolicLink() ||
    (stat.mode & 0o777) !== 0o600 ||
    (uid !== undefined && stat.uid !== uid)
  ) {
    throw new Error(
      "局域网访问配置 lan-access 必须是当前用户所有的 0600 普通文件。",
    );
  }
  const lines = readFileSync(filePath, "utf8")
    .split("\n")
    .map((line) => line.trim())
    .filter((line) => line && !line.startsWith("#"));
  if (lines.length !== 1 || lines[0] !== "enabled") {
    throw new Error(
      "局域网访问配置 lan-access 只接受单行字面值 enabled（可含注释行）。",
    );
  }
  return { enabled: true };
}

function lanCorsOrigins(interfaces) {
  const origins = [];
  for (const entries of Object.values(interfaces ?? {})) {
    for (const entry of entries ?? []) {
      if (entry.family !== "IPv4" || entry.internal) continue;
      if (!isPrivateIpv4(entry.address)) continue;
      for (const port of LAN_CLIENT_PORTS) {
        origins.push(`http://${entry.address}:${port}`);
      }
    }
  }
  return origins;
}

function buildSidecarEnvironment(
  baseEnvironment,
  { dataRoot, sessionOwnerToken, readyNonce, lan },
) {
  const environment = { ...baseEnvironment };
  const taskExecutionPublicOrigin =
    baseEnvironment[TASK_EXECUTION_PUBLIC_ORIGIN_ENV];
  delete environment.PYTHONHOME;
  delete environment.PYTHONPATH;
  delete environment.CENTAURAI_POCKET_OWNER_TOKEN;
  delete environment[TASK_EXECUTION_PUBLIC_ORIGIN_ENV];
  if (taskExecutionPublicOrigin !== undefined) {
    environment[TASK_EXECUTION_PUBLIC_ORIGIN_ENV] =
      taskExecutionPublicOrigin;
  }
  environment.CENTAURAI_POCKET_DATA_DIR = dataRoot;
  environment.CENTAURAI_POCKET_HOST = "127.0.0.1";
  environment.CENTAURAI_POCKET_PORT = "8718";
  if (lan?.enabled) {
    environment.CENTAURAI_POCKET_HOST = "0.0.0.0";
    environment.CENTAURAI_POCKET_CORS_ORIGINS = [
      ...DEFAULT_LOCAL_CORS_ORIGINS,
      ...(lan.corsOrigins ?? []),
    ].join(",");
  }
  environment.CENTAURAI_POCKET_DESKTOP_SESSION_TOKEN = sessionOwnerToken;
  environment.CENTAURAI_POCKET_DESKTOP_NONCE = readyNonce;
  environment.CENTAURAI_POCKET_DESKTOP_READY_FD = "3";
  return environment;
}

function isInside(root, candidate) {
  const relative = path.relative(root, candidate);
  return (
    relative === "" ||
    (!relative.startsWith("..") && !path.isAbsolute(relative))
  );
}

function firstExistingFile(root, candidates) {
  for (const relative of candidates) {
    const candidate = path.resolve(root, relative);
    if (!isInside(root, candidate)) continue;
    try {
      if (fs.statSync(candidate).isFile()) return candidate;
    } catch {
      // Try the next static-route form.
    }
  }
  return null;
}

function resolveContentPath(requestUrl, webRoot, desktopAssetsRoot) {
  const parsed = new URL(requestUrl);
  if (parsed.protocol !== "centaur-pocket:" || parsed.hostname !== "app") {
    return null;
  }

  let pathname;
  try {
    pathname = decodeURIComponent(parsed.pathname);
  } catch {
    return null;
  }
  if (pathname.includes("\0") || pathname.includes("\\")) return null;

  if (pathname === "/loading" || pathname === "/loading.html") {
    return firstExistingFile(desktopAssetsRoot, ["loading.html"]);
  }

  const relative = pathname.replace(/^\/+/, "");
  if (!relative) return firstExistingFile(webRoot, ["index.html"]);
  if (path.extname(relative)) return firstExistingFile(webRoot, [relative]);
  return firstExistingFile(webRoot, [
    `${relative}.html`,
    path.join(relative, "index.html"),
  ]);
}

function contentTypeFor(filePath) {
  return (
    CONTENT_TYPES.get(path.extname(filePath).toLowerCase()) ??
    "application/octet-stream"
  );
}

function normalizeApiRequest(input) {
  if (!input || typeof input !== "object" || Array.isArray(input)) {
    throw new TypeError("桌面 API 请求格式无效");
  }

  const method =
    typeof input.method === "string" ? input.method.toUpperCase() : "GET";
  if (!API_METHODS.has(method)) throw new TypeError("桌面 API 方法不受支持");

  const apiPath = typeof input.path === "string" ? input.path.trim() : "";
  if (
    !apiPath ||
    apiPath.length > 2048 ||
    !API_PATH_PATTERN.test(apiPath) ||
    apiPath.includes("..") ||
    apiPath.startsWith("//")
  ) {
    throw new TypeError("桌面 API 路径无效");
  }
  if (
    !API_ROUTE_RULES.some(
      ([allowedMethod, routePattern]) =>
        allowedMethod === method && routePattern.test(apiPath),
    )
  ) {
    throw new TypeError("桌面页面无权调用此 API");
  }

  const timeoutCandidate = Number(input.timeoutMs ?? 9000);
  const timeoutMs = Number.isFinite(timeoutCandidate)
    ? Math.min(Math.max(Math.trunc(timeoutCandidate), 1000), 120000)
    : 9000;

  const idempotencyKey =
    typeof input.idempotencyKey === "string"
      ? input.idempotencyKey.trim().slice(0, 256)
      : "";
  const body = input.body;
  if (body !== undefined) {
    const serialized = JSON.stringify(body);
    if (serialized.length > 20 * 1024 * 1024) {
      throw new TypeError("桌面 API 请求内容过大");
    }
  }

  return {
    method,
    path: apiPath,
    timeoutMs,
    idempotencyKey,
    body,
  };
}

function isPocketHealth(payload) {
  return (
    payload !== null &&
    typeof payload === "object" &&
    !Array.isArray(payload) &&
    payload.status === "ok" &&
    payload.service === "centaurai-pocket" &&
    typeof payload.version === "string"
  );
}

function desktopPortalOpenUriArgs(url) {
  if (url !== WECHAT_WEB_URL) {
    throw new TypeError("桌面端只允许打开固定的微信网页地址");
  }
  return [
    "call",
    "--session",
    "--dest",
    DESKTOP_PORTAL_DESTINATION,
    "--object-path",
    DESKTOP_PORTAL_OBJECT_PATH,
    "--method",
    DESKTOP_PORTAL_OPEN_URI_METHOD,
    "",
    url,
    "{}",
  ];
}

module.exports = {
  WECHAT_WEB_URL,
  buildSidecarEnvironment,
  contentTypeFor,
  desktopPortalOpenUriArgs,
  isPocketHealth,
  lanCorsOrigins,
  normalizeApiRequest,
  resolveContentPath,
  resolveLanAccess,
};
