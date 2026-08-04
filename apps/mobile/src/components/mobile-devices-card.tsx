import { useCallback, useEffect, useState } from "react";
import { Alert, StyleSheet, Text, View } from "react-native";

import { Button, Notice, Pill } from "@/components/ui";
import type { PocketApi } from "@/lib/api";
import type { MobileDevice, MobilePairing } from "@/lib/types";
import { colors, radii } from "@/theme/colors";

function timeLabel(value: string | null): string {
  if (!value) return "未记录";
  const parsed = Date.parse(value);
  if (!Number.isFinite(parsed)) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(parsed));
}

function statusLabel(status: MobileDevice["status"]): string {
  if (status === "active") return "已连接";
  if (status === "revoked") return "已撤销";
  if (status === "expired") return "已过期";
  return "状态未知";
}

export function MobileDevicesCard({
  api,
  enabled,
}: {
  api: PocketApi;
  enabled: boolean;
}) {
  const [devices, setDevices] = useState<MobileDevice[]>([]);
  const [pairing, setPairing] = useState<MobilePairing | null>(null);
  const [loading, setLoading] = useState(false);
  const [creating, setCreating] = useState(false);
  const [revokingId, setRevokingId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const reload = useCallback(async () => {
    if (!enabled) return;
    setLoading(true);
    setError(null);
    try {
      setDevices(await api.mobileDevices());
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "无法读取手机设备");
    } finally {
      setLoading(false);
    }
  }, [api, enabled]);

  useEffect(() => {
    const timer = setTimeout(() => void reload(), 0);
    return () => clearTimeout(timer);
  }, [reload]);

  async function createPairing() {
    setCreating(true);
    setError(null);
    try {
      const created = await api.createMobilePairing();
      if (!created.id || !created.code) {
        throw new Error("服务器没有返回有效配对码");
      }
      setPairing(created);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "生成配对码失败");
    } finally {
      setCreating(false);
    }
  }

  function confirmRevoke(device: MobileDevice) {
    Alert.alert(
      "撤销手机连接？",
      `${device.displayName} 将立即失去访问权限；之后需要重新配对。`,
      [
        { text: "取消", style: "cancel" },
        {
          text: "撤销",
          style: "destructive",
          onPress: () => void revoke(device),
        },
      ],
    );
  }

  async function revoke(device: MobileDevice) {
    setRevokingId(device.id);
    setError(null);
    try {
      await api.revokeMobileDevice(device.id);
      setPairing(null);
      await reload();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "撤销设备失败");
    } finally {
      setRevokingId(null);
    }
  }

  if (!enabled) {
    return (
      <Notice
        title="先连接数据中心"
        message="连接成功后，才能为半人马 AI 超级秘书签发一次性手机配对码。"
        tone="warning"
      />
    );
  }

  return (
    <View style={styles.card}>
      <View style={styles.leadRow}>
        <View style={styles.leadCopy}>
          <Text style={styles.title}>半人马 AI 超级秘书</Text>
          <Text style={styles.help}>
            手机只获得短期、可撤销的设备会话，不接触长期 Owner token。
          </Text>
        </View>
        <Button
          compact
          label="生成配对码"
          loading={creating}
          disabled={creating}
          onPress={() => void createPairing()}
        />
      </View>

      {pairing ? (
        <View style={styles.pairingCard} accessibilityLiveRegion="polite">
          <Text style={styles.pairingLabel}>一次性配对码</Text>
          <Text selectable style={styles.pairingCode}>
            {pairing.code}
          </Text>
          <Text style={styles.pairingHelp}>
            在秘书 App 的“连接私有服务器”中输入；{timeLabel(pairing.expiresAt)}失效，使用一次后立即作废。
          </Text>
        </View>
      ) : null}

      {error ? (
        <Notice title="设备管理未完成" message={error} tone="danger" />
      ) : null}

      <View style={styles.listHeader}>
        <Text style={styles.listTitle}>已配对设备</Text>
        <Button
          compact
          tone="ghost"
          label="刷新"
          loading={loading}
          onPress={() => void reload()}
        />
      </View>

      {!loading && devices.length === 0 ? (
        <Text style={styles.empty}>还没有手机连接这台私人服务器。</Text>
      ) : (
        devices.map((device) => (
          <View key={device.id} style={styles.deviceRow}>
            <View style={styles.deviceIcon}>
              <Text style={styles.deviceIconText}>▯</Text>
            </View>
            <View style={styles.deviceCopy}>
              <View style={styles.deviceTitleRow}>
                <Text style={styles.deviceName}>{device.displayName}</Text>
                <Pill
                  label={statusLabel(device.status)}
                  tone={device.status === "active" ? "primary" : "neutral"}
                />
              </View>
              <Text style={styles.deviceMeta}>
                {[device.platform, device.appVersion && `v${device.appVersion}`]
                  .filter(Boolean)
                  .join(" · ") || "设备信息未上报"}
              </Text>
              <Text style={styles.deviceMeta}>
                最近使用：{timeLabel(device.lastSeenAt ?? device.createdAt)}
              </Text>
            </View>
            {device.status === "active" ? (
              <Button
                compact
                tone="danger"
                label="撤销"
                loading={revokingId === device.id}
                disabled={revokingId !== null}
                onPress={() => confirmRevoke(device)}
              />
            ) : null}
          </View>
        ))
      )}
    </View>
  );
}

const styles = StyleSheet.create({
  card: {
    borderRadius: radii.large,
    borderWidth: 1,
    borderColor: colors.borderSoft,
    backgroundColor: colors.surface,
    padding: 17,
    gap: 16,
  },
  leadRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: 12,
  },
  leadCopy: { flex: 1, gap: 5 },
  title: { color: colors.text, fontSize: 14, fontWeight: "700" },
  help: { color: colors.textMuted, fontSize: 12, lineHeight: 18 },
  pairingCard: {
    borderRadius: radii.medium,
    borderWidth: 1,
    borderColor: colors.primaryBorder,
    backgroundColor: colors.primarySoft,
    padding: 15,
    gap: 7,
  },
  pairingLabel: { color: colors.primaryDark, fontSize: 11, fontWeight: "700" },
  pairingCode: {
    color: colors.text,
    fontSize: 25,
    lineHeight: 33,
    fontWeight: "800",
    letterSpacing: 2,
  },
  pairingHelp: { color: colors.textMuted, fontSize: 11, lineHeight: 17 },
  listHeader: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    borderTopWidth: 1,
    borderTopColor: colors.borderSoft,
    paddingTop: 12,
  },
  listTitle: { color: colors.text, fontSize: 12, fontWeight: "700" },
  empty: { color: colors.textMuted, fontSize: 12, lineHeight: 18 },
  deviceRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: 11,
    paddingVertical: 5,
  },
  deviceIcon: {
    width: 38,
    height: 38,
    borderRadius: 12,
    alignItems: "center",
    justifyContent: "center",
    backgroundColor: colors.backgroundRaised,
  },
  deviceIconText: { color: colors.primaryDark, fontSize: 21, fontWeight: "700" },
  deviceCopy: { flex: 1, gap: 3 },
  deviceTitleRow: { flexDirection: "row", alignItems: "center", gap: 7 },
  deviceName: { color: colors.text, fontSize: 13, fontWeight: "700", flexShrink: 1 },
  deviceMeta: { color: colors.textMuted, fontSize: 10, lineHeight: 15 },
});
