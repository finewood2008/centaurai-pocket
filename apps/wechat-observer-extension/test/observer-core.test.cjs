const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const core = require("../observer-core.js");

function matchesSimple(element, selector) {
  const id = selector.match(/#([A-Za-z0-9_-]+)/);
  if (id && element.id !== id[1]) return false;
  for (const className of [...selector.matchAll(/\.([A-Za-z0-9_-]+)/g)].map((match) => match[1])) {
    if (!element.classes.has(className)) return false;
  }
  for (const attribute of [...selector.matchAll(/\[([A-Za-z0-9_-]+)\]/g)].map((match) => match[1])) {
    if (!element.attributes.has(attribute)) return false;
  }
  return Boolean(id || selector.includes(".") || selector.includes("["));
}

function matchesSelector(element, selector) {
  return selector.split(",").some((candidate) => {
    const parts = candidate.trim().split(/\s+/);
    let current = element;
    if (!matchesSimple(current, parts.pop())) return false;
    while (parts.length) {
      const expected = parts.pop();
      current = current.parent;
      while (current && !matchesSimple(current, expected)) current = current.parent;
      if (!current) return false;
    }
    return true;
  });
}

class FixtureElement {
  constructor(spec, parent = null) {
    this.nodeType = 1;
    this.parent = parent;
    this.parentElement = parent;
    this.id = spec.id || "";
    this.classes = new Set(spec.classes || []);
    this.className = [...this.classes].join(" ");
    this.classList = { contains: (name) => this.classes.has(name) };
    this.attributes = new Map(Object.entries(spec.attributes || {}));
    this.hidden = Boolean(spec.hidden);
    this.visible = spec.visible !== false;
    this.ownText = spec.text || "";
    this.children = (spec.children || []).map((child) => new FixtureElement(child, this));
    this.ownerDocument = null;
  }

  get textContent() {
    return this.ownText + this.children.map((child) => child.textContent).join("");
  }

  setOwnerDocument(documentLike) {
    this.ownerDocument = documentLike;
    for (const child of this.children) child.setOwnerDocument(documentLike);
  }

  getAttribute(name) {
    return this.attributes.has(name) ? this.attributes.get(name) : null;
  }

  hasAttribute(name) {
    return this.attributes.has(name);
  }

  matches(selector) {
    return matchesSelector(this, selector);
  }

  closest(selector) {
    let current = this;
    while (current) {
      if (matchesSelector(current, selector)) return current;
      current = current.parent;
    }
    return null;
  }

  querySelectorAll(selector) {
    const matches = [];
    for (const child of this.children) {
      if (matchesSelector(child, selector)) matches.push(child);
      matches.push(...child.querySelectorAll(selector));
    }
    return matches;
  }

  querySelector(selector) {
    return this.querySelectorAll(selector)[0] || null;
  }

  getClientRects() {
    return this.visible ? [{ width: 10, height: 10 }] : [];
  }
}

function loadFixture() {
  const fixturePath = path.join(__dirname, "fixtures", "rendered-chat.json");
  const root = new FixtureElement(JSON.parse(fs.readFileSync(fixturePath, "utf8")));
  const documentLike = {
    defaultView: {
      getComputedStyle(element) {
        return element.visible
          ? { display: "block", visibility: "visible" }
          : { display: "none", visibility: "hidden" };
      },
    },
    querySelector() {
      return null;
    },
  };
  root.setOwnerDocument(documentLike);
  return root;
}

test("extracts only a visible #chatArea message into the collector contract", () => {
  const root = loadFixture();
  const message = root.querySelector('[data-cm]');
  const event = core.extractMessage(message, {
    now: () => new Date("2026-07-31T12:00:00.000Z"),
  });
  assert.deepEqual(event, {
    provider_msgid: "m-1001",
    provider_conversation_id: "@@leadership-room",
    conversation_type: "group",
    direction: "incoming",
    message_type: "text",
    observed_at: "2026-07-31T12:00:00.000Z",
    conversation_name: "管理层例会",
    sender_provider_id: "@alice",
    sender_display_name: "Alice",
    text: "明天 10:00 开会\n\n请带方案",
    displayed_time_text: "昨天 18:30",
  });
});

test("rejects prerendered and layout-hidden message nodes", () => {
  const root = loadFixture();
  const prerendered = root.querySelectorAll('[data-cm]')[1];
  assert.equal(core.extractMessage(prerendered), null);

  const visible = root.querySelector('[data-cm]');
  visible.visible = false;
  assert.equal(core.extractMessage(visible), null);
});

test("normalizes every WeChat type to the backend's seven-value contract", () => {
  const allowed = new Set(["text", "image", "voice", "file", "video", "system", "other"]);
  for (const rawType of [1, 3, 34, 37, 42, 43, 47, 48, 49, 62, 10000, 999999, "bad/type"]) {
    assert.equal(allowed.has(core.mapMessageType(rawType)), true);
  }
  assert.equal(core.mapMessageType(49), "file");
  assert.equal(core.mapMessageType(62), "video");
  assert.equal(core.mapMessageType(999999), "other");
});

test("uses an avatar title as sender display name without reading its text", () => {
  const root = new FixtureElement({
    id: "chatArea",
    attributes: { "data-username": "@bob", "data-conversation-name": "Bob" },
    children: [
      {
        classes: ["message", "you"],
        attributes: { "data-cm": '{"msgId":"m-avatar","msgType":3}' },
        children: [{ classes: ["avatar"], attributes: { title: "Bob Zhang" } }],
      },
    ],
  });
  const documentLike = {
    defaultView: { getComputedStyle: () => ({ display: "block", visibility: "visible" }) },
    querySelector: () => null,
  };
  root.setOwnerDocument(documentLike);
  assert.equal(core.extractMessage(root.querySelector('[data-cm]')).sender_display_name, "Bob Zhang");
});

test("manifest has only the intended Firefox permissions and exact WeChat origin", () => {
  const manifest = JSON.parse(fs.readFileSync(path.join(__dirname, "..", "manifest.json"), "utf8"));
  assert.equal(manifest.manifest_version, 3);
  assert.deepEqual(manifest.permissions.sort(), ["nativeMessaging", "storage"]);
  assert.deepEqual(manifest.host_permissions, ["https://wx.qq.com/*"]);
  assert.equal(
    manifest.browser_specific_settings.gecko.id,
    "centaur-pocket-wechat-observer@centaur.ai",
  );
  for (const forbidden of ["cookies", "webRequest", "debugger", "downloads", "tabs", "history"]) {
    assert.equal(manifest.permissions.includes(forbidden), false);
  }
});

test("malformed data-cm and a text node without visible content are not emitted", () => {
  assert.equal(core.parseDataCm("not-json"), null);
  assert.equal(core.parseDataCm('{"actualSender":"@alice"}'), null);

  const root = new FixtureElement({
    id: "chatArea",
    attributes: { "data-username": "@empty", "data-conversation-name": "Empty" },
    children: [{ classes: ["message", "you"], attributes: { "data-cm": '{"msgId":"m-empty","msgType":1}' } }],
  });
  const documentLike = {
    defaultView: { getComputedStyle: () => ({ display: "block", visibility: "visible" }) },
    querySelector: () => null,
  };
  root.setOwnerDocument(documentLike);
  assert.equal(core.extractMessage(root.querySelector('[data-cm]')), null);
});
