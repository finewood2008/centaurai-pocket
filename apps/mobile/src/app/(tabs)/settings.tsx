import { useState } from "react";
import { useRouter } from "expo-router";
import {
  Alert,
  Platform,
  Pressable,
  StyleSheet,
  Text,
  TextInput,
  View,
} from "react-native";

import { BrandHeader, Screen } from "@/components/screen";
import { Button, LoadingCards, Notice, Pill, SectionHeader } from "@/components/ui";
import { usePocket } from "@/context/pocket-context";
import { apiBaseUrl, serverUrlSecurityError } from "@/lib/api";
import { colors, radii } from "@/theme/colors";

export default function SettingsScreen() {
  const { ready } = usePocket();

  if (!ready) {
    return (
      <Screen>
        <BrandHeader eyebrow="PRIVATE CONTROL" title="设置" subtitle="正在读取安全设置" />
        <LoadingCards count={2} />
      </Screen>
    );
  }

  return <ReadySettingsScreen />;
}

function ReadySettingsScreen() {
  const router = useRouter();
  const {
    settings,
    mutations,
    inactiveMutationCount,
    isFlushing,
    queueLoadError,
    lastQueueError,
    saveSettings,
    testConnection,
    retryQueueNow,
    discardQueue,
    discardInactiveQueue,
    clearUnreadableQueue,
  } = usePocket();
  const [serverUrl, setServerUrl] = useState(settings.serverUrl);
  const [ownerToken, setOwnerToken] = useState(settings.ownerToken);
  const [tokenVisible, setTokenVisible] = useState(false);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [result, setResult] = useState<{
    kind: "success" | "error";
    message: string;
  } | null>(null);
  const [queueResult, setQueueResult] = useState<{
    kind: "success" | "error";
    message: string;
  } | null>(null);

  async function save() {
    setSaving(true);
    setResult(null);
    try {
      await saveSettings({
        serverUrl,
        ownerToken,
        profileId: settings.profileId,
      });
      setResult({ kind: "success", message: "连接设置已安全保存" });
    } catch (error) {
      setResult({
        kind: "error",
        message: error instanceof Error ? error.message : "保存失败",
      });
    } finally {
      setSaving(false);
    }
  }

  async function test() {
    setTesting(true);
    setResult(null);
    try {
      await testConnection({
        serverUrl,
        ownerToken: ownerToken.trim(),
        profileId: settings.profileId,
      });
      setResult({ kind: "success", message: "连接成功，私人数据中心可用" });
    } catch (error) {
      setResult({
        kind: "error",
        message: error instanceof Error ? error.message : "连接失败",
      });
    } finally {
      setTesting(false);
    }
  }

  function confirmDiscard() {
    Alert.alert(
      "清空离线队列？",
      "当前连接尚未提交的治理操作和分享内容会被移除，此操作无法撤销。",
      [
        { text: "取消", style: "cancel" },
        {
          text: "清空",
          style: "destructive",
          onPress: () => void clearCurrentQueue(),
        },
      ],
    );
  }

  function confirmDiscardInactive() {
    Alert.alert(
      "删除旧连接的离线操作？",
      `${inactiveMutationCount} 项属于之前的服务地址或 Owner token。删除后无法恢复。`,
      [
        { text: "取消", style: "cancel" },
        {
          text: "删除",
          style: "destructive",
          onPress: () => void clearInactiveQueue(),
        },
      ],
    );
  }

  function confirmClearUnreadableQueue() {
    Alert.alert(
      "清除无法读取的离线队列？",
      "旧操作当前无法解密。清除后可继续使用，但这些尚未同步的操作将永久丢失，且无法恢复。",
      [
        { text: "取消", style: "cancel" },
        {
          text: "永久清除",
          style: "destructive",
          onPress: () => void clearCorruptQueue(),
        },
      ],
    );
  }

  async function retryQueue() {
    setQueueResult(null);
    try {
      await retryQueueNow();
    } catch (error) {
      setQueueResult({
        kind: "error",
        message: error instanceof Error ? error.message : "重试失败",
      });
    }
  }

  async function clearCurrentQueue() {
    setQueueResult(null);
    try {
      await discardQueue();
      setQueueResult({ kind: "success", message: "当前连接的离线队列已清空" });
    } catch (error) {
      setQueueResult({
        kind: "error",
        message: error instanceof Error ? error.message : "清空失败",
      });
    }
  }

  async function clearInactiveQueue() {
    setQueueResult(null);
    try {
      await discardInactiveQueue();
      setQueueResult({ kind: "success", message: "旧连接的离线操作已删除" });
    } catch (error) {
      setQueueResult({
        kind: "error",
        message: error instanceof Error ? error.message : "删除失败",
      });
    }
  }

  async function clearCorruptQueue() {
    setQueueResult(null);
    try {
      await clearUnreadableQueue();
      setQueueResult({
        kind: "success",
        message: "无法读取的旧队列已清除，现在可以重新保存操作",
      });
    } catch (error) {
      setQueueResult({
        kind: "error",
        message: error instanceof Error ? error.message : "清除失败",
      });
    }
  }

  const failedCount = mutations.filter((mutation) => mutation.lastError).length;
  const attentionCount = mutations.filter(
    (mutation) => mutation.state === "needs-attention",
  ).length;
  const tokenMissing = !ownerToken.trim();
  const urlSecurityError = serverUrlSecurityError(serverUrl);

  return (
    <Screen>
      <BrandHeader
        eyebrow="PRIVATE CONTROL"
        title="设置"
        subtitle="只连接你自己的数据服务"
        trailing={<Pill label="单人模式" tone="primary" />}
      />

      <View style={styles.ownerCard}>
        <View style={styles.avatar}>
          <Text style={styles.avatarText}>我</Text>
          <View style={styles.avatarDot} />
        </View>
        <View style={styles.ownerCopy}>
          <Text style={styles.ownerTitle}>私人数据所有者</Text>
          <Text style={styles.ownerSubtitle}>此设备 · 无团队角色与审批流程</Text>
        </View>
        <View style={styles.lock}>
          <Text style={styles.lockText}>⌁</Text>
        </View>
      </View>

      <View style={styles.section}>
        <SectionHeader
          title="数据中心连接"
          caption="地址与旧 database / DataHub 完全独立"
        />
        <View style={styles.formCard}>
          <View style={styles.field}>
            <Text style={styles.label}>服务地址</Text>
            <TextInput
              value={serverUrl}
              onChangeText={setServerUrl}
              autoCapitalize="none"
              autoCorrect={false}
              keyboardType="url"
              placeholder="https://pocket.example.com"
              placeholderTextColor={colors.textDim}
              style={styles.input}
            />
            <Text style={styles.help}>
              真机请填写电脑或 NAS 的局域网地址；请求目标：
              {apiBaseUrl(serverUrl)}
            </Text>
            {urlSecurityError ? (
              <Text style={styles.requiredHelp}>{urlSecurityError}</Text>
            ) : null}
          </View>

          <View style={styles.divider} />

          <View style={styles.field}>
            <Text style={styles.label}>Owner token</Text>
            <View style={styles.tokenRow}>
              <TextInput
                value={ownerToken}
                onChangeText={setOwnerToken}
                autoCapitalize="none"
                autoCorrect={false}
                secureTextEntry={!tokenVisible}
                placeholder="粘贴首次启动生成的 owner-token"
                placeholderTextColor={colors.textDim}
                style={[styles.input, styles.tokenInput]}
              />
              <Pressable
                accessibilityRole="button"
                onPress={() => setTokenVisible((visible) => !visible)}
                style={styles.showButton}
              >
                <Text style={styles.showButtonText}>
                  {tokenVisible ? "隐藏" : "显示"}
                </Text>
              </Pressable>
            </View>
            <Text style={styles.help}>
              原生端存入 SecureStore；不会复用旧项目账号、密码或 JWT。
            </Text>
            {tokenMissing ? (
              <Text style={styles.requiredHelp}>
                Owner token 为必填项，连接测试会验证受保护的数据概览接口。
              </Text>
            ) : null}
          </View>

          {result ? (
            <Notice
              title={result.kind === "success" ? "已就绪" : "连接未完成"}
              message={result.message}
              tone={result.kind === "success" ? "primary" : "danger"}
            />
          ) : null}

          <View style={styles.formActions}>
            <Button
              style={styles.testButton}
              tone="secondary"
              label="测试连接"
              loading={testing}
              disabled={tokenMissing || Boolean(urlSecurityError)}
              onPress={() => void test()}
            />
            <Button
              style={styles.saveButton}
              label="保存设置"
              loading={saving}
              disabled={tokenMissing || Boolean(urlSecurityError)}
              onPress={() => void save()}
            />
          </View>
        </View>
      </View>

      <View style={styles.section}>
        <SectionHeader
          title="离线操作"
          caption="断网时也可以继续治理"
          action={
            <Pill
              label={
                queueLoadError
                  ? "需要处理"
                  : isFlushing
                    ? "同步中"
                    : `${mutations.length} 待同步`
              }
              tone={lastQueueError ? "danger" : mutations.length ? "warning" : "primary"}
            />
          }
        />
        <View style={styles.queueCard}>
          <View style={styles.queueTop}>
            <View style={styles.queueIcon}>
              <Text style={styles.queueIconText}>⇅</Text>
            </View>
            <View style={styles.queueCopy}>
              <Text style={styles.queueTitle}>持久化写入队列</Text>
              <Text style={styles.queueText}>
                {queueLoadError
                  ? "旧队列尚未载入。为避免覆盖原数据，新的写入已暂停。"
                  : mutations.length === 0
                  ? "队列已清空，所有操作均已提交。"
                  : `${mutations.length} 项保存在本机，${failedCount} 项有错误，${attentionCount} 项需手动确认。`}
              </Text>
            </View>
          </View>
          {queueLoadError ? (
            <Notice
              title="离线队列无法读取"
              message={`${queueLoadError}。只有确认不再恢复旧操作后，才应永久清除。`}
              tone="danger"
              action={
                <Button
                  compact
                  tone="danger"
                  label="永久清除"
                  onPress={confirmClearUnreadableQueue}
                />
              }
            />
          ) : lastQueueError ? (
            <Text style={styles.queueError}>{lastQueueError}</Text>
          ) : null}
          {queueResult ? (
            <Notice
              title={queueResult.kind === "success" ? "操作完成" : "操作未完成"}
              message={queueResult.message}
              tone={queueResult.kind === "success" ? "primary" : "danger"}
            />
          ) : null}
          {!queueLoadError && inactiveMutationCount > 0 ? (
            <View style={styles.inactiveQueueRow}>
              <Text style={styles.inactiveQueue}>
                另有 {inactiveMutationCount} 项属于之前的连接配置，绝不会发送到当前服务。
              </Text>
              <Button
                compact
                tone="danger"
                label="删除旧队列"
                onPress={confirmDiscardInactive}
              />
            </View>
          ) : null}
          {!queueLoadError && mutations.length > 0 ? (
            <View style={styles.queueActions}>
              <Button
                style={styles.queueAction}
                tone="secondary"
                label="立即重试"
                loading={isFlushing}
                onPress={() => void retryQueue()}
              />
              <Button
                style={styles.queueAction}
                tone="danger"
                label="清空队列"
                onPress={confirmDiscard}
              />
            </View>
          ) : null}
        </View>
      </View>

      <View style={styles.section}>
        <SectionHeader title="分享接收" caption="测试手机碎片内容的采集入口" />
        <Pressable
          accessibilityRole="button"
          onPress={() => router.push("/handle-share")}
          style={({ pressed }) => [
            styles.settingLink,
            pressed && styles.linkPressed,
          ]}
        >
          <View style={styles.linkIcon}>
            <Text style={styles.linkIconText}>↗</Text>
          </View>
          <View style={styles.linkCopy}>
            <Text style={styles.linkTitle}>打开分享接收页</Text>
            <Text style={styles.linkSubtitle}>
              centaur-pocket://handle-share
            </Text>
          </View>
          <Text style={styles.chevron}>›</Text>
        </Pressable>
      </View>

      <View style={styles.identity}>
        <Text style={styles.identityName}>CentaurAI Pocket · 0.1.0</Text>
        <Text style={styles.identityMeta}>
          {Platform.OS === "android"
            ? "ai.centaur.pocket"
            : Platform.OS === "ios"
              ? "ai.centaur.pocket"
              : "独立 Web 预览存储"}
        </Text>
      </View>
    </Screen>
  );
}

