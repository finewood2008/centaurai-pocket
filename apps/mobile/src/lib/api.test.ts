import assert from "node:assert/strict";
import test from "node:test";

import {
  normalizeTasks,
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
