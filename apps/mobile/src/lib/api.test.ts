import assert from "node:assert/strict";
import test from "node:test";

import {
  normalizeSourceCoverageGaps,
  normalizeMobileDevices,
  normalizeMobilePairing,
  normalizeSourcePairing,
  normalizeSources,
  normalizeTasks,
  normalizeWechatObserverStatus,
  serverUrlSecurityError,
} from "./api";

test("LAN plaintext HTTP is rejected while HTTPS and loopback stay usable", () => {
  assert.equal(serverUrlSecurityError("https://pocket.example.com"), null);
  assert.equal(serverUrlSecurityError("http://127.0.0.1:8718"), null);
  assert.equal(serverUrlSecurityError("http://10.0.2.2:8718"), null);
  assert.match(
    serverUrlSecurityError("http://192.168.1.20:8718") ?? "",
    /明文 HTTP/,
  );
});

test("governance task normalization keeps proposal fields and deletion kind", () => {
  const [task] = normalizeTasks({
    items: [
      {
        id: "task-1",
        kind: "source_deleted",
        title: "原始标题",
        proposal: {
          patch: {
            title: "建议标题",
            tags: ["家庭", "保险"],
            category: "历史资料",
          },
        },
        item: {
          title: "原始标题",
          tags: ["原标签"],
          category: "家庭财务",
        },
      },
    ],
  });

  assert.equal(task?.kind, "deletion");
  assert.equal(task?.suggestedTitle, "建议标题");
  assert.deepEqual(task?.suggestedTags, ["家庭", "保险"]);
  assert.equal(task?.currentCategory, "家庭财务");
  assert.equal(task?.suggestedCategory, "历史资料");
});

test("WeChat observer payloads normalize source kind and status variants", () => {
  const [source] = normalizeSources({
    items: [
      {
        id: "wechat-1",
        kind: "wechat_visible_web",
        display_name: "个人微信网页版",
        state: "awaiting_pairing",
      },
    ],
  });
  assert.equal(source?.type, "wechat_visible_web");

  const status = normalizeWechatObserverStatus({
    data: {
      state: "active",
      last_event_at: "2026-07-31T07:59:58Z",
      enabled: true,
      conversation_count: 8,
      message_count: 120,
      open_gap_count: 1,
      last_session: {
        extension_version: "1.2.0",
        parser_version: "2026.07",
        current_conversation_id: "conversation-1",
        current_conversation_name: "产品讨论群",
        last_heartbeat_at: "2026-07-31T08:00:00Z",
        unread_conversation_count: 3,
      },
    },
  });
  assert.equal(status.state, "active");
  assert.equal(status.currentConversationName, "产品讨论群");
  assert.equal(status.unreadConversationCount, 3);
  assert.equal(status.messageCount, 120);
  assert.equal(status.paused, false);
});

test("pairing codes and nested coverage gaps preserve one-time UI data", () => {
  const pairing = normalizeSourcePairing({
    id: "pairing-1",
    source_id: "wechat-1",
    pairing_code: "ABCD-EFGH",
    expires_at: "2026-07-31T08:15:00Z",
  });
  assert.equal(pairing.pairingCode, "ABCD-EFGH");

  const gaps = normalizeSourceCoverageGaps({
    data: {
      items: [
        {
          id: "gap-1",
          kind: "browser_offline",
          started_at: "2026-07-31T07:00:00Z",
          details: { message: "浏览器心跳中断" },
        },
      ],
      total: 4,
    },
  });
  assert.equal(gaps.total, 4);
  assert.equal(gaps.items[0]?.details, "浏览器心跳中断");
});

test("mobile device responses preserve pairing and revocation metadata", () => {
  const pairing = normalizeMobilePairing({
    pairing_id: "mobile-pairing-1",
    code: "ABCD-EFGH-JKLM",
    expires_at: "2026-08-02T08:10:00Z",
  });
  assert.equal(pairing.id, "mobile-pairing-1");
  assert.equal(pairing.code, "ABCD-EFGH-JKLM");

  const [device] = normalizeMobileDevices({
    items: [
      {
        id: "device-record-1",
        device_id: "ios-device-1",
        display_name: "主人的 iPhone",
        platform: "ios",
        app_version: "0.5.0",
        status: "revoked",
        last_seen_at: "2026-08-02T08:00:00Z",
        revoked_at: "2026-08-02T08:05:00Z",
      },
    ],
  });
  assert.equal(device?.displayName, "主人的 iPhone");
  assert.equal(device?.status, "revoked");
  assert.equal(device?.revokedAt, "2026-08-02T08:05:00Z");
});
