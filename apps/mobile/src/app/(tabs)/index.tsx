import { useCallback, useRef, useState } from "react";
import { useFocusEffect, useRouter } from "expo-router";
import { Pressable, StyleSheet, Text, View } from "react-native";

import { BrandHeader, Screen } from "@/components/screen";
import { Button, LoadingCards, Notice, Pill, SectionHeader } from "@/components/ui";
import { usePocket } from "@/context/pocket-context";
import { useRemoteResource } from "@/hooks/use-remote-resource";
import { demoDashboard } from "@/lib/demo";
import { clampPercent, formatNumber, formatRelativeTime, todayLabel } from "@/lib/format";
import { colors, radii, shadows } from "@/theme/colors";

export default function TodayScreen() {
  const router = useRouter();
  const {
    api,
    mutations,
    isFlushing,
    queueLoadError,
    lastQueueError,
    retryQueueNow,
  } = usePocket();
  const [retryError, setRetryError] = useState<string | null>(null);
  const loadDashboard = useCallback(() => api.dashboard(), [api]);
  const resource = useRemoteResource(loadDashboard, demoDashboard);
  const reloadDashboard = resource.reload;
  const hasFocusedRef = useRef(false);
  useFocusEffect(
    useCallback(() => {
      if (!hasFocusedRef.current) {
        hasFocusedRef.current = true;
        return;
      }
      void reloadDashboard();
    }, [reloadDashboard]),
  );
  const dashboard = resource.data;
  const governedPercent = clampPercent(dashboard.qualityScore);

  async function retryQueue() {
    setRetryError(null);
    try {
      await retryQueueNow();
    } catch (error) {
      setRetryError(error instanceof Error ? error.message : "重试失败");
    }
  }

  const visibleQueueError = retryError ?? lastQueueError;

  return (
    <Screen
      refreshing={resource.refreshing}
      onRefresh={() => void reloadDashboard()}
    >
      <BrandHeader
        eyebrow="CENTAURAI · POCKET"
        title="今日"
        subtitle={todayLabel()}
        trailing={
          <View style={styles.logo}>
            <Text style={styles.logoGlyph}>C</Text>
            <View style={styles.logoDot} />
          </View>
        }
      />

      {resource.isDemo ? (
        <Notice
          title="当前为离线演示"
          message={`${resource.error ?? "未连接到私人数据中心"}。下面是明确标记的演示数据，不代表真实状态。`}
          tone="warning"
          action={
            <Button
              compact
              tone="ghost"
              label="重试"
              onPress={() => void reloadDashboard()}
            />
          }
        />
      ) : null}

      {mutations.length > 0 || lastQueueError ? (
        <Notice
          title={
            queueLoadError
              ? "离线队列需要处理"
              : isFlushing
                ? "正在同步离线操作"
                : `${mutations.length} 项等待同步`
          }
          message={
            visibleQueueError ??
            "操作已安全保存在本机，连接恢复后会按顺序自动提交。"
          }
          tone={visibleQueueError ? "danger" : "primary"}
          action={
            !isFlushing ? (
              <Button
                compact
                tone="ghost"
                label={queueLoadError ? "去设置" : "立即重试"}
                onPress={
                  queueLoadError
                    ? () => router.push("/settings")
                    : () => void retryQueue()
                }
              />
            ) : null
          }
        />
      ) : null}

      {resource.loading ? (
        <LoadingCards count={3} />
      ) : (
        <>
          <View style={styles.heroCard}>
            <View style={styles.heroTop}>
              <View>
                <Text style={styles.heroEyebrow}>数据治理进度</Text>
                <View style={styles.heroNumberRow}>
                  <Text style={styles.heroNumber}>{Math.round(governedPercent)}</Text>
                  <Text style={styles.heroPercent}>%</Text>
                </View>
              </View>
              <View style={styles.qualityBadge}>
                <Text style={styles.qualityValue}>{dashboard.qualityScore}</Text>
                <Text style={styles.qualityLabel}>质量分</Text>
              </View>
            </View>
            <View style={styles.progressTrack}>
              <View
                style={[styles.progressFill, { width: `${governedPercent}%` }]}
              />
            </View>
            <View style={styles.heroBottom}>
              <Text style={styles.heroMeta}>
                {formatNumber(dashboard.readyItems)} 条已可供个人 Agent 使用
              </Text>
              <Text style={styles.heroMeta}>
                同步于 {formatRelativeTime(dashboard.lastSyncAt)}
              </Text>
            </View>
          </View>

          <View style={styles.metricGrid}>
            <Metric
              label="待你处理"
              value={dashboard.pendingTasks}
              hint="治理建议"
              tone="warning"
            />
            <Metric
              label="今日完成"
              value={dashboard.processedToday}
              hint="条操作"
              tone="primary"
            />
            <Metric
              label="数据总量"
              value={dashboard.totalItems}
              hint="条记录"
              tone="violet"
            />
            <Metric
              label="同步源"
              value={dashboard.sourceCount}
              hint={`${dashboard.healthySources} 个正常`}
              tone="blue"
            />
          </View>

          <View style={styles.section}>
            <SectionHeader
              title="下一步"
              caption="用一分钟把数据整理好"
              action={<Pill label={`${dashboard.pendingTasks} 项`} tone="warning" />}
            />
            <Pressable
              accessibilityRole="button"
              onPress={() => router.push("/inbox")}
              style={({ pressed }) => [
                styles.actionCard,
                pressed && styles.pressed,
              ]}
            >
              <View style={styles.actionIcon}>
                <Text style={styles.actionIconText}>◇</Text>
              </View>
              <View style={styles.actionCopy}>
                <Text style={styles.actionTitle}>清理治理收件箱</Text>
                <Text style={styles.actionSubtitle}>
                  接受分类、合并重复或补全缺失字段
                </Text>
              </View>
              <Text style={styles.chevron}>›</Text>
            </Pressable>
          </View>

          <View style={styles.sourceStrip}>
            <View style={styles.liveDot} />
            <View style={styles.sourceStripCopy}>
              <Text style={styles.sourceStripTitle}>
                {dashboard.healthySources}/{dashboard.sourceCount} 个数据源在线
              </Text>
              <Text style={styles.sourceStripSubtitle}>
                自动同步只把治理完成的数据开放给 Agent
              </Text>
            </View>
            <Button
              compact
              tone="secondary"
              label="查看"
              onPress={() => router.push("/sources")}
            />
          </View>
        </>
      )}
    </Screen>
  );
}

