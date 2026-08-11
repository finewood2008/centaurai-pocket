"use strict";

const assert = require("node:assert/strict");
const childProcess = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const {
  WECHAT_WEB_URL,
  buildSidecarEnvironment,
  contentTypeFor,
  desktopPortalOpenUriArgs,
  isPocketHealth,
  normalizeApiRequest,
  resolveContentPath,
} = require("../runtime.cjs");

test("static routes stay inside the packaged roots", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "pocket-desktop-"));
  const web = path.join(root, "web");
  const assets = path.join(root, "assets");
  fs.mkdirSync(path.join(web, "_expo"), { recursive: true });
  fs.mkdirSync(assets);
  fs.writeFileSync(path.join(web, "index.html"), "home");
  fs.writeFileSync(path.join(web, "inbox.html"), "inbox");
  fs.writeFileSync(path.join(web, "_expo", "entry.js"), "js");
  fs.writeFileSync(path.join(assets, "loading.html"), "loading");

  assert.equal(
    resolveContentPath("centaur-pocket://app/", web, assets),
    path.join(web, "index.html"),
  );
  assert.equal(
    resolveContentPath("centaur-pocket://app/inbox", web, assets),
    path.join(web, "inbox.html"),
  );
  assert.equal(
    resolveContentPath("centaur-pocket://app/_expo/entry.js", web, assets),
    path.join(web, "_expo", "entry.js"),
  );
  assert.equal(
    resolveContentPath("centaur-pocket://app/loading", web, assets),
    path.join(assets, "loading.html"),
  );
  assert.equal(
    resolveContentPath("centaur-pocket://app/%2e%2e/secret", web, assets),
    null,
  );
  assert.equal(resolveContentPath("https://example.com/", web, assets), null);
});

test("API proxy accepts only bounded relative requests", () => {
  assert.deepEqual(
    normalizeApiRequest({
      path: "/governance/tasks?status=pending&limit=50",
      method: "get",
      timeoutMs: 500000,
    }),
    {
      body: undefined,
      idempotencyKey: "",
      method: "GET",
      path: "/governance/tasks?status=pending&limit=50",
      timeoutMs: 120000,
    },
  );
  assert.throws(() => normalizeApiRequest({ path: "https://example.com" }));
  assert.throws(() => normalizeApiRequest({ path: "/../owner-token" }));
  assert.throws(() =>
    normalizeApiRequest({ path: "/health", method: "CONNECT" }),
  );
  assert.throws(() =>
    normalizeApiRequest({ path: "/agent/token/rotate", method: "POST" }),
  );

  for (const request of [
    { path: "/mobile/pairings", method: "POST" },
    { path: "/mobile/devices", method: "GET" },
    { path: "/mobile/devices/device-1", method: "DELETE" },
    { path: "/sources/source-1/observer-status", method: "GET" },
    { path: "/sources/source-1/coverage-gaps?limit=20", method: "GET" },
    { path: "/sources/source-1/pairings", method: "POST" },
    {
      path: "/sources/source-1/pairings/pairing-1",
      method: "DELETE",
    },
    { path: "/sources/source-1/pause", method: "POST" },
    { path: "/sources/source-1/resume", method: "POST" },
  ]) {
    assert.equal(normalizeApiRequest(request).path, request.path);
  }
  assert.throws(() =>
    normalizeApiRequest({
      path: "/sources/source-1/coverage-gaps?limit=all",
      method: "GET",
    }),
  );
});

test("WeChat launcher is fixed to the official HTTPS URL", () => {
  assert.equal(WECHAT_WEB_URL, "https://wx.qq.com/");
  assert.deepEqual(desktopPortalOpenUriArgs(WECHAT_WEB_URL), [
    "call",
    "--session",
    "--dest",
    "org.freedesktop.portal.Desktop",
    "--object-path",
    "/org/freedesktop/portal/desktop",
    "--method",
    "org.freedesktop.portal.OpenURI.OpenURI",
    "",
    WECHAT_WEB_URL,
    "{}",
  ]);
  assert.throws(
    () => desktopPortalOpenUriArgs("https://example.com/"),
    /固定的微信网页地址/,
  );
});

