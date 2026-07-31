import {
  createContext,
  type PropsWithChildren,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { AppState } from "react-native";

import {
  ApiError,
  createPocketApi,
  DEFAULT_SERVER_URL,
  serverUrlSecurityError,
  type PocketApi,
} from "@/lib/api";
import {
  createIdempotencyKey,
  createQueuedMutation,
  enqueueMutation,
  isRetryableHttpStatus,
  markMutationFailed,
  markMutationNeedsAttention,
  markMutationSucceeded,
  retryMutationsForProfile,
  runnableMutations,
  type NewMutation,
  type QueuedMutation,
} from "@/lib/mutation-queue";
import {
  clearMutationQueue,
  loadMutationQueue,
  saveMutationQueue,
} from "@/lib/mutation-storage";
import {
  DESKTOP_MANAGED_OWNER,
  isDesktopRuntime,
} from "@/lib/desktop-bridge";
import {
  loadConnectionSettings,
  saveConnectionSettings,
} from "@/lib/settings-storage";
import type {
  ConnectionSettings,
  FolderSourceInput,
  GovernancePatch,
  TaskAction,
} from "@/lib/types";

type CaptureInput = {
  title: string;
  text: string;
  url: string;
  mimeType?: string;
};

type QueueInput = Omit<NewMutation, "idempotencyKey" | "profileId">;

type PocketContextValue = {
  ready: boolean;
  settings: ConnectionSettings;
  api: PocketApi;
  mutations: QueuedMutation[];
  inactiveMutationCount: number;
  isConfigured: boolean;
  isFlushing: boolean;
  queueLoadError: string | null;
  lastQueueError: string | null;
  saveSettings: (settings: ConnectionSettings) => Promise<void>;
  testConnection: (settings?: ConnectionSettings) => Promise<void>;
  enqueue: (input: QueueInput) => Promise<QueuedMutation>;
  queueTaskAction: (
    taskId: string,
    action: TaskAction,
    patch?: GovernancePatch,
  ) => Promise<QueuedMutation>;
  queueSourceSync: (sourceId: string) => Promise<QueuedMutation>;
  queueFolderSource: (source: FolderSourceInput) => Promise<QueuedMutation>;
  queueCapture: (capture: CaptureInput) => Promise<QueuedMutation>;
  flushQueue: () => Promise<void>;
  retryQueueNow: () => Promise<void>;
  discardQueue: () => Promise<void>;
  discardInactiveQueue: () => Promise<void>;
  clearUnreadableQueue: () => Promise<void>;
};

const startsInDesktopRuntime = isDesktopRuntime();
const initialSettings: ConnectionSettings = {
  serverUrl: DEFAULT_SERVER_URL,
  ownerToken: startsInDesktopRuntime ? DESKTOP_MANAGED_OWNER : "",
  profileId: "connection-loading",
  managedByDesktop: startsInDesktopRuntime,
};

const PocketContext = createContext<PocketContextValue | null>(null);

export function PocketProvider({ children }: PropsWithChildren) {
  const [ready, setReady] = useState(false);
  const [settings, setSettings] = useState<ConnectionSettings>(initialSettings);
  const [storedMutations, setStoredMutations] = useState<QueuedMutation[]>([]);
  const [isFlushing, setIsFlushing] = useState(false);
  const [queueLoadError, setQueueLoadError] = useState<string | null>(null);
  const [queueWriteError, setQueueWriteError] = useState<string | null>(null);
  const [settingsLoadError, setSettingsLoadError] = useState<string | null>(
    null,
  );
  const queueRef = useRef<QueuedMutation[]>([]);
  const persistenceRef = useRef<Promise<void>>(Promise.resolve());
  const flushingRef = useRef(false);
  const api = useMemo(() => createPocketApi(settings), [settings]);
  const mutations = useMemo(
    () =>
      storedMutations.filter(
        (mutation) => mutation.profileId === settings.profileId,
      ),
    [settings.profileId, storedMutations],
  );
  const inactiveMutationCount = storedMutations.length - mutations.length;
  const isConfigured =
    !settingsLoadError &&
    (settings.managedByDesktop === true || Boolean(settings.ownerToken.trim()));
  const lastQueueError =
    queueLoadError ??
    settingsLoadError ??
    queueWriteError ??
    mutations.find(
      (mutation) =>
        mutation.state === "needs-attention" || mutation.lastError,
    )?.lastError ??
    null;

  const commitQueue = useCallback(async (next: QueuedMutation[]) => {
    const previous = queueRef.current;
    queueRef.current = next;
    const persistence = persistenceRef.current
      .catch(() => undefined)
      .then(() => saveMutationQueue(next));
    persistenceRef.current = persistence;
    try {
      await persistence;
      setQueueWriteError(null);
      if (queueRef.current === next) setStoredMutations(next);
    } catch (error) {
      if (queueRef.current === next) {
        queueRef.current = previous;
        setStoredMutations(previous);
      }
      setQueueWriteError(
        error instanceof Error
          ? `写入本机离线队列失败：${error.message}`
          : "写入本机离线队列失败",
      );
      throw error;
    }
  }, []);

  useEffect(() => {
    let active = true;
    Promise.allSettled([loadConnectionSettings(), loadMutationQueue()])
      .then(([settingsResult, queueResult]) => {
        if (!active) return;
        if (settingsResult.status === "fulfilled") {
          setSettings(settingsResult.value);
        } else {
          setSettingsLoadError(
            settingsResult.reason instanceof Error
              ? settingsResult.reason.message
              : "读取本机连接设置失败",
          );
        }
        if (queueResult.status === "fulfilled") {
          queueRef.current = queueResult.value;
          setStoredMutations(queueResult.value);
        } else {
          setQueueLoadError(
            queueResult.reason instanceof Error
              ? queueResult.reason.message
              : "读取本机离线队列失败",
          );
        }
      })
      .finally(() => {
        if (active) setReady(true);
      });
    return () => {
      active = false;
    };
  }, []);

  const flushQueue = useCallback(async () => {
    if (!ready || flushingRef.current) return;
    try {
      await persistenceRef.current;
    } catch {
      return;
    }
    const runnable = runnableMutations(
      queueRef.current,
      settings.profileId,
    );
    if (runnable.length === 0) return;

    flushingRef.current = true;
    setIsFlushing(true);
    try {
      for (const mutation of runnable) {
        let requestError: unknown = null;
        try {
          await api.request({
            path: mutation.path,
            method: mutation.method,
            body: mutation.body,
            idempotencyKey: mutation.idempotencyKey,
            timeoutMs: mutation.kind === "source-sync" ? 120_000 : undefined,
          });
        } catch (error) {
          requestError = error;
        }

        try {
          if (requestError === null) {
            await commitQueue(
              markMutationSucceeded(queueRef.current, mutation.id),
            );
            continue;
          }
          const message =
            requestError instanceof Error
              ? requestError.message
              : "写入失败，稍后自动重试";
          await commitQueue(
            requestError instanceof ApiError &&
              !isRetryableHttpStatus(requestError.status)
              ? markMutationNeedsAttention(
                  queueRef.current,
                  mutation.id,
                  `${message}。请修正设置或内容后手动重试`,
                )
              : markMutationFailed(queueRef.current, mutation.id, message),
          );
        } catch {
          break;
        }
      }
    } finally {
      flushingRef.current = false;
      setIsFlushing(false);
    }
  }, [api, commitQueue, ready, settings.profileId]);

  useEffect(() => {
    if (!ready || mutations.length === 0) return;
    const timer = setTimeout(() => void flushQueue(), 0);
    return () => clearTimeout(timer);
  }, [flushQueue, mutations.length, ready]);

  useEffect(() => {
    if (!ready) return;
    void flushQueue();
    const interval = setInterval(() => void flushQueue(), 20_000);
    const subscription = AppState.addEventListener("change", (state) => {
      if (state === "active") void flushQueue();
    });
    return () => {
      clearInterval(interval);
      subscription.remove();
    };
  }, [flushQueue, ready]);

  const enqueue = useCallback(
    async (input: QueueInput) => {
      if (queueLoadError) {
        throw new Error(
          "本机离线队列无法读取。请先到设置页处理旧队列，避免覆盖尚未恢复的操作",
        );
      }
      if (settingsLoadError) {
        throw new Error(
          "连接设置无法读取。桌面安全桥未就绪时不会保存或发送操作",
        );
      }
      if (!settings.ownerToken.trim()) {
        throw new Error("请先到设置页完成服务地址与 Owner token 配置");
      }
      const key = createIdempotencyKey(input.kind);
      const mutation = createQueuedMutation({
        ...input,
        profileId: settings.profileId,
        idempotencyKey: key,
      });
      await commitQueue(enqueueMutation(queueRef.current, mutation));
      setTimeout(() => void flushQueue(), 0);
      return mutation;
    },
    [
      commitQueue,
      flushQueue,
      queueLoadError,
      settingsLoadError,
      settings.ownerToken,
      settings.profileId,
    ],
  );

  const queueTaskAction = useCallback(
    (taskId: string, action: TaskAction, patch?: GovernancePatch) =>
      enqueue({
        kind: "task-action",
        entityId: taskId,
        method: "POST",
        path: `/governance/tasks/${encodeURIComponent(taskId)}/${action}`,
        body:
          action === "apply"
            ? { action, patch: patch ?? { state: "ready" } }
            : { action },
      }),
    [enqueue],
  );

  const queueSourceSync = useCallback(
    (sourceId: string) =>
      enqueue({
        kind: "source-sync",
        entityId: sourceId,
        method: "POST",
        path: `/sources/${encodeURIComponent(sourceId)}/sync`,
        body: {},
      }),
    [enqueue],
  );

  const queueFolderSource = useCallback(
    (source: FolderSourceInput) =>
      enqueue({
        kind: "source-create",
        entityId: source.path,
        method: "POST",
        path: "/sources",
        body: {
          kind: "folder",
          display_name: source.displayName.trim(),
          config: {
            path: source.path.trim(),
            recursive: true,
            include_hidden: false,
          },
          schedule: source.schedule,
          enabled: true,
        },
      }),
    [enqueue],
  );

  const queueCapture = useCallback(
    (capture: CaptureInput) =>
      enqueue({
        kind: "capture",
        entityId: null,
        method: "POST",
        path: "/captures",
        body: {
          ...capture,
          origin: "mobile-share",
        },
      }),
    [enqueue],
  );

  const persistSettings = useCallback(
    async (next: ConnectionSettings) => {
      if (!next.managedByDesktop && !next.ownerToken.trim()) {
        throw new Error("Owner token 不能为空");
      }
      const securityError = serverUrlSecurityError(next.serverUrl);
      if (securityError) throw new Error(securityError);
      setSettings(await saveConnectionSettings(next));
      setSettingsLoadError(null);
    },
    [],
  );

  const testConnection = useCallback(
    async (candidate = settings) => {
      if (!candidate.managedByDesktop && !candidate.ownerToken.trim()) {
        throw new Error("请先填写 Owner token");
      }
      const candidateApi = createPocketApi(candidate);
      await candidateApi.health();
      await candidateApi.dashboard();
    },
    [settings],
  );

  const retryQueueNow = useCallback(async () => {
    if (queueLoadError) {
      throw new Error("离线队列尚未解密，不能重试；请先到设置页处理");
    }
    await commitQueue(
      retryMutationsForProfile(queueRef.current, settings.profileId),
    );
    setTimeout(() => void flushQueue(), 0);
  }, [commitQueue, flushQueue, queueLoadError, settings.profileId]);

  const discardQueue = useCallback(async () => {
    if (queueLoadError) {
      throw new Error("离线队列尚未解密，请使用“清除无法读取的队列”");
    }
    await commitQueue(
      queueRef.current.filter(
        (mutation) => mutation.profileId !== settings.profileId,
      ),
    );
  }, [commitQueue, queueLoadError, settings.profileId]);

  const discardInactiveQueue = useCallback(async () => {
    if (queueLoadError) {
      throw new Error("离线队列尚未解密，请使用“清除无法读取的队列”");
    }
    await commitQueue(
      queueRef.current.filter(
        (mutation) => mutation.profileId === settings.profileId,
      ),
    );
  }, [commitQueue, queueLoadError, settings.profileId]);

  const clearUnreadableQueue = useCallback(async () => {
    if (!queueLoadError) return;
    await clearMutationQueue();
    queueRef.current = [];
    persistenceRef.current = Promise.resolve();
    setStoredMutations([]);
    setQueueLoadError(null);
  }, [queueLoadError]);

  const value = useMemo<PocketContextValue>(
    () => ({
      ready,
      settings,
      api,
      mutations,
      inactiveMutationCount,
      isConfigured,
      isFlushing,
      queueLoadError,
      lastQueueError,
      saveSettings: persistSettings,
      testConnection,
      enqueue,
      queueTaskAction,
      queueSourceSync,
      queueFolderSource,
      queueCapture,
      flushQueue,
      retryQueueNow,
      discardQueue,
      discardInactiveQueue,
      clearUnreadableQueue,
    }),
    [
      api,
      clearUnreadableQueue,
      discardQueue,
      discardInactiveQueue,
      enqueue,
      flushQueue,
      inactiveMutationCount,
      isConfigured,
      isFlushing,
      lastQueueError,
      mutations,
      persistSettings,
      queueCapture,
      queueFolderSource,
      queueLoadError,
      queueSourceSync,
      queueTaskAction,
      ready,
      retryQueueNow,
      settings,
      testConnection,
    ],
  );

  return <PocketContext.Provider value={value}>{children}</PocketContext.Provider>;
}

export function usePocket(): PocketContextValue {
  const value = useContext(PocketContext);
  if (!value) throw new Error("usePocket must be used inside PocketProvider");
  return value;
}
