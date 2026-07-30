import assert from "node:assert/strict";
import test from "node:test";

import { RequestGeneration } from "./request-generation";

test("an older private request cannot overwrite a newer profile response", async () => {
  const gate = new RequestGeneration();
  const applied: string[] = [];
  let resolveFirst: ((value: string) => void) | undefined;
  let resolveSecond: ((value: string) => void) | undefined;

  const applyWhenCurrent = async (
    generation: number,
    promise: Promise<string>,
  ) => {
    const value = await promise;
    if (gate.isCurrent(generation)) applied.push(value);
  };

  const firstGeneration = gate.begin();
  const first = applyWhenCurrent(
    firstGeneration,
    new Promise<string>((resolve) => {
      resolveFirst = resolve;
    }),
  );
  const secondGeneration = gate.begin();
  const second = applyWhenCurrent(
    secondGeneration,
    new Promise<string>((resolve) => {
      resolveSecond = resolve;
    }),
  );

  resolveSecond?.("profile-b");
  await second;
  resolveFirst?.("profile-a");
  await first;

  assert.deepEqual(applied, ["profile-b"]);

  gate.invalidate();
  assert.equal(gate.isCurrent(secondGeneration), false);
});
