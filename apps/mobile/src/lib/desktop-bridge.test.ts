import assert from "node:assert/strict";
import test from "node:test";

import {
  DESKTOP_MANAGED_OWNER,
  isDesktopApiResponse,
} from "./desktop-bridge";

test("desktop bridge responses require an explicit status and payload", () => {
  assert.equal(
    isDesktopApiResponse({ ok: true, status: 200, payload: { status: "ok" } }),
    true,
  );
  assert.equal(
    isDesktopApiResponse({
      ok: false,
      status: null,
      payload: { detail: "offline" },
    }),
    true,
  );
  assert.equal(isDesktopApiResponse({ ok: true, payload: null }), false);
  assert.match(DESKTOP_MANAGED_OWNER, /^centaur-pocket-desktop-/);
});