function Metric({
  label,
  value,
  hint,
  tone,
}: {
  label: string;
  value: number;
  hint: string;
  tone: "primary" | "warning" | "violet" | "blue";
}) {
  return (
    <View style={styles.metric}>
      <View style={[styles.metricAccent, metricTones[tone]]} />
      <Text style={styles.metricLabel}>{label}</Text>
      <Text style={styles.metricValue}>{formatNumber(value)}</Text>
      <Text style={styles.metricHint}>{hint}</Text>
    </View>
  );
}

const metricTones = StyleSheet.create({
  primary: { backgroundColor: colors.primary },
  warning: { backgroundColor: colors.warning },
  violet: { backgroundColor: colors.violet },
  blue: { backgroundColor: colors.blue },
});

const styles = StyleSheet.create({
  logo: {
    width: 46,
    height: 46,
    borderRadius: 17,
    borderWidth: 1,
    borderColor: "#356B66",
    backgroundColor: "#102D2E",
    alignItems: "center",
    justifyContent: "center",
  },
  logoGlyph: {
    color: colors.primary,
    fontSize: 22,
    fontWeight: "900",
    fontStyle: "italic",
  },
  logoDot: {
    position: "absolute",
    right: 7,
    top: 7,
    width: 5,
    height: 5,
    borderRadius: 3,
    backgroundColor: colors.violet,
  },
  heroCard: {
    borderRadius: 26,
    padding: 22,
    gap: 18,
    backgroundColor: "#102026",
    borderWidth: 1,
    borderColor: "#224844",
    ...shadows.card,
  },
  heroTop: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
  },
  heroEyebrow: {
    color: colors.textMuted,
    fontSize: 12,
    fontWeight: "700",
  },
  heroNumberRow: {
    flexDirection: "row",
    alignItems: "flex-end",
    marginTop: 6,
  },
  heroNumber: {
    color: colors.text,
    fontSize: 50,
    lineHeight: 55,
    fontWeight: "800",
    letterSpacing: -2,
  },
  heroPercent: {
    color: colors.primary,
    fontSize: 20,
    fontWeight: "800",
    marginBottom: 7,
    marginLeft: 2,
  },
  qualityBadge: {
    width: 72,
    height: 72,
    borderRadius: 36,
    borderWidth: 4,
    borderColor: colors.primary,
    backgroundColor: colors.primaryDark,
    alignItems: "center",
    justifyContent: "center",
  },
  qualityValue: {
    color: colors.text,
    fontSize: 22,
    lineHeight: 25,
    fontWeight: "800",
  },
  qualityLabel: {
    color: colors.textMuted,
    fontSize: 10,
  },
  progressTrack: {
    height: 7,
    borderRadius: 4,
    backgroundColor: "#213437",
    overflow: "hidden",
  },
  progressFill: {
    height: "100%",
    borderRadius: 4,
    backgroundColor: colors.primary,
  },
  heroBottom: {
    flexDirection: "row",
    justifyContent: "space-between",
    flexWrap: "wrap",
    gap: 8,
  },
  heroMeta: {
    color: colors.textMuted,
    fontSize: 11,
  },
  metricGrid: {
    flexDirection: "row",
    flexWrap: "wrap",
    gap: 12,
  },
  metric: {
    width: "48%",
    flexGrow: 1,
    minWidth: 140,
    borderRadius: radii.large,
    padding: 17,
    backgroundColor: colors.surface,
    borderWidth: 1,
    borderColor: colors.borderSoft,
    overflow: "hidden",
  },
  metricAccent: {
    position: "absolute",
    top: 0,
    left: 0,
    right: 0,
    height: 2,
  },
  metricLabel: {
    color: colors.textMuted,
    fontSize: 12,
  },
  metricValue: {
    color: colors.text,
    fontSize: 28,
    fontWeight: "800",
    marginTop: 7,
  },
  metricHint: {
    color: colors.textDim,
    fontSize: 11,
    marginTop: 3,
  },
  section: {
    gap: 12,
  },
  actionCard: {
    borderRadius: radii.large,
    padding: 16,
    backgroundColor: colors.surface,
    borderWidth: 1,
    borderColor: colors.borderSoft,
    flexDirection: "row",
    alignItems: "center",
    gap: 14,
  },
  pressed: {
    opacity: 0.75,
    transform: [{ scale: 0.99 }],
  },
  actionIcon: {
    width: 46,
    height: 46,
    borderRadius: 16,
    backgroundColor: colors.violetSoft,
    alignItems: "center",
    justifyContent: "center",
  },
  actionIconText: {
    color: colors.violet,
    fontSize: 25,
  },
  actionCopy: {
    flex: 1,
    gap: 4,
  },
  actionTitle: {
    color: colors.text,
    fontSize: 15,
    fontWeight: "700",
  },
  actionSubtitle: {
    color: colors.textMuted,
    fontSize: 12,
    lineHeight: 18,
  },
  chevron: {
    color: colors.textDim,
    fontSize: 28,
    fontWeight: "300",
  },
  sourceStrip: {
    borderRadius: radii.medium,
    padding: 15,
    backgroundColor: colors.backgroundRaised,
    borderWidth: 1,
    borderColor: colors.borderSoft,
    flexDirection: "row",
    alignItems: "center",
    gap: 12,
  },
  liveDot: {
    width: 8,
    height: 8,
    borderRadius: 4,
    backgroundColor: colors.success,
    shadowColor: colors.success,
    shadowOpacity: 0.8,
    shadowRadius: 8,
  },
  sourceStripCopy: {
    flex: 1,
    gap: 3,
  },
  sourceStripTitle: {
    color: colors.text,
    fontSize: 13,
    fontWeight: "700",
  },
  sourceStripSubtitle: {
    color: colors.textMuted,
    fontSize: 11,
    lineHeight: 16,
  },
});
