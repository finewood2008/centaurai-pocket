import { useState } from "react";
import { ActivityIndicator, Pressable, StyleSheet, Text, View } from "react-native";

import { Button, Notice, Pill } from "@/components/ui";
import { useWechatObserver } from "@/hooks/use-wechat-observer";
import type { PocketApi } from "@/lib/api";
import {
  canOpenWechatWebOnDesktop,
  openWechatWebOnDesktop,
} from "@/lib/desktop-bridge";
import { formatNumber, formatRelativeTime } from "@/lib/format";
import type {
  DataSource,
  SourceCoverageGap,
  WechatObserverState,
} from "@/lib/types";
import { colors, radii } from "@/theme/colors";

type StateMeta = {
  label: string;
  message: string;
  tone: "primary" | "warning" | "danger" | "neutral";
  dot: string;
};

const stateMeta: Record<WechatObserverState, StateMeta> = {
  extension_missing: {
    label: "等待扩展",
    message: "安装浏览器扩展后，用一次性配对码连接这个来源。",
    tone: "warning",
    dot: colors.warning,
  },
  awaiting_pairing: {
    label: "等待配对",
    message: "观察器尚未与浏览器扩展配对。",
    tone: "warning",
    dot: colors.warning,
  },
  login_required: {
    label: "需要登录",
    message: "打开微信网页版并使用手机扫码登录。",
    tone: "warning",
    dot: colors.warning,
  },
  awaiting_phone_confirm: {
    label: "手机确认",
    message: "请在手机微信中确认本次网页登录。",
    tone: "warning",
    dot: colors.warning,
  },
  active: {
    label: "正在观察",
    message: "当前网页中实际渲染的消息会自动进入私人数据中心。",
    tone: "primary",
    dot: colors.success,
  },
  capture_paused: {
    label: "已暂停",
    message: "浏览器仍可保持登录，但新消息不会进入 Pocket。",
    tone: "neutral",
    dot: colors.textDim,
  },
  browser_offline: {
    label: "浏览器离线",
    message: "没有收到扩展心跳，离线期间可能存在采集缺口。",
    tone: "danger",
    dot: colors.danger,
  },
  parser_degraded: {
    label: "解析器异常",
    message: "微信页面结构可能已经变化，已停止不确定消息的采集。",
    tone: "danger",
    dot: colors.danger,
  },
  account_rejected: {
    label: "账号被拒绝",
    message: "微信拒绝了当前网页登录，观察器不会尝试绕过风控。",
    tone: "danger",
    dot: colors.danger,
  },
  unknown: {
    label: "状态未知",
    message: "暂时没有足够信息判断浏览器观察状态。",
    tone: "neutral",
    dot: colors.textDim,
  },
};

const gapLabels: Record<string, string> = {
  before_enabled: "启用前历史",
  pre_enable: "启用前历史",
  browser_offline: "浏览器离线",
  browser_closed: "浏览器关闭",
  computer_sleep: "电脑休眠",
  login_expired: "登录失效",
  account_rejected: "账号被拒绝",
  unopened_conversation: "未打开会话",
  unopened_conversations: "未打开会话",
  heartbeat_missing: "扩展心跳中断",
  capture_paused: "采集已暂停",
  parser_degraded: "页面解析异常",
  queue_rejected: "本机队列拒绝",
  version_incompatible: "版本不兼容",
};

