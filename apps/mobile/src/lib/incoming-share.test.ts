import assert from "node:assert/strict";
import test from "node:test";

import { incomingShareDraft } from "./incoming-share";

test("incomingShareDraft maps native text and URL payloads", () => {
  const draft = incomingShareDraft([
    {
      shareType: "text",
      value: "稍后阅读 https://example.com/article",
      mimeType: "text/plain",
    },
    {
      shareType: "url",
      value: "https://expo.dev",
      mimeType: "text/uri-list",
    },
  ]);

  assert.equal(draft.text, "稍后阅读 https://example.com/article\n\nhttps://expo.dev");
  assert.equal(draft.url, "https://example.com/article");
  assert.equal(draft.mimeType, "text/plain");
  assert.equal(draft.acceptedCount, 2);
  assert.equal(draft.unsupportedCount, 0);
});

test("incomingShareDraft does not pretend file payloads are imported", () => {
  const draft = incomingShareDraft([
    {
      shareType: "file",
      value: "file:///private/report.pdf",
      mimeType: "application/pdf",
    },
  ]);

  assert.equal(draft.text, "");
  assert.equal(draft.url, "");
  assert.equal(draft.acceptedCount, 0);
  assert.equal(draft.unsupportedCount, 1);
});
