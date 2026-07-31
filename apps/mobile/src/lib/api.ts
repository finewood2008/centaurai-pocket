import type {
  ConnectionSettings,
  DashboardSummary,
  DataSource,
  GovernanceTask,
  GovernanceTaskKind,
  SourceStatus,
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
    .map((source, index) => ({
      id: pickString(source, ["id", "source_id", "sourceId"], `source-${index}`),
      name: pickString(source, ["name", "title", "display_name"], "未命名数据源"),
      type: pickString(source, ["type", "source_type", "connector_type"], "connector"),
      status: sourceStatus(pickString(source, ["status", "sync_status", "state"], "unknown")),
      itemCount: pickNumber(source, ["item_count", "itemCount", "records", "record_count"]),
      pendingCount: pickNumber(source, ["pending_count", "pendingCount", "pending"]),
      lastSyncAt: pickNullableString(source, ["last_sync_at", "lastSyncAt", "synced_at"]),
      errorMessage: pickNullableString(source, ["error_message", "errorMessage", "last_error"]),
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
    async tasks(): Promise<GovernanceTask[]> {
      return normalizeTasks(
        await request({ path: "/governance/tasks?status=pending&limit=50" }),
      );
    },
    async sources(): Promise<DataSource[]> {
      return normalizeSources(await request({ path: "/sources" }));
    },
  };
}
