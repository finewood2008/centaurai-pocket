export type QueuedMutationKind =
  | "task-action"
  | "source-sync"
  | "source-create"
  | "capture";

export type QueuedMutationState = "pending" | "needs-attention";

export type QueuedMutation = {
  id: string;
  idempotencyKey: string;
  profileId: string;
  kind: QueuedMutationKind;
  entityId: string | null;
  method: "POST" | "PATCH" | "PUT" | "DELETE";
  path: string;
  body: Record<string, unknown>;
  createdAt: number;
  attempts: number;
  nextAttemptAt: number;
  lastError: string | null;
  state: QueuedMutationState;
};

export type NewMutation = Omit<
  QueuedMutation,
  | "id"
  | "idempotencyKey"
  | "createdAt"
  | "attempts"
  | "nextAttemptAt"
  | "lastError"
  | "state"
> & {
  idempotencyKey?: string;
};

function randomPart(): string {
  const cryptoApi = globalThis.crypto as { randomUUID?: () => string } | undefined;
  if (cryptoApi?.randomUUID) return cryptoApi.randomUUID();
  return `${Math.random().toString(36).slice(2)}-${Math.random().toString(36).slice(2)}`;
}

export function createIdempotencyKey(scope: string, now = Date.now()): string {
  return `pocket-${scope}-${now.toString(36)}-${randomPart()}`;
}

export function createQueuedMutation(
  input: NewMutation,
  now = Date.now(),
): QueuedMutation {
  const idempotencyKey =
    input.idempotencyKey ?? createIdempotencyKey(input.kind, now);
  return {
    ...input,
    id: idempotencyKey,
    idempotencyKey,
    createdAt: now,
    attempts: 0,
    nextAttemptAt: now,
    lastError: null,
    state: "pending",
  };
}

export function enqueueMutation(
  queue: QueuedMutation[],
  mutation: QueuedMutation,
): QueuedMutation[] {
  if (queue.some((item) => item.idempotencyKey === mutation.idempotencyKey)) {
    return queue;
  }
  return [...queue, mutation].sort((a, b) => a.createdAt - b.createdAt);
}

export function runnableMutations(
  queue: QueuedMutation[],
  profileId: string,
  now = Date.now(),
  limit = 20,
): QueuedMutation[] {
  const ordered = queue
    .filter((mutation) => mutation.profileId === profileId)
    .sort((a, b) => a.createdAt - b.createdAt);
  const firstTaskActionByEntity = new Set<string>();
  const runnable: QueuedMutation[] = [];

  for (const mutation of ordered) {
    if (mutation.kind === "task-action" && mutation.entityId) {
      if (firstTaskActionByEntity.has(mutation.entityId)) continue;
      firstTaskActionByEntity.add(mutation.entityId);
    }
    if (
      mutation.state !== "pending" ||
      mutation.nextAttemptAt > now
    ) {
      continue;
    }
    runnable.push(mutation);
    if (runnable.length === limit) break;
  }

  return runnable;
}

export function mutationBackoffMs(
  attempts: number,
  random: () => number = Math.random,
): number {
  const normalizedAttempts = Math.max(1, Math.floor(attempts));
  const base = 2_000 * 2 ** (normalizedAttempts - 1);
  const randomValue = Math.max(0, Math.min(1, random()));
  const jitterFactor = 0.8 + randomValue * 0.4;
  return Math.min(5 * 60_000, Math.round(base * jitterFactor));
}

export function markMutationSucceeded(
  queue: QueuedMutation[],
  id: string,
): QueuedMutation[] {
  return queue.filter((mutation) => mutation.id !== id);
}

export function markMutationFailed(
  queue: QueuedMutation[],
  id: string,
  error: string,
  now = Date.now(),
  random: () => number = Math.random,
): QueuedMutation[] {
  return queue.map((mutation) => {
    if (mutation.id !== id) return mutation;
    const attempts = mutation.attempts + 1;
    return {
      ...mutation,
      attempts,
      lastError: error,
      nextAttemptAt: now + mutationBackoffMs(attempts, random),
    };
  });
}

export function markMutationNeedsAttention(
  queue: QueuedMutation[],
  id: string,
  error: string,
): QueuedMutation[] {
  return queue.map((mutation) =>
    mutation.id === id
      ? {
          ...mutation,
          state: "needs-attention",
          lastError: error,
        }
      : mutation,
  );
}

export function retryMutationsForProfile(
  queue: QueuedMutation[],
  profileId: string,
  now = Date.now(),
): QueuedMutation[] {
  return queue.map((mutation) =>
    mutation.profileId === profileId
      ? {
          ...mutation,
          state: "pending",
          nextAttemptAt: now,
          lastError: null,
        }
      : mutation,
  );
}

export function isRetryableHttpStatus(status: number | null): boolean {
  if (status === null) return true;
  if (status === 408 || status === 429) return true;
  return status < 400 || status >= 500;
}

export function pendingTaskAction(
  queue: QueuedMutation[],
  taskId: string,
): "apply" | "skip" | "undo" | null {
  for (let index = queue.length - 1; index >= 0; index -= 1) {
    const mutation = queue[index];
    if (mutation.kind !== "task-action" || mutation.entityId !== taskId) continue;
    const action = mutation.body.action;
    if (action === "apply" || action === "skip" || action === "undo") return action;
  }
  return null;
}