export function WechatObserverCard({
  source,
  api,
}: {
  source: DataSource;
  api: PocketApi;
}) {
  const observer = useWechatObserver(api, source.id);
  const [launchError, setLaunchError] = useState<string | null>(null);
  const canLaunch = canOpenWechatWebOnDesktop();
  const meta = stateMeta[observer.status?.state ?? "unknown"];
  const paused = observer.status?.paused === true;

  async function launchWechat() {
    setLaunchError(null);
    try {
      await openWechatWebOnDesktop();
    } catch (caught) {
      setLaunchError(caught instanceof Error ? caught.message : "无法打开微信网页版");
    }
  }

  return (
    <View style={[styles.card, styles.observerCard]}>
      <View style={styles.cardTop}>
        <View style={styles.sourceIcon}>
          <Text style={styles.sourceIconText}>微</Text>
          <View style={[styles.statusDot, { backgroundColor: meta.dot }]} />
        </View>
        <View style={styles.cardCopy}>
          <Text style={styles.cardTitle}>{source.name}</Text>
          <View style={styles.cardMeta}>
            <Pill label={meta.label} tone={meta.tone} />
            <Text style={styles.typeText}>个人微信 · 网页可见观察</Text>
          </View>
        </View>
        {observer.loading ? (
          <ActivityIndicator color={colors.primary} />
        ) : (
          <Pressable
            accessibilityRole="button"
            accessibilityLabel="刷新微信网页观察器状态"
            disabled={observer.refreshing}
            onPress={() => void observer.reload()}
            style={styles.refreshButton}
          >
            <Text style={styles.refreshText}>{observer.refreshing ? "…" : "↻"}</Text>
          </Pressable>
        )}
      </View>

      <View style={styles.statePanel}>
        <Text style={styles.stateMessage}>{meta.message}</Text>
        <View style={styles.factGrid}>
          <ObserverFact
            label="已观察消息"
            value={`${formatNumber(observer.status?.messageCount ?? 0)} 条`}
          />
          <ObserverFact
            label="已发现会话"
            value={`${formatNumber(observer.status?.conversationCount ?? 0)} 个`}
          />
          <ObserverFact
            label="最近采集"
            value={formatRelativeTime(observer.status?.lastEventAt ?? source.lastSyncAt)}
          />
          <ObserverFact
            label="扩展心跳"
            value={formatRelativeTime(observer.status?.lastHeartbeatAt ?? null)}
          />
          <ObserverFact
            label="当前会话"
            value={observer.status?.currentConversationName ?? "尚未打开会话"}
          />
          <ObserverFact
            label="未打开未读"
            value={`${formatNumber(observer.status?.unreadConversationCount ?? 0)} 个会话`}
          />
        </View>
        {observer.status?.extensionVersion || observer.status?.parserVersion ? (
          <Text style={styles.versionText}>
            扩展 {observer.status.extensionVersion ?? "—"} · 解析器 {observer.status.parserVersion ?? "—"}
          </Text>
        ) : null}
      </View>

      {observer.pairing ? (
        <View style={styles.pairingCard}>
          <View style={styles.pairingLead}>
            <Text style={styles.pairingLabel}>一次性配对码</Text>
            <Text
              selectable
              adjustsFontSizeToFit
              minimumFontScale={0.5}
              numberOfLines={1}
              style={styles.pairingCode}
            >
              {observer.pairing.pairingCode}
            </Text>
            <Text selectable style={styles.sourceIdText}>
              来源 ID：{source.id}
            </Text>
            <Text style={styles.pairingHelp}>
              在 Firefox 工具栏的 CentaurAI 微信观察器中输入 · 配对码只在当前页面显示
              {observer.pairing.expiresAt
                ? ` · ${formatExpiry(observer.pairing.expiresAt)}失效`
                : ""}
            </Text>
          </View>
          <Button
            compact
            tone="ghost"
            label="撤销"
            loading={observer.busyAction === "revoke"}
            onPress={() => void observer.revokePairing()}
          />
        </View>
      ) : null}

      <View style={styles.actions}>
        {canLaunch ? (
          <Button
            compact
            tone="secondary"
            label="打开微信网页版"
            icon="↗"
            onPress={() => void launchWechat()}
          />
        ) : null}
        <Button
          compact
          tone="secondary"
          label={observer.status?.state === "active" ? "重新配对" : "生成配对码"}
          icon="#"
          loading={observer.busyAction === "pairing"}
          disabled={observer.busyAction !== null || Boolean(observer.pairing)}
          onPress={() => void observer.createPairing().catch(() => undefined)}
        />
        <Button
          compact
          tone={paused ? "primary" : "ghost"}
          label={paused ? "恢复采集" : "暂停采集"}
          icon={paused ? "▶" : "Ⅱ"}
          loading={
            observer.busyAction === "pause" || observer.busyAction === "resume"
          }
          disabled={observer.busyAction !== null || !observer.status}
          onPress={() => void observer.setPaused(!paused)}
        />
      </View>

      {observer.gaps.total > 0 ? (
        <View style={styles.gaps}>
          <View style={styles.gapHeader}>
            <Text style={styles.gapTitle}>覆盖缺口</Text>
            <Pill label={`${observer.gaps.total} 段`} tone="warning" />
          </View>
          {observer.gaps.items.slice(0, 3).map((gap) => (
            <CoverageGapRow key={gap.id} gap={gap} />
          ))}
          {observer.gaps.total > 3 ? (
            <Text style={styles.moreGaps}>另有 {observer.gaps.total - 3} 段缺口</Text>
          ) : null}
        </View>
      ) : null}

      {observer.error || launchError || source.errorMessage ? (
        <Notice
          title="观察器需要处理"
          message={observer.error ?? launchError ?? source.errorMessage ?? "未知错误"}
          tone="danger"
        />
      ) : null}

      <Text style={styles.disclaimer}>
        实验性、非官方且非完整来源 · {observer.status?.coverageNotice ?? "仅采集网页实际渲染内容"}
        ，不读取 Cookie 或自动翻看历史
      </Text>
    </View>
  );
}

function ObserverFact({ label, value }: { label: string; value: string }) {
  return (
    <View style={styles.fact}>
      <Text style={styles.factValue} numberOfLines={1}>
        {value}
      </Text>
      <Text style={styles.factLabel}>{label}</Text>
    </View>
  );
}