test("health identity is validated", () => {
  assert.equal(
    isPocketHealth({
      status: "ok",
      service: "centaurai-pocket",
      version: "0.3.0",
    }),
    true,
  );
  assert.equal(isPocketHealth({ status: "ok", service: "other" }), false);
  assert.equal(contentTypeFor("entry.js"), "text/javascript; charset=utf-8");
});

test("desktop sidecar keeps credentials separate and preserves configured public origin", () => {
  const publicOrigin = "https://tasks.private.example:8443";
  const environment = buildSidecarEnvironment(
    {
      CENTAURAI_POCKET_OWNER_TOKEN: "must-not-leak",
      CENTAURAI_POCKET_TASK_EXECUTION_PUBLIC_ORIGIN: publicOrigin,
      PYTHONHOME: "/unsafe/python",
      UNRELATED_VALUE: "preserved",
    },
    {
      dataRoot: "/private/pocket",
      sessionOwnerToken: "cp_desktop_session-test",
      readyNonce: "ready-test",
    },
  );

  assert.equal(environment.CENTAURAI_POCKET_OWNER_TOKEN, undefined);
  assert.equal(environment.PYTHONHOME, undefined);
  assert.equal(
    environment.CENTAURAI_POCKET_DESKTOP_SESSION_TOKEN,
    "cp_desktop_session-test",
  );
  assert.equal(environment.CENTAURAI_POCKET_DATA_DIR, "/private/pocket");
  assert.equal(environment.CENTAURAI_POCKET_DESKTOP_NONCE, "ready-test");
  assert.equal(
    environment.CENTAURAI_POCKET_TASK_EXECUTION_PUBLIC_ORIGIN,
    publicOrigin,
  );
  assert.equal(environment.UNRELATED_VALUE, "preserved");
});

test("desktop sidecar does not invent an external task execution origin", () => {
  const environment = buildSidecarEnvironment(
    { UNRELATED_VALUE: "preserved" },
    {
      dataRoot: "/private/pocket",
      sessionOwnerToken: "cp_desktop_session-test",
      readyNonce: "ready-test",
    },
  );

  assert.equal(
    Object.hasOwn(
      environment,
      "CENTAURAI_POCKET_TASK_EXECUTION_PUBLIC_ORIGIN",
    ),
    false,
  );
});

test("portable launcher forwards only an explicitly configured public origin", (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "pocket-launcher-"));
  t.after(() => fs.rmSync(root, { force: true, recursive: true }));

  const desktopRoot = path.join(root, "desktop");
  const application = path.join(
    desktopRoot,
    "release",
    "linux-unpacked",
    "centaurai-pocket",
  );
  const fakeBin = path.join(root, "bin");
  const fakeBubblewrap = path.join(fakeBin, "bwrap");
  fs.mkdirSync(path.dirname(application), { recursive: true });
  fs.mkdirSync(fakeBin);
  fs.copyFileSync(
    path.join(__dirname, "..", "launch-portable.sh"),
    path.join(desktopRoot, "launch-portable.sh"),
  );
  fs.writeFileSync(application, "#!/bin/sh\nexit 0\n", { mode: 0o755 });
  fs.writeFileSync(fakeBubblewrap, '#!/bin/sh\nprintf "%s\\0" "$@"\n', {
    mode: 0o755,
  });

  const originName = "CENTAURAI_POCKET_TASK_EXECUTION_PUBLIC_ORIGIN";
  const baseEnvironment = {
    ...process.env,
    CENTAURAI_POCKET_DATA_DIR: path.join(root, "data"),
    CENTAURAI_POCKET_DESKTOP_FOREGROUND: "true",
    HOME: path.join(root, "home"),
    PATH: `${fakeBin}:${process.env.PATH ?? "/usr/bin:/bin"}`,
  };
  delete baseEnvironment[originName];

  const runLauncher = (environment) => {
    const result = childProcess.spawnSync(
      "bash",
      [path.join(desktopRoot, "launch-portable.sh")],
      { encoding: "utf8", env: environment },
    );
    assert.equal(result.status, 0, result.stderr);
    return result.stdout.split("\0").filter(Boolean);
  };

  const withoutOrigin = runLauncher(baseEnvironment);
  assert.equal(withoutOrigin.includes(originName), false);

  const persistedOrigin = "https://tasks.persisted.example:8443";
  const persistedOriginFile = path.join(
    baseEnvironment.CENTAURAI_POCKET_DATA_DIR,
    "task-execution-public-origin",
  );
  fs.writeFileSync(persistedOriginFile, `${persistedOrigin}\n`, { mode: 0o600 });
  const fromPersistedFile = runLauncher(baseEnvironment);
  const persistedArgument = fromPersistedFile.indexOf(originName);
  assert.notEqual(persistedArgument, -1);
  assert.deepEqual(
    fromPersistedFile.slice(persistedArgument - 1, persistedArgument + 2),
    ["--setenv", originName, persistedOrigin],
  );

  const publicOrigin = "https://tasks.private.example:8443";
  const withOrigin = runLauncher({
    ...baseEnvironment,
    [originName]: publicOrigin,
  });
  const originArgument = withOrigin.indexOf(originName);
  assert.notEqual(originArgument, -1);
  assert.deepEqual(withOrigin.slice(originArgument - 1, originArgument + 2), [
    "--setenv",
    originName,
    publicOrigin,
  ]);
});

