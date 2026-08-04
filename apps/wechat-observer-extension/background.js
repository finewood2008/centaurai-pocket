"use strict";

const NATIVE_HOST = "ai.centaur.pocket.wechat_observer";
const EXTENSION_ID = "centaur-pocket-wechat-observer@centaur.ai";
const WECHAT_ORIGIN = "https://wx.qq.com";
const PARSER_VERSION = "visible-dom-v1";
const REQUEST_TIMEOUT_MS = 10_000;

let nativePort = null;
let requestCounter = 0;
const pending = new Map();

function rejectPending(reason) {
  for (const { reject, timer } of pending.values()) {
    clearTimeout(timer);
    reject(new Error(reason));
  }
  pending.clear();
}

function connectNativeHost() {
  if (nativePort) return nativePort;
  const port = browser.runtime.connectNative(NATIVE_HOST);
  nativePort = port;
  port.onMessage.addListener((response) => {
    const requestId = response && response.request_id;
    if (typeof requestId !== "string" || !pending.has(requestId)) return;
    const waiter = pending.get(requestId);
    pending.delete(requestId);
    clearTimeout(waiter.timer);
    if (response.ok) waiter.resolve(response);
    else waiter.reject(new Error(response.error || "本机观察器拒绝了请求"));
  });
  port.onDisconnect.addListener(() => {
    const detail = browser.runtime.lastError && browser.runtime.lastError.message;
    if (nativePort === port) nativePort = null;
    rejectPending(detail || "本机观察器连接已断开");
  });
  return port;
}

function isTrustedSender(sender) {
  if (!sender || sender.id !== EXTENSION_ID || !sender.url || !sender.tab) return false;
  try {
    return new URL(sender.url).origin === WECHAT_ORIGIN;
  } catch (_error) {
    return false;
  }
}

function isTrustedExtensionPage(sender) {
  return Boolean(
    sender &&
      sender.id === EXTENSION_ID &&
      !sender.tab &&
      typeof sender.url === "string" &&
      sender.url.startsWith(browser.runtime.getURL("")),
  );
}

async function extensionMetadata() {
  const manifest = browser.runtime.getManifest();
  let browserInfo = { name: "Firefox" };
  if (typeof browser.runtime.getBrowserInfo === "function") {
    try {
      browserInfo = await browser.runtime.getBrowserInfo();
    } catch (_error) {
      // Firefox exposes this API without extra permissions; keep a conservative
      // fallback for compatible Gecko builds that do not.
    }
  }
  const metadata = {
    extension_id: EXTENSION_ID,
    extension_version: manifest.version,
    browser_name: "firefox",
  };
  const browserVersion = String(browserInfo.version || "").slice(0, 64);
  if (browserVersion) metadata.browser_version = browserVersion;
  return metadata;
}

async function sendToNative(type, body) {
  const requestId = `${Date.now().toString(36)}-${(++requestCounter).toString(36)}`;
  const port = connectNativeHost();
  const responsePromise = new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      pending.delete(requestId);
      reject(new Error("本机观察器响应超时"));
    }, REQUEST_TIMEOUT_MS);
    pending.set(requestId, { resolve, reject, timer });
  });
  try {
    port.postMessage({ type, request_id: requestId, body });
  } catch (error) {
    const waiter = pending.get(requestId);
    if (waiter) clearTimeout(waiter.timer);
    pending.delete(requestId);
    if (nativePort === port) nativePort = null;
    throw error;
  }
  return responsePromise;
}

browser.runtime.onMessage.addListener((message, sender) => {
  const fromWechat = isTrustedSender(sender);
  const fromExtensionPage = isTrustedExtensionPage(sender);
  if (!fromWechat && !fromExtensionPage) {
    return Promise.reject(new Error("拒绝非微信页面消息"));
  }
  if (!message || typeof message !== "object" || typeof message.type !== "string") {
    return Promise.reject(new Error("无效的观察器消息"));
  }

  if (message.type === "observer.configure" && fromExtensionPage) {
    return extensionMetadata()
      .then((metadata) =>
        sendToNative("configure", {
          extension_id: EXTENSION_ID,
          api_base: message.api_base,
          source_id: message.source_id,
          pairing_code: message.pairing_code,
        }).then(() => metadata),
      )
      .then((metadata) =>
        sendToNative("handshake", {
          ...metadata,
          parser_version: PARSER_VERSION,
        }),
      );
  }
  if (message.type === "observer.handshake") {
    if (!fromWechat) return Promise.reject(new Error("握手只能由微信页面发起"));
    return extensionMetadata().then((metadata) =>
      sendToNative("handshake", {
        ...metadata,
        parser_version: PARSER_VERSION,
      }),
    );
  }
  if (message.type === "observer.heartbeat") {
    if (!fromWechat) return Promise.reject(new Error("心跳只能由微信页面发起"));
    return extensionMetadata().then((metadata) => {
      const body = { ...message.body };
      if (metadata.browser_version) body.browser_version = metadata.browser_version;
      body.extension_version = metadata.extension_version;
      body.parser_version = PARSER_VERSION;
      return sendToNative("heartbeat", body);
    });
  }
  if (message.type === "observer.events") {
    if (!fromWechat) return Promise.reject(new Error("消息只能由微信页面发起"));
    return sendToNative("events", message.body);
  }
  return Promise.reject(new Error("不支持的观察器消息类型"));
});
