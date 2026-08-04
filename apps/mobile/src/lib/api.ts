import type {
  ConnectionSettings,
  DashboardSummary,
  DataSource,
  GovernanceTask,
  GovernanceTaskKind,
  MobileDevice,
  MobileDeviceStatus,
  MobilePairing,
  SourceCoverageGap,
  SourceCoverageGaps,
  SourcePairing,
  SourceStatus,
  WechatObserverState,
  WechatObserverStatus,
} from "@/lib/types";
import {
  getDesktopBridge,
  isDesktopApiResponse,
} from "@/lib/desktop-bridge";

type JsonRecord = Record<string, unknown>;

export const DEFAULT_SERVER_URL =
  process.env.EXPO_PUBLIC_POCKET_API_URL?.trim() ||
  "http://127.0.0.1:8718";
const ALLOW_INSECURE_HTTP =
  process.env.EXPO_PUBLIC_ALLOW_INSECURE_HTTP?.trim().toLowerCase() === "true";

export class ApiError extends Error {
  readonly status: number | null;

  constructor(message: string, status: number | null = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

export function serverUrlSecurityError(value: string): string | null {
  let url: URL;
  try {
    url = new URL(normalizeServerUrl(value));
  } catch {
    return "服务地址不是有效 URL";
  }
  if (url.protocol === "https:") return null;
  if (url.protocol !== "http:") return "服务地址只支持 HTTPS 或 HTTP";

  const hostname = url.hostname.toLowerCase();
  const loopbackHosts = new Set([
    "localhost",
    "127.0.0.1",
    "::1",
    "[::1]",
    "10.0.2.2",
  ]);
  if (loopbackHosts.has(hostname) || ALLOW_INSECURE_HTTP) return null;
  return "局域网明文 HTTP 默认禁用，请使用 HTTPS/VPN；仅开发构建可显式启用 EXPO_PUBLIC_ALLOW_INSECURE_HTTP=true";
}

function isRecord(value: unknown): value is JsonRecord {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function firstRecord(value: unknown): JsonRecord {
  if (!isRecord(value)) return {};
  if (isRecord(value.data)) return value.data;
  if (isRecord(value.dashboard)) return value.dashboard;
  if (isRecord(value.summary)) return value.summary;
  return value;
}

function firstArray(value: unknown): unknown[] {
  if (Array.isArray(value)) return value;
  if (!isRecord(value)) return [];
  for (const key of ["items", "data", "tasks", "sources", "results"]) {
    if (Array.isArray(value[key])) return value[key];
  }
  if (isRecord(value.data)) return firstArray(value.data);
  return [];
}

function pickString(
  source: JsonRecord,
  keys: string[],
  fallback = "",
): string {
  for (const key of keys) {
    const value = source[key];
    if (typeof value === "string" && value.trim()) return value;
    if (typeof value === "number") return String(value);
  }
  return fallback;
}

function pickNullableString(source: JsonRecord, keys: string[]): string | null {
  const value = pickString(source, keys);
  return value || null;
}

function pickNumber(source: JsonRecord, keys: string[], fallback = 0): number {
  for (const key of keys) {
    const value = source[key];
    if (typeof value === "number" && Number.isFinite(value)) return value;
    if (typeof value === "string" && value.trim()) {
      const parsed = Number(value);
      if (Number.isFinite(parsed)) return parsed;
    }
  }
  return fallback;
}

function pickStringArray(source: JsonRecord, keys: string[]): string[] {
  for (const key of keys) {
    const value = source[key];
    if (!Array.isArray(value)) continue;
    return value.filter(
      (item): item is string => typeof item === "string" && Boolean(item.trim()),
    );
  }
  return [];
}

function taskKind(value: string): GovernanceTaskKind {
  const normalized = value.toLowerCase();
  if (["deduplicate", "duplicate", "dedupe", "merge"].includes(normalized)) {
    return "deduplicate";
  }
  if (["classify", "classification", "category", "tag"].includes(normalized)) {
    return "classify";
  }
  if (["quality", "repair", "complete"].includes(normalized)) return "quality";
  if (["normalize", "normalization", "format"].includes(normalized)) {
    return "normalize";
  }
  if (
    ["deletion", "delete", "source_deleted", "source-missing"].includes(
      normalized,
    )
  ) {
    return "deletion";
  }
  if (["review", "manual_review"].includes(normalized)) return "review";
  return "unknown";
}

function sourceStatus(value: string): SourceStatus {
  const normalized = value.toLowerCase();
  if (["healthy", "ready", "active", "success", "ok", "idle"].includes(normalized)) {
    return "healthy";
  }
  if (["syncing", "running", "pending"].includes(normalized)) return "syncing";
  if (["paused", "disabled", "inactive"].includes(normalized)) return "paused";
  if (["error", "failed", "unhealthy"].includes(normalized)) return "error";
  return "unknown";
}

export function normalizeDashboard(payload: unknown): DashboardSummary {
  const source = firstRecord(payload);
  const sourceStats = isRecord(source.sources) ? source.sources : {};
  const itemStats = isRecord(source.items) ? source.items : {};

  return {
    pendingTasks: pickNumber(source, ["pending_tasks", "pendingTasks", "pending_count"]),
    readyItems: pickNumber(source, ["ready_items", "readyItems", "ready_count"], pickNumber(itemStats, ["ready"])),
    totalItems: pickNumber(source, ["total_items", "totalItems", "item_count"], pickNumber(itemStats, ["total"])),
    sourceCount: pickNumber(source, ["source_count", "sourceCount", "total_sources"], pickNumber(sourceStats, ["total"])),
    healthySources: pickNumber(
      source,
      ["healthy_sources", "healthySources", "active_sources"],
      pickNumber(sourceStats, ["healthy", "active"]),
    ),
    qualityScore: pickNumber(source, ["quality_score", "qualityScore", "quality"], 0),
    processedToday: pickNumber(source, ["processed_today", "processedToday", "today_count"]),
    lastSyncAt: pickNullableString(source, ["last_sync_at", "lastSyncAt", "synced_at"]),
  };
}

export function normalizeTasks(payload: unknown): GovernanceTask[] {
  return firstArray(payload)
    .filter(isRecord)
    .map((source, index) => {
      const proposal = isRecord(source.proposal) ? source.proposal : {};
      const proposalPatch = isRecord(proposal.patch) ? proposal.patch : {};
      const item = isRecord(source.item) ? source.item : {};
      const confidenceRaw = pickNumber(
        source,
        ["confidence", "score"],
        pickNumber(proposal, ["confidence", "score"], -1),
      );
      const statusRaw = pickString(source, ["status", "state"], "pending").toLowerCase();
      const status: GovernanceTask["status"] =
        statusRaw === "applied" || statusRaw === "accepted" || statusRaw === "ready"
          ? "applied"
          : statusRaw === "skipped" || statusRaw === "rejected"
            ? "skipped"
            : "pending";

      const currentCategory = pickString(item, ["category"]);
      const suggestedCategory = Object.hasOwn(proposalPatch, "category")
        ? typeof proposalPatch.category === "string"
          ? proposalPatch.category.trim()
          : ""
        : currentCategory;

      return {
        id: pickString(source, ["id", "task_id", "taskId"], `task-${index}`),
        kind: taskKind(pickString(source, ["kind", "type", "task_type"], "unknown")),
        title: pickString(source, ["title", "name", "display_name"], "未命名治理建议"),
        preview: pickString(source, ["preview", "summary", "description", "content_preview"]),
        sourceName: pickString(source, ["source_name", "sourceName", "source"], "个人数据"),
        suggestion: pickString(source, ["suggestion", "proposed_change", "recommendation", "action_label"]),
        reason: pickString(source, ["reason", "explanation", "quality_issue"]),
        confidence:
          confidenceRaw < 0
            ? null
            : confidenceRaw <= 1
              ? confidenceRaw
              : confidenceRaw / 100,
        createdAt: pickNullableString(source, ["created_at", "createdAt", "detected_at"]),
        status,
        suggestedTitle: pickString(
          proposalPatch,
          ["title"],
          pickString(item, ["title"], pickString(source, ["title"])),
        ),
        suggestedTags: pickStringArray(proposalPatch, ["tags"]).length
          ? pickStringArray(proposalPatch, ["tags"])
          : pickStringArray(item, ["tags"]),
        currentCategory,
        suggestedCategory,
      };
    });
}

export function normalizeSources(payload: unknown): DataSource[] {
  return firstArray(payload)
    .filter(isRecord)
    .map((source, index) => normalizeSource(source, index));
}

function normalizeSource(source: JsonRecord, index = 0): DataSource {
  return {
    id: pickString(source, ["id", "source_id", "sourceId"], `source-${index}`),
    name: pickString(
      source,
      ["name", "title", "display_name"],
      "未命名数据源",
    ),
    type: pickString(
      source,
      ["type", "kind", "source_type", "connector_type"],
      "connector",
    ),
    status: sourceStatus(
      pickString(source, ["status", "sync_status", "state"], "unknown"),
    ),
    itemCount: pickNumber(source, [
      "item_count",
      "itemCount",
      "records",
      "record_count",
    ]),
    pendingCount: pickNumber(source, [
      "pending_count",
      "pendingCount",
      "pending",
    ]),
    lastSyncAt: pickNullableString(source, [
      "last_sync_at",
      "lastSyncAt",
      "synced_at",
    ]),
    errorMessage: pickNullableString(source, [
      "error_message",
      "errorMessage",
      "last_error",
    ]),
  };
}

function observerState(value: string): WechatObserverState {
  const normalized = value.trim().toLowerCase();
  const knownStates = new Set<WechatObserverState>([
    "extension_missing",
    "awaiting_pairing",
    "login_required",
    "awaiting_phone_confirm",
    "active",
    "capture_paused",
    "browser_offline",
    "parser_degraded",
    "account_rejected",
    "unknown",
  ]);
  return knownStates.has(normalized as WechatObserverState)
    ? (normalized as WechatObserverState)
    : "unknown";
}

export function normalizeWechatObserverStatus(
  payload: unknown,
): WechatObserverStatus {
  const source = firstRecord(payload);
  const conversation = isRecord(source.current_conversation)
    ? source.current_conversation
    : isRecord(source.currentConversation)
      ? source.currentConversation
      : {};
  const session = isRecord(source.last_session)
    ? source.last_session
    : isRecord(source.lastSession)
      ? source.lastSession
      : {};
  const state = observerState(
    pickString(source, ["state", "observer_state", "observerState"], "unknown"),
  );
  return {
    state,
    extensionVersion:
      pickNullableString(source, ["extension_version", "extensionVersion"]) ??
      pickNullableString(session, ["extension_version", "extensionVersion"]),
    parserVersion:
      pickNullableString(source, ["parser_version", "parserVersion"]) ??
      pickNullableString(session, ["parser_version", "parserVersion"]),
    currentConversationId:
      pickNullableString(source, [
        "current_conversation_id",
        "currentConversationId",
      ]) ??
      pickNullableString(session, [
        "current_conversation_id",
        "currentConversationId",
      ]) ??
      pickNullableString(conversation, ["id", "conversation_id"]),
    currentConversationName:
      pickNullableString(source, [
        "current_conversation_name",
        "currentConversationName",
        "current_conversation",
      ]) ??
      pickNullableString(session, [
        "current_conversation_name",
        "currentConversationName",
      ]) ??
      pickNullableString(conversation, ["name", "display_name", "title"]),
    lastHeartbeatAt:
      pickNullableString(source, ["last_heartbeat_at", "lastHeartbeatAt"]) ??
      pickNullableString(session, ["last_heartbeat_at", "lastHeartbeatAt"]),
    lastEventAt: pickNullableString(source, ["last_event_at", "lastEventAt"]),
    unreadConversationCount: pickNumber(
      source,
      [
        "unread_conversation_count",
        "unopened_unread_count",
        "unreadConversationCount",
        "unopenedUnreadCount",
      ],
      pickNumber(session, [
        "unread_conversation_count",
        "unreadConversationCount",
      ]),
    ),
    conversationCount: pickNumber(source, [
      "conversation_count",
      "conversationCount",
    ]),
    messageCount: pickNumber(source, ["message_count", "messageCount"]),
    openGapCount: pickNumber(source, ["open_gap_count", "openGapCount"]),
    coverageNotice: pickNullableString(source, [
      "coverage_notice",
      "coverageNotice",
    ]),
    paused:
      typeof source.paused === "boolean"
        ? source.paused
        : typeof source.enabled === "boolean"
          ? !source.enabled
          : state === "capture_paused",
  };
}

function normalizeCoverageGap(
  source: JsonRecord,
  index: number,
): SourceCoverageGap {
  const rawDetails = source.details;
  const unreadCount = isRecord(rawDetails)
    ? pickNumber(rawDetails, [
        "unread_conversation_count",
        "unreadConversationCount",
      ])
    : 0;
  return {
    id: pickString(source, ["id", "gap_id", "gapId"], `gap-${index}`),
    kind: pickString(source, ["kind", "type", "reason"], "unknown"),
    startedAt: pickNullableString(source, [
      "started_at",
      "startedAt",
      "start_at",
    ]),
    endedAt: pickNullableString(source, ["ended_at", "endedAt", "end_at"]),
    details:
      typeof rawDetails === "string"
        ? rawDetails
        : isRecord(rawDetails)
          ? pickNullableString(rawDetails, ["message", "description", "summary"]) ??
            (unreadCount > 0 ? `${unreadCount} 个未打开的未读会话` : null)
          : pickNullableString(source, ["message", "description"]),
  };
}

export function normalizeSourceCoverageGaps(payload: unknown): SourceCoverageGaps {
  const root = firstRecord(payload);
  const items = firstArray(payload)
    .filter(isRecord)
    .map(normalizeCoverageGap);
  return {
    items,
    total: pickNumber(root, ["total", "count"], items.length),
  };
}

export function normalizeSourcePairing(payload: unknown): SourcePairing {
  const source = firstRecord(payload);
  return {
    id: pickString(source, ["id", "pairing_id", "pairingId"]),
    sourceId: pickString(source, ["source_id", "sourceId"]),
    pairingCode: pickString(source, ["pairing_code", "pairingCode", "code"]),
    expiresAt: pickNullableString(source, ["expires_at", "expiresAt"]),
    createdAt: pickNullableString(source, ["created_at", "createdAt"]),
  };
}

function mobileDeviceStatus(value: string): MobileDeviceStatus {
  const normalized = value.trim().toLowerCase();
  if (normalized === "active") return "active";
  if (normalized === "revoked") return "revoked";
  if (normalized === "expired") return "expired";
  return "unknown";
}

export function normalizeMobilePairing(payload: unknown): MobilePairing {
  const source = firstRecord(payload);
  return {
    id: pickString(source, ["pairing_id", "pairingId", "id"]),
    code: pickString(source, ["code", "pairing_code", "pairingCode"]),
    expiresAt: pickNullableString(source, ["expires_at", "expiresAt"]),
    createdAt: pickNullableString(source, ["created_at", "createdAt"]),
  };
}

export function normalizeMobileDevices(payload: unknown): MobileDevice[] {
  return firstArray(payload)
    .filter(isRecord)
    .map((source, index) => ({
      id: pickString(source, ["id", "mobile_device_id", "mobileDeviceId"], `device-${index}`),
      deviceId: pickString(source, ["device_id", "deviceId"]),
      displayName: pickString(
        source,
        ["display_name", "displayName", "name"],
        "未命名手机",
      ),
      platform: pickString(source, ["platform", "os"], "unknown"),
      appVersion: pickString(source, ["app_version", "appVersion"]),
      status: mobileDeviceStatus(
        pickString(source, ["status", "state"], "unknown"),
      ),
      lastSeenAt: pickNullableString(source, ["last_seen_at", "lastSeenAt"]),
      createdAt: pickNullableString(source, ["created_at", "createdAt"]),
      revokedAt: pickNullableString(source, ["revoked_at", "revokedAt"]),
    }));
}

export function normalizeServerUrl(value: string): string {
  const trimmed = value.trim().replace(/\/+$/, "");
  return trimmed || DEFAULT_SERVER_URL;
}

export function apiBaseUrl(serverUrl: string): string {
  const normalized = normalizeServerUrl(serverUrl);
  if (/\/api\/v1$/i.test(normalized)) return normalized;
  if (/\/api$/i.test(normalized)) return `${normalized}/v1`;
  return `${normalized}/api/v1`;
}

export type ApiRequest = {
  path: string;
  method?: "GET" | "POST" | "PATCH" | "PUT" | "DELETE";
  body?: unknown;
  idempotencyKey?: string;
  timeoutMs?: number;
};

export type PocketApi = ReturnType<typeof createPocketApi>;

export function createPocketApi(settings: ConnectionSettings) {
  const baseUrl = apiBaseUrl(settings.serverUrl);

  async function request<T = unknown>({
    path,
    method = "GET",
    body,
    idempotencyKey,
    timeoutMs = 9000,
  }: ApiRequest): Promise<T> {
    const securityError = serverUrlSecurityError(settings.serverUrl);
    if (securityError) throw new ApiError(securityError);
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    const normalizedPath = path.startsWith("/") ? path : `/${path}`;

    try {
      const desktopBridge = getDesktopBridge();
      if (desktopBridge) {
        const desktopResponse = await desktopBridge.request({
          path: normalizedPath,
          method,
          body,
          idempotencyKey,
          timeoutMs,
        });
        if (!isDesktopApiResponse(desktopResponse)) {
          throw new ApiError("桌面主进程返回了无效响应");
        }
        if (!desktopResponse.ok) {
          const message =
            isRecord(desktopResponse.payload) &&
            typeof desktopResponse.payload.detail === "string"
              ? desktopResponse.payload.detail
              : desktopResponse.status === null
                ? "无法连接本地数据服务"
                : `服务返回 ${desktopResponse.status}`;
          throw new ApiError(message, desktopResponse.status);
        }
        return desktopResponse.payload as T;
      }

      const response = await fetch(`${baseUrl}${normalizedPath}`, {
        method,
        headers: {
          Accept: "application/json",
          ...(body === undefined ? {} : { "Content-Type": "application/json" }),
          ...(settings.ownerToken
            ? {
                Authorization: `Bearer ${settings.ownerToken}`,
                "X-Owner-Token": settings.ownerToken,
              }
            : {}),
          ...(idempotencyKey ? { "Idempotency-Key": idempotencyKey } : {}),
        },
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: controller.signal,
      });

      const text = await response.text();
      let payload: unknown = null;
      if (text) {
        try {
          payload = JSON.parse(text);
        } catch {
          payload = text;
        }
      }

      if (!response.ok) {
        const message =
          isRecord(payload) && typeof payload.detail === "string"
            ? payload.detail
            : `服务返回 ${response.status}`;
        throw new ApiError(message, response.status);
      }

      return payload as T;
    } catch (error) {
      if (error instanceof ApiError) throw error;
      if (error instanceof Error && error.name === "AbortError") {
        throw new ApiError("连接超时，请检查服务地址");
      }
      throw new ApiError(
        error instanceof Error ? `无法连接数据中心：${error.message}` : "无法连接数据中心",
      );
    } finally {
      clearTimeout(timer);
    }
  }

  return {
    request,
    async health(): Promise<void> {
      await request({ path: "/health", timeoutMs: 5000 });
    },
    async dashboard(): Promise<DashboardSummary> {
      return normalizeDashboard(await request({ path: "/dashboard" }));
    },
    async mobileDevices(): Promise<MobileDevice[]> {
      return normalizeMobileDevices(await request({ path: "/mobile/devices" }));
    },
    async createMobilePairing(): Promise<MobilePairing> {
      return normalizeMobilePairing(
        await request({
          path: "/mobile/pairings",
          method: "POST",
          body: {},
          timeoutMs: 20_000,
        }),
      );
    },
    async revokeMobileDevice(deviceId: string): Promise<void> {
      await request({
        path: `/mobile/devices/${encodeURIComponent(deviceId)}`,
        method: "DELETE",
      });
    },
    async tasks(): Promise<GovernanceTask[]> {
      return normalizeTasks(
        await request({ path: "/governance/tasks?status=pending&limit=50" }),
      );
    },
    async sources(): Promise<DataSource[]> {
      return normalizeSources(await request({ path: "/sources" }));
    },
    async createWechatObserverSource(
      displayName: string,
      idempotencyKey?: string,
    ): Promise<DataSource> {
      const payload = await request({
        path: "/sources",
        method: "POST",
        body: {
          kind: "wechat_visible_web",
          display_name: displayName.trim(),
          config: { capture_mode: "visible_dom" },
          schedule: "continuous",
          enabled: true,
        },
        idempotencyKey,
        timeoutMs: 20_000,
      });
      return normalizeSource(firstRecord(payload));
    },
    async observerStatus(sourceId: string): Promise<WechatObserverStatus> {
      return normalizeWechatObserverStatus(
        await request({
          path: `/sources/${encodeURIComponent(sourceId)}/observer-status`,
        }),
      );
    },
    async sourceCoverageGaps(
      sourceId: string,
      limit = 20,
    ): Promise<SourceCoverageGaps> {
      const boundedLimit = Math.min(100, Math.max(1, Math.trunc(limit)));
      return normalizeSourceCoverageGaps(
        await request({
          path: `/sources/${encodeURIComponent(sourceId)}/coverage-gaps?limit=${boundedLimit}`,
        }),
      );
    },
    async createObserverPairing(sourceId: string): Promise<SourcePairing> {
      return normalizeSourcePairing(
        await request({
          path: `/sources/${encodeURIComponent(sourceId)}/pairings`,
          method: "POST",
          body: {},
          timeoutMs: 20_000,
        }),
      );
    },
    async revokeObserverPairing(
      sourceId: string,
      pairingId: string,
    ): Promise<void> {
      await request({
        path: `/sources/${encodeURIComponent(sourceId)}/pairings/${encodeURIComponent(pairingId)}`,
        method: "DELETE",
      });
    },
    async setObserverPaused(
      sourceId: string,
      paused: boolean,
    ): Promise<DataSource> {
      const payload = await request({
        path: `/sources/${encodeURIComponent(sourceId)}/${paused ? "pause" : "resume"}`,
        method: "POST",
        body: {},
        timeoutMs: 20_000,
      });
      return normalizeSource(firstRecord(payload));
    },
  };
}
