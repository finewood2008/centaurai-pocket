export type DashboardSummary = {
  pendingTasks: number;
  readyItems: number;
  totalItems: number;
  sourceCount: number;
  healthySources: number;
  qualityScore: number;
  processedToday: number;
  lastSyncAt: string | null;
};

export type GovernanceTaskKind =
  | "deduplicate"
  | "classify"
  | "quality"
  | "normalize"
  | "deletion"
  | "review"
  | "unknown";

export type GovernanceTask = {
  id: string;
  kind: GovernanceTaskKind;
  title: string;
  preview: string;
  sourceName: string;
  suggestion: string;
  reason: string;
  confidence: number | null;
  createdAt: string | null;
  status: "pending" | "applied" | "skipped";
  suggestedTitle: string;
  suggestedTags: string[];
  currentCategory: string;
  suggestedCategory: string;
};

export type SourceStatus = "healthy" | "syncing" | "paused" | "error" | "unknown";

export type DataSource = {
  id: string;
  name: string;
  type: string;
  status: SourceStatus;
  itemCount: number;
  pendingCount: number;
  lastSyncAt: string | null;
  errorMessage: string | null;
};

export type WechatObserverState =
  | "extension_missing"
  | "awaiting_pairing"
  | "login_required"
  | "awaiting_phone_confirm"
  | "active"
  | "capture_paused"
  | "browser_offline"
  | "parser_degraded"
  | "account_rejected"
  | "unknown";

export type WechatObserverStatus = {
  state: WechatObserverState;
  extensionVersion: string | null;
  parserVersion: string | null;
  currentConversationId: string | null;
  currentConversationName: string | null;
  lastHeartbeatAt: string | null;
  lastEventAt: string | null;
  unreadConversationCount: number;
  conversationCount: number;
  messageCount: number;
  openGapCount: number;
  coverageNotice: string | null;
  paused: boolean;
};

export type SourceCoverageGap = {
  id: string;
  kind: string;
  startedAt: string | null;
  endedAt: string | null;
  details: string | null;
};

export type SourceCoverageGaps = {
  items: SourceCoverageGap[];
  total: number;
};

export type SourcePairing = {
  id: string;
  sourceId: string;
  pairingCode: string;
  expiresAt: string | null;
  createdAt: string | null;
};

export type MobilePairing = {
  id: string;
  code: string;
  expiresAt: string | null;
  createdAt: string | null;
};

export type MobileDeviceStatus = "active" | "revoked" | "expired" | "unknown";

export type MobileDevice = {
  id: string;
  deviceId: string;
  displayName: string;
  platform: string;
  appVersion: string;
  status: MobileDeviceStatus;
  lastSeenAt: string | null;
  createdAt: string | null;
  revokedAt: string | null;
};

export type ConnectionSettings = {
  serverUrl: string;
  ownerToken: string;
  profileId: string;
  managedByDesktop?: boolean;
};

export type TaskAction = "apply" | "skip" | "undo";

export type GovernancePatch = {
  title?: string;
  tags?: string[];
  category?: string | null;
  state?: "ready" | "archived";
};

export type FolderSourceSchedule = "manual" | "hourly" | "daily";

export type FolderSourceInput = {
  displayName: string;
  path: string;
  schedule: FolderSourceSchedule;
};
