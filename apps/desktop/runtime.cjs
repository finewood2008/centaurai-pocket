"use strict";

const fs = require("node:fs");
const path = require("node:path");

const API_METHODS = new Set(["GET", "POST", "PATCH", "PUT", "DELETE"]);
const API_PATH_PATTERN = /^\/[A-Za-z0-9._~!$&'()*+,;=:@%/?-]*$/;
const API_ROUTE_RULES = [
  ["GET", /^\/health$/],
  ["GET", /^\/dashboard$/],
  ["GET", /^\/governance\/tasks(?:\?[A-Za-z0-9._~!$&'()*+,;=:@%/?-]*)?$/],
  ["POST", /^\/governance\/tasks\/[^/?]+\/(?:apply|skip|undo)$/],
  ["GET", /^\/sources$/],
  ["POST", /^\/sources$/],
  ["POST", /^\/sources\/[^/?]+\/sync$/],
  ["POST", /^\/captures$/],
];
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

module.exports = {
  contentTypeFor,
  isPocketHealth,
  normalizeApiRequest,
  resolveContentPath,
};
