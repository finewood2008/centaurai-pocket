import { useCallback, useMemo, useRef, useState } from "react";
import { useFocusEffect, useRouter } from "expo-router";
import { StyleSheet, Text, View } from "react-native";

import { BrandHeader, Screen } from "@/components/screen";
import {
  Button,
  EmptyState,
  LoadingCards,
  Notice,
  Pill,
  SectionHeader,
} from "@/components/ui";
import { usePocket } from "@/context/pocket-context";
import { useRemoteResource } from "@/hooks/use-remote-resource";
import { demoSources } from "@/lib/demo";
import { formatNumber, formatRelativeTime } from "@/lib/format";
import type { DataSource, SourceStatus } from "@/lib/types";
import { colors, radii } from "@/theme/colors";

const statusMeta: Record<
  SourceStatus,
  {
    label: string;
    tone: "primary" | "warning" | "danger" | "neutral";
    dot: string;
  }
> = {
  healthy: { label: "正常", tone: "primary", dot: colors.success },
  syncing: { label: "同步中", tone: "warning", dot: colors.warning },
  paused: { label: "已暂停", tone: "neutral", dot: colors.textDim },
  error: { label: "异常", tone: "danger", dot: colors.danger },
  unknown: { label: "未知", tone: "neutral", dot: colors.textDim },
};

export default function SourcesScreen() {
  const router = useRouter();
  const { api, mutations, queueSourceSync } = usePocket();
  const [recentlyQueued, setRecentlyQueued] = useState<Record<string, boolean>>({});
  const [syncError, setSyncError] = useState<string | null>(null);
  const sourceMutationFingerprint = useMemo(
    () =>
      mutations
        .filter(
          (mutation) =>
            mutation.kind === "source-create" ||
            mutation.kind === "source-sync",
        )
        .map(
          (mutation) =>
            `${mutation.id}:${mutation.state}:${mutation.attempts}`,
        )
        .join("|"),
    [mutations],
  );
  const loadSources = useCallback(() => api.sources(), [api]);
  const resource = useRemoteResource(
    loadSources,
    demoSources,
    sourceMutationFingerprint,
  );
  const reloadSources = resource.reload;
  const hasFocusedRef = useRef(false);
  useFocusEffect(
    useCallback(() => {
      if (!hasFocusedRef.current) {
        hasFocusedRef.current = true;
        return;
      }
      void reloadSources();
    }, [reloadSources]),
  );
  const queuedFolders = useMemo(
    () =>
      mutations
        .filter((mutation) => mutation.kind === "source-create")
        .map((mutation) => ({
          id: mutation.id,
          name:
            typeof mutation.body.display_name === "string"
              ? mutation.body.display_name
              : "新文件夹来源",
          path:
            typeof mutation.body.config === "object" &&
            mutation.body.config !== null &&
            "path" in mutation.body.config &&
            typeof mutation.body.config.path === "string"
              ? mutation.body.config.path
              : "",
          schedule:
            typeof mutation.body.schedule === "string"
              ? mutation.body.schedule
              : "manual",
          needsAttention: mutation.state === "needs-attention",
          error: mutation.lastError,
        })),
    [mutations],
  );

  const queuedSourceIds = useMemo(
    () =>
      new Set(
        mutations
          .filter((mutation) => mutation.kind === "source-sync" && mutation.entityId)
          .map((mutation) => mutation.entityId as string),
      ),
    [mutations],
  );

  async function sync(source: DataSource) {
    setSyncError(null);
    setRecentlyQueued((current) => ({ ...current, [source.id]: true }));
    try {
      if (!resource.isDemo) await queueSourceSync(source.id);
      setTimeout(
        () =>
          setRecentlyQueued((current) => {
            const next = { ...current };
            delete next[source.id];
            return next;
          }),
        3000,
      );
    } catch (error) {
      setRecentlyQueued((current) => {
        const next = { ...current };
        delete next[source.id];
        return next;
      });
      setSyncError(
        error instanceof Error
          ? error.message
          : "同步请求没有写入本机离线队列",
      );
    }
  }

  const healthyCount = resource.data.filter(
    (source) => source.status === "healthy" || source.status === "syncing",
  ).length;

  return (
    <Screen
      refreshing={resource.refreshing}
      onRefresh={() => void reloadSources()}
    >
      <BrandHeader
        eyebrow="AUTOMATIC SYNC"
        title="同步源"
        subtitle="设置一次，让私人数据持续自动归集"
        trailing={
          <Button
            compact
            label="新增"
            icon="+"
            onPress={() => router.push("/add-source")}
          />
        }
      />

      {resource.isDemo ? (
        <Notice
          title="正在展示演示源"
          message={`${resource.error ?? "未连接到私人数据中心"}。这里显示的连接器均为演示数据。`}
          tone="warning"
        />
      ) : null}

      {syncError ? (
        <Notice
          title="同步请求没有保存"
          message={`${syncError}。按钮状态已恢复，请处理后重试。`}
          tone="danger"
        />
      ) : null}

      {queuedFolders.length > 0 ? (
        <View style={styles.queuedSection}>
          <SectionHeader
            title="等待连接"
            caption="保存在本机并绑定当前连接配置"
          />
          {queuedFolders.map((source) => (
            <View key={source.id} style={styles.queuedCard}>
              <View style={styles.queuedIcon}>
                <Text style={styles.queuedIconText}>▱</Text>
              </View>
              <View style={styles.queuedCopy}>
                <Text style={styles.queuedTitle}>{source.name}</Text>
                <Text style={styles.queuedPath} numberOfLines={1}>
                  {source.path}
                </Text>
                {source.error ? (
                  <Text style={styles.queuedError}>{source.error}</Text>
                ) : null}
              </View>
              <Pill
                label={
                  source.needsAttention
                    ? "需处理"
                    : source.schedule === "hourly"
                      ? "每小时"
                      : source.schedule === "daily"
                        ? "每天"
                        : "手动"
                }
                tone={source.needsAttention ? "danger" : "warning"}
              />
            </View>
          ))}
        </View>
      ) : null}

      {!resource.loading ? (
        <View style={styles.overview}>
          <View style={styles.overviewCopy}>
            <Text style={styles.overviewLabel}>自动同步状态</Text>
            <Text style={styles.overviewValue}>
              {healthyCount}/{resource.data.length} 正常
            </Text>
          </View>
          <View style={styles.overviewDivider} />
          <View style={styles.overviewCopy}>
            <Text style={styles.overviewLabel}>等待进入治理</Text>
            <Text style={styles.overviewValue}>
              {formatNumber(
                resource.data.reduce((total, source) => total + source.pendingCount, 0),
              )}{" "}
              条
            </Text>
          </View>
        </View>
      ) : null}

      {resource.loading ? (
        <LoadingCards count={3} />
      ) : resource.data.length === 0 ? (
        <EmptyState
          symbol="+"
          title="还没有同步源"
          message="直接在手机上登记数据中心服务器中的文件夹。"
          action={
            <Button
              label="添加文件夹来源"
              icon="+"
              onPress={() => router.push("/add-source")}
            />
          }
        />
      ) : (
        <View style={styles.list}>
          <SectionHeader
            title="我的数据源"
            caption="下拉页面可刷新全部状态"
          />
          {resource.data.map((source) => {
            const queued = queuedSourceIds.has(source.id) || recentlyQueued[source.id];
            return (
              <SourceCard
                key={source.id}
                source={source}
                queued={Boolean(queued)}
                onSync={() => void sync(source)}
              />
            );
          })}
        </View>
      )}

      <View style={styles.agentRule}>
        <View style={styles.shield}>
          <Text style={styles.shieldText}>A</Text>
        </View>
        <View style={styles.agentRuleCopy}>
          <Text style={styles.agentRuleTitle}>Agent 数据隔离规则</Text>
          <Text style={styles.agentRuleText}>
            新同步的数据先进入治理区，只有 Ready 状态的数据才会被 Agent 查询。
          </Text>
        </View>
      </View>
    </Screen>
  );
}