function CoverageGapRow({ gap }: { gap: SourceCoverageGap }) {
  const end = gap.endedAt ? formatRelativeTime(gap.endedAt) : "仍在持续";
  return (
    <View style={styles.gapRow}>
      <View style={styles.gapDot} />
      <View style={styles.gapCopy}>
        <Text style={styles.gapKind}>{gapLabels[gap.kind] ?? gap.kind}</Text>
        <Text style={styles.gapTime}>
          {formatRelativeTime(gap.startedAt)} → {end}
        </Text>
        {gap.details ? <Text style={styles.gapDetails}>{gap.details}</Text> : null}
      </View>
    </View>
  );
}

function formatExpiry(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return `${value} `;
  return `${new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
  }).format(date)} `;
}

const styles = StyleSheet.create({
  card: {
    borderRadius: radii.large,
    borderWidth: 1,
    borderColor: colors.borderSoft,
    backgroundColor: colors.surface,
    padding: 17,
    gap: 15,
  },
  observerCard: {
    borderColor: colors.primaryBorder,
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
    backgroundColor: colors.primarySoft,
    alignItems: "center",
    justifyContent: "center",
  },
  sourceIconText: {
    color: colors.primaryDark,
    fontSize: 18,
    fontWeight: "800",
  },
  statusDot: {
    position: "absolute",
    width: 9,
    height: 9,
    borderRadius: 5,
    right: 4,
    bottom: 4,
    borderWidth: 2,
    borderColor: colors.primarySoft,
  },
  cardCopy: {
    flex: 1,
    minWidth: 0,
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
    flexWrap: "wrap",
    gap: 8,
  },
  typeText: {
    color: colors.textDim,
    fontSize: 10,
  },
  refreshButton: {
    width: 36,
    height: 36,
    alignItems: "center",
    justifyContent: "center",
    borderRadius: radii.small,
    backgroundColor: colors.backgroundRaised,
  },
  refreshText: {
    color: colors.textMuted,
    fontSize: 18,
    fontWeight: "700",
  },
  statePanel: {
    borderRadius: radii.medium,
    backgroundColor: colors.backgroundRaised,
    padding: 13,
    gap: 12,
  },
  stateMessage: {
    color: colors.textMuted,
    fontSize: 11,
    lineHeight: 17,
  },
  factGrid: {
    flexDirection: "row",
    flexWrap: "wrap",
    rowGap: 12,
  },
  fact: {
    width: "50%",
    paddingRight: 10,
    gap: 3,
  },
  factValue: {
    color: colors.text,
    fontSize: 12,
    fontWeight: "700",
  },
  factLabel: {
    color: colors.textDim,
    fontSize: 9,
  },
  versionText: {
    color: colors.textDim,
    fontSize: 9,
  },
  pairingCard: {
    borderRadius: radii.medium,
    borderWidth: 1,
    borderColor: colors.primary,
    backgroundColor: colors.primarySoft,
    padding: 13,
    flexDirection: "row",
    alignItems: "center",
    gap: 12,
  },
  pairingLead: {
    flex: 1,
    gap: 3,
  },
  pairingLabel: {
    color: colors.textMuted,
    fontSize: 9,
    fontWeight: "700",
    letterSpacing: 0.8,
  },
  pairingCode: {
    color: colors.primaryDark,
    width: "100%",
    fontSize: 16,
    lineHeight: 23,
    fontWeight: "800",
    letterSpacing: 0.3,
  },
  pairingHelp: {
    color: colors.textMuted,
    fontSize: 9,
  },
  sourceIdText: {
    color: colors.textMuted,
    fontSize: 9,
  },
  actions: {
    flexDirection: "row",
    alignItems: "center",
    flexWrap: "wrap",
    gap: 8,
  },
  gaps: {
    borderTopWidth: 1,
    borderTopColor: colors.borderSoft,
    paddingTop: 13,
    gap: 10,
  },
  gapHeader: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
  },
  gapTitle: {
    color: colors.text,
    fontSize: 12,
    fontWeight: "700",
  },
  gapRow: {
    flexDirection: "row",
    gap: 9,
  },
  gapDot: {
    width: 7,
    height: 7,
    borderRadius: 4,
    backgroundColor: colors.warning,
    marginTop: 5,
  },
  gapCopy: {
    flex: 1,
    gap: 2,
  },
  gapKind: {
    color: colors.text,
    fontSize: 11,
    fontWeight: "700",
  },
  gapTime: {
    color: colors.textMuted,
    fontSize: 9,
  },
  gapDetails: {
    color: colors.textDim,
    fontSize: 9,
    lineHeight: 14,
  },
  moreGaps: {
    color: colors.textMuted,
    fontSize: 10,
  },
  disclaimer: {
    color: colors.textDim,
    fontSize: 9,
    lineHeight: 14,
  },
});