const styles = StyleSheet.create({
  ownerCard: {
    borderRadius: radii.large,
    borderWidth: 1,
    borderColor: "#23534E",
    backgroundColor: "#102628",
    padding: 16,
    flexDirection: "row",
    alignItems: "center",
    gap: 13,
  },
  avatar: {
    width: 48,
    height: 48,
    borderRadius: 17,
    backgroundColor: colors.primarySoft,
    alignItems: "center",
    justifyContent: "center",
  },
  avatarText: {
    color: colors.primary,
    fontSize: 17,
    fontWeight: "800",
  },
  avatarDot: {
    position: "absolute",
    right: 3,
    bottom: 3,
    width: 10,
    height: 10,
    borderRadius: 5,
    borderWidth: 2,
    borderColor: colors.primarySoft,
    backgroundColor: colors.success,
  },
  ownerCopy: {
    flex: 1,
    gap: 4,
  },
  ownerTitle: {
    color: colors.text,
    fontSize: 15,
    fontWeight: "700",
  },
  ownerSubtitle: {
    color: colors.textMuted,
    fontSize: 11,
  },
  lock: {
    width: 32,
    height: 32,
    borderRadius: 11,
    backgroundColor: "#183536",
    alignItems: "center",
    justifyContent: "center",
  },
  lockText: {
    color: colors.primary,
    fontSize: 18,
  },
  section: {
    gap: 12,
  },
  formCard: {
    borderRadius: radii.large,
    borderWidth: 1,
    borderColor: colors.borderSoft,
    backgroundColor: colors.surface,
    padding: 17,
    gap: 17,
  },
  field: {
    gap: 8,
  },
  label: {
    color: colors.text,
    fontSize: 12,
    fontWeight: "700",
  },
  input: {
    minHeight: 48,
    borderRadius: 13,
    borderWidth: 1,
    borderColor: colors.border,
    backgroundColor: colors.backgroundRaised,
    color: colors.text,
    paddingHorizontal: 13,
    fontSize: 13,
  },
  tokenRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
  },
  tokenInput: {
    flex: 1,
  },
  showButton: {
    height: 48,
    borderRadius: 13,
    borderWidth: 1,
    borderColor: colors.border,
    backgroundColor: colors.surfaceHighlight,
    paddingHorizontal: 13,
    alignItems: "center",
    justifyContent: "center",
  },
  showButtonText: {
    color: colors.textMuted,
    fontSize: 12,
    fontWeight: "700",
  },
  help: {
    color: colors.textDim,
    fontSize: 10,
    lineHeight: 16,
  },
  requiredHelp: {
    color: colors.warning,
    fontSize: 10,
    lineHeight: 16,
  },
  divider: {
    height: 1,
    backgroundColor: colors.borderSoft,
  },
  formActions: {
    flexDirection: "row",
    gap: 10,
  },
  testButton: {
    flex: 1,
  },
  saveButton: {
    flex: 1,
  },
  queueCard: {
    borderRadius: radii.large,
    borderWidth: 1,
    borderColor: colors.borderSoft,
    backgroundColor: colors.surface,
    padding: 16,
    gap: 13,
  },
  queueTop: {
    flexDirection: "row",
    alignItems: "center",
    gap: 12,
  },
  queueIcon: {
    width: 42,
    height: 42,
    borderRadius: 14,
    backgroundColor: colors.blueSoft,
    alignItems: "center",
    justifyContent: "center",
  },
  queueIconText: {
    color: colors.blue,
    fontSize: 20,
    fontWeight: "800",
  },
  queueCopy: {
    flex: 1,
    gap: 4,
  },
  queueTitle: {
    color: colors.text,
    fontSize: 13,
    fontWeight: "700",
  },
  queueText: {
    color: colors.textMuted,
    fontSize: 11,
    lineHeight: 16,
  },
  queueError: {
    color: colors.danger,
    fontSize: 11,
    lineHeight: 17,
  },
  inactiveQueue: {
    flex: 1,
    color: colors.warning,
    fontSize: 11,
    lineHeight: 17,
  },
  inactiveQueueRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: 10,
  },
  queueActions: {
    flexDirection: "row",
    gap: 10,
  },
  queueAction: {
    flex: 1,
  },
  settingLink: {
    borderRadius: radii.large,
    borderWidth: 1,
    borderColor: colors.borderSoft,
    backgroundColor: colors.surface,
    padding: 16,
    flexDirection: "row",
    alignItems: "center",
    gap: 13,
  },
  linkPressed: {
    opacity: 0.75,
  },
  linkIcon: {
    width: 42,
    height: 42,
    borderRadius: 14,
    backgroundColor: colors.violetSoft,
    alignItems: "center",
    justifyContent: "center",
  },
  linkIconText: {
    color: colors.violet,
    fontSize: 21,
  },
  linkCopy: {
    flex: 1,
    gap: 4,
  },
  linkTitle: {
    color: colors.text,
    fontSize: 13,
    fontWeight: "700",
  },
  linkSubtitle: {
    color: colors.textDim,
    fontSize: 10,
  },
  chevron: {
    color: colors.textDim,
    fontSize: 25,
  },
  identity: {
    alignItems: "center",
    gap: 5,
    paddingVertical: 12,
  },
  identityName: {
    color: colors.textMuted,
    fontSize: 11,
    fontWeight: "700",
  },
  identityMeta: {
    color: colors.textDim,
    fontSize: 10,
  },
});