function SourceCard({
  source,
  queued,
  onSync,
}: {
  source: DataSource;
  queued: boolean;
  onSync: () => void;
}) {
  const meta = statusMeta[source.status];
  const typeLabel: Record<string, string> = {
    folder: "文件夹",
    webdav: "WebDAV",
    email: "邮箱",
    rss: "订阅",
    database: "数据库",
  };

  return (
    <View style={styles.card}>
      <View style={styles.cardTop}>
        <View style={styles.sourceIcon}>
          <Text style={styles.sourceIconText}>
            {source.type === "folder" ? "▱" : source.type === "email" ? "@" : "◫"}
          </Text>
          <View style={[styles.statusDot, { backgroundColor: meta.dot }]} />
        </View>
        <View style={styles.cardCopy}>
          <Text style={styles.cardTitle}>{source.name}</Text>
          <View style={styles.cardMeta}>
            <Pill label={meta.label} tone={meta.tone} />
            <Text style={styles.typeText}>
              {typeLabel[source.type.toLowerCase()] ?? source.type}
            </Text>
          </View>
        </View>
        <Button
          compact
          tone={queued ? "ghost" : "secondary"}
          label={queued ? "已排队" : source.status === "syncing" ? "同步中" : "同步"}
          icon={queued ? "✓" : "↻"}
          disabled={queued || source.status === "syncing"}
          onPress={onSync}
        />
      </View>

      <View style={styles.cardStats}>
        <View style={styles.cardStat}>
          <Text style={styles.cardStatValue}>{formatNumber(source.itemCount)}</Text>
          <Text style={styles.cardStatLabel}>已收录</Text>
        </View>
        <View style={styles.cardStatDivider} />
        <View style={styles.cardStat}>
          <Text style={styles.cardStatValue}>{source.pendingCount}</Text>
          <Text style={styles.cardStatLabel}>待治理</Text>
        </View>
        <View style={styles.cardStatDivider} />
        <View style={[styles.cardStat, styles.lastSyncStat]}>
          <Text style={styles.cardStatValueSmall}>
            {formatRelativeTime(source.lastSyncAt)}
          </Text>
          <Text style={styles.cardStatLabel}>上次同步</Text>
        </View>
      </View>

      {source.errorMessage ? (
        <Text style={styles.sourceError}>{source.errorMessage}</Text>
      ) : null}
    </View>
  );
}

