"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const {
  contentTypeFor,
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
});

test("health identity is validated", () => {
  assert.equal(
    isPocketHealth({
      status: "ok",
      service: "centaurai-pocket",
      version: "0.1.0",
    }),
    true,
  );
  assert.equal(isPocketHealth({ status: "ok", service: "other" }), false);
  assert.equal(contentTypeFor("entry.js"), "text/javascript; charset=utf-8");
});
