import assert from "node:assert/strict";
import test from "node:test";

import {
  governanceApplyPatch,
  governanceTagsError,
  parseGovernanceTags,
} from "./governance-input";

test("governance tags accept Chinese punctuation and remove duplicates", () => {
  assert.deepEqual(
    parseGovernanceTags("家庭，保险; #重要\n家庭"),
    ["家庭", "保险", "重要"],
  );
});

test("ordinary apply includes category while deletion only archives", () => {
  assert.deepEqual(
    governanceApplyPatch("classify", {
      title: "家庭保单",
      tags: ["保险"],
      category: " 家庭财务 ",
    }),
    {
      state: "ready",
      title: "家庭保单",
      tags: ["保险"],
      category: "家庭财务",
    },
  );
  assert.deepEqual(
    governanceApplyPatch("deletion", {
      title: "不应提交",
      tags: ["不应提交"],
      category: "不应提交",
    }),
    { state: "archived" },
  );
});

test("governance tags enforce the backend per-tag length", () => {
  assert.equal(governanceTagsError(["a".repeat(64)]), null);
  assert.equal(
    governanceTagsError(["a".repeat(65)]),
    "单个标签不能超过 64 个字符",
  );
});