const styles = StyleSheet.create({
  syncGlyph: {
    width: 46,
    height: 46,
    borderRadius: 14,
    backgroundColor: colors.blueSoft,
    borderWidth: 1,
    borderColor: colors.blue,
    alignItems: "center",
    justifyContent: "center",
  },
  syncGlyphText: {
    color: colors.blue,
    fontSize: 26,
    fontWeight: "700",
  },
  queuedSection: {
    gap: 10,
  },
  queuedCard: {
    borderRadius: radii.medium,
    borderWidth: 1,
    borderColor: colors.warning,
    backgroundColor: colors.warningSoft,
    padding: 14,
    flexDirection: "row",
    alignItems: "center",
    gap: 11,
  },
  queuedIcon: {
    width: 38,
    height: 38,
    borderRadius: 12,
    backgroundColor: colors.goldSoft,
    alignItems: "center",
    justifyContent: "center",
  },
  queuedIconText: {
    color: colors.warning,
    fontSize: 19,
    fontWeight: "700",
  },
  queuedCopy: {
    flex: 1,
    gap: 3,
  },
  queuedTitle: {
    color: colors.text,
    fontSize: 13,
    fontWeight: "700",
  },
  queuedPath: {
    color: colors.textMuted,
    fontSize: 10,
  },
  queuedError: {
    color: colors.danger,
    fontSize: 10,
    lineHeight: 15,
  },
  overview: {
    flexDirection: "row",
    borderRadius: radii.large,
    borderWidth: 1,
    borderColor: colors.borderSoft,
    backgroundColor: colors.surface,
    padding: 18,
  },
  overviewCopy: {
    flex: 1,
    gap: 6,
  },
  overviewDivider: {
    width: 1,
    backgroundColor: colors.border,
    marginHorizontal: 16,
  },
  overviewLabel: {
    color: colors.textMuted,
    fontSize: 11,
  },
  overviewValue: {
    color: colors.text,
    fontSize: 20,
    fontWeight: "600",
  },
  list: {
    gap: 13,
  },
  card: {
    borderRadius: radii.large,
    borderWidth: 1,
    borderColor: colors.borderSoft,
    backgroundColor: colors.surface,
    padding: 17,
    gap: 16,
  },
  cardTop: {
    flexDirection: "row",
    alignItems: "center",
    gap: 12,
  },
  sourceIcon: {
    width: 46,
    height: 46,
    borderRadius: 15,
    backgroundColor: colors.surfaceHighlight,
    alignItems: "center",
    justifyContent: "center",
  },
  sourceIconText: {
    color: colors.text,
    fontSize: 21,
    fontWeight: "700",
  },
  statusDot: {
    position: "absolute",
    width: 9,
    height: 9,
    borderRadius: 5,
    right: 4,
    bottom: 4,
    borderWidth: 2,
    borderColor: colors.surfaceHighlight,
  },
  cardCopy: {
    flex: 1,
    gap: 7,
  },
  cardTitle: {
    color: colors.text,
    fontSize: 15,
    fontWeight: "700",
  },
  cardMeta: {
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
  },
  typeText: {
    color: colors.textDim,
    fontSize: 11,
  },
  cardStats: {
    flexDirection: "row",
    borderTopWidth: 1,
    borderTopColor: colors.borderSoft,
    paddingTop: 14,
  },
  cardStat: {
    flex: 1,
    gap: 3,
  },
  lastSyncStat: {
    flex: 1.3,
  },
  cardStatDivider: {
    width: 1,
    backgroundColor: colors.borderSoft,
    marginHorizontal: 12,
  },
  cardStatValue: {
    color: colors.text,
    fontSize: 17,
    fontWeight: "600",
  },
  cardStatValueSmall: {
    color: colors.text,
    fontSize: 13,
    fontWeight: "700",
    marginTop: 2,
  },
  cardStatLabel: {
    color: colors.textDim,
    fontSize: 10,
  },
  sourceError: {
    color: colors.danger,
    fontSize: 11,
    lineHeight: 17,
  },
  agentRule: {
    borderRadius: radii.large,
    borderWidth: 1,
    borderColor: colors.violet,
    backgroundColor: colors.violetSoft,
    padding: 16,
    flexDirection: "row",
    gap: 13,
  },
  shield: {
    width: 36,
    height: 36,
    borderRadius: 12,
    backgroundColor: colors.surfaceHighlight,
    alignItems: "center",
    justifyContent: "center",
  },
  shieldText: {
    color: colors.violet,
    fontSize: 16,
    fontWeight: "900",
  },
  agentRuleCopy: {
    flex: 1,
    gap: 4,
  },
  agentRuleTitle: {
    color: colors.text,
    fontSize: 13,
    fontWeight: "700",
  },
  agentRuleText: {
    color: colors.textMuted,
    fontSize: 11,
    lineHeight: 17,
  },
});