test("resolveLanAccess: 缺文件即关闭，合规文件开启，越权文件直接失败", () => {
  const { resolveLanAccess } = require("../runtime.cjs");
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "pocket-lan-"));
  try {
    assert.deepEqual(resolveLanAccess(root), { enabled: false });

    const file = path.join(root, "lan-access");
    fs.writeFileSync(file, "# 局域网访问\nenabled\n", { mode: 0o600 });
    assert.deepEqual(resolveLanAccess(root), { enabled: true });

    // 内容不是字面 enabled：失败而不是当作关闭
    fs.writeFileSync(file, "enabled=maybe\n", { mode: 0o600 });
    assert.throws(() => resolveLanAccess(root), /单行字面值 enabled/);

    // 权限过宽：失败
    fs.writeFileSync(file, "enabled\n", { mode: 0o600 });
    fs.chmodSync(file, 0o644);
    assert.throws(() => resolveLanAccess(root), /0600/);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("lanCorsOrigins: 只收私网 IPv4，按客户端端口展开", () => {
  const { lanCorsOrigins } = require("../runtime.cjs");
  const origins = lanCorsOrigins({
    lo: [{ family: "IPv4", address: "127.0.0.1", internal: true }],
    eth0: [
      { family: "IPv4", address: "192.168.1.20", internal: false },
      { family: "IPv6", address: "fe80::1", internal: false },
    ],
    wan: [{ family: "IPv4", address: "203.0.113.9", internal: false }],
  });
  assert.deepEqual(origins, [
    "http://192.168.1.20:17818",
    "http://192.168.1.20:8081",
    "http://192.168.1.20:19006",
  ]);
});

test("buildSidecarEnvironment: LAN 开启时绑 0.0.0.0 并合并 CORS，默认仍是回环", () => {
  const { buildSidecarEnvironment } = require("../runtime.cjs");
  const base = { PATH: "/usr/bin" };
  const closedEnvironment = buildSidecarEnvironment(base, {
    dataRoot: "/tmp/x",
    sessionOwnerToken: "cp_owner_test",
    readyNonce: "nonce",
  });
  assert.equal(closedEnvironment.CENTAURAI_POCKET_HOST, "127.0.0.1");
  assert.equal(closedEnvironment.CENTAURAI_POCKET_CORS_ORIGINS, undefined);

  const lanEnvironment = buildSidecarEnvironment(base, {
    dataRoot: "/tmp/x",
    sessionOwnerToken: "cp_owner_test",
    readyNonce: "nonce",
    lan: { enabled: true, corsOrigins: ["http://192.168.1.20:17818"] },
  });
  assert.equal(lanEnvironment.CENTAURAI_POCKET_HOST, "0.0.0.0");
  assert.ok(
    lanEnvironment.CENTAURAI_POCKET_CORS_ORIGINS.endsWith(
      "http://192.168.1.20:17818",
    ),
  );
  assert.ok(
    lanEnvironment.CENTAURAI_POCKET_CORS_ORIGINS.includes(
      "http://127.0.0.1:17818",
    ),
  );
});
