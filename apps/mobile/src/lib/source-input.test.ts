import assert from "node:assert/strict";
import test from "node:test";

import { isAbsoluteServerPath } from "./source-input";

test("server folder path must be absolute", () => {
  assert.equal(isAbsoluteServerPath("/srv/personal-docs"), true);
  assert.equal(isAbsoluteServerPath("C:\\Personal\\Docs"), true);
  assert.equal(isAbsoluteServerPath("\\\\nas\\private"), true);
  assert.equal(isAbsoluteServerPath("relative/folder"), false);
  assert.equal(isAbsoluteServerPath("~/Documents"), false);
});
