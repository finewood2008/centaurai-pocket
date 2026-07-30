import assert from "node:assert/strict";
import test from "node:test";

import {
  createQueuedMutation,
  enqueueMutation,
  isRetryableHttpStatus,
  markMutationFailed,
  markMutationNeedsAttention,
  markMutationSucceeded,
  mutationBackoffMs,
  pendingTaskAction,
  retryMutationsForProfile,
  runnableMutations,
} from "./mutation-queue";

test("enqueueMutation deduplicates the same idempotency key", () => {
  const mutation = createQueuedMutation(
    {
      idempotencyKey: "stable-key",
      profileId: "profile-a",
      kind: "source-sync",
      entityId: "source-1",
      method: "POST",
      path: "/sources/source-1/sync",
      body: {},
    },
    100,
  );

  const once = enqueueMutation([], mutation);
  const twice = enqueueMutation(once, mutation);

  assert.equal(once.length, 1);
  assert.equal(twice.length, 1);
  assert.equal(twice[0]?.idempotencyKey, "stable-key");
});

test("failed mutations back off and become runnable later", () => {
  const mutation = createQueuedMutation(
    {
      idempotencyKey: "retry-key",
      profileId: "profile-a",
      kind: "capture",
      entityId: null,
      method: "POST",
      path: "/captures",
      body: { text: "fragment" },
    },
    1_000,
  );

  const failed = markMutationFailed(
    [mutation],
    mutation.id,
    "offline",
    1_000,
    () => 0.5,
  );
  assert.equal(failed[0]?.attempts, 1);
  assert.equal(
    failed[0]?.nextAttemptAt,
    1_000 + mutationBackoffMs(1, () => 0.5),
  );
  assert.equal(runnableMutations(failed, "profile-a", 2_999).length, 0);
  assert.equal(runnableMutations(failed, "profile-a", 3_000).length, 1);
});

test("backoff adds bounded jitter", () => {
  assert.equal(mutationBackoffMs(2, () => 0), 3_200);
  assert.equal(mutationBackoffMs(2, () => 0.5), 4_000);
  assert.equal(mutationBackoffMs(2, () => 1), 4_800);
  assert.equal(mutationBackoffMs(20, () => 1), 5 * 60_000);
});

test("success removes a mutation and task actions keep ordering", () => {
  const apply = createQueuedMutation(
    {
      idempotencyKey: "apply-key",
      profileId: "profile-a",
      kind: "task-action",
      entityId: "task-1",
      method: "POST",
      path: "/governance/tasks/task-1/actions",
      body: { action: "apply" },
    },
    100,
  );
  const undo = createQueuedMutation(
    {
      idempotencyKey: "undo-key",
      profileId: "profile-a",
      kind: "task-action",
      entityId: "task-1",
      method: "POST",
      path: "/governance/tasks/task-1/actions",
      body: { action: "undo" },
    },
    101,
  );
  const queue = enqueueMutation(enqueueMutation([], apply), undo);

  assert.equal(pendingTaskAction(queue, "task-1"), "undo");
  assert.deepEqual(
    markMutationSucceeded(queue, apply.id).map((item) => item.id),
    [undo.id],
  );
});

test("mutations can only run against the connection profile that created them", () => {
  const fromFirstServer = createQueuedMutation(
    {
      idempotencyKey: "server-a-write",
      profileId: "profile-a",
      kind: "capture",
      entityId: null,
      method: "POST",
      path: "/captures",
      body: { text: "private for A" },
    },
    100,
  );
  const fromSecondServer = createQueuedMutation(
    {
      idempotencyKey: "server-b-write",
      profileId: "profile-b",
      kind: "capture",
      entityId: null,
      method: "POST",
      path: "/captures",
      body: { text: "private for B" },
    },
    101,
  );
  const queue = [fromFirstServer, fromSecondServer];

  assert.deepEqual(
    runnableMutations(queue, "profile-a", 200).map((item) => item.id),
    [fromFirstServer.id],
  );
  assert.deepEqual(
    runnableMutations(queue, "profile-b", 200).map((item) => item.id),
    [fromSecondServer.id],
  );
});

