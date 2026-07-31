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