test("non-retryable client failures wait for explicit manual retry", () => {
  const mutation = createQueuedMutation(
    {
      idempotencyKey: "invalid-source",
      profileId: "profile-a",
      kind: "source-create",
      entityId: "/missing",
      method: "POST",
      path: "/sources",
      body: { display_name: "missing" },
    },
    100,
  );
  const blocked = markMutationNeedsAttention(
    [mutation],
    mutation.id,
    "服务返回 422",
  );

  assert.equal(blocked[0]?.state, "needs-attention");
  assert.equal(runnableMutations(blocked, "profile-a", 1_000).length, 0);

  const retried = retryMutationsForProfile(blocked, "profile-a", 1_000);
  assert.equal(retried[0]?.state, "pending");
  assert.equal(runnableMutations(retried, "profile-a", 1_000).length, 1);
  assert.equal(isRetryableHttpStatus(422), false);
  assert.equal(isRetryableHttpStatus(408), true);
  assert.equal(isRetryableHttpStatus(429), true);
  assert.equal(isRetryableHttpStatus(503), true);
  assert.equal(isRetryableHttpStatus(null), true);
});

test("one failed mutation does not make later operations unrunnable", () => {
  const first = createQueuedMutation(
    {
      idempotencyKey: "first",
      profileId: "profile-a",
      kind: "source-sync",
      entityId: "source-1",
      method: "POST",
      path: "/sources/source-1/sync",
      body: {},
    },
    100,
  );
  const second = createQueuedMutation(
    {
      idempotencyKey: "second",
      profileId: "profile-a",
      kind: "capture",
      entityId: null,
      method: "POST",
      path: "/captures",
      body: { text: "keep moving" },
    },
    101,
  );

  const queue = markMutationFailed(
    [first, second],
    first.id,
    "temporary upstream error",
    200,
    () => 0.5,
  );
  assert.deepEqual(
    runnableMutations(queue, "profile-a", 201).map((item) => item.id),
    [second.id],
  );
});

test("task actions keep causal order per entity without blocking other work", () => {
  const apply = createQueuedMutation(
    {
      idempotencyKey: "task-1-apply",
      profileId: "profile-a",
      kind: "task-action",
      entityId: "task-1",
      method: "POST",
      path: "/governance/tasks/task-1/apply",
      body: { action: "apply" },
    },
    100,
  );
  const undo = createQueuedMutation(
    {
      idempotencyKey: "task-1-undo",
      profileId: "profile-a",
      kind: "task-action",
      entityId: "task-1",
      method: "POST",
      path: "/governance/tasks/task-1/undo",
      body: { action: "undo" },
    },
    101,
  );
  const otherTask = createQueuedMutation(
    {
      idempotencyKey: "task-2-skip",
      profileId: "profile-a",
      kind: "task-action",
      entityId: "task-2",
      method: "POST",
      path: "/governance/tasks/task-2/skip",
      body: { action: "skip" },
    },
    102,
  );
  const capture = createQueuedMutation(
    {
      idempotencyKey: "capture-after-actions",
      profileId: "profile-a",
      kind: "capture",
      entityId: null,
      method: "POST",
      path: "/captures",
      body: { text: "independent" },
    },
    103,
  );
  const queue = [apply, undo, otherTask, capture];

  assert.deepEqual(
    runnableMutations(queue, "profile-a", 200).map((item) => item.id),
    [apply.id, otherTask.id, capture.id],
  );

  const failedApply = markMutationFailed(
    queue,
    apply.id,
    "temporary error",
    200,
    () => 0.5,
  );
  assert.deepEqual(
    runnableMutations(failedApply, "profile-a", 201).map((item) => item.id),
    [otherTask.id, capture.id],
  );

  const afterApply = markMutationSucceeded(queue, apply.id);
  assert.deepEqual(
    runnableMutations(afterApply, "profile-a", 200).map((item) => item.id),
    [undo.id, otherTask.id, capture.id],
  );
});
